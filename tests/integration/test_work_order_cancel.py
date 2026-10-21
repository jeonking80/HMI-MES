"""C1 unbound cancellation, immutable recovery and bind races on real PostgreSQL."""

import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
from alembic import command
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from apps.api.auth import UserAuth
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.commands import apply_result, apply_submission, claim_next
from apps.api.database import migration_config
from apps.api.ingestion import IngestionService
from apps.api.user_http import UserHTTPConfig, create_user_app
from apps.api.work_order_cancel import CancellationIntent, cancel_work_order
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
from contracts.models import UINT64_MAX, CommandResult, CommandSubmission, Parameters
from contracts.tags.validate import load_contract
from contracts.transport import BatchBody, GatewayBatch, batch_digest

AT = datetime(2026, 10, 28, tzinfo=UTC)
ORIGIN = "https://cancel.example.test"
SCOPE = {"line_id": LINE_ID, "source_id": "source", "stream_id": "stream"}
GATEWAY = {
    "authenticated_gateway_id": "gateway",
    "authenticated_source_id": "source",
    "authenticated_stream_id": "stream",
}
PARAMETERS = Parameters(temperature_c=150.0, speed_m_per_min=6.0)


def account(auth, name, roles, initial=False):
    password = secrets.token_urlsafe(24)
    auth.bootstrap_user(name, password, roles=roles)
    challenge = auth.begin_login()
    session = auth.login(
        name,
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=ORIGIN,
    )
    return (
        session
        if initial
        else auth.change_password(
            session.token, secrets.token_urlsafe(24), csrf_token=session.csrf_token, origin=ORIGIN
        )
    )


def definition(identity):
    return WorkOrderDefinition(
        id=identity,
        item_id="item",
        line_id=LINE_ID,
        recipe_version_id="recipe-v1",
        target_quantity=4,
        planned_date="2026-10-28",
    )


@pytest.fixture
def state(migrated):
    bootstrap_reference_data(migrated)
    auth = UserAuth(migrated, allowed_origins={ORIGIN}, clock=lambda: AT)
    session = account(auth, "manager", {"production_manager", "operator"})
    service = IngestionService(migrated)
    service.configure_source(**SCOPE, gateway_id="gateway", capture_generation=1, now=AT)
    with migrated.begin() as db:
        register_recipe_version(
            db,
            RecipeDefinition(
                id="recipe-v1",
                recipe_id="recipe",
                version="v1",
                item_id="item",
                parameters=PARAMETERS,
            ),
            actor="manager",
            now=AT,
        )
        for identity in ("work-A", "work-B"):
            register_work_order(db, definition(identity), actor="manager", now=AT)
    value = SimpleNamespace(
        engine=migrated,
        auth=auth,
        session=session,
        service=service,
        app=create_user_app(auth, UserHTTPConfig(origin=ORIGIN)),
        seq=0,
    )
    capture(value)
    return value


def capture(state, **overrides):
    state.seq += 1
    values = {
        "Line.CounterEpoch": "epoch",
        "Line.ControlMode": 1,
        "Line.ControlVersion": 42,
        **overrides,
    }
    samples = []
    for tag in load_contract()["tags"]:
        name, kind = tag["logical_name"], tag["data_type"]
        default = (
            False
            if kind == "Boolean"
            else ""
            if kind == "String"
            else 0.0
            if kind == "Double"
            else 0
        )
        samples.append(
            {
                "sample_id": f"{state.seq}-{name}",
                "tag_id": name,
                "source_timestamp": AT,
                "server_timestamp": AT,
                "acquired_at": AT,
                "status_code": 0,
                "reading": {"data_type": kind, "value": values.get(name, default)},
            }
        )
    body = BatchBody(
        schema_version="1.1",
        batch_id=f"batch-{state.seq}",
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        capture_generation=1,
        capture_seq=state.seq,
        captured_at=AT,
        samples=samples,
        communication={
            "state": "GOOD",
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
        },
    )
    state.service.ingest(
        GatewayBatch(body=body, content_sha256=batch_digest(body)), **GATEWAY, now=AT
    )


@pytest.fixture
async def client(state):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=state.app), base_url=ORIGIN
    ) as value:
        value.cookies.set("hmi_session", state.session.token)
        yield value


def headers(state):
    return {"Origin": ORIGIN, "X-CSRF-Token": state.session.csrf_token}


def cancel(state, identity="work-A", version=1, cancellation_id="cancel-A", clock=lambda: AT):
    with state.engine.begin() as db:
        return cancel_work_order(
            db,
            identity,
            CancellationIntent(cancellation_id=cancellation_id, expected_row_version=version),
            principal=state.session.principal,
            clock=clock,
        )


def release(state):
    with state.engine.begin() as db:
        return release_work_order(
            db,
            "work-A",
            ReleaseIntent(release_id="release-A", expected_row_version=1),
            principal=state.session.principal,
            clock=lambda: AT,
        )


def bind(state):
    with state.engine.begin() as db:
        return bind_work_order(
            db,
            "work-A",
            BindIntent(
                command_id="load-A",
                **SCOPE,
                expected_work_order_version=2,
                expected_control_version=42,
            ),
            principal=state.session.principal,
            clock=lambda: AT,
        )


def rows(state, table):
    with state.engine.connect() as db:
        return list(db.execute(text("SELECT * FROM " + table)).mappings())


@pytest.mark.parametrize("released", [False, True])
async def test_cancel_unbound_draft_or_released_and_passively_recover_original(
    state, client, released
):
    original_release = release(state) if released else None
    version = 2 if released else 1
    response = await client.post(
        "/api/v1/work-orders/work-A/cancel",
        headers=headers(state),
        json={"cancellation_id": "cancel-A", "expected_row_version": str(version)},
    )
    assert response.status_code == 200
    receipt = response.json()
    assert receipt["status"] == "CANCELLED" and receipt["row_version"] == str(version + 1)

    def forbidden():
        raise AssertionError("Exact cancellation replay must not consume time")

    assert cancel(state, version=version, clock=forbidden) == receipt
    if released:
        assert release(state) == original_release
    with pytest.raises(RegistrationConflict):
        bind(state)
    recovered = await client.get("/api/v1/work-orders/work-A/cancellations/cancel-A")
    assert recovered.status_code == 200 and recovered.headers["cache-control"] == "no-store"
    assert recovered.json() == {
        "intent": {
            "work_order_id": "work-A",
            "actor": "manager",
            "cancellation_id": "cancel-A",
            "expected_row_version": str(version),
        },
        "receipt": receipt,
    }
    assert (await client.get("/api/v1/work-orders/work-A")).json()["status"] == "CANCELLED"
    with state.engine.begin() as db:
        replay = register_work_order(db, definition("work-A"), actor="manager", now=AT)
    assert replay.status == "CANCELLED"  # Identical registration cannot resurrect the draft.
    assert rows(state, "production_executions") == rows(state, "line_execution_reservations") == []
    assert (
        len([x for x in rows(state, "mes_management_audit") if x["action"] == "CANCEL_WORK_ORDER"])
        == 1
    )


@pytest.mark.parametrize(
    "failure",
    [
        "UPDATE work_orders",
        "INSERT INTO mes_management_audit",
        "INSERT INTO work_order_cancellations",
    ],
)
def test_cancellation_status_audit_and_receipt_rollback_together(state, failure):
    before = rows(state, "work_orders"), rows(state, "mes_management_audit")

    def reject(db, cursor, statement, parameters, context, many):
        if statement.startswith(failure):
            raise SQLAlchemyError("Injected transaction failure")

    event.listen(state.engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(SQLAlchemyError):
            cancel(state)
    finally:
        event.remove(state.engine, "before_cursor_execute", reject)
    assert (rows(state, "work_orders"), rows(state, "mes_management_audit")) == before
    assert rows(state, "work_order_cancellations") == []


def test_bind_and_cancellation_race_has_one_winner_without_orphan_execution_or_slot(state):
    release(state)
    barrier = Barrier(2)

    def run(action):
        barrier.wait(timeout=10)
        try:
            return action()
        except RegistrationConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(run, lambda: bind(state)),
            pool.submit(run, lambda: cancel(state, version=2)),
        ]
        outcomes = [future.result(timeout=20) for future in futures]
    assert sum(value is not None for value in outcomes) == 1
    bound = outcomes[0] is not None
    assert (
        len(rows(state, "production_executions"))
        == len(rows(state, "line_execution_reservations"))
        == int(bound)
    )
    assert len(rows(state, "work_order_cancellations")) == int(not bound)


def test_same_global_cancellation_id_cannot_cancel_two_work_orders_concurrently(state):
    barrier = Barrier(2)

    def run(identity):
        barrier.wait(timeout=10)
        try:
            return cancel(state, identity=identity)
        except RegistrationConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, identity) for identity in ("work-A", "work-B")]
        outcomes = [future.result(timeout=20) for future in futures]
    assert sum(value is not None for value in outcomes) == 1
    assert len(rows(state, "work_order_cancellations")) == 1


@pytest.mark.parametrize("version", [2, UINT64_MAX])
def test_stale_version_and_version_exhaustion_cannot_change_the_order(state, version):
    if version == UINT64_MAX:
        with state.engine.begin() as db:
            db.execute(
                text("UPDATE work_orders SET row_version=:v WHERE id='work-A'"), {"v": version}
            )
    before = rows(state, "work_orders"), rows(state, "mes_management_audit")

    def forbidden():
        raise AssertionError("Rejected version must not consume a cancellation timestamp")

    with pytest.raises(RegistrationConflict):
        cancel(state, version=version, clock=forbidden)
    assert (rows(state, "work_orders"), rows(state, "mes_management_audit")) == before
    assert rows(state, "work_order_cancellations") == []


@pytest.mark.parametrize(
    "phase",
    [
        "REQUESTED",
        "OUTCOME_UNKNOWN",
        "ACCEPTED",
        "SUCCEEDED",
        "FAILED",
        "REJECTED",
        "EXPIRED",
        "RUNNING",
        "ENDED",
    ],
)
async def test_every_bound_phase_refuses_cancellation_without_touching_original_evidence(
    state, client, phase
):
    release(state)
    bound = bind(state)
    if phase != "REQUESTED":
        with state.engine.begin() as db:
            claim_next(db, **GATEWAY, now=AT + timedelta(seconds=6) if phase == "EXPIRED" else AT)
            if phase == "ACCEPTED":
                apply_submission(
                    db,
                    CommandSubmission(command_id="load-A", disposition="ACCEPTED", received_at=AT),
                    **GATEWAY,
                    now=AT,
                )
            elif phase in {"SUCCEEDED", "FAILED", "REJECTED", "RUNNING", "ENDED"}:
                success = phase in {"SUCCEEDED", "RUNNING", "ENDED"}
                extra = (
                    {
                        "applied_control_version": 43,
                        "evidence": {
                            "state": "READY",
                            "execution_id": bound["execution_id"],
                            "recipe_version": "recipe-v1",
                        },
                    }
                    if success
                    else {
                        "reason_code": "FIXTURE_REJECTION",
                        "reason": "Explicit negative PLC fixture result",
                    }
                )
                apply_result(
                    db,
                    CommandResult(
                        command_id="load-A",
                        status="SUCCEEDED" if success else phase,
                        updated_at=AT,
                        delivered_at=AT,
                        completed_at=AT,
                        **extra,
                    ),
                    **GATEWAY,
                    now=AT,
                )
        if phase in {"RUNNING", "ENDED"}:
            capture(
                state,
                **{
                    "Line.State": 2 if phase == "RUNNING" else 0,
                    "Line.ActiveExecutionId": bound["execution_id"],
                },
            )
    tables = (
        "work_orders",
        "work_order_execution_links",
        "line_execution_reservations",
        "command_requests",
        "command_request_audit",
        "execution_totals",
        "execution_plans",
        "mes_management_audit",
        "production_events",
    )
    before = {table: rows(state, table) for table in tables}
    assert before["command_requests"][0]["status"] == (
        "SUCCEEDED" if phase in {"RUNNING", "ENDED"} else phase
    )
    response = await client.post(
        "/api/v1/work-orders/work-A/cancel",
        headers=headers(state),
        json={"cancellation_id": "cancel-A", "expected_row_version": "3"},
    )
    assert response.status_code == 409
    assert {table: rows(state, table) for table in tables} == before
    assert rows(state, "work_order_cancellations") == []
    assert (await client.get("/api/v1/work-orders/work-A/bindings/load-A")).json()[
        "receipt"
    ] == bound


async def test_legacy_text_alias_is_not_a_managed_link_and_original_execution_is_untouched(
    state, client
):
    state.service.register_execution(
        execution_id="legacy",
        source_id="source",
        stream_id="stream",
        work_order_id="work-A",
        recipe_version="recipe-v1",
        parameters=PARAMETERS,
        counter_epoch="epoch",
        target_quantity=4,
        now=AT,
    )
    before = {
        table: rows(state, table)
        for table in ("production_executions", "execution_totals", "execution_plans")
    }
    assert cancel(state)["status"] == "CANCELLED"
    assert {table: rows(state, table) for table in before} == before
    result = await client.get("/api/v1/executions/legacy", params=SCOPE)
    assert result.status_code == 200 and result.json()["binding"] is None


@pytest.mark.parametrize(
    "role,status",
    [("viewer", 403), ("operator", 403), ("equipment_admin", 403), ("production_manager", 200)],
)
async def test_mutation_permissions_and_cross_actor_replay(state, client, role, status):
    session = account(state.auth, role, {role})
    client.cookies.set("hmi_session", session.token)
    response = await client.post(
        "/api/v1/work-orders/work-A/cancel",
        headers={"Origin": ORIGIN, "X-CSRF-Token": session.csrf_token},
        json={"cancellation_id": "cancel-A", "expected_row_version": "1"},
    )
    assert response.status_code == status
    if status == 200:
        assert response.json()["cancelled_by"] == role
        with pytest.raises(RegistrationConflict):
            cancel(state)


async def test_validation_csrf_origin_initial_password_and_original_intent_conflicts(state, client):
    path = "/api/v1/work-orders/work-A/cancel"
    body = {"cancellation_id": "cancel-A", "expected_row_version": "1"}
    assert (await client.post(path, json=body)).status_code == 403
    assert (
        await client.post(
            path, headers={**headers(state), "Origin": "https://other.test"}, json=body
        )
    ).status_code == 403
    for invalid in ({**body, "actor": "forged"}, {**body, "expected_row_version": True}):
        assert (await client.post(path, headers=headers(state), json=invalid)).status_code == 422
    session = account(state.auth, "initial", {"production_manager"}, initial=True)
    client.cookies.set("hmi_session", session.token)
    assert (
        await client.post(
            path, headers={"Origin": ORIGIN, "X-CSRF-Token": session.csrf_token}, json=body
        )
    ).status_code == 403
    cancel(state)
    for arguments in ({"identity": "work-B"}, {"version": 2}, {"cancellation_id": "different"}):
        with pytest.raises(RegistrationConflict):
            cancel(state, **arguments)


@pytest.mark.parametrize("kind", ["anonymous", "revoked"])
async def test_invalid_sessions_cannot_cancel_or_recover(state, client, kind):
    if kind == "anonymous":
        client.cookies.clear()
    else:
        state.auth.revoke(state.session.token, csrf_token=state.session.csrf_token, origin=ORIGIN)
    assert (
        await client.post(
            "/api/v1/work-orders/work-A/cancel",
            headers=headers(state),
            json={"cancellation_id": "cancel-A", "expected_row_version": "1"},
        )
    ).status_code == 401
    assert (
        await client.get("/api/v1/work-orders/work-A/cancellations/cancel-A")
    ).status_code == 401


async def test_recovery_exact_scope_and_read_only_no_idle_refresh(state, client):
    receipt = cancel(state)
    statements = []

    def record(db, cursor, sql, parameters, context, many):
        statements.append(sql)

    event.listen(state.engine, "before_cursor_execute", record)
    try:
        for role in ("viewer", "operator", "equipment_admin"):
            # Account setup is outside the monitored recovery request.
            event.remove(state.engine, "before_cursor_execute", record)
            session = account(state.auth, role, {role})
            event.listen(state.engine, "before_cursor_execute", record)
            client.cookies.set("hmi_session", session.token)
            response = await client.get("/api/v1/work-orders/work-A/cancellations/cancel-A")
            assert response.status_code == 200 and response.json()["receipt"] == receipt
        assert (
            await client.get("/api/v1/work-orders/work-B/cancellations/cancel-A")
        ).status_code == 404
        assert (
            await client.get("/api/v1/work-orders/work-A/cancellations/missing")
        ).status_code == 404
        assert (
            await client.get("/api/v1/work-orders/work-A/cancellations/cancel-A?x=1")
        ).status_code == 422
    finally:
        event.remove(state.engine, "before_cursor_execute", record)
    assert not any(
        sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements
    )
    assert not any(
        "FOR UPDATE" in sql.upper() and "auth_sessions" not in sql and "FROM users" not in sql
        for sql in statements
    )


@pytest.mark.parametrize(
    "sql",
    ["UPDATE work_order_cancellations SET actor='manager'", "DELETE FROM work_order_cancellations"],
)
def test_cancellation_receipt_is_append_only_even_for_direct_database_mutation(state, sql):
    receipt = cancel(state)
    with pytest.raises(DBAPIError), state.engine.begin() as db:
        db.execute(text(sql))
    assert cancel(state) == receipt


def test_used_downgrade_refuses_and_unused_roundtrip_preserves_original_definitions(state):
    before = rows(state, "work_orders"), rows(state, "mes_management_audit")
    with state.engine.connect() as db:
        command.downgrade(migration_config(db), "0012_alarm_line_fault")
    assert (rows(state, "work_orders"), rows(state, "mes_management_audit")) == before
    with state.engine.connect() as db:
        command.upgrade(migration_config(db), "0013_work_order_cancel")
    receipt = cancel(state)
    with pytest.raises(DBAPIError), state.engine.connect() as db:
        command.downgrade(migration_config(db), "0012_alarm_line_fault")
    assert cancel(state) == receipt
    assert rows(state, "alembic_version")[0]["version_num"] == "0013_work_order_cancel"
