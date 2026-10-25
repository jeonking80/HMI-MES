"""Exact-scope passive execution evidence using real PostgreSQL and user sessions."""

import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import event, text

from apps.api.auth import UserAuth
from apps.api.bootstrap import ASSET_IDS, LINE_ID, bootstrap_reference_data
from apps.api.commands import apply_result, claim_next
from apps.api.execution_read import ExecutionLookup, read_execution
from apps.api.ingestion import IngestionService
from apps.api.settlement import record_final_summary
from apps.api.user_http import UserHTTPConfig, create_user_app
from apps.api.work_order_workflow import (
    BindIntent,
    ReleaseIntent,
    bind_work_order,
    release_work_order,
)
from apps.api.work_orders import (
    RecipeDefinition,
    WorkOrderDefinition,
    register_recipe_version,
    register_work_order,
)
from contracts.models import UINT64_MAX, CommandResult, Parameters, RunSummary
from contracts.tags.validate import load_contract
from contracts.transport import BatchBody, GatewayBatch, batch_digest
from tests.integration.item_seed import seed_item

AT = datetime(2026, 10, 27, tzinfo=UTC)
ORIGIN = "https://execution.example.test"
SCOPE = {"line_id": LINE_ID, "source_id": "source", "stream_id": "stream"}
GATEWAY = {
    "authenticated_gateway_id": "gateway",
    "authenticated_source_id": "source",
    "authenticated_stream_id": "stream",
}
PARAMETERS = Parameters(temperature_c=150.0, speed_m_per_min=6.0)


def account(auth, name, roles, *, initial=False):
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
    if initial:
        return session
    return auth.change_password(
        session.token, secrets.token_urlsafe(24), csrf_token=session.csrf_token, origin=ORIGIN
    )


@pytest.fixture
def state(migrated):
    bootstrap_reference_data(migrated)
    auth = UserAuth(migrated, allowed_origins={ORIGIN}, clock=lambda: AT)
    session = account(auth, "manager", {"production_manager", "operator"})
    seed_item(migrated, "item", actor="manager", now=AT)
    service = IngestionService(migrated)
    service.configure_source(
        **{k: v for k, v in SCOPE.items()}, gateway_id="gateway", capture_generation=1, now=AT
    )
    values = {"Line.CounterEpoch": "epoch", "Line.ControlMode": 1, "Line.ControlVersion": 42}
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
                "sample_id": "sample-" + name,
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
        batch_id="initial",
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        capture_generation=1,
        capture_seq=1,
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
    service.ingest(GatewayBatch(body=body, content_sha256=batch_digest(body)), **GATEWAY, now=AT)
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
        register_work_order(
            db,
            WorkOrderDefinition(
                id="work-A",
                item_id="item",
                line_id=LINE_ID,
                recipe_version_id="recipe-v1",
                target_quantity=4,
                planned_date="2026-10-27",
            ),
            actor="manager",
            now=AT,
        )
        release_work_order(
            db,
            "work-A",
            ReleaseIntent(release_id="release-A", expected_row_version=1),
            principal=session.principal,
            clock=lambda: AT,
        )
        bound = bind_work_order(
            db,
            "work-A",
            BindIntent(
                command_id="load-A",
                **SCOPE,
                expected_work_order_version=2,
                expected_control_version=42,
            ),
            principal=session.principal,
            clock=lambda: AT,
        )
    return SimpleNamespace(
        engine=migrated,
        auth=auth,
        session=session,
        service=service,
        bound=bound,
        execution=bound["execution_id"],
        app=create_user_app(auth, UserHTTPConfig(origin=ORIGIN)),
    )


@pytest.fixture
async def client(state):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=state.app), base_url=ORIGIN
    ) as value:
        value.cookies.set("hmi_session", state.session.token)
        yield value


def path(state):
    return "/api/v1/executions/" + state.execution


def view(state):
    with state.engine.begin() as db:
        return read_execution(db, ExecutionLookup(execution_id=state.execution, **SCOPE))


def settle(state, **changes):
    with state.engine.begin() as db:
        claim_next(db, **GATEWAY, now=AT)
        apply_result(
            db,
            CommandResult(
                command_id="load-A",
                status="SUCCEEDED",
                updated_at=AT,
                delivered_at=AT,
                completed_at=AT,
                applied_control_version=43,
                evidence={
                    "state": "READY",
                    "execution_id": state.execution,
                    "recipe_version": "recipe-v1",
                },
            ),
            **GATEWAY,
            now=AT,
        )
    template = json.loads(
        (
            Path(__file__).resolve().parents[2] / "contracts/examples/transport-batch.json"
        ).read_text()
    )["body"]["events"][0]
    events = []
    for seq in range(1, 5):
        item = json.loads(json.dumps(template))
        item.update(
            seq=str(seq),
            event_id=f"event-{seq}",
            execution_id=state.execution,
            equipment_id=ASSET_IDS["Outfeed"],
            source_id="source",
            stream_id="stream",
        )
        item["payload"]["quantity"] = "1"
        item["payload"]["counter_epoch"] = "epoch"
        events.append(item)
    body = BatchBody(
        schema_version="1.0",
        batch_id="production",
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        capture_generation=1,
        capture_seq=2,
        captured_at=AT,
        samples=(),
        events=events,
    )
    state.service.ingest(
        GatewayBatch(body=body, content_sha256=batch_digest(body)), **GATEWAY, now=AT
    )
    summary = RunSummary(
        **(
            {
                "source_id": "source",
                "stream_id": "stream",
                "execution_id": state.execution,
                "state": "ENDED",
                "as_of": AT,
                "ended_at": AT,
                "counter_epoch": "epoch",
                "recipe_version": "recipe-v1",
                "parameters": PARAMETERS,
                "input_quantity": 4,
                "good_quantity": 4,
                "reject_quantity": 0,
                "scrap_quantity": 0,
                "wip_quantity": 0,
                "last_event_seq": 4,
                "final": True,
            }
            | changes
        )
    )
    with state.engine.begin() as db:
        return record_final_summary(db, summary, **GATEWAY, now=AT)


async def test_managed_view_shows_pending_then_real_settlement_without_mutating_receipt(
    state, client
):
    pending = await client.get(path(state), params=SCOPE)
    assert pending.status_code == 200
    value = pending.json()
    assert value["binding"]["receipt"] == state.bound
    assert value["load"]["request"] == state.bound["load_request"]
    assert value["load"]["result"]["status"] == "REQUESTED"
    assert value["settlement"] == {
        "status": "PENDING",
        "reason_code": "FINAL_SUMMARY_REQUIRED",
        "checked_at": None,
        "received_at": None,
        "summary": None,
    }
    assert value["execution"]["target_quantity"] == 4
    assert value["totals"] == {"good_quantity": "0", "reject_quantity": "0", "scrap_quantity": "0"}
    assert settle(state).status == "SETTLED"
    final = await client.get(path(state), params=SCOPE)
    assert final.status_code == 200 and final.headers["cache-control"] == "no-store"
    value = final.json()
    assert value["binding"]["receipt"] == state.bound
    assert value["load"]["result"]["status"] == "SUCCEEDED"
    assert value["settlement"]["status"] == "SETTLED"
    assert (
        value["settlement"]["summary"]["good_quantity"] == value["totals"]["good_quantity"] == "4"
    )
    assert value["source"]["verified_through_seq"] == "4"
    assert "ready_for_next" not in value and "archive_allowed" not in value


def test_legacy_execution_remains_explicitly_unmanaged_despite_matching_catalog_name(state):
    state.service.register_execution(
        execution_id="legacy",
        source_id="source",
        stream_id="stream",
        work_order_id="work-A",
        recipe_version="recipe-v1",
        parameters=PARAMETERS,
        counter_epoch="epoch",
        now=AT,
    )
    state.execution = "legacy"
    value = view(state)
    assert value["binding"] is None and value["load"] is None
    assert value["execution"]["target_quantity"] is None


def test_legitimate_mismatch_preserves_original_epoch_observation_without_fake_settled(state):
    assert settle(state, counter_epoch="different-epoch").status == "MISMATCH"
    value = view(state)
    assert value["execution"]["counter_epoch"] == "epoch"
    assert value["settlement"]["status"] == "MISMATCH"
    assert value["settlement"]["summary"]["counter_epoch"] == "different-epoch"


@pytest.mark.parametrize("role", ["viewer", "operator", "production_manager", "equipment_admin"])
async def test_all_existing_roles_can_read_without_mutation_protection(state, client, role):
    session = account(state.auth, role, {role})
    client.cookies.set("hmi_session", session.token)
    assert (await client.get(path(state), params=SCOPE)).status_code == 200


@pytest.mark.parametrize("kind,status", [("anonymous", 401), ("revoked", 401), ("initial", 403)])
async def test_session_restrictions(state, client, kind, status):
    if kind == "anonymous":
        client.cookies.clear()
    elif kind == "revoked":
        state.auth.revoke(state.session.token, csrf_token=state.session.csrf_token, origin=ORIGIN)
    else:
        session = account(state.auth, "initial", {"viewer"}, initial=True)
        client.cookies.set("hmi_session", session.token)
    assert (await client.get(path(state), params=SCOPE)).status_code == status


@pytest.mark.parametrize(
    "scope,status",
    [
        ({**SCOPE, "source_id": "foreign"}, 404),
        ({**SCOPE, "stream_id": "foreign"}, 404),
        ({**SCOPE, "line_id": "foreign"}, 404),
        ({"line_id": LINE_ID}, 422),
        ({**SCOPE, "gateway_id": "gateway"}, 422),
        ({**SCOPE, "source_id": "bad id"}, 422),
        ([*SCOPE.items(), ("source_id", "source")], 422),
    ],
)
async def test_exact_scope_and_query_validation(state, client, scope, status):
    assert (await client.get(path(state), params=scope)).status_code == status
    assert (await client.get("/api/v1/executions/missing", params=SCOPE)).status_code == 404


async def test_read_uses_one_business_statement_no_idle_business_writes_or_reconciliation(
    state, client, monkeypatch
):
    import apps.api.settlement as settlement

    def forbidden(*args, **kwargs):
        raise AssertionError("Passive execution GET must not reconcile")

    monkeypatch.setattr(settlement, "reconcile_execution", forbidden)
    statements = []

    def record(db, cursor, sql, parameters, context, many):
        statements.append(sql)

    event.listen(state.engine, "before_cursor_execute", record)
    try:
        assert (await client.get(path(state), params=SCOPE)).status_code == 200
    finally:
        event.remove(state.engine, "before_cursor_execute", record)
    assert not any(
        sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements
    )
    business = [sql for sql in statements if "FROM production_executions" in sql]
    assert len(business) == 1 and "FOR UPDATE" not in business[0]
    assert "run_reconciliations" in business[0] and "execution_totals" in business[0]
    assert not any(
        "FOR UPDATE" in sql.upper() and "auth_sessions" not in sql and "FROM users" not in sql
        for sql in statements
    )


def test_uint64_totals_preserve_exact_decimal_json(state):
    with state.engine.begin() as db:
        db.execute(
            text("UPDATE execution_totals SET good_quantity=:n WHERE execution_id=:id"),
            {"n": UINT64_MAX, "id": state.execution},
        )
    assert view(state)["totals"]["good_quantity"] == str(UINT64_MAX)


@pytest.mark.parametrize(
    "damage",
    [
        "binding-id",
        "load-kind",
        "load-success-id",
        "settled-epoch",
        "summary-scope",
        "missing-totals",
    ],
)
async def test_corrupt_stored_evidence_is_unavailable_instead_of_archive_permission(
    state, client, damage
):
    settle(state)
    with state.engine.begin() as db:
        statements = {
            "binding-id": "UPDATE work_order_execution_links SET receipt=jsonb_set("
            "receipt,'{execution_id}','\"foreign\"')",
            "load-kind": "UPDATE command_requests SET request=jsonb_set(request,'{command,kind}',"
            "'\"START\"') WHERE command_id='load-A'",
            "load-success-id": "UPDATE command_requests SET result=jsonb_set(result,"
            "'{evidence,execution_id}','\"foreign\"') WHERE command_id='load-A'",
            "settled-epoch": "UPDATE run_reconciliations SET final_summary=jsonb_set(final_summary,"
            "'{counter_epoch}','\"foreign\"')",
            "summary-scope": "UPDATE run_reconciliations SET final_summary=jsonb_set(final_summary,"
            "'{source_id}','\"foreign\"')",
            "missing-totals": "DELETE FROM execution_totals",
        }
        db.execute(text(statements[damage]))
    response = await client.get(path(state), params=SCOPE)
    assert response.status_code == 503
    assert "foreign" not in response.text


def test_concurrent_commits_never_mix_totals_and_last_checked_settlement(state):
    settle(state)

    def write(value):
        with state.engine.begin() as db:
            db.execute(text("UPDATE execution_totals SET good_quantity=:n"), {"n": value})
            db.execute(
                text("UPDATE run_reconciliations SET status='PENDING',reason_code=:r"),
                {"r": "PAIR_" + str(value)},
            )

    write(4)
    stop = Event()

    def writer():
        while not stop.is_set():
            write(5)
            write(4)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer)
        try:
            for _ in range(25):
                value = view(state)
                assert (
                    value["settlement"]["reason_code"] == "PAIR_" + value["totals"]["good_quantity"]
                )
        finally:
            stop.set()
            future.result(timeout=10)
