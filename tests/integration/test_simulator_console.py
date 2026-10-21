"""HTTP policy on the owning asyncio loop, with real simulator SQLite transactions."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest

from apps.simulator.auth import ABSOLUTE_TIMEOUT, SimulatorAuth
from apps.simulator.console import PREAUTH_COOKIE, SESSION_COOKIE, ConsoleConfig, create_app
from apps.simulator.controller import SimulatorController
from apps.simulator.process import ProcessConfig
from apps.simulator.storage import SimulatorStorage
from contracts.models import CommandRequest

pytestmark = pytest.mark.asyncio
ORIGIN = "https://simulator.example.test"
INITIAL = "fixture initial password only"
CHANGED = "fixture changed password only"
CHECKS = {
    name: True
    for name in (
        "outputs_stopped",
        "positions_verified",
        "storage_reconciled",
        "commands_reconciled",
        "operator_confirmed",
    )
}


@pytest.fixture
async def environment(tmp_path):
    now = [datetime(2026, 10, 20, 12, tzinfo=UTC)]
    with SimulatorStorage(tmp_path / "console.sqlite3") as store:
        controller = SimulatorController(
            store,
            now=now[0],
            process_config=ProcessConfig(stage_lengths_mm=(1, 1, 1), ambient_temperature_c=150.0),
        )
        auth = SimulatorAuth(store.connection, allowed_origins={ORIGIN}, clock=lambda: now[0])
        with store.transaction():
            auth.initialize_schema()
            auth.bootstrap_user("operator", INITIAL, role="simulator_operator")
            auth.bootstrap_user("viewer", INITIAL, role="viewer")
        config = ConsoleConfig(origin=ORIGIN, clock=lambda: now[0])
        app = create_app(controller, auth, config)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ORIGIN,
        ) as client:
            yield SimpleNamespace(
                store=store,
                controller=controller,
                auth=auth,
                config=config,
                client=client,
                now=now,
                app=app,
            )


async def challenge(client):
    response = await client.post(
        "/auth/challenge",
        headers={"Origin": ORIGIN, "X-Simulator-Request": "1"},
    )
    assert response.status_code == 200, response.text
    return response


async def sign_in(client, *, login="operator", password=INITIAL, change=True):
    preauth = await challenge(client)
    response = await client.post(
        "/auth/login",
        json={"login": login, "password": password},
        headers={"Origin": ORIGIN, "X-CSRF-Token": preauth.json()["csrf_token"]},
    )
    assert response.status_code == 200, response.text
    if change:
        response = await post(
            client,
            "/auth/password",
            response.json()["csrf_token"],
            {"new_password": CHANGED},
        )
        assert response.status_code == 200, response.text
    return response


async def post(client, path, csrf, body=None, *, origin=ORIGIN):
    return await client.post(path, json=body, headers={"Origin": origin, "X-CSRF-Token": csrf})


def command(env, kind="LOAD_JOB", *, command_id="load", requested_by="operator"):
    args = (
        {
            "work_order_id": "work-A",
            "recipe_version": "recipe-1",
            "target_quantity": 100,
            "parameters": {"temperature_c": 150.0, "speed_m_per_min": 6.0},
        }
        if kind == "LOAD_JOB"
        else {}
    )
    return CommandRequest(
        command_id=command_id,
        target_id="Line",
        execution_id="run-A",
        requested_by=requested_by,
        requested_at=env.now[0],
        expires_at=env.now[0] + timedelta(seconds=10),
        expected_control_version=env.controller.state.control_version,
        command={"kind": kind, "args": args},
    ).model_dump(mode="json")


async def ready(env, csrf):
    response = await post(env.client, "/recover", csrf, CHECKS)
    assert response.status_code == 200, response.text
    response = await post(
        env.client,
        "/inputs",
        csrf,
        {
            "inputs": {"temperature_c": 150.0},
            "control_mode": "LOCAL",
            "operation_mode": "AUTO",
        },
    )
    assert response.status_code == 200, response.text


async def load(env, csrf):
    await ready(env, csrf)
    response = await post(env.client, "/commands", csrf, command(env))
    assert (
        response.status_code == 200 and response.json()["submission"]["disposition"] == "ACCEPTED"
    )
    env.controller.tick(now=env.now[0])
    assert env.controller.get_result("load").status == "SUCCEEDED"


async def test_challenge_cookie_and_login_token_never_in_json(environment):
    env = environment
    response = await challenge(env.client)
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert "Domain=" not in cookie
    assert env.client.cookies[PREAUTH_COOKIE] not in response.text
    response = await sign_in(env.client, change=False)
    token = env.client.cookies[SESSION_COOKIE]
    assert token not in response.text and "token" not in response.json()
    assert response.json()["must_change_password"]
    assert PREAUTH_COOKIE not in env.client.cookies
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    snapshot = await env.client.get("/snapshot")
    assert snapshot.status_code == 200
    assert snapshot.json()["environment"] == "SIMULATOR TEST ENVIRONMENT"


@pytest.mark.parametrize(
    "path",
    [
        "/snapshot",
        "/auth/me",
        "/commands/unknown",
        "/runs/unknown",
        "/command-record?command_id=unknown",
        "/run-summary?execution_id=unknown",
    ],
)
async def test_anonymous_reads_are_401(environment, path):
    response = await environment.client.get(path)
    assert response.status_code == 401


@pytest.mark.parametrize(
    "path,body",
    [
        ("/commands", {}),
        ("/inputs", {"inputs": {"estop": True}}),
        ("/recover", CHECKS),
        ("/scrap", {"item_ids": ["1"], "reason": "test", "positions_verified": True}),
        ("/reconcile/original", CHECKS),
        ("/command-reconciliation?command_id=original", CHECKS),
        ("/run-settlement?execution_id=original", {"operator_confirmed": True}),
        ("/runs/original/settle", {"operator_confirmed": True}),
    ],
)
async def test_anonymous_mutations_are_401(environment, path, body):
    response = await post(environment.client, path, "missing", body)
    assert response.status_code == 401
    assert environment.controller.state.recovery_required


@pytest.mark.parametrize(
    "path,body",
    [
        ("/commands", {}),
        ("/inputs", {"inputs": {"estop": True}}),
        ("/recover", CHECKS),
        ("/scrap", {"item_ids": ["1"], "reason": "test", "positions_verified": True}),
        ("/reconcile/original", CHECKS),
        ("/command-reconciliation?command_id=original", CHECKS),
        ("/run-settlement?execution_id=original", {"operator_confirmed": True}),
        ("/runs/original/settle", {"operator_confirmed": True}),
    ],
)
async def test_viewer_reads_and_direct_mutation_is_403(environment, path, body):
    session = await sign_in(environment.client, login="viewer")
    assert (await environment.client.get("/snapshot")).status_code == 200
    response = await post(environment.client, path, session.json()["csrf_token"], body)
    assert response.status_code == 403
    assert environment.controller.state.recovery_required


async def test_initial_password_gates_controls_and_password_rotation_revokes_old_session(
    environment,
):
    env = environment
    session = await sign_in(env.client, change=False)
    old_token = env.client.cookies[SESSION_COOKIE]
    csrf = session.json()["csrf_token"]
    assert (await post(env.client, "/recover", csrf, CHECKS)).status_code == 403
    changed = await post(env.client, "/auth/password", csrf, {"new_password": CHANGED})
    assert changed.status_code == 200
    assert not changed.json()["must_change_password"]
    assert env.client.cookies[SESSION_COOKIE] != old_token
    new_token = env.client.cookies[SESSION_COOKIE]
    env.client.cookies.set(SESSION_COOKIE, old_token, domain="simulator.example.test", path="/")
    assert (await env.client.get("/auth/me")).status_code == 401
    env.client.cookies.set(SESSION_COOKIE, new_token, domain="simulator.example.test", path="/")
    assert (
        await post(env.client, "/recover", changed.json()["csrf_token"], CHECKS)
    ).status_code == 200


@pytest.mark.parametrize(
    "path,body",
    [
        ("/auth/password", {"new_password": "another fixture password"}),
        ("/auth/logout", None),
        ("/inputs", {"inputs": {"estop": True}}),
        ("/recover", CHECKS),
    ],
)
async def test_all_authenticated_mutations_require_origin_and_bound_csrf(environment, path, body):
    env = environment
    session = await sign_in(env.client)
    csrf = session.json()["csrf_token"]
    response = await post(env.client, path, csrf, body, origin="https://evil.example.test")
    assert response.status_code == 403
    response = await post(env.client, path, "incorrect", body)
    assert response.status_code == 403
    response = await env.client.post(path, json=body, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 403
    assert (await env.client.get("/auth/me")).status_code == 200


async def test_challenge_requires_exact_origin_and_non_simple_header(environment):
    client = environment.client
    for headers in ({}, {"Origin": ORIGIN}, {"Origin": "null", "X-Simulator-Request": "1"}):
        assert (await client.post("/auth/challenge", headers=headers)).status_code == 403
    assert PREAUTH_COOKIE not in client.cookies


async def test_login_failure_audit_commits_after_auth_rollback_without_secrets(environment):
    env = environment
    preauth = await challenge(env.client)
    response = await post(
        env.client,
        "/auth/login",
        preauth.json()["csrf_token"],
        {
            "login": "operator",
            "password": "deliberately wrong fixture password",
        },
    )
    assert response.status_code == 401
    assert not env.store.connection.in_transaction
    rows = env.store.connection.execute(
        "SELECT actor,action,details_json FROM console_request_audit"
    ).fetchall()
    assert len(rows) == 1 and rows[0]["action"] == "REQUEST_REJECTED"
    assert json.loads(rows[0]["details_json"]) == {"route": "login", "status": 401}
    dump = " ".join(env.store.connection.iterdump())
    for secret in (
        "deliberately wrong fixture password",
        preauth.json()["csrf_token"],
        env.client.cookies[PREAUTH_COOKIE],
    ):
        assert secret not in dump and secret not in response.text
    assert env.store.connection.execute("SELECT count(*) FROM console_sessions").fetchone()[0] == 0


async def test_login_and_challenge_have_bounded_attempt_limits(environment):
    env = environment
    config = replace(env.config, login_limit=2, challenge_limit=3, max_throttle_keys=8)
    app = create_app(env.controller, env.auth, config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        preauth = await challenge(client)
        for _ in range(2):
            response = await post(
                client,
                "/auth/login",
                preauth.json()["csrf_token"],
                {
                    "login": "operator",
                    "password": "wrong fixture password",
                },
            )
            assert response.status_code == 401
        response = await post(
            client,
            "/auth/login",
            preauth.json()["csrf_token"],
            {
                "login": "other-account",
                "password": INITIAL,
            },
        )
        assert response.status_code == 429 and response.headers["Retry-After"] == "60"
        await challenge(client)
        await challenge(client)
        response = await client.post(
            "/auth/challenge",
            headers={"Origin": ORIGIN, "X-Simulator-Request": "1"},
        )
        assert response.status_code == 429


async def test_account_attempt_limit_spans_distinct_peer_ips(environment):
    env = environment
    app = create_app(env.controller, env.auth, replace(env.config, login_limit=1))
    for index, expected in ((1, 401), (2, 429)):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(f"192.0.2.{index}", 1234)),
            base_url=ORIGIN,
        ) as client:
            preauth = await challenge(client)
            response = await post(
                client,
                "/auth/login",
                preauth.json()["csrf_token"],
                {
                    "login": "OPERATOR" if index == 2 else "operator",
                    "password": "wrong fixture password",
                },
            )
            assert response.status_code == expected


async def test_throttle_refuses_new_keys_when_capacity_is_full(environment):
    env = environment
    app = create_app(env.controller, env.auth, replace(env.config, max_throttle_keys=1))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        preauth = await challenge(client)
        response = await post(
            client,
            "/auth/login",
            preauth.json()["csrf_token"],
            {
                "login": "operator",
                "password": INITIAL,
            },
        )
        assert response.status_code == 429


async def test_local_command_identity_authority_duplicate_and_state_enforcement(environment):
    env = environment
    session = await sign_in(env.client)
    csrf = session.json()["csrf_token"]
    await ready(env, csrf)
    spoofed = await post(env.client, "/commands", csrf, command(env, requested_by="viewer"))
    assert spoofed.status_code == 403
    payload = command(env)
    response = await post(env.client, "/commands", csrf, payload)
    assert response.status_code == 200 and response.json()["created"]
    snapshot = (await env.client.get("/snapshot")).json()
    assert snapshot["pending_commands"][0]["command_id"] == "load"
    assert snapshot["state"]["pending_command_id"] is None
    assert (
        env.store.connection.execute("SELECT authority FROM runtime_command_authority").fetchone()[
            0
        ]
        == "LOCAL"
    )
    env.controller.tick(now=env.now[0])
    result = await env.client.get("/commands/load")
    assert result.json()["status"] == "SUCCEEDED"
    version = env.controller.state.control_version
    duplicate = await post(env.client, "/commands", csrf, payload)
    assert duplicate.status_code == 200 and not duplicate.json()["created"]
    assert env.controller.state.control_version == version
    assert (await env.client.get("/runs/run-A")).json()["final"] is False
    response = await post(
        env.client,
        "/inputs",
        csrf,
        {
            "inputs": {"temperature_c": 150.0},
            "control_mode": "REMOTE",
            "operation_mode": "AUTO",
        },
    )
    assert response.status_code == 200
    rejected = await post(
        env.client, "/commands", csrf, command(env, "START", command_id="remote-denied")
    )
    assert rejected.json()["submission"]["disposition"] == "REJECTED"
    assert rejected.json()["submission"]["reason_code"] == "REMOTE_MODE"
    assert not env.controller.state.run_output
    assert all(
        row[0] == "operator"
        for row in env.store.connection.execute(
            "SELECT actor FROM console_request_audit WHERE action<>'REQUEST_REJECTED'"
        )
    )


async def test_command_wire_uint64_rejects_numeric_value_and_validation_does_not_echo_secrets(
    environment,
):
    env = environment
    session = await sign_in(env.client)
    csrf = session.json()["csrf_token"]
    payload = command(env)
    payload["expected_control_version"] = 0
    assert (await post(env.client, "/commands", csrf, payload)).status_code == 422
    secret = "secret input must not be echoed"
    response = await post(env.client, "/auth/password", csrf, {"new_password": {"secret": secret}})
    assert response.status_code == 422 and secret not in response.text
    response = await env.client.post(
        "/auth/password",
        content=b"{" + secret.encode(),
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    assert response.status_code == 422 and secret not in response.text


async def test_session_expiry_logout_and_auth_only_in_cookie(environment):
    env = environment
    session = await sign_in(env.client)
    csrf = session.json()["csrf_token"]
    token = env.client.cookies[SESSION_COOKIE]
    response = await post(env.client, "/auth/logout", csrf)
    assert response.status_code == 200 and SESSION_COOKIE not in env.client.cookies
    response = await env.client.get("/snapshot", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 401
    env.client.cookies.set(SESSION_COOKIE, token, domain="simulator.example.test", path="/")
    assert (await env.client.get("/snapshot")).status_code == 401
    await sign_in(env.client, password=CHANGED, change=False)
    env.now[0] += ABSOLUTE_TIMEOUT
    assert (await env.client.get("/snapshot")).status_code == 401


async def test_inputs_and_operator_audit_rollback_together_when_audit_insert_fails(environment):
    env = environment
    session = await sign_in(env.client)
    csrf = session.json()["csrf_token"]
    before = env.controller.state
    with env.store.transaction():
        env.store.connection.execute(
            "CREATE TRIGGER fail_console_audit BEFORE INSERT ON console_request_audit "
            "WHEN NEW.action='INPUTS_CHANGED' "
            "BEGIN SELECT RAISE(ABORT,'injected audit failure'); END"
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app, raise_app_exceptions=False),
        base_url=ORIGIN,
        cookies=env.client.cookies,
    ) as client:
        response = await post(client, "/inputs", csrf, {"inputs": {"estop": True}})
    assert response.status_code == 500
    assert env.controller.state == before
    assert not env.store.connection.in_transaction
    assert (
        env.store.connection.execute(
            "SELECT count(*) FROM console_request_audit WHERE action='INPUTS_CHANGED'"
        ).fetchone()[0]
        == 0
    )


async def test_explicit_recovery_checks_and_scrap_require_operator_evidence(environment):
    env = environment
    session = await sign_in(env.client)
    csrf = session.json()["csrf_token"]
    failed = await post(env.client, "/recover", csrf, {**CHECKS, "positions_verified": False})
    assert failed.status_code == 409 and env.controller.state.recovery_required
    await load(env, csrf)
    started = await post(env.client, "/commands", csrf, command(env, "START", command_id="start"))
    assert started.json()["submission"]["disposition"] == "ACCEPTED"
    env.controller.tick(now=env.now[0])
    assert env.controller.state.wip_quantity == 1
    response = await post(env.client, "/inputs", csrf, {"inputs": {"estop": True}})
    assert response.status_code == 200 and not env.controller.state.run_output
    process = (await env.client.get("/snapshot")).json()["process"]
    item_id = next(item["item_id"] for item in process["slots"] if item)
    body = {"item_ids": [item_id], "reason": "explicit fixture scrap", "positions_verified": True}
    assert (
        await post(env.client, "/scrap", csrf, {**body, "positions_verified": False})
    ).status_code == 409
    assert (await post(env.client, "/scrap", csrf, {**body, "reason": ""})).status_code == 422
    response = await post(env.client, "/scrap", csrf, body)
    assert response.status_code == 200 and response.json()["wip_quantity"] == 0
    summary = (await env.client.get("/runs/run-A")).json()
    assert summary["scrap_quantity"] == "1"
    audit = env.store.connection.execute(
        "SELECT actor,details_json FROM console_request_audit WHERE action='WIP_SCRAPPED'"
    ).fetchone()
    assert (
        audit["actor"] == "operator"
        and json.loads(audit["details_json"])["reason"] == body["reason"]
    )


async def test_local_http_load_start_pause_end_and_explicit_settlement(environment):
    env = environment
    session = await sign_in(env.client)
    csrf = session.json()["csrf_token"]
    await load(env, csrf)
    for kind, identifier in (("START", "start"), ("PAUSE", "pause"), ("END_RUN", "end")):
        response = await post(
            env.client,
            "/commands",
            csrf,
            command(
                env,
                kind,
                command_id=identifier,
            ),
        )
        assert response.status_code == 200, response.text
        assert response.json()["submission"]["disposition"] == "ACCEPTED", response.text
        for _ in range(10):
            env.controller.tick(now=env.now[0])
            result = await env.client.get("/commands/" + identifier)
            if result.json()["status"] != "ACCEPTED":
                break
        assert result.json()["status"] == "SUCCEEDED", result.text
    summary = (await env.client.get("/runs/run-A")).json()
    assert summary["final"] and summary["wip_quantity"] == 0
    assert summary["input_quantity"] == "1" and summary["good_quantity"] == "1"
    assert not env.controller.state.previous_execution_settled
    response = await post(env.client, "/runs/run-A/settle", csrf, {"operator_confirmed": False})
    assert response.status_code == 409
    response = await post(env.client, "/runs/run-A/settle", csrf, {"operator_confirmed": True})
    assert response.status_code == 200 and response.json()["previous_execution_settled"]
    assert (await env.client.get("/runs/run-A")).json() == summary
    payload = command(env, command_id="second-load")
    payload["execution_id"] = "run-B"
    response = await post(env.client, "/commands", csrf, payload)
    assert (
        response.status_code == 200 and response.json()["submission"]["disposition"] == "ACCEPTED"
    )


async def test_body_limit_and_no_wildcard_cors(environment):
    response = await environment.client.post("/auth/login", content=b"x" * 16_385)
    assert response.status_code == 413
    response = await environment.client.options(
        "/inputs",
        headers={
            "Origin": "https://evil.example.test",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in response.headers


async def test_real_static_console_assets_and_safe_paths(environment):
    client = environment.client
    for path, content_type in (
        ("/", "text/html"),
        ("/static/console.js", "javascript"),
        ("/static/console.css", "text/css"),
    ):
        response = await client.get(path)
        assert response.status_code == 200, response.text
        assert content_type in response.headers["content-type"]
        assert response.headers["Cache-Control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert SESSION_COOKIE not in client.cookies
    assert (await client.get("/static/%2e%2e/auth.py")).status_code == 404
    assert (await client.get("/", headers={"Host": "untrusted.example.test"})).status_code == 400


@pytest.mark.parametrize(
    "origin,dev",
    [
        ("http://simulator.example.test", False),
        ("http://simulator.example.test", True),
        ("http://127.0.0.1:8001", False),
        ("*", False),
        (ORIGIN + "/", False),
    ],
)
async def test_secure_transport_configuration(origin, dev):
    with pytest.raises(ValueError):
        ConsoleConfig(origin=origin, allow_insecure_loopback=dev)


async def test_explicit_loopback_development_cookie_is_http_only(environment):
    env = environment
    origin = "http://127.0.0.1:8001"
    auth = SimulatorAuth(env.store.connection, allowed_origins={origin}, clock=lambda: env.now[0])
    app = create_app(
        env.controller, auth, ConsoleConfig(origin=origin, allow_insecure_loopback=True)
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
        response = await client.post(
            "/auth/challenge",
            headers={"Origin": origin, "X-Simulator-Request": "1"},
        )
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" not in response.headers["set-cookie"]


async def test_sensor_offset_operator_input_immediate_readback_and_invalid_auth(environment):
    env = environment
    authenticated = await sign_in(env.client)
    csrf = authenticated.json()["csrf_token"]
    await ready(env, csrf)
    env.controller.submit(
        CommandRequest.model_validate(command(env)), now=env.now[0], authority="LOCAL"
    )
    env.controller.tick(now=env.now[0])
    before = env.controller.storage.load_execution().temperature_c
    state = env.controller.state
    biased = state.inputs.model_dump(mode="json") | {"temperature_offset_c": 15.0}
    denied = await post(env.client, "/inputs", "incorrect-csrf", {"inputs": biased})
    assert denied.status_code == 403
    assert env.controller.state.inputs.temperature_offset_c == 0.0
    accepted = await post(env.client, "/inputs", csrf, {"inputs": biased})
    assert accepted.status_code == 200
    assert accepted.json()["inputs"]["temperature_c"] == before + 15.0
    snapshot = (await env.client.get("/snapshot")).json()
    assert snapshot["process"]["temperature_c"] == before
    assert snapshot["tags"]["Sealer.TemperaturePV"] == before + 15.0
    assert not snapshot["state"]["ready"]
    audit = env.store.connection.execute(
        "SELECT details_json FROM console_request_audit "
        "WHERE action='INPUTS_CHANGED' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert audit is not None and "temperature_offset_c" in audit[0]
    invalid = await post(
        env.client, "/inputs", csrf, {"inputs": biased | {"temperature_offset_c": 51.0}}
    )
    assert invalid.status_code == 422
    assert env.controller.state.inputs.temperature_offset_c == 15.0
    cleared = await post(
        env.client, "/inputs", csrf, {"inputs": biased | {"temperature_offset_c": 0.0}}
    )
    assert cleared.status_code == 200
    assert cleared.json()["inputs"]["temperature_c"] == before
    assert not env.controller.state.run_output


async def test_sensor_offset_viewer_cannot_change_measured_temperature(environment):
    env = environment
    session = await sign_in(env.client, login="viewer")
    before = env.controller.state
    response = await post(
        env.client,
        "/inputs",
        session.json()["csrf_token"],
        {"inputs": before.inputs.model_dump(mode="json") | {"temperature_offset_c": -15.0}},
    )
    assert response.status_code == 403
    assert env.controller.state == before


@pytest.mark.parametrize("identity", ["ordinary", "a/b", ".", "..", "a%2Fb", "작업-é-Ａ"])
async def test_query_identity_roundtrip_and_local_settlement(environment, identity):
    env = environment
    csrf = (await sign_in(env.client)).json()["csrf_token"]
    await ready(env, csrf)
    payload = command(env, command_id=identity)
    payload["execution_id"] = identity
    response = await post(env.client, "/commands", csrf, payload)
    assert response.status_code == 200, response.text
    assert response.json()["submission"]["disposition"] == "ACCEPTED"
    env.controller.tick(now=env.now[0])
    command_path = "/command-record?" + urlencode({"command_id": identity})
    result = await env.client.get(command_path)
    assert result.status_code == 200 and result.json()["command_id"] == identity
    assert result.json()["status"] == "SUCCEEDED"
    summary_path = "/run-summary?" + urlencode({"execution_id": identity})
    summary = await env.client.get(summary_path)
    assert summary.status_code == 200 and summary.json()["execution_id"] == identity
    end = command(env, "END_RUN", command_id="finish-exact-identity")
    end["execution_id"] = identity
    response = await post(env.client, "/commands", csrf, end)
    assert response.status_code == 200, response.text
    for _ in range(10):
        env.controller.tick(now=env.now[0])
        if env.controller.get_result(end["command_id"]).status != "ACCEPTED":
            break
    assert env.controller.get_result(end["command_id"]).status == "SUCCEEDED"
    final = (await env.client.get(summary_path)).json()
    assert final["final"] and final["execution_id"] == identity
    settle_path = "/run-settlement?" + urlencode({"execution_id": identity})
    denied = await post(env.client, settle_path, "wrong", {"operator_confirmed": True})
    assert denied.status_code == 403
    assert not env.controller.state.previous_execution_settled
    response = await post(env.client, settle_path, csrf, {"operator_confirmed": True})
    assert response.status_code == 200 and response.json()["previous_execution_settled"]
    assert (await env.client.get(summary_path)).json() == final
    assert (await env.client.get(command_path)).json() == result.json()


@pytest.mark.parametrize(
    "path,key,method",
    [
        ("/command-record", "command_id", "GET"),
        ("/run-summary", "execution_id", "GET"),
        ("/run-settlement", "execution_id", "POST"),
        ("/command-reconciliation", "command_id", "POST"),
    ],
)
async def test_identity_alias_rejects_missing_duplicate_extra_invalid_query(
    environment, path, key, method
):
    env = environment
    csrf = (await sign_in(env.client)).json()["csrf_token"]
    before = env.controller.state.model_dump(mode="json")
    for query in ("", f"{key}=a&{key}=b", f"{key}=a&extra=b", f"{key}=", f"{key}=has+space"):
        response = await env.client.request(
            method,
            path + "?" + query,
            json=CHECKS if method == "POST" else None,
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        assert response.status_code == 422, response.text
    assert env.controller.state.model_dump(mode="json") == before


@pytest.mark.parametrize("identity", ["original/pending", ".."])
async def test_query_reconciliation_preserves_original_id_after_real_reopen(environment, identity):
    env = environment
    csrf = (await sign_in(env.client)).json()["csrf_token"]
    await load(env, csrf)
    pending = command(env, "START", command_id=identity)
    response = await post(env.client, "/commands", csrf, pending)
    assert response.status_code == 200
    assert response.json()["submission"]["disposition"] == "ACCEPTED"
    path = env.store.path
    env.store.close()
    env.now[0] += timedelta(seconds=8)
    with SimulatorStorage(path) as reopened:
        controller = SimulatorController(reopened, now=env.now[0])
        auth = SimulatorAuth(
            reopened.connection, allowed_origins={ORIGIN}, clock=lambda: env.now[0]
        )
        app = create_app(controller, auth, env.config)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as client:
            csrf = (await sign_in(client, password=CHANGED, change=False)).json()["csrf_token"]
            lookup = "/command-record?" + urlencode({"command_id": identity})
            original = (await client.get(lookup)).json()
            assert original["command_id"] == identity and original["status"] == "OUTCOME_UNKNOWN"
            reconcile = "/command-reconciliation?" + urlencode({"command_id": identity})
            response = await post(client, reconcile, csrf, CHECKS)
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["command_id"] == identity and result["status"] == "FAILED"
            assert result["reason_code"] == "RESTART_INTERRUPTED"
            assert (await client.get(lookup)).json() == result
            assert controller.state.recovery_required and not controller.state.run_output
            repeated = await post(client, reconcile, csrf, CHECKS)
            assert repeated.status_code == 409
            assert (await client.get(lookup)).json() == result
