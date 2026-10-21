"""Service HTTP intake using the shared disposable real PostgreSQL fixture."""

from __future__ import annotations

import hashlib
import json
import secrets
import socket
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, text

from apps.api.bootstrap import ASSET_IDS, LINE_ID, bootstrap_reference_data
from apps.api.ingestion import IngestionService
from apps.api.internal import InternalConfig, ServiceCredential, create_internal_app
from contracts.models import Parameters
from contracts.transport import BatchBody, DurableAck, GatewayBatch, batch_digest, validate_ack

pytestmark = pytest.mark.asyncio
AT = datetime(2026, 10, 20, 12, 1, tzinfo=UTC)
ORIGIN = "https://api.example.test"
EXAMPLE = Path(__file__).resolve().parents[2] / "contracts/examples/transport-batch.json"


def batch(*, quantity=1, source_id="simulator-1", stream_id="stream-1", gateway_id="gateway-1"):
    raw = json.loads(EXAMPLE.read_text())["body"]
    raw.update(source_id=source_id, stream_id=stream_id, gateway_id=gateway_id)
    for event in raw["events"]:
        event.update(source_id=source_id, stream_id=stream_id, equipment_id=ASSET_IDS["Outfeed"])
        event["payload"]["quantity"] = str(quantity)
    body = BatchBody.model_validate(raw)
    return GatewayBatch(body=body, content_sha256=batch_digest(body))


def binding(tmp_path, **changes):
    token = secrets.token_urlsafe(32)
    path = tmp_path / (secrets.token_hex(8) + ".secret")
    path.write_text(token + "\n")
    path.chmod(0o600)
    credential = ServiceCredential.from_secret_file(
        key_id="gateway-key-1",
        gateway_id="gateway-1",
        source_id="simulator-1",
        stream_id="stream-1",
        secret_file=path,
        expires_at=AT + timedelta(hours=1),
    )
    return replace(credential, **changes), token


@pytest.fixture
async def environment(migrated, tmp_path):
    bootstrap_reference_data(migrated)
    service = IngestionService(migrated)
    service.configure_source(
        source_id="simulator-1",
        stream_id="stream-1",
        gateway_id="gateway-1",
        line_id=LINE_ID,
        capture_generation=1,
        now=AT,
    )
    service.register_execution(
        execution_id="run-A",
        source_id="simulator-1",
        stream_id="stream-1",
        work_order_id="work-A",
        recipe_version="recipe-1",
        counter_epoch="epoch-1",
        parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
        now=AT,
    )
    credential, token = binding(tmp_path)
    config = InternalConfig(clock=lambda: AT)
    app = create_internal_app(service, [credential], config=config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        yield SimpleNamespace(
            engine=migrated,
            service=service,
            credential=credential,
            token=token,
            client=client,
            config=config,
            app=app,
        )


async def submit(client, token, value=None, **kwargs):
    raw = (value or batch()).model_dump_json()
    return await client.post(
        "/internal/v1/batches",
        content=raw,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        **kwargs,
    )


async def test_authenticated_ack_is_visible_from_independent_postgres_connection(environment):
    env = environment
    value = batch()
    response = await submit(env.client, env.token, value)
    assert response.status_code == 200, response.text
    ack = DurableAck.model_validate_json(response.content)
    validate_ack(value, ack)
    assert ack.durability == "COMMITTED" and ack.events[0].disposition == "APPLIED"
    with env.engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM ingest_batches")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM production_events")).scalar_one() == 1
        assert (
            connection.execute(text("SELECT good_quantity FROM execution_totals")).scalar_one() == 1
        )
        stored = connection.execute(text("SELECT ack FROM ingest_batches")).scalar_one()
        assert stored == response.json()
    assert env.token not in response.text
    assert response.headers["Cache-Control"] == "no-store"


async def test_lost_ack_retry_returns_same_receipt_without_duplicate_application(environment):
    env = environment
    first = await submit(env.client, env.token)
    retry = await submit(env.client, env.token)
    assert first.status_code == retry.status_code == 200
    assert first.json() == retry.json()
    with env.engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM ingest_batches")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM event_inbox")).scalar_one() == 1
        assert (
            connection.execute(text("SELECT good_quantity FROM execution_totals")).scalar_one() == 1
        )


@pytest.mark.parametrize(
    "header", [None, "Basic invalid", "Bearer wrong", "Bearer " + secrets.token_urlsafe(32)]
)
async def test_missing_or_invalid_service_credentials_are_401(environment, header):
    headers = {} if header is None else {"Authorization": header}
    response = await environment.client.post(
        "/internal/v1/batches",
        content=batch().model_dump_json(),
        headers=headers,
    )
    assert response.status_code == 401 and "durability" not in response.json()
    with environment.engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM ingest_batches")).scalar_one() == 0


async def test_browser_cookie_and_payload_identity_do_not_grant_service_authority(environment):
    env = environment
    env.client.cookies.set("simulator_session", env.token)
    response = await env.client.post(
        "/internal/v1/batches",
        content=batch().model_dump_json(),
        headers={"X-User-Role": "equipment_admin", "X-Gateway-Id": "gateway-1"},
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "field,value",
    [
        ("gateway_id", "other-gateway"),
        ("source_id", "other-source"),
        ("stream_id", "other-stream"),
    ],
)
async def test_valid_token_cannot_choose_another_payload_scope(environment, field, value):
    env = environment
    response = await submit(env.client, env.token, batch(**{field: value}))
    assert response.status_code == 403 and "durability" not in response.json()


async def test_configured_credential_for_unknown_source_still_rejected_by_persisted_binding(
    environment,
):
    env = environment
    credential = replace(env.credential, source_id="not-configured")
    app = create_internal_app(env.service, [credential], config=env.config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await submit(client, env.token, batch(source_id="not-configured"))
    assert response.status_code == 403


@pytest.mark.parametrize("change", [{"revoked": True}, {"expires_at": AT}])
async def test_revoked_and_expired_service_credentials_are_401(environment, change):
    env = environment
    app = create_internal_app(env.service, [replace(env.credential, **change)], config=env.config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        assert (await submit(client, env.token)).status_code == 401


@pytest.mark.parametrize(
    "mutation", ["invalid_json", "bad_hash", "numeric_uint64", "extra_identity"]
)
async def test_invalid_batch_has_no_ack_and_does_not_echo_input(environment, mutation):
    env = environment
    raw = batch().model_dump(mode="json")
    marker = "private invalid input must not be echoed"
    if mutation == "bad_hash":
        raw["content_sha256"] = "0" * 64
    elif mutation == "numeric_uint64":
        raw["body"]["capture_seq"] = 1
    elif mutation == "extra_identity":
        raw["authenticated_gateway_id"] = marker
    content = ("{" + marker) if mutation == "invalid_json" else json.dumps(raw)
    response = await env.client.post(
        "/internal/v1/batches",
        content=content,
        headers={"Authorization": "Bearer " + env.token},
    )
    assert response.status_code == 422
    assert "durability" not in response.json() and marker not in response.text
    with env.engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM ingest_batches")).scalar_one() == 0


async def test_same_batch_id_changed_content_returns_409_without_ack(environment):
    env = environment
    assert (await submit(env.client, env.token)).status_code == 200
    response = await submit(env.client, env.token, batch(quantity=2))
    assert response.status_code == 409 and "durability" not in response.json()
    with env.engine.connect() as connection:
        assert (
            connection.execute(text("SELECT good_quantity FROM execution_totals")).scalar_one() == 1
        )
        assert connection.execute(text("SELECT count(*) FROM ingest_conflicts")).scalar_one() == 1


async def test_actual_postgres_commit_failure_returns_503_and_rolls_back_all_receipts(environment):
    env = environment
    with env.engine.begin() as connection:
        connection.execute(
            text("""
            CREATE FUNCTION fail_deferred_intake_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected deferred commit failure'; END; $$
        """)
        )
        connection.execute(
            text("""
            CREATE CONSTRAINT TRIGGER fail_deferred_intake
            AFTER INSERT ON ingest_batches DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION fail_deferred_intake_commit()
        """)
        )
    response = await submit(env.client, env.token)
    assert response.status_code == 503 and "durability" not in response.json()
    assert "injected" not in response.text and env.token not in response.text
    with env.engine.connect() as connection:
        for table in ("ingest_batches", "event_inbox", "production_events"):
            assert connection.execute(text("SELECT count(*) FROM " + table)).scalar_one() == 0
        assert (
            connection.execute(text("SELECT good_quantity FROM execution_totals")).scalar_one() == 0
        )


async def test_body_size_bound_is_enforced_on_actual_stream(environment):
    env = environment
    response = await env.client.post(
        "/internal/v1/batches",
        content=b"x" * (4 * 1024 * 1024 + 1),
        headers={"Authorization": "Bearer " + env.token},
    )
    assert response.status_code == 413


async def test_ready_requires_service_auth_and_exact_supported_postgres_revision(environment):
    env = environment
    assert (await env.client.get("/health/live")).json() == {"status": "alive"}
    assert (await env.client.get("/health/ready")).status_code == 401
    headers = {"Authorization": "Bearer " + env.token}
    assert (await env.client.get("/health/ready", headers=headers)).json() == {"status": "ready"}
    with env.engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num='unsupported'"))
    response = await env.client.get("/health/ready", headers=headers)
    assert response.status_code == 503 and "unsupported" not in response.text


async def test_real_database_connection_failure_is_safe_503(environment):
    env = environment
    with socket.socket() as unavailable_port:
        unavailable_port.bind(("127.0.0.1", 0))
        port = unavailable_port.getsockname()[1]
        url = env.engine.url.set(port=port, query={"connect_timeout": "1"})
        unavailable = create_engine(url, hide_parameters=True)
        try:
            app = create_internal_app(
                IngestionService(unavailable), [env.credential], config=env.config
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN
            ) as client:
                response = await submit(client, env.token)
                ready = await client.get(
                    "/health/ready", headers={"Authorization": "Bearer " + env.token}
                )
            assert response.status_code == ready.status_code == 503
            assert env.token not in response.text and "postgres" not in response.text
        finally:
            unavailable.dispose()


async def test_remote_plain_http_rejected_and_explicit_loopback_development_works(environment):
    env = environment
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://remote.test"
    ) as client:
        assert (await submit(client, env.token)).status_code == 403
    config = replace(env.config, allow_insecure_loopback=True)
    app = create_internal_app(env.service, [env.credential], config=config)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        assert (await submit(client, env.token)).status_code == 200
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.1", 1234)),
        base_url="http://127.0.0.1:8000",
    ) as client:
        assert (await submit(client, env.token)).status_code == 403


async def test_no_execution_registration_or_browser_cors_route(environment):
    env = environment
    response = await env.client.post("/internal/v1/executions", json={"role": "equipment_admin"})
    assert response.status_code == 404
    response = await env.client.options(
        "/internal/v1/batches",
        headers={
            "Origin": "https://browser.example.test",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in response.headers


async def test_explicit_secret_provisioning_only_stores_digest_and_hides_repr(tmp_path):
    credential, token = binding(tmp_path)
    assert credential.token_digest == hashlib.sha256(token.encode()).digest()
    assert token not in repr(credential) and "token_digest" not in repr(credential)


@pytest.mark.parametrize(
    "kind", ["missing", "world_readable", "symlink", "directory", "short", "malformed"]
)
async def test_secret_file_validation_has_safe_errors_and_no_defaults(tmp_path, kind):
    token = secrets.token_urlsafe(32)
    path = tmp_path / "credential.secret"
    if kind == "directory":
        path.mkdir()
    elif kind != "missing":
        path.write_text(
            "short" if kind == "short" else token + ("!" if kind == "malformed" else "\n")
        )
        path.chmod(0o644 if kind == "world_readable" else 0o600)
        if kind == "symlink":
            other = tmp_path / "link.secret"
            other.symlink_to(path)
            path = other
    with pytest.raises(ValueError) as caught:
        ServiceCredential.from_secret_file(
            key_id="key",
            gateway_id="gateway-1",
            source_id="simulator-1",
            stream_id="stream-1",
            secret_file=path,
            expires_at=AT + timedelta(hours=1),
        )
    assert token not in str(caught.value) and str(path) not in str(caught.value)


async def test_no_empty_or_ambiguous_credential_configuration(environment):
    env = environment
    with pytest.raises(ValueError):
        create_internal_app(env.service, [], config=env.config)
    with pytest.raises(ValueError):
        create_internal_app(env.service, [env.credential, env.credential], config=env.config)


def journal_command(env):
    from apps.api.auth import Principal
    from apps.api.commands import enqueue_command
    from contracts.models import CommandRequest

    request = CommandRequest(
        command_id="remote-start",
        target_id="Line",
        execution_id="run-A",
        command={"kind": "START", "args": {}},
        requested_by="operator-a",
        requested_at=AT,
        expires_at=AT + timedelta(seconds=5),
        expected_control_version=3,
    )
    with env.engine.begin() as connection:
        enqueue_command(
            connection,
            request,
            principal=Principal("operator-a", frozenset({"operator"}), False),
            line_id=LINE_ID,
            source_id="simulator-1",
            stream_id="stream-1",
            now=AT,
        )
    return request


async def command_post(env, action, payload):
    return await env.client.post(
        "/internal/v1/commands/" + action,
        json=payload,
        headers={"Authorization": "Bearer " + env.token},
    )


async def test_command_ticket_is_committed_once_and_lost_response_queries_original(environment):
    env = environment
    request = journal_command(env)
    claimed = await command_post(env, "claim", {})
    assert claimed.status_code == 200
    assert claimed.json()["work"]["action"] == "SUBMIT"
    assert claimed.json()["work"]["request"] == request.model_dump(mode="json")
    with env.engine.connect() as connection:
        assert (
            connection.execute(text("SELECT status FROM command_requests")).scalar_one()
            == "OUTCOME_UNKNOWN"
        )
    # Discard the first response as though it was lost in transit.
    again = await command_post(env, "claim", {})
    assert again.json()["work"]["action"] == "QUERY"
    assert again.json()["work"]["request"]["command_id"] == request.command_id
    assert "command" not in again.json()["work"]["request"]


async def test_command_receipt_is_scope_checked_committed_and_terminal(environment):
    from contracts.models import CommandSubmission

    env = environment
    request = journal_command(env)
    await command_post(env, "claim", {})
    submission = CommandSubmission(
        command_id=request.command_id,
        disposition="REJECTED",
        received_at=AT,
        reason_code="PLC_NOT_READY",
        reason="PLC rejected the readiness condition",
    )
    foreign = submission.model_copy(update={"command_id": "other-source-command"})
    assert (
        await command_post(env, "submission", foreign.model_dump(mode="json"))
    ).status_code == 403
    response = await command_post(env, "submission", submission.model_dump(mode="json"))
    assert response.json() == {"committed": True, "command_id": request.command_id}
    with env.engine.connect() as connection:
        assert (
            connection.execute(text("SELECT status FROM command_requests")).scalar_one()
            == "REJECTED"
        )
    assert (await command_post(env, "claim", {})).json() == {"work": None}
    # Repeated exact receipt is harmless; malformed or incompatible state cannot overwrite it.
    assert (
        await command_post(env, "submission", submission.model_dump(mode="json"))
    ).status_code == 200
    assert (
        await command_post(env, "result", {"command_id": request.command_id})
    ).status_code == 422


async def test_command_commit_failure_returns_no_ticket_and_rolls_back_reservation(environment):
    env = environment
    journal_command(env)
    with env.engine.begin() as connection:
        connection.execute(
            text("""
            CREATE FUNCTION reject_dispatch_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'forced deferred dispatch failure'; END $$
        """)
        )
        connection.execute(
            text("""
            CREATE CONSTRAINT TRIGGER reject_dispatch_commit AFTER UPDATE ON command_requests
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_dispatch_commit()
        """)
        )
    response = await command_post(env, "claim", {})
    assert response.status_code == 503 and "work" not in response.json()
    with env.engine.begin() as connection:
        assert (
            connection.execute(text("SELECT status FROM command_requests")).scalar_one()
            == "REQUESTED"
        )
        assert (
            connection.execute(text("SELECT count(*) FROM command_request_audit")).scalar_one() == 1
        )
        connection.execute(text("DROP TRIGGER reject_dispatch_commit ON command_requests"))
    assert (await command_post(env, "claim", {})).json()["work"]["action"] == "SUBMIT"


async def test_command_routes_require_service_identity_and_reject_supplied_scope(environment):
    env = environment
    assert (await env.client.post("/internal/v1/commands/claim", json={})).status_code == 401
    assert (await command_post(env, "claim", {"source_id": "foreign"})).status_code == 422
    assert (await command_post(env, "submission", {"password": "not-a-command"})).status_code == 422


async def test_clock_context_reads_original_epoch_without_asserting_sync_or_mutating(environment):
    env = environment
    headers = {"Authorization": "Bearer " + env.token}
    route = "/internal/v1/alarm-clock-context"
    empty = await env.client.get(route, headers=headers)
    assert empty.status_code == 200
    assert empty.json()["latest_capture"] is None
    assert empty.json()["clock_assessment"] == "NOT_VERIFIED"
    raw = json.loads(EXAMPLE.with_name("transport-batch-capture-time.json").read_text())["body"]
    raw["events"] = []
    body = BatchBody.model_validate(raw)
    value = GatewayBatch(body=body, content_sha256=batch_digest(body))
    assert (await submit(env.client, env.token, value)).status_code == 200
    response = await env.client.get(route, headers=headers)
    assert response.status_code == 200
    observed = response.json()
    assert observed["scope"] == {
        "line_id": LINE_ID,
        "gateway_id": "gateway-1",
        "source_id": "simulator-1",
        "stream_id": "stream-1",
        "capture_generation": "1",
    }
    assert observed["clock_assessment"] == "NOT_VERIFIED"
    assert observed["latest_capture"]["capture_time"] == raw["capture_time"]
    assert observed["latest_capture"]["content_sha256"] == value.content_sha256
    assert observed["latest_capture"]["capture_seq"] == "1"
    assert response.headers["Cache-Control"] == "no-store"
    assert env.token not in response.text
    # A later legacy capture must not make an old collector epoch appear current.
    legacy = batch().body.model_dump(mode="json")
    legacy.update(batch_id="legacy-new", capture_seq="2", events=[])
    legacy["samples"][0]["sample_id"] = "legacy-new-sample"
    legacy_body = BatchBody.model_validate(legacy)
    legacy_value = GatewayBatch(body=legacy_body, content_sha256=batch_digest(legacy_body))
    assert (await submit(env.client, env.token, legacy_value)).status_code == 200
    later = await env.client.get(route, headers=headers)
    assert later.json()["latest_capture"]["capture_time"] is None
    assert later.json()["latest_capture"]["capture_seq"] == "2"
    with env.engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM ingest_batches")).scalar_one() == 2
        assert db.execute(text("SELECT count(*) FROM alarm_capture_frontiers")).scalar_one() == 0


async def test_clock_context_rejects_scope_override_and_requires_bound_credentials(environment):
    env = environment
    route = "/internal/v1/alarm-clock-context"
    assert (await env.client.get(route)).status_code == 401
    headers = {"Authorization": "Bearer " + env.token}
    assert (await env.client.get(route + "?source_id=other", headers=headers)).status_code == 422
    env.service.configure_source(
        source_id="other",
        stream_id="other",
        gateway_id="other",
        line_id=LINE_ID,
        capture_generation=1,
        now=AT,
    )
    foreign = batch(source_id="other", stream_id="other", gateway_id="other").body
    env.service.ingest(
        GatewayBatch(body=foreign, content_sha256=batch_digest(foreign)),
        authenticated_gateway_id="other",
        authenticated_source_id="other",
        authenticated_stream_id="other",
        now=AT,
    )
    result = await env.client.get(route, headers=headers)
    assert result.status_code == 200 and result.json()["latest_capture"] is None
