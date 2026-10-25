"""Browser control gate against actual PostgreSQL and the real ASGI adapter."""

import asyncio
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa

from apps.api.auth import UserAuth
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.ingestion import IngestionService
from apps.api.settlement import record_final_summary
from apps.api.user_http import UserHTTPConfig, create_user_app
from contracts.models import CommandRequest, Parameters, RunSummary
from contracts.tags.validate import load_contract
from contracts.transport import BatchBody, GatewayBatch, batch_digest

AT = datetime(2026, 10, 21, 12, tzinfo=UTC)
ORIGIN = "https://hmi.example.test"
SCOPE = {"line_id": LINE_ID, "source_id": "source-1", "stream_id": "stream-1"}
GATEWAY = dict(
    authenticated_gateway_id="gateway-1",
    authenticated_source_id="source-1",
    authenticated_stream_id="stream-1",
)
PARAMETERS = Parameters(temperature_c=150.0, speed_m_per_min=6.0)


def user(auth, login="operator", role="operator"):
    password = secrets.token_urlsafe(24)
    auth.bootstrap_user(login, password, roles={role})
    challenge = auth.begin_login()
    initial = auth.login(
        login,
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=ORIGIN,
    )
    return auth.change_password(
        initial.token, secrets.token_urlsafe(24), csrf_token=initial.csrf_token, origin=ORIGIN
    )


@pytest.fixture
def state(migrated, request):
    bootstrap_reference_data(migrated)
    service = IngestionService(migrated)
    for suffix in ("1", "foreign"):
        service.configure_source(
            source_id=f"source-{suffix}",
            stream_id=f"stream-{suffix}",
            gateway_id=f"gateway-{suffix}",
            line_id=LINE_ID,
            capture_generation=1,
            now=AT,
        )
    service.register_execution(
        execution_id="run-1",
        source_id="source-1",
        stream_id="stream-1",
        work_order_id="job-1",
        recipe_version="recipe-1",
        counter_epoch="epoch-1",
        parameters=PARAMETERS,
        target_quantity=10,
        now=AT,
    )
    clock = [AT]
    auth = UserAuth(migrated, allowed_origins={ORIGIN}, clock=lambda: clock[0])
    session = user(auth)
    app = create_user_app(auth, UserHTTPConfig(origin=ORIGIN))
    result = SimpleNamespace(
        engine=migrated,
        service=service,
        clock=clock,
        auth=auth,
        session=session,
        app=app,
        capture_seq=0,
    )
    # Missing-tag cases must start without that tag. Immutable clock evidence
    # prevents deleting the original intake later to manufacture missing data.
    missing = getattr(request, "param", None) == "missing-temperature"
    capture(result, omit="Sealer.TemperaturePV" if missing else None)
    return result


@pytest.fixture
async def client(state):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=state.app), base_url=ORIGIN
    ) as client:
        client.cookies.set("hmi_session", state.session.token)
        yield client


def capture(
    state,
    *,
    values=None,
    bad=None,
    source_offset=0,
    source_offsets=None,
    unknown_source=False,
    communication_state="GOOD",
    captured=None,
    omit=None,
):
    now = captured or state.clock[0]
    values = {
        "Line.State": 1,
        "Line.ControlMode": 1,
        "Line.ControlVersion": 42,
        "Line.EventSeq": 0,
        "Line.ActiveExecutionId": "run-1",
        "Line.CounterEpoch": "epoch-1",
        **(values or {}),
    }
    state.capture_seq += 1
    samples = []
    for tag in load_contract()["tags"]:
        if tag["logical_name"] == omit:
            continue
        data_type = tag["data_type"]
        value = values.get(
            tag["logical_name"],
            False
            if data_type == "Boolean"
            else ""
            if data_type == "String"
            else 1.0
            if data_type == "Double"
            else 0,
        )
        samples.append(
            {
                "sample_id": f"s-{state.capture_seq}-{tag['logical_name']}",
                "tag_id": tag["logical_name"],
                "source_timestamp": None
                if unknown_source
                else now
                + timedelta(seconds=(source_offsets or {}).get(tag["logical_name"], source_offset)),
                "server_timestamp": None,
                "acquired_at": now,
                "status_code": 0x80000000 if tag["logical_name"] == bad else 0,
                "reading": {"data_type": data_type, "value": value},
            }
        )
    communication = {
        "state": communication_state,
        "connected": True,
        "read_age_seconds": 0.0,
        "heartbeat_ages_seconds": {
            name: 0.0
            for name in (
                "Line.Heartbeat",
                "Infeed.LinkHeartbeat",
                "Sealer.LinkHeartbeat",
                "Outfeed.LinkHeartbeat",
            )
        },
    }
    body = BatchBody(
        schema_version="1.1",
        batch_id=f"capture-{state.capture_seq}",
        gateway_id="gateway-1",
        source_id="source-1",
        stream_id="stream-1",
        capture_generation=1,
        capture_seq=state.capture_seq,
        captured_at=now,
        samples=samples,
        communication=communication,
    )
    return state.service.ingest(
        GatewayBatch(body=body, content_sha256=batch_digest(body)), **GATEWAY, now=now
    )


def body(
    state,
    *,
    command_id="cmd-1",
    kind="START",
    actor="operator",
    execution="run-1",
    args=None,
    **changes,
):
    request = CommandRequest(
        command_id=command_id,
        target_id="Line",
        execution_id=execution,
        command={"kind": kind, "args": args or {}},
        requested_by=actor,
        requested_at=state.clock[0],
        expires_at=state.clock[0] + timedelta(seconds=5),
        expected_control_version=42,
    )
    return {**SCOPE, "request": request.model_dump(mode="json"), **changes}


def headers(state):
    return {"Origin": ORIGIN, "X-CSRF-Token": state.session.csrf_token}


def counts(state):
    with state.engine.connect() as connection:
        return tuple(
            connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in ("command_requests", "command_request_audit")
        )


async def test_authorized_command_is_requested_and_get_is_scoped_authenticated(state, client):
    response = await client.post("/api/v1/commands", headers=headers(state), json=body(state))
    assert response.status_code == 200
    assert response.json()["status"] == "REQUESTED"
    assert response.json()["delivered_at"] is None
    read = await client.get("/api/v1/commands/cmd-1", params=SCOPE)
    assert read.status_code == 200
    assert set(read.json()) == {"request", "result"}
    assert read.json()["request"]["expected_control_version"] == "42"
    for scope in (
        {**SCOPE, "line_id": "foreign-line"},
        {**SCOPE, "source_id": "source-foreign", "stream_id": "stream-foreign"},
    ):
        hidden = await client.get("/api/v1/commands/cmd-1", params=scope)
        assert hidden.status_code == 404
        assert "run-1" not in hidden.text
    client.cookies.clear()
    assert (await client.get("/api/v1/commands/cmd-1", params=SCOPE)).status_code == 401
    assert counts(state) == (1, 1)


@pytest.mark.parametrize("role", ["viewer", "production_manager", "equipment_admin"])
async def test_roles_do_not_inherit_operator_control_and_all_can_read(state, client, role):
    initial = await client.post("/api/v1/commands", headers=headers(state), json=body(state))
    assert initial.status_code == 200
    other = user(state.auth, login=role, role=role)
    client.cookies.set("hmi_session", other.token)
    denied = await client.post(
        "/api/v1/commands",
        headers={"Origin": ORIGIN, "X-CSRF-Token": other.csrf_token},
        json=body(state, actor=role),
    )
    assert denied.status_code == 403
    assert counts(state) == (1, 1)
    read = await client.get("/api/v1/commands/cmd-1", params=SCOPE)
    assert read.status_code == 200
    assert read.json()["result"] == initial.json()


@pytest.mark.parametrize(
    "mutation",
    [
        "bad",
        "stale",
        "unknown_source",
        "clock",
        "local",
        "version",
        "unknown_health",
        "epoch",
        "execution",
        "source_conflict",
        "reconciliation",
        "gap",
    ],
)
async def test_durable_evidence_failures_restrict_control(state, client, mutation):
    request = body(state)
    if mutation == "bad":
        capture(state, bad="Sealer.TemperaturePV")
    elif mutation == "stale":
        state.clock[0] += timedelta(seconds=4)
        request = body(state)
    elif mutation == "unknown_source":
        capture(state, unknown_source=True)
    elif mutation == "clock":
        capture(state, source_offset=3)
    elif mutation == "local":
        capture(state, values={"Line.ControlMode": 0})
    elif mutation == "version":
        request["request"]["expected_control_version"] = "43"
    elif mutation == "unknown_health":
        capture(state, communication_state="UNKNOWN")
    elif mutation == "epoch":
        capture(state, values={"Line.CounterEpoch": "different-epoch"})
    elif mutation == "source_conflict":
        with state.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE source_streams SET conflicted=true WHERE source_id='source-1'")
            )
    elif mutation == "reconciliation":
        with state.engine.begin() as connection:
            connection.execute(
                sa.text(
                    "UPDATE source_streams SET reconciliation_required=true "
                    "WHERE source_id='source-1'"
                )
            )
    elif mutation == "gap":
        gap = BatchBody(
            batch_id="missing-events",
            gateway_id="gateway-1",
            source_id="source-1",
            stream_id="stream-1",
            capture_generation=1,
            capture_seq=state.capture_seq + 1,
            captured_at=AT,
            gaps=[
                {
                    "from_seq": 1,
                    "through_seq": 1,
                    "irrecoverable": False,
                    "detected_at": AT,
                    "reason_code": "MISSING_EVENTS",
                    "reason": "A source event has not arrived",
                }
            ],
        )
        state.service.ingest(
            GatewayBatch(body=gap, content_sha256=batch_digest(gap)), **GATEWAY, now=AT
        )
    else:
        capture(state, values={"Line.ActiveExecutionId": "another-execution"})
    response = await client.post("/api/v1/commands", headers=headers(state), json=request)
    assert response.status_code == 409
    expected_reason = {
        "local": "STATE_NOT_ALLOWED",
        "version": "CONTROL_VERSION_STALE",
        "epoch": "PHYSICAL_EXECUTION_MISMATCH",
        "execution": "PHYSICAL_EXECUTION_MISMATCH",
    }.get(mutation, "FRESH_SOURCE_REQUIRED")
    assert response.json() == {
        "detail": "current evidence does not permit this command",
        "reason_code": expected_reason,
    }
    assert counts(state) == (0, 0)


@pytest.mark.parametrize("state", ["missing-temperature"], indirect=True)
async def test_missing_required_tag_restricts_control(state, client):
    response = await client.post("/api/v1/commands", headers=headers(state), json=body(state))
    assert response.status_code == 409
    assert counts(state) == (0, 0)


async def test_expired_replay_precedes_freshness_and_canonical_collision_never_overwrites(
    state, client
):
    original = body(state)
    first = await client.post("/api/v1/commands", headers=headers(state), json=original)
    state.clock[0] += timedelta(seconds=10)
    replay = await client.post("/api/v1/commands", headers=headers(state), json=original)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    changed = json.loads(json.dumps(original))
    changed["request"]["expected_control_version"] = "99"
    collision = await client.post("/api/v1/commands", headers=headers(state), json=changed)
    assert collision.status_code == 409
    assert collision.json()["reason_code"] == "ID_CONTENT_CONFLICT"
    assert counts(state) == (1, 1)


async def test_concurrent_same_request_writes_one_request_and_audit(state, client):
    request = body(state)
    responses = await asyncio.gather(
        *(client.post("/api/v1/commands", headers=headers(state), json=request) for _ in range(2))
    )
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assert counts(state) == (1, 1)


async def test_csrf_origin_actor_and_unknown_scope_cannot_write(state, client):
    for protection in ({"Origin": ORIGIN}, {**headers(state), "Origin": "https://other.test"}):
        assert (
            await client.post("/api/v1/commands", headers=protection, json=body(state))
        ).status_code == 403
    assert (
        await client.post(
            "/api/v1/commands", headers=headers(state), json=body(state, actor="forged")
        )
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/commands", headers=headers(state), json=body(state, source_id="unknown")
        )
    ).status_code == 404
    assert counts(state) == (0, 0)


@pytest.mark.parametrize("mutation", ["expired", "future", "long_ttl", "json_uint"])
async def test_new_request_time_bounds_and_uint64_wire_are_enforced(state, client, mutation):
    request = body(state)
    if mutation == "expired":
        state.clock[0] += timedelta(seconds=5)
    elif mutation == "future":
        request["request"]["requested_at"] = (AT + timedelta(seconds=3)).isoformat()
    elif mutation == "long_ttl":
        request["request"]["expires_at"] = (AT + timedelta(seconds=6)).isoformat()
    else:
        request["request"]["expected_control_version"] = 42
    response = await client.post("/api/v1/commands", headers=headers(state), json=request)
    assert response.status_code == (422 if mutation == "json_uint" else 409)
    assert counts(state) == (0, 0)


async def test_load_job_matches_durable_plan_and_initial_empty_execution_is_allowed(state, client):
    capture(state, values={"Line.State": 0, "Line.ActiveExecutionId": ""})
    args = {
        "work_order_id": "job-1",
        "recipe_version": "recipe-1",
        "target_quantity": 10,
        "parameters": PARAMETERS.model_dump(),
    }
    mismatch = body(state, kind="LOAD_JOB", args={**args, "target_quantity": 11})
    assert (
        await client.post("/api/v1/commands", headers=headers(state), json=mismatch)
    ).status_code == 409
    response = await client.post(
        "/api/v1/commands", headers=headers(state), json=body(state, kind="LOAD_JOB", args=args)
    )
    assert response.status_code == 200
    assert response.json()["status"] == "REQUESTED"


async def test_load_rejects_registered_execution_from_another_counter_epoch(state, client):
    capture(
        state,
        values={"Line.State": 0, "Line.ActiveExecutionId": "", "Line.CounterEpoch": "new-epoch"},
    )
    args = {
        "work_order_id": "job-1",
        "recipe_version": "recipe-1",
        "target_quantity": 10,
        "parameters": PARAMETERS.model_dump(),
    }
    response = await client.post(
        "/api/v1/commands", headers=headers(state), json=body(state, kind="LOAD_JOB", args=args)
    )
    assert response.status_code == 409
    assert counts(state) == (0, 0)


async def test_load_previous_execution_requires_same_transaction_reconciliation(state, client):
    state.service.register_execution(
        execution_id="run-old",
        source_id="source-1",
        stream_id="stream-1",
        work_order_id="old-job",
        recipe_version="recipe-1",
        counter_epoch="epoch-1",
        parameters=PARAMETERS,
        now=AT,
    )
    capture(
        state,
        values={
            "Line.State": 0,
            "Line.ActiveExecutionId": "run-old",
            "Line.CounterEpoch": "epoch-1",
        },
    )
    args = {
        "work_order_id": "job-1",
        "recipe_version": "recipe-1",
        "target_quantity": 10,
        "parameters": PARAMETERS.model_dump(),
    }
    request = body(state, kind="LOAD_JOB", args=args)
    assert (
        await client.post("/api/v1/commands", headers=headers(state), json=request)
    ).status_code == 409
    summary = RunSummary(
        source_id="source-1",
        stream_id="stream-1",
        execution_id="run-old",
        state="ENDED",
        as_of=AT,
        ended_at=AT,
        counter_epoch="epoch-1",
        recipe_version="recipe-1",
        parameters=PARAMETERS,
        input_quantity=0,
        good_quantity=0,
        reject_quantity=0,
        scrap_quantity=0,
        wip_quantity=0,
        last_event_seq=0,
        final=True,
    )
    with state.engine.begin() as connection:
        assert record_final_summary(connection, summary, **GATEWAY, now=AT).status == "SETTLED"
    assert (
        await client.post("/api/v1/commands", headers=headers(state), json=request)
    ).status_code == 200


async def test_parameters_cannot_rewrite_active_execution_configuration(state, client):
    production = user(state.auth, "production", "production_manager")
    client.cookies.set("hmi_session", production.token)
    protection = {"Origin": ORIGIN, "X-CSRF-Token": production.csrf_token}
    request = body(
        state,
        kind="SET_PARAMETERS",
        actor="production",
        execution=None,
        args={"recipe_version": "recipe-2", "parameters": PARAMETERS.model_dump()},
    )
    assert (
        await client.post("/api/v1/commands", headers=protection, json=request)
    ).status_code == 409
    capture(state, values={"Line.ActiveExecutionId": ""})
    assert (
        await client.post("/api/v1/commands", headers=protection, json=request)
    ).status_code == 200


async def test_event_frontier_and_quarantine_are_checked_before_control(state, client):
    capture(state, values={"Line.EventSeq": 1})
    assert (
        await client.post("/api/v1/commands", headers=headers(state), json=body(state))
    ).status_code == 409
    capture(state, values={"Line.EventSeq": 0})
    example = json.loads(
        (
            Path(__file__).resolve().parents[2] / "contracts/examples/transport-batch.json"
        ).read_text()
    )["body"]["events"][0]
    example.update(source_id="source-1", stream_id="stream-1", execution_id="unknown-execution")
    batch = BatchBody(
        batch_id="quarantined",
        gateway_id="gateway-1",
        source_id="source-1",
        stream_id="stream-1",
        capture_generation=1,
        capture_seq=state.capture_seq + 1,
        captured_at=AT,
        events=[example],
    )
    state.service.ingest(
        GatewayBatch(body=batch, content_sha256=batch_digest(batch)), **GATEWAY, now=AT
    )
    assert (
        await client.post("/api/v1/commands", headers=headers(state), json=body(state))
    ).status_code == 409
    assert counts(state) == (0, 0)


async def test_old_source_time_on_stable_good_values_does_not_block_control(state, client):
    capture(
        state,
        source_offsets={
            tag["logical_name"]: -86400
            for tag in load_contract()["tags"]
            if not tag["logical_name"].endswith("Heartbeat")
        },
    )
    response = await client.post("/api/v1/commands", headers=headers(state), json=body(state))
    assert response.status_code == 200
    assert response.json()["status"] == "REQUESTED"


@pytest.mark.parametrize("offset", [-3, 3])
async def test_heartbeat_clock_skew_restricts_even_with_reported_good_health(state, client, offset):
    capture(state, source_offsets={"Line.Heartbeat": offset})
    response = await client.post("/api/v1/commands", headers=headers(state), json=body(state))
    assert response.status_code == 409
    assert counts(state) == (0, 0)


@pytest.mark.parametrize("path", ["/api/v1/snapshot", "/api/v1/commands/cmd-1"])
async def test_passive_screen_and_command_polling_do_not_extend_idle_session(state, client, path):
    assert (
        await client.post("/api/v1/commands", headers=headers(state), json=body(state))
    ).status_code == 200
    state.clock[0] += timedelta(minutes=29)
    assert (await client.get(path, params=SCOPE)).status_code == 200
    state.clock[0] += timedelta(minutes=1)
    assert (await client.get(path, params=SCOPE)).status_code == 401


async def test_rest_snapshot_waits_for_line_write_then_reads_committed_health(state, client):
    task = None
    try:
        with state.engine.begin() as writer:
            writer.execute(
                sa.text("SELECT id FROM lines WHERE id=:line FOR UPDATE"), {"line": LINE_ID}
            )
            task = asyncio.create_task(client.get("/api/v1/snapshot", params=SCOPE))
            deadline = asyncio.get_running_loop().time() + 5
            while True:
                with state.engine.connect() as observer:
                    waiting = observer.execute(
                        sa.text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE datname=current_database() AND wait_event_type='Lock' "
                            "AND query LIKE 'SELECT lines.id%FOR SHARE%')"
                        )
                    ).scalar_one()
                if waiting:
                    break
                assert not task.done(), "Snapshot escaped the coherent line read boundary"
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.01)
            writer.execute(
                sa.text("UPDATE source_streams SET conflicted=true WHERE source_id='source-1'")
            )
            assert not task.done()
    finally:
        if task is not None:
            response = await asyncio.wait_for(task, timeout=5)
    assert response.status_code == 200
    assert response.json()["health"]["source_conflicted"] is True
    assert response.json()["screen"]["view"]["control_restricted"] is True
