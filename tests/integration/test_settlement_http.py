"""Actual PostgreSQL and ASGI final-evidence transport; no PLC/network claims."""

import secrets
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI, HTTPException

from apps.api.bootstrap import ASSET_IDS, LINE_ID, bootstrap_reference_data
from apps.api.ingestion import IngestionService
from apps.api.settlement_http import mount_settlement_routes
from contracts.models import Parameters, RunSummary
from contracts.transport import BatchBody, GatewayBatch, batch_digest

AT = datetime(2026, 10, 21, tzinfo=UTC)


def summary(**changes):
    body = dict(
        source_id="source-1",
        stream_id="stream-1",
        execution_id="run-1",
        state="ENDED",
        as_of=AT,
        ended_at=AT,
        counter_epoch="epoch-1",
        recipe_version="recipe-1",
        parameters={"temperature_c": 150.0, "speed_m_per_min": 6.0},
        input_quantity=1,
        good_quantity=1,
        reject_quantity=0,
        scrap_quantity=0,
        wip_quantity=0,
        last_event_seq=1,
        final=True,
    )
    return RunSummary.model_validate(body | changes)


@pytest.fixture
def state(migrated):
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
            execution_id=f"run-{suffix}",
            source_id=f"source-{suffix}",
            stream_id=f"stream-{suffix}",
            work_order_id=f"job-{suffix}",
            recipe_version="recipe-1",
            counter_epoch="epoch-1",
            parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
            now=AT,
        )
    token = secrets.token_urlsafe(32)
    binding = SimpleNamespace(gateway_id="gateway-1", source_id="source-1", stream_id="stream-1")
    app = FastAPI()

    def authenticate(request):
        if request.headers.get("authorization") != "Bearer " + token:
            raise HTTPException(401, "service authentication required")
        return binding

    mount_settlement_routes(app, migrated, authenticate, lambda: AT)
    return SimpleNamespace(service=service, engine=migrated, token=token, app=app, binding=binding)


@pytest.fixture
async def client(state):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=state.app),
        base_url="https://intake.test",
        headers={"Authorization": "Bearer " + state.token},
    ) as client:
        yield client


def count(state, table):
    with state.engine.connect() as db:
        return db.execute(sa.text("SELECT count(*) FROM " + table)).scalar_one()


def ingest(state):
    event = dict(
        schema_version="1.0",
        event_id="production-1",
        source_id="source-1",
        stream_id="stream-1",
        seq=1,
        equipment_id=ASSET_IDS["Outfeed"],
        execution_id="run-1",
        occurred_at=AT,
        kind="PRODUCTION_GOOD",
        payload={"quantity": 1, "counter_epoch": "epoch-1"},
    )
    body = BatchBody(
        batch_id="batch-1",
        gateway_id="gateway-1",
        source_id="source-1",
        stream_id="stream-1",
        capture_generation=1,
        capture_seq=1,
        captured_at=AT,
        events=[event],
    )
    state.service.ingest(
        GatewayBatch(body=body, content_sha256=batch_digest(body)),
        authenticated_gateway_id="gateway-1",
        authenticated_source_id="source-1",
        authenticated_stream_id="stream-1",
        now=AT,
    )


async def test_final_receipt_pending_then_actual_events_settle_and_list_excludes_settled(
    client, state
):
    pending = await client.post("/internal/v1/runs/pending", json={})
    assert pending.json() == {"execution_ids": ["run-1"], "next_cursor": None}
    payload = summary().model_dump(mode="json")
    receipt = await client.post("/internal/v1/runs/summary", json=payload)
    assert receipt.status_code == 200
    assert receipt.json() == {
        "committed": True,
        "execution_id": "run-1",
        "status": "PENDING",
        "reason_code": "EVENTS_PENDING",
    }
    assert count(state, "run_reconciliations") == 1
    assert (await client.post("/internal/v1/runs/pending", json={})).json()["execution_ids"] == [
        "run-1"
    ]
    ingest(state)
    receipt = await client.post("/internal/v1/runs/summary", json=payload)
    assert receipt.json()["status"] == "SETTLED" and receipt.json()["reason_code"] == "MATCHED"
    assert (await client.post("/internal/v1/runs/pending", json={})).json() == {
        "execution_ids": [],
        "next_cursor": None,
    }
    # Lost ACK replay preserves original execution/final evidence and remains idempotent.
    replay = await client.post("/internal/v1/runs/summary", json=payload)
    assert replay.json() == receipt.json()
    assert count(state, "run_reconciliations") == 1


async def test_pending_scope_and_deterministic_pagination(client, state):
    for execution in ("run-0", "run-2", "run-3"):
        state.service.register_execution(
            execution_id=execution,
            source_id="source-1",
            stream_id="stream-1",
            work_order_id="job-" + execution,
            recipe_version="recipe-1",
            counter_epoch="epoch-1",
            parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
            now=AT,
        )
    pages = []
    cursor = None
    while True:
        response = await client.post(
            "/internal/v1/runs/pending", json={"after_execution_id": cursor, "limit": 2}
        )
        assert response.status_code == 200
        body = response.json()
        pages.extend(body["execution_ids"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages == ["run-0", "run-1", "run-2", "run-3"]
    state.binding.gateway_id = "gateway-foreign"
    assert (await client.post("/internal/v1/runs/pending", json={})).status_code == 403


@pytest.mark.parametrize(
    "payload",
    [
        {"limit": True},
        {"limit": 0},
        {"limit": 101},
        {"limit": "1"},
        {"gateway_id": "other"},
        {"after_execution_id": "bad id"},
        [],
    ],
)
async def test_strict_pending_input(client, payload):
    assert (await client.post("/internal/v1/runs/pending", json=payload)).status_code == 422


async def test_scope_auth_nonfinal_invalid_and_bound(client, state):
    assert (
        await client.post(
            "/internal/v1/runs/pending", json={}, headers={"Authorization": "Bearer invalid"}
        )
    ).status_code == 401
    foreign = summary(
        source_id="source-foreign", stream_id="stream-foreign", execution_id="run-foreign"
    )
    assert (
        await client.post("/internal/v1/runs/summary", json=foreign.model_dump(mode="json"))
    ).status_code == 403
    unknown = summary(execution_id="missing")
    assert (
        await client.post("/internal/v1/runs/summary", json=unknown.model_dump(mode="json"))
    ).status_code == 403
    nonfinal = summary(final=False, state="RUNNING", ended_at=None)
    assert (
        await client.post("/internal/v1/runs/summary", json=nonfinal.model_dump(mode="json"))
    ).status_code == 422
    assert (await client.post("/internal/v1/runs/summary", content=b"{")).status_code == 422
    assert (await client.post("/internal/v1/runs/summary", content=b"x" * 65537)).status_code == 413
    assert count(state, "run_reconciliations") == 0


async def test_commit_failure_returns_no_receipt_and_rolls_back(client, state):
    def fail_commit(connection):
        assert (
            connection.execute(sa.text("SELECT count(*) FROM run_reconciliations")).scalar_one()
            == 1
        )
        assert count(state, "run_reconciliations") == 0
        raise sa.exc.SQLAlchemyError("injected commit failure")

    sa.event.listen(state.engine, "commit", fail_commit)
    try:
        response = await client.post(
            "/internal/v1/runs/summary", json=summary().model_dump(mode="json")
        )
    finally:
        sa.event.remove(state.engine, "commit", fail_commit)
    assert response.status_code == 503 and "committed" not in response.json()
    assert count(state, "run_reconciliations") == 0
