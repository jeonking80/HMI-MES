"""Real PostgreSQL and authenticated ASGI alarm center behavior/recovery."""

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

from apps.api.alarm_lifecycle import AlarmEvidence, AlarmRule, AlarmScope, QualifiedCondition
from apps.api.alarm_qualification import PressureRuleConfig
from apps.api.alarm_store import apply_condition, install_rule
from apps.api.auth import Principal, UserAuth
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.ingestion import IngestionService
from apps.api.user_http import UserHTTPConfig, create_user_app

AT = datetime(2026, 10, 24, tzinfo=UTC)
ORIGIN = "https://alarm-http.test"
SCOPE = AlarmScope(line_id=LINE_ID, source_id="source", stream_id="stream", asset_id="Sealer")
CONFIG = PressureRuleConfig(rule=AlarmRule(rule_id="pressure", version="v1", severity="HIGH"))
QUERY = SCOPE.model_dump() | {"rule_id": "pressure"}


def member(auth, login="operator", roles=frozenset({"operator", "equipment_admin"}), changed=True):
    password = secrets.token_urlsafe(24)
    auth.bootstrap_user(login, password, roles=roles)
    challenge = auth.begin_login()
    session = auth.login(
        login,
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=ORIGIN,
    )
    if changed:
        session = auth.change_password(
            session.token, secrets.token_urlsafe(24), csrf_token=session.csrf_token, origin=ORIGIN
        )
    return session


@pytest.fixture
def state(migrated):
    bootstrap_reference_data(migrated)
    clock = [AT + timedelta(seconds=10)]
    auth = UserAuth(migrated, allowed_origins={ORIGIN}, clock=lambda: clock[0])
    session = member(auth)
    IngestionService(migrated).configure_source(
        source_id="source",
        stream_id="stream",
        gateway_id="gateway",
        line_id=LINE_ID,
        capture_generation=1,
        now=AT,
    )
    with migrated.begin() as db:
        install_rule(
            db,
            SCOPE,
            CONFIG,
            input_id="install",
            principal=Principal("operator", frozenset({"operator", "equipment_admin"}), False),
            at=AT,
        )
    return SimpleNamespace(engine=migrated, auth=auth, session=session, clock=clock)


@pytest.fixture
async def client(state):
    app = create_user_app(state.auth, UserHTTPConfig(origin=ORIGIN))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        client.cookies.set("hmi_session", state.session.token)
        yield client


def condition(
    state, identity="raise", position=1, value="ACTIVE", alarm="alarm-A", rule="pressure"
):
    with state.engine.begin() as db:
        return apply_condition(
            db,
            rule,
            QualifiedCondition(
                event_id=identity,
                scope=SCOPE,
                rule_version="v1",
                position=position,
                condition=value,
                new_alarm_id=alarm,
                evidence=AlarmEvidence(
                    observed_at=AT + timedelta(seconds=position), references=(identity,)
                ),
            ),
        )


def intent(identity="ack", alarm="alarm-A", kind="ACKNOWLEDGE", actor="operator", **changes):
    return (
        dict(
            action_id=identity,
            scope=SCOPE.model_dump(),
            rule_id="pressure",
            alarm_id=alarm,
            actor=actor,
            kind=kind,
        )
        | changes
    )


def protection(state):
    return {"Origin": ORIGIN, "X-CSRF-Token": state.session.csrf_token}


async def act(client, state, body=None, **changes):
    body = body or intent(**changes)
    return await client.post(
        f"/api/v1/alarms/{body['alarm_id']}/actions", json=body, headers=protection(state)
    )


def counts(state):
    with state.engine.connect() as db:
        return {
            name: db.execute(text("SELECT count(*) FROM alarm_" + name)).scalar_one()
            for name in ("instances", "actions", "receipts", "outbox")
        }


async def test_exact_instance_four_states_returned_unack_attention_and_old_ack_isolation(
    client, state
):
    condition(state)

    async def status(alarm="alarm-A"):
        result = await client.get(f"/api/v1/alarms/{alarm}", params=QUERY)
        assert result.status_code == 200
        return result.json()["status"]

    assert await status() == "ACTIVE_UNACKNOWLEDGED"
    assert (await act(client, state)).status_code == 200
    assert await status() == "ACTIVE_ACKNOWLEDGED"
    condition(state, "normal", 2, "NORMAL")
    assert await status() == "RETURNED_ACKNOWLEDGED"
    condition(state, "recurrence", 3, alarm="alarm-B")
    assert (await act(client, state, identity="ack-old-again")).json()["receipt"][
        "disposition"
    ] == "NO_CHANGE"
    assert await status("alarm-B") == "ACTIVE_UNACKNOWLEDGED"
    condition(state, "normal-again", 4, "NORMAL", alarm="alarm-B")
    assert await status("alarm-B") == "RETURNED_UNACKNOWLEDGED"
    attention = await client.get("/api/v1/alarms", params=QUERY)
    assert [item["instance"]["alarm_id"] for item in attention.json()["items"]] == ["alarm-B"]
    all_states = await client.get("/api/v1/alarms", params=QUERY | {"view": "all"})
    assert len(all_states.json()["items"]) == 2
    original_outbox = counts(state)["outbox"]
    assert (await act(client, state, identity="ack-returned", alarm="alarm-B")).status_code == 200
    assert counts(state)["outbox"] == original_outbox


async def test_notes_keep_before_after_history_and_exact_edit_parent(client, state):
    condition(state)
    added = await act(
        client, state, identity="note-add", kind="NOTE_ADD", note_id="note", text="before"
    )
    assert added.status_code == 200
    edited = await act(
        client,
        state,
        identity="note-edit",
        kind="NOTE_EDIT",
        note_id="note",
        text="after",
        expected_previous_action_id="note-add",
    )
    assert edited.status_code == 200
    assert edited.json()["action"]["before"] == "before"
    assert edited.json()["action"]["after"] == "after"
    stale = await act(
        client,
        state,
        identity="stale-edit",
        kind="NOTE_EDIT",
        note_id="note",
        text="stale",
        expected_previous_action_id="note-add",
    )
    assert stale.status_code == 409
    page = await client.get("/api/v1/alarms/alarm-A/actions", params=QUERY | {"limit": 1})
    assert page.status_code == 200 and page.json()["has_more"]
    assert page.json()["items"][0]["action_id"] == "note-edit"
    next_page = await client.get(
        "/api/v1/alarms/alarm-A/actions",
        params=QUERY | {"limit": 1, "before_action_id": page.json()["next_before_action_id"]},
    )
    assert next_page.json()["items"][0]["action_id"] == "note-add"
    assert not next_page.json()["has_more"]


async def test_lost_response_recovery_and_exact_retry_keep_original_server_time(client, state):
    condition(state)
    body = intent("original-action")
    response = await act(client, state, body)
    assert response.status_code == 200
    original = response.json()
    before = counts(state)
    state.clock[0] += timedelta(seconds=15)
    # A refreshed client models recovery after the committed response was lost.
    async with httpx.AsyncClient(transport=client._transport, base_url=ORIGIN) as recovered:
        recovered.cookies.set("hmi_session", state.session.token)
        result = await recovered.get(
            "/api/v1/alarms/alarm-A/action-receipts/original-action", params=QUERY
        )
        assert result.status_code == 200 and result.json() == original
        retried = await act(recovered, state, body)
    assert retried.status_code == 200 and retried.json()["receipt"]["replayed"]
    assert retried.json()["recorded_at"] == original["recorded_at"]
    assert retried.json()["action"] == original["action"] and counts(state) == before
    assert (
        await act(client, state, body | {"kind": "NOTE_ADD", "note_id": "x", "text": "changed"})
    ).status_code == 409
    assert (
        await client.get("/api/v1/alarms/alarm-A/action-receipts/not-committed", params=QUERY)
    ).status_code == 404
    assert counts(state) == before


async def test_no_change_ack_receipt_is_recoverable_without_an_extra_action(client, state):
    condition(state)
    await act(client, state)
    response = await act(client, state, identity="second-ack")
    assert response.status_code == 200
    assert response.json()["action"] is None
    assert response.json()["receipt"]["disposition"] == "NO_CHANGE"
    before = counts(state)
    result = await client.get("/api/v1/alarms/alarm-A/action-receipts/second-ack", params=QUERY)
    assert result.json() == response.json()
    assert counts(state) == before


@pytest.mark.parametrize("role", ["viewer", "operator", "production_manager", "equipment_admin"])
async def test_all_read_roles_and_operator_only_actions(client, state, role):
    condition(state)
    session = member(state.auth, "member", {role})
    client.cookies.set("hmi_session", session.token)
    state.session = session
    assert (await client.get("/api/v1/alarms", params=QUERY)).status_code == 200
    result = await act(client, state, actor="member")
    assert result.status_code == (200 if role == "operator" else 403)


@pytest.mark.parametrize(
    "failure",
    [
        "origin",
        "csrf",
        "duplicate-origin",
        "duplicate-csrf",
        "actor",
        "path",
        "extra",
        "note-on-ack",
        "no-parent",
        "unknown-kind",
        "client-time",
    ],
)
async def test_operator_boundary_rejects_invalid_protection_and_intent(client, state, failure):
    condition(state)
    body, headers = intent(), protection(state)
    expected = 422
    if failure == "origin":
        headers["Origin"] += "/"
        expected = 403
    elif failure == "csrf":
        headers["X-CSRF-Token"] = "x" * 43
        expected = 403
    elif failure.startswith("duplicate"):
        name = "Origin" if failure.endswith("origin") else "X-CSRF-Token"
        headers = [*headers.items(), (name, headers[name])]
        expected = 403
    elif failure == "actor":
        body["actor"] = "foreign"
        expected = 403
    elif failure == "path":
        body["alarm_id"] = "foreign"
    elif failure == "extra":
        body["unexpected"] = True
    elif failure == "note-on-ack":
        body["note_id"] = "note"
    elif failure == "no-parent":
        body.update(kind="NOTE_EDIT", note_id="note", text="edit")
    elif failure == "client-time":
        body["at"] = AT.isoformat()
    else:
        body["kind"] = "RESET_FAULT"
    before = counts(state)
    response = await client.post("/api/v1/alarms/alarm-A/actions", json=body, headers=headers)
    assert response.status_code == expected and counts(state) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"stream_id": "foreign"},
        {"source_id": "foreign"},
        {"line_id": "foreign"},
        {"asset_id": "Outfeed"},
        {"rule_id": "foreign"},
    ],
)
async def test_exact_instance_action_and_receipt_reads_do_not_leak_other_scopes(
    client, state, changes
):
    condition(state)
    await act(client, state)
    before = counts(state)
    for suffix in ("", "/actions", "/action-receipts/ack"):
        response = await client.get("/api/v1/alarms/alarm-A" + suffix, params=QUERY | changes)
        assert response.status_code == 404
    if "rule_id" not in changes:
        body = intent("foreign-scope", scope=SCOPE.model_dump() | changes)
        assert (await act(client, state, body)).status_code == 404
    assert counts(state) == before


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"view": "anything"},
        {"extra": "x"},
        {"before_alarm_id": "foreign"},
    ],
)
async def test_bounded_filters_and_foreign_cursors(client, state, params):
    condition(state)
    response = await client.get("/api/v1/alarms", params=QUERY | params)
    assert response.status_code == 422
    duplicate = list(QUERY.items()) + [("rule_id", "pressure")]
    assert (await client.get("/api/v1/alarms", params=duplicate)).status_code == 422


async def test_pages_across_rules_and_equal_occurrence_times_do_not_repeat(client, state):
    condition(state)
    config = PressureRuleConfig(
        rule=AlarmRule(rule_id="pressure-second", version="v1", severity="LOW")
    )
    with state.engine.begin() as db:
        install_rule(
            db,
            SCOPE,
            config,
            input_id="install-second",
            principal=Principal("operator", frozenset({"equipment_admin"}), False),
            at=AT,
        )
    condition(state, "raise-B", 1, alarm="alarm-B", rule="pressure-second")
    page_query = SCOPE.model_dump() | {"view": "all", "limit": 1}
    first = await client.get("/api/v1/alarms", params=page_query)
    assert first.json()["items"][0]["instance"]["alarm_id"] == "alarm-B"
    second = await client.get(
        "/api/v1/alarms",
        params=page_query | {"before_alarm_id": first.json()["next_before_alarm_id"]},
    )
    assert second.json()["items"][0]["instance"]["alarm_id"] == "alarm-A"
    assert not second.json()["has_more"]
    filtered = await client.get("/api/v1/alarms", params=page_query | {"severity": "HIGH"})
    assert [row["instance"]["alarm_id"] for row in filtered.json()["items"]] == ["alarm-A"]
    assert (
        await client.get(
            "/api/v1/alarms", params=page_query | {"severity": "HIGH", "before_alarm_id": "alarm-B"}
        )
    ).status_code == 422
    assert (
        await client.get("/api/v1/alarms", params=QUERY | {"before_alarm_id": "alarm-B"})
    ).status_code == 422


async def test_concurrent_same_id_retry_and_competing_note_edits_commit_once(client, state):
    condition(state)
    body = intent("same-note", kind="NOTE_ADD", note_id="note", text="before")
    responses = await asyncio.gather(act(client, state, body), act(client, state, body))
    assert [r.status_code for r in responses] == [200, 200]
    assert sum(r.json()["receipt"]["replayed"] for r in responses) == 1
    assert counts(state)["actions"] == 1
    edits = [
        intent(
            name,
            kind="NOTE_EDIT",
            note_id="note",
            text=name,
            expected_previous_action_id="same-note",
        )
        for name in ("edit-A", "edit-B")
    ]
    results = await asyncio.gather(*(act(client, state, body) for body in edits))
    assert sorted(result.status_code for result in results) == [200, 409]
    assert counts(state)["actions"] == 2


@pytest.mark.parametrize("failure", ["idle", "absolute", "revoke", "initial", "role"])
async def test_passive_reads_do_not_renew_idle_and_live_auth_guards_retry(client, state, failure):
    condition(state)
    await act(client, state)
    with state.engine.connect() as db:
        original = db.execute(
            text("SELECT max(last_seen_at) FROM auth_sessions WHERE login='operator'")
        ).scalar_one()
    state.clock[0] += timedelta(minutes=29)
    assert (await client.get("/api/v1/alarms", params=QUERY)).status_code == 200
    assert (
        await client.get("/api/v1/alarms/alarm-A/action-receipts/ack", params=QUERY)
    ).status_code == 200
    with state.engine.connect() as db:
        assert (
            db.execute(
                text("SELECT max(last_seen_at) FROM auth_sessions WHERE login='operator'")
            ).scalar_one()
            == original
        )
    expected = 401
    if failure == "idle":
        state.clock[0] = original + timedelta(minutes=30)
    elif failure == "absolute":
        state.clock[0] = original + timedelta(hours=8)
    elif failure == "revoke":
        state.auth.revoke(state.session.token, csrf_token=state.session.csrf_token, origin=ORIGIN)
    elif failure == "initial":
        session = member(state.auth, "initial", {"operator"}, changed=False)
        client.cookies.set("hmi_session", session.token)
        state.session = session
        expected = 403
    else:
        state.auth.set_user_roles(
            state.session.token,
            "operator",
            {"viewer"},
            csrf_token=state.session.csrf_token,
            origin=ORIGIN,
        )
        expected = 401  # auth_version invalidates this session before retry.
    before = counts(state)
    assert (await act(client, state)).status_code == expected
    if failure != "role":
        assert (await client.get("/api/v1/alarms", params=QUERY)).status_code == expected
    assert counts(state) == before


async def test_size_media_type_missing_scope_and_no_store_install_routes(client, state):
    condition(state)
    assert (
        await client.post("/api/v1/alarms/alarm-A/actions", content="x", headers=protection(state))
    ).status_code == 415
    response = await client.post(
        "/api/v1/alarms/alarm-A/actions",
        content="x" * 16385,
        headers=protection(state) | {"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert (await client.get("/api/v1/alarms")).status_code == 422
    assert (await client.post("/api/v1/alarm-rules", json={})).status_code == 404


async def test_severity_filter_is_exact_identifier_unknown_empty_and_malformed_rejected(
    client, state
):
    condition(state)
    known = await client.get("/api/v1/alarms", params=QUERY | {"severity": "HIGH"})
    assert len(known.json()["items"]) == 1
    unknown = await client.get(
        "/api/v1/alarms", params=QUERY | {"severity": "uninstalled-severity"}
    )
    assert unknown.status_code == 200 and unknown.json()["items"] == []
    assert (
        await client.get("/api/v1/alarms", params=QUERY | {"severity": "bad severity"})
    ).status_code == 422


async def test_empty_list_coverage_reports_installed_channels_without_claiming_live_consumer(
    client, state
):
    before = counts(state)
    result = await client.get("/api/v1/alarms", params=QUERY)
    assert result.status_code == 200 and result.json()["items"] == []
    coverage = result.json()["coverage"]
    assert coverage == {
        "scope": SCOPE.model_dump(),
        "implemented_kinds": ["TEMPERATURE_DEVIATION", "PRESSURE_LOW"],
        "installed_channel_count": 1,
        "installed_channels": [
            {
                "rule_id": "pressure",
                "rule_version": "v1",
                "kind": "PRESSURE_LOW",
                "severity": "HIGH",
            }
        ],
        "has_more_installed_channels": False,
        "consumer_status": "UNKNOWN",
        "runtime_configuration_status": "UNKNOWN",
    }
    # Filtering to unknown severity does not erase evidence that a rule is installed.
    filtered = await client.get("/api/v1/alarms", params=QUERY | {"severity": "missing"})
    assert filtered.json()["coverage"] == coverage
    assert counts(state) == before


async def test_failure_before_commit_rolls_back_action_and_receipt_then_original_id_can_retry(
    client, state, monkeypatch
):
    import apps.api.alarm_http as alarm_http

    condition(state)
    before = counts(state)
    original = alarm_http.submit_operator_action

    def fail_after_writes(*args, **kwargs):
        from sqlalchemy.exc import SQLAlchemyError

        original(*args, **kwargs)
        raise SQLAlchemyError("simulated failure before caller commit")

    monkeypatch.setattr(alarm_http, "submit_operator_action", fail_after_writes)
    body = intent("rollback-action")
    response = await act(client, state, body)
    assert response.status_code == 503 and counts(state) == before
    assert (
        await client.get("/api/v1/alarms/alarm-A/action-receipts/rollback-action", params=QUERY)
    ).status_code == 404
    monkeypatch.setattr(alarm_http, "submit_operator_action", original)
    retry = await act(client, state, body)
    assert retry.status_code == 200 and retry.json()["receipt"]["replayed"] is False
    assert counts(state)["actions"] == before["actions"] + 1


async def test_waiting_http_action_observes_deactivation_committed_under_user_lock(client, state):
    condition(state)
    before = counts(state)
    with state.engine.begin() as blocker:
        blocker.execute(text("SELECT login FROM users WHERE login='operator' FOR UPDATE"))
        task = asyncio.create_task(act(client, state, intent("queued-action")))
        try:
            for _ in range(100):
                with state.engine.connect() as probe:
                    blocked = probe.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock' "
                            "AND pid <> pg_backend_pid() AND query LIKE '%users%'"
                        )
                    ).scalar_one()
                if blocked:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("HTTP action did not wait on live user authorization lock")
            blocker.execute(text("UPDATE users SET active=false WHERE login='operator'"))
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
    response = await asyncio.wait_for(task, 3)
    assert response.status_code == 401 and counts(state) == before
