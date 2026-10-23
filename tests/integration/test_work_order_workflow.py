"""Real PostgreSQL and authenticated ASGI release/binding atomicity and contention."""

import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
from alembic import command
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError

from apps.api.auth import Principal, UserAuth
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.commands import CommandConflict, enqueue_command
from apps.api.database import migration_config
from apps.api.ingestion import IngestionService
from apps.api.user_http import UserHTTPConfig, create_user_app
from apps.api.work_order_workflow import (
    BindIntent,
    ReleaseIntent,
    bind_work_order,
    release_work_order,
)
from apps.api.work_orders import (
    RecipeDefinition,
    RegistrationConflict,
    WorkOrderDefinition,
    register_recipe_version,
    register_work_order,
)
from contracts.models import CommandRequest, Parameters
from contracts.tags.validate import load_contract
from contracts.transport import BatchBody, GatewayBatch, batch_digest

AT = datetime(2026, 10, 23, tzinfo=UTC)
ORIGIN = "https://workflow.example.test"


def account(auth, name, role):
    password = secrets.token_urlsafe(24)
    auth.bootstrap_user(name, password, roles={role} if isinstance(role, str) else set(role))
    challenge = auth.begin_login()
    session = auth.login(
        name,
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=ORIGIN,
    )
    return auth.change_password(
        session.token, secrets.token_urlsafe(24), csrf_token=session.csrf_token, origin=ORIGIN
    )


@pytest.fixture
def state(migrated, request):
    if getattr(request, "param", None) == "legacy17":
        # Exercise the workflow's own downgrade barrier, before schema18 adds
        # independent immutable clock evidence for the fixture's initial capture.
        with migrated.connect() as db:
            command.downgrade(migration_config(db), "0017_guest_viewer")
    bootstrap_reference_data(migrated)
    clock = [AT]
    auth = UserAuth(migrated, allowed_origins={ORIGIN}, clock=lambda: clock[0])
    session = account(auth, "manager", {"production_manager", "operator"})
    principal = Principal("manager", frozenset({"production_manager", "operator"}), False)
    service = IngestionService(migrated)
    service.configure_source(
        source_id="source",
        stream_id="stream",
        gateway_id="gateway",
        line_id=LINE_ID,
        capture_generation=1,
        now=AT,
    )
    with migrated.begin() as db:
        register_recipe_version(
            db,
            RecipeDefinition(
                id="recipe-v1",
                recipe_id="recipe",
                version="v1",
                item_id="item",
                parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
            ),
            actor="manager",
            now=AT,
        )
        for identity in ("work-A", "work-B"):
            register_work_order(
                db,
                WorkOrderDefinition(
                    id=identity,
                    item_id="item",
                    line_id=LINE_ID,
                    recipe_version_id="recipe-v1",
                    target_quantity=4,
                    planned_date="2026-10-24",
                ),
                actor="manager",
                now=AT,
            )
    state = SimpleNamespace(
        engine=migrated,
        auth=auth,
        session=session,
        principal=principal,
        service=service,
        clock=clock,
        capture_seq=0,
    )
    state.app = create_user_app(auth, UserHTTPConfig(origin=ORIGIN))
    capture(state)
    return state


def capture(state, *, values=None, omit=None, bad=None, null=None, quality="GOOD"):
    now = state.clock[0]
    values = {
        "Line.State": 0,
        "Line.ControlMode": 1,
        "Line.ControlVersion": 42,
        "Line.CounterEpoch": "epoch",
        "Line.EventSeq": 0,
        "Line.ActiveExecutionId": "",
        "Line.WipCount": 0,
        **(values or {}),
    }
    state.capture_seq += 1
    samples = []
    for tag in load_contract()["tags"]:
        name, kind = tag["logical_name"], tag["data_type"]
        if name == omit:
            continue
        value = values.get(
            name,
            False
            if kind == "Boolean"
            else ""
            if kind == "String"
            else 0.0
            if kind == "Double"
            else 0,
        )
        samples.append(
            {
                "sample_id": f"s-{state.capture_seq}-{name}",
                "tag_id": name,
                "source_timestamp": now,
                "server_timestamp": now,
                "acquired_at": now,
                "status_code": 0x80000000 if name in {bad, null} else 0,
                "reading": {"data_type": kind, "value": None if name == null else value},
            }
        )
    body = BatchBody(
        schema_version="1.1",
        batch_id=f"batch-{state.capture_seq}",
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        capture_generation=1,
        capture_seq=state.capture_seq,
        captured_at=now,
        samples=samples,
        communication={
            "state": quality,
            "connected": True,
            "read_age_seconds": 3.0 if quality == "DELAYED" else 0.0,
            "heartbeat_ages_seconds": {
                name: 0.0
                for name in (
                    "Line.Heartbeat",
                    "Infeed.LinkHeartbeat",
                    "Sealer.LinkHeartbeat",
                    "Outfeed.LinkHeartbeat",
                )
            },
        },
    )
    state.service.ingest(
        GatewayBatch(body=body, content_sha256=batch_digest(body)),
        authenticated_gateway_id="gateway",
        authenticated_source_id="source",
        authenticated_stream_id="stream",
        now=now,
    )


def release(state, identity="work-A", *, release_id="release-A", actor=None, version=1, clock=None):
    with state.engine.begin() as db:
        return release_work_order(
            db,
            identity,
            ReleaseIntent(release_id=release_id, expected_row_version=version),
            principal=actor or state.principal,
            clock=clock or (lambda: state.clock[0]),
        )


def intent(**changes):
    return BindIntent(
        command_id="load-A",
        line_id=LINE_ID,
        source_id="source",
        stream_id="stream",
        expected_work_order_version=2,
        expected_control_version=42,
        **changes,
    )


def bind(state, identity="work-A", *, definition=None, actor=None, clock=None):
    with state.engine.begin() as db:
        return bind_work_order(
            db,
            identity,
            definition or intent(),
            principal=actor or state.principal,
            clock=clock or (lambda: state.clock[0]),
        )


def scalar(state, sql):
    with state.engine.connect() as db:
        return db.execute(text(sql)).scalar_one()


@pytest.fixture
async def client(state):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=state.app), base_url=ORIGIN
    ) as client:
        client.cookies.set("hmi_session", state.session.token)
        yield client


def headers(state):
    return {"Origin": ORIGIN, "X-CSRF-Token": state.session.csrf_token}


def test_atomic_release_bind_and_exact_receipt_retries_after_state_and_ttl_change(state):
    released = release(state)
    receipt = bind(state)
    assert receipt["load_request"]["command"]["args"]["target_quantity"] == 4
    assert receipt["load_request"]["requested_by"] == "manager"
    assert receipt["work_order_row_version"] == "3"
    assert scalar(state, "SELECT target_quantity FROM execution_plans") == 4
    assert scalar(state, "SELECT counter_epoch FROM production_executions") == "epoch"
    assert scalar(state, "SELECT count(*) FROM line_execution_reservations") == 1
    state.clock[0] += timedelta(days=1)

    def no_new_time():
        raise AssertionError("Replay must not regenerate server time")

    assert release(state, clock=no_new_time) == released
    assert bind(state, clock=no_new_time) == receipt
    assert scalar(state, "SELECT count(*) FROM production_executions") == 1
    assert scalar(state, "SELECT count(*) FROM command_requests") == 1
    assert scalar(state, "SELECT count(*) FROM mes_management_audit") == 5
    for identity, version in (("work-B", 1), ("work-A", 2)):
        with pytest.raises(RegistrationConflict):
            release(state, identity, version=version)
    other = account(state.auth, "other", "production_manager")
    assert other.principal.login == "other"
    with pytest.raises(RegistrationConflict):
        release(state, actor=Principal("other", frozenset({"production_manager"}), False))
    with pytest.raises(RegistrationConflict):
        bind(
            state,
            definition=BindIntent(**{**intent().model_dump(), "expected_control_version": 43}),
        )


@pytest.mark.parametrize(
    "target",
    [
        "work_order_releases",
        "execution_totals",
        "execution_plans",
        "command_requests",
        "work_order_execution_links",
        "line_execution_reservations",
        "mes_management_audit",
    ],
)
def test_each_write_failure_rolls_back_workflow(state, target):
    if target != "work_order_releases":
        release(state)
    before = scalar(state, "SELECT count(*) FROM mes_management_audit")

    def fail(_db, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().startswith("INSERT INTO " + target):
            raise SQLAlchemyError("Injected workflow write failure")

    event.listen(state.engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(SQLAlchemyError):
            release(state) if target == "work_order_releases" else bind(state)
    finally:
        event.remove(state.engine, "before_cursor_execute", fail)
    assert scalar(state, "SELECT count(*) FROM production_executions") == 0
    assert scalar(state, "SELECT count(*) FROM command_requests") == 0
    assert scalar(state, "SELECT count(*) FROM line_execution_reservations") == 0
    assert scalar(state, "SELECT count(*) FROM mes_management_audit") == before
    assert scalar(state, "SELECT row_version FROM work_orders WHERE id='work-A'") == (
        1 if target == "work_order_releases" else 2
    )


@pytest.mark.parametrize(
    "bad_capture",
    [
        {"values": {"Line.State": 2}},
        {"values": {"Line.WipCount": 1}},
        {"values": {"Line.ControlMode": 0}},
        {"bad": "Line.State"},
        {"null": "Line.CounterEpoch"},
        {"omit": "Line.CounterEpoch"},
        {"quality": "DELAYED"},
    ],
)
async def test_invalid_evidence_returns_409_and_no_provisional_execution(
    state, client, bad_capture
):
    release(state)
    if "omit" in bad_capture:
        # The prior valid epoch may be retained while fresh. Let it age before omitting it.
        state.clock[0] += timedelta(seconds=4)
    capture(state, **bad_capture)
    response = await client.post(
        "/api/v1/work-orders/work-A/bind",
        json=intent().model_dump(mode="json"),
        headers=headers(state),
    )
    assert response.status_code == 409
    assert scalar(state, "SELECT count(*) FROM production_executions") == 0
    assert scalar(state, "SELECT count(*) FROM command_requests") == 0


def test_concurrent_different_jobs_reserve_one_execution_and_original_same_intent_replays(state):
    release(state)
    release(state, "work-B", release_id="release-B")
    barrier = Barrier(2)

    def write(identity):
        barrier.wait(timeout=5)
        try:
            return bind(
                state,
                identity,
                definition=BindIntent(
                    **{**intent().model_dump(), "command_id": "load-" + identity}
                ),
            )
        except RegistrationConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ("work-A", "work-B")))
    assert sum(row is not None for row in results) == 1
    assert scalar(state, "SELECT count(*) FROM production_executions") == 1
    assert scalar(state, "SELECT count(*) FROM line_execution_reservations") == 1


async def test_authenticated_workflow_and_protected_draft_low_level_bypass(state, client):
    # A catalog-name alias remains a legacy execution; it cannot create a new managed LOAD.
    state.service.register_execution(
        execution_id="legacy",
        source_id="source",
        stream_id="stream",
        work_order_id="work-A",
        recipe_version="recipe-v1",
        counter_epoch="epoch",
        target_quantity=4,
        parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
        now=AT,
    )
    request = CommandRequest(
        command_id="legacy-draft-bypass",
        target_id="Line",
        execution_id="legacy",
        requested_by="manager",
        requested_at=AT,
        expires_at=AT + timedelta(seconds=5),
        expected_control_version=42,
        command={
            "kind": "LOAD_JOB",
            "args": {
                "work_order_id": "work-A",
                "recipe_version": "recipe-v1",
                "target_quantity": 4,
                "parameters": {"temperature_c": 150.0, "speed_m_per_min": 6.0},
            },
        },
    )
    body = {
        "line_id": LINE_ID,
        "source_id": "source",
        "stream_id": "stream",
        "request": request.model_dump(mode="json"),
    }
    assert (
        await client.post("/api/v1/commands", json=body, headers=headers(state))
    ).status_code == 409
    released = await client.post(
        "/api/v1/work-orders/work-A/release",
        json={"release_id": "release-A", "expected_row_version": "1"},
        headers=headers(state),
    )
    assert released.status_code == 200
    result = await client.post(
        "/api/v1/work-orders/work-A/bind",
        json=intent().model_dump(mode="json"),
        headers=headers(state),
    )
    assert result.status_code == 200
    assert result.json()["execution_id"] != "legacy"
    assert scalar(state, "SELECT count(*) FROM work_order_execution_links") == 1
    assert scalar(state, "SELECT count(*) FROM command_requests") == 1
    assert (
        await client.post(
            "/api/v1/work-orders/work-A/cancel",
            json={"cancellation_id": "cancel-bound-A", "expected_row_version": "3"},
            headers=headers(state),
        )
    ).status_code == 409
    assert scalar(state, "SELECT count(*) FROM work_order_cancellations") == 0
    assert scalar(state, "SELECT count(*) FROM work_order_execution_links") == 1
    assert scalar(state, "SELECT count(*) FROM command_requests") == 1


def test_reservation_blocks_other_legacy_load_and_start_but_not_safety_end(state):
    release(state)
    bind(state)
    state.service.register_execution(
        execution_id="legacy",
        source_id="source",
        stream_id="stream",
        work_order_id="unmanaged",
        recipe_version="recipe-v1",
        counter_epoch="epoch",
        target_quantity=4,
        parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
        now=AT,
    )
    for kind in ("START", "LOAD_JOB", "END_RUN"):
        args = (
            {
                "work_order_id": "unmanaged",
                "recipe_version": "recipe-v1",
                "target_quantity": 4,
                "parameters": {"temperature_c": 150.0, "speed_m_per_min": 6.0},
            }
            if kind == "LOAD_JOB"
            else {}
        )
        request = CommandRequest(
            command_id="legacy-" + kind,
            target_id="Line",
            execution_id="legacy",
            requested_by="manager",
            requested_at=AT,
            expires_at=AT + timedelta(seconds=5),
            expected_control_version=42,
            command={"kind": kind, "args": args},
        )
        if kind != "END_RUN":
            with pytest.raises(CommandConflict), state.engine.begin() as db:
                enqueue_command(
                    db,
                    request,
                    principal=state.principal,
                    line_id=LINE_ID,
                    source_id="source",
                    stream_id="stream",
                    now=AT,
                )
        else:
            with state.engine.begin() as db:
                assert (
                    enqueue_command(
                        db,
                        request,
                        principal=state.principal,
                        line_id=LINE_ID,
                        source_id="source",
                        stream_id="stream",
                        now=AT,
                    ).status
                    == "REQUESTED"
                )


@pytest.mark.parametrize("state", ["legacy17"], indirect=True)
def test_used_workflow_downgrade_refuses_and_preserves_receipt(state):
    initial_revision = scalar(state, "SELECT version_num FROM alembic_version")
    receipt = release(state)
    with pytest.raises(DBAPIError), state.engine.connect() as db:
        command.downgrade(migration_config(db), "0008_work_orders")
    assert release(state) == receipt
    assert scalar(state, "SELECT version_num FROM alembic_version") == initial_revision


@pytest.mark.parametrize("state", ["legacy17"], indirect=True)
def test_unused_workflow_downgrade_and_upgrade_preserve_original_draft_definitions(state):
    with state.engine.connect() as db:
        command.downgrade(migration_config(db), "0008_work_orders")
    assert scalar(state, "SELECT version_num FROM alembic_version") == "0008_work_orders"
    assert scalar(state, "SELECT count(*) FROM work_orders WHERE status='DRAFT'") == 2
    assert scalar(state, "SELECT count(*) FROM mes_management_audit") == 3
    with state.engine.connect() as db:
        command.upgrade(migration_config(db), "0009_work_order_binding")
    assert scalar(state, "SELECT count(*) FROM work_order_execution_links") == 0
    assert release(state)["row_version"] == "2"


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE command_requests SET kind='START' WHERE command_id='load-A'",
        "UPDATE production_executions SET work_order_id='other' WHERE execution_id=:id",
        "UPDATE work_order_execution_links SET line_id='different' WHERE execution_id=:id",
        "UPDATE source_streams SET line_id='different' "
        "WHERE source_id='source' AND stream_id='stream'",
        "UPDATE line_execution_reservations SET execution_id='unlinked'",
    ],
)
def test_composite_foreign_keys_reject_identity_and_scope_mutations(state, sql):
    release(state)
    bound = bind(state)
    with pytest.raises(IntegrityError), state.engine.begin() as db:
        db.execute(text(sql), {"id": bound["execution_id"]})
    assert (
        scalar(state, "SELECT execution_id FROM line_execution_reservations")
        == bound["execution_id"]
    )


async def test_historical_canonical_replay_is_read_only_after_new_line_reservation(state, client):
    state.service.register_execution(
        execution_id="legacy",
        source_id="source",
        stream_id="stream",
        work_order_id="alias-later",
        recipe_version="recipe-v1",
        counter_epoch="epoch",
        target_quantity=4,
        parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
        now=AT,
    )
    old = CommandRequest(
        command_id="old-load",
        target_id="Line",
        execution_id="legacy",
        requested_by="manager",
        requested_at=AT - timedelta(seconds=10),
        expires_at=AT - timedelta(seconds=5),
        expected_control_version=42,
        command={
            "kind": "LOAD_JOB",
            "args": {
                "work_order_id": "alias-later",
                "recipe_version": "recipe-v1",
                "target_quantity": 4,
                "parameters": {"temperature_c": 150.0, "speed_m_per_min": 6.0},
            },
        },
    )
    with state.engine.begin() as db:
        assert (
            enqueue_command(
                db,
                old,
                principal=state.principal,
                line_id=LINE_ID,
                source_id="source",
                stream_id="stream",
                now=AT,
            ).status
            == "EXPIRED"
        )
        register_work_order(
            db,
            WorkOrderDefinition(
                id="alias-later",
                item_id="item",
                line_id=LINE_ID,
                recipe_version_id="recipe-v1",
                target_quantity=4,
                planned_date="2026-10-24",
            ),
            actor="manager",
            now=AT,
        )
    release(state)
    bound = bind(state)
    capture(state, values={"Line.ControlMode": 0})
    before = scalar(state, "SELECT count(*) FROM command_request_audit")
    response = await client.post(
        "/api/v1/commands",
        headers=headers(state),
        json={
            "line_id": LINE_ID,
            "source_id": "source",
            "stream_id": "stream",
            "request": old.model_dump(mode="json"),
        },
    )
    assert response.status_code == 200 and response.json()["status"] == "EXPIRED"
    assert scalar(state, "SELECT count(*) FROM command_request_audit") == before
    assert (
        scalar(state, "SELECT execution_id FROM line_execution_reservations")
        == bound["execution_id"]
    )
    pause = CommandRequest(
        **{
            **old.model_dump(),
            "command_id": "safe-pause",
            "requested_at": AT,
            "expires_at": AT + timedelta(seconds=5),
            "command": {"kind": "PAUSE"},
        }
    )
    with state.engine.begin() as db:
        assert (
            enqueue_command(
                db,
                pause,
                principal=state.principal,
                line_id=LINE_ID,
                source_id="source",
                stream_id="stream",
                now=AT,
            ).status
            == "REQUESTED"
        )


def test_empty_physical_active_id_cannot_release_an_unsettled_exact_reservation(state):
    release(state)
    first = bind(state)
    release(state, "work-B", release_id="release-B")
    capture(state, values={"Line.ActiveExecutionId": ""})
    with pytest.raises(RegistrationConflict):
        bind(
            state,
            "work-B",
            definition=BindIntent(**{**intent().model_dump(), "command_id": "load-B"}),
        )
    assert scalar(state, "SELECT count(*) FROM production_executions") == 1
    assert (
        scalar(state, "SELECT execution_id FROM line_execution_reservations")
        == first["execution_id"]
    )


@pytest.mark.parametrize(
    "role,release_status,bind_status",
    [
        ("viewer", 403, 403),
        ("equipment_admin", 403, 403),
        ("operator", 403, 200),
    ],
)
async def test_release_and_bind_permission_matrix(state, client, role, release_status, bind_status):
    release(state)
    session = account(state.auth, role, role)
    client.cookies.set("hmi_session", session.token)
    protection = {"Origin": ORIGIN, "X-CSRF-Token": session.csrf_token}
    response = await client.post(
        "/api/v1/work-orders/work-B/release",
        headers=protection,
        json={"release_id": "release-B", "expected_row_version": "1"},
    )
    assert response.status_code == release_status
    response = await client.post(
        "/api/v1/work-orders/work-A/bind", headers=protection, json=intent().model_dump(mode="json")
    )
    assert response.status_code == bind_status
    if bind_status == 200:
        assert response.json()["load_request"]["requested_by"] == role


async def test_workflow_rejects_forged_server_fields_and_missing_csrf(state, client):
    path = "/api/v1/work-orders/work-A/release"
    body = {"release_id": "release-A", "expected_row_version": "1"}
    assert (await client.post(path, json=body)).status_code == 403
    assert (
        await client.post(path, headers=headers(state), json={**body, "actor": "forged"})
    ).status_code == 422
    release(state)
    for field, value in (
        ("execution_id", "forged"),
        ("requested_at", AT.isoformat()),
        ("expires_at", AT.isoformat()),
        ("target_quantity", 1),
    ):
        response = await client.post(
            "/api/v1/work-orders/work-A/bind",
            headers=headers(state),
            json={**intent().model_dump(mode="json"), field: value},
        )
        assert response.status_code == 422
    assert scalar(state, "SELECT count(*) FROM production_executions") == 0


async def test_passive_recovery_returns_committed_original_intents_without_state_or_idle_writes(
    state, client, monkeypatch
):
    import apps.api.work_order_workflow as workflow

    # Simulate losing both successful POST responses: recover by the original IDs only.
    released = await client.post(
        "/api/v1/work-orders/work-A/release",
        headers=headers(state),
        json={"release_id": "release-A", "expected_row_version": "1"},
    )
    assert released.status_code == 200
    bound = await client.post(
        "/api/v1/work-orders/work-A/bind",
        headers=headers(state),
        json=intent().model_dump(mode="json"),
    )
    assert bound.status_code == 200
    state.clock[0] += timedelta(seconds=10)
    capture(state, values={"Line.ControlMode": 0})

    def forbidden(*args, **kwargs):
        raise AssertionError("Passive recovery cannot inspect or change operational state")

    for name in (
        "_lock",
        "_order_lock",
        "_configured",
        "_preflight",
        "reconcile_execution",
        "read_snapshot",
        "enqueue_command",
        "uuid4",
    ):
        monkeypatch.setattr(workflow, name, forbidden)
    statements = []

    def record(db, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(state.engine, "before_cursor_execute", record)
    try:
        for _ in range(2):
            release_response = await client.get("/api/v1/work-orders/work-A/releases/release-A")
            bind_response = await client.get("/api/v1/work-orders/work-A/bindings/load-A")
            assert release_response.status_code == bind_response.status_code == 200
            assert release_response.headers["cache-control"] == "no-store"
            assert bind_response.headers["cache-control"] == "no-store"
            assert release_response.json() == {
                "intent": {
                    "work_order_id": "work-A",
                    "actor": "manager",
                    "release_id": "release-A",
                    "expected_row_version": "1",
                },
                "receipt": released.json(),
            }
            assert bind_response.json() == {
                "intent": {
                    "work_order_id": "work-A",
                    "actor": "manager",
                    **intent().model_dump(mode="json"),
                },
                "receipt": bound.json(),
            }
    finally:
        event.remove(state.engine, "before_cursor_execute", record)
    assert not any(
        sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements
    )
    assert not any(
        "FOR UPDATE" in sql.upper() and "auth_sessions" not in sql and "FROM users" not in sql
        for sql in statements
    )


@pytest.mark.parametrize("role", ["viewer", "operator", "production_manager", "equipment_admin"])
async def test_recovery_read_permissions_do_not_grant_original_actor_replay(state, client, role):
    released, bound = release(state), bind(state)
    session = account(state.auth, "reader-" + role, role)
    client.cookies.set("hmi_session", session.token)
    assert (await client.get("/api/v1/work-orders/work-A/releases/release-A")).json()[
        "receipt"
    ] == released
    assert (await client.get("/api/v1/work-orders/work-A/bindings/load-A")).json()[
        "receipt"
    ] == bound
    response = await client.post(
        "/api/v1/work-orders/work-A/bind",
        headers={"Origin": ORIGIN, "X-CSRF-Token": session.csrf_token},
        json=intent().model_dump(mode="json"),
    )
    assert response.status_code == (409 if role in {"operator", "production_manager"} else 403)


@pytest.mark.parametrize(
    "path,status",
    [
        ("work-B/releases/release-A", 404),
        ("work-B/bindings/load-A", 404),
        ("missing/releases/release-A", 404),
        ("work-A/releases/missing", 404),
        ("work-A/bindings/missing", 404),
        ("work-A/releases/bad%20id", 422),
        ("bad%20id/bindings/load-A", 422),
        ("work-A/releases/release-A?x=1", 422),
        ("work-A/bindings/load-A?command_id=load-A&command_id=load-A", 422),
    ],
)
async def test_recovery_requires_exact_path_scope_and_no_query_fallback(
    state, client, path, status
):
    release(state)
    bind(state)
    assert (await client.get("/api/v1/work-orders/" + path)).status_code == status


@pytest.mark.parametrize("session_state", ["anonymous", "revoked", "initial"])
async def test_recovery_rejects_invalid_or_initial_sessions(state, client, session_state):
    release(state)
    bind(state)
    if session_state == "anonymous":
        client.cookies.clear()
    elif session_state == "revoked":
        state.auth.revoke(state.session.token, csrf_token=state.session.csrf_token, origin=ORIGIN)
    else:
        password = secrets.token_urlsafe(24)
        state.auth.bootstrap_user("initial", password, roles={"viewer"})
        challenge = state.auth.begin_login()
        session = state.auth.login(
            "initial",
            password,
            challenge_token=challenge.token,
            csrf_token=challenge.csrf_token,
            origin=ORIGIN,
        )
        client.cookies.set("hmi_session", session.token)
    for suffix in ("releases/release-A", "bindings/load-A"):
        response = await client.get("/api/v1/work-orders/work-A/" + suffix)
        assert response.status_code == (403 if session_state == "initial" else 401)


@pytest.mark.parametrize("kind", ["release", "binding"])
async def test_recovery_refuses_incoherent_stored_intent_without_returning_it(state, client, kind):
    release(state)
    bind(state)
    table = "work_order_releases" if kind == "release" else "work_order_execution_links"
    with state.engine.begin() as db:
        db.execute(
            text(
                f"UPDATE {table} SET original_intent=jsonb_set("
                "original_intent,'{actor}','\"incoherent-actor\"'::jsonb)"
            )
        )
    suffix = "releases/release-A" if kind == "release" else "bindings/load-A"
    response = await client.get("/api/v1/work-orders/work-A/" + suffix)
    assert response.status_code == 503
    assert "incoherent-actor" not in response.text
    assert scalar(state, "SELECT count(*) FROM production_executions") == 1
