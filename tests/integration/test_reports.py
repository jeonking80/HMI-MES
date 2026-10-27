"""Real PostgreSQL/ASGI reporting: source evidence, midnight, counts, permissions."""

import secrets
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text
from test_alarm_consumer import ADMIN, AT, PRESSURE, SCOPE, capture, config, start

from apps.api.alarm_consumer import AlarmConsumer
from apps.api.alarm_store import install_rule
from apps.api.auth import UserAuth
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.ingestion import IngestionService
from apps.api.reports import read_daily_report, read_execution_reports, read_execution_timeline
from apps.api.reports_http import register_report_routes
from apps.api.user_http import UserHTTPConfig, create_user_app
from contracts.models import UINT64_MAX, Parameters
from contracts.transport import BatchBody, GatewayBatch, batch_digest

ORIGIN = "https://reports.test"
GATEWAY = dict(
    authenticated_gateway_id="gateway",
    authenticated_source_id="source",
    authenticated_stream_id="stream",
)
PARAMETERS = Parameters(temperature_c=150.0, speed_m_per_min=6.0)


@pytest.fixture
def state(migrated):
    bootstrap_reference_data(migrated)
    auth = UserAuth(migrated, allowed_origins={ORIGIN}, clock=lambda: AT + timedelta(days=1))
    auth.bootstrap_user("admin", secrets.token_urlsafe(24), roles={"equipment_admin"})
    password = secrets.token_urlsafe(24)
    auth.bootstrap_user("viewer", password, roles={"viewer"})
    challenge = auth.begin_login()
    session = auth.login(
        "viewer",
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=ORIGIN,
    )
    session = auth.change_password(
        session.token, secrets.token_urlsafe(24), csrf_token=session.csrf_token, origin=ORIGIN
    )
    service = IngestionService(migrated)
    service.configure_source(
        line_id=LINE_ID,
        source_id="source",
        stream_id="stream",
        gateway_id="gateway",
        capture_generation=1,
        now=AT - timedelta(hours=1),
    )
    with migrated.begin() as db:
        install_rule(
            db, SCOPE, PRESSURE, input_id="install", principal=ADMIN, at=AT - timedelta(minutes=1)
        )
    consumer = AlarmConsumer(config(rules=(PRESSURE,)), consumer_epoch="reports-test")
    start(migrated, consumer)
    service = IngestionService(
        migrated, alarm_consumer=consumer, trend_clock_policy=consumer.config.clock_policy
    )
    for name in ("run", "other"):
        service.register_execution(
            execution_id=name,
            source_id="source",
            stream_id="stream",
            work_order_id=name,
            recipe_version="recipe",
            parameters=PARAMETERS,
            counter_epoch="epoch",
            now=AT,
        )
    app = create_user_app(auth, UserHTTPConfig(origin=ORIGIN))
    if not any(r.path == "/api/v1/reports/daily" for r in app.routes):
        register_report_routes(app, auth)
    return SimpleNamespace(engine=migrated, service=service, auth=auth, session=session, app=app)


def ingest(state, batch):
    return state.service.ingest(
        batch, **GATEWAY, now=batch.body.captured_at + timedelta(seconds=0.1)
    )


def production(state, seq, execution, at, quantity=1):
    body = BatchBody(
        schema_version="1.0",
        batch_id=f"events-{seq}",
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        capture_generation=1,
        capture_seq=seq,
        captured_at=at,
        events=[
            dict(
                kind="PRODUCTION_GOOD",
                event_id=f"event-{seq}",
                seq=seq - 9,
                equipment_id="Outfeed",
                execution_id=execution,
                source_id="source",
                stream_id="stream",
                occurred_at=at,
                payload=dict(quantity=quantity, counter_epoch="epoch"),
            )
        ],
    )
    ingest(state, GatewayBatch(body=body, content_sha256=batch_digest(body)))


def states(state, seq, rows, *, execution="run"):
    """One event batch of PLC Line STATE_CHANGED events: (seconds, previous, state)."""
    body = BatchBody(
        schema_version="1.0",
        batch_id=f"states-{seq}",
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        capture_generation=1,
        capture_seq=seq,
        captured_at=AT + timedelta(seconds=rows[-1][0]),
        events=[
            dict(
                kind="STATE_CHANGED",
                event_id=f"state-{seq}-{index}",
                seq=seq * 10 + index,
                equipment_id="Line",
                execution_id=execution,
                source_id="source",
                stream_id="stream",
                occurred_at=AT + timedelta(seconds=seconds),
                payload=dict(previous_state=previous, state=current, control_version=seq),
            )
            for index, (seconds, previous, current) in enumerate(rows)
        ],
    )
    ingest(state, GatewayBatch(body=body, content_sha256=batch_digest(body)))


def test_timeline_follows_plc_state_events_and_unobserved_time_is_unknown(state):
    # Captures every second for 0..20 s and 40..50 s; nothing in between (plant off).
    seq = 0
    for second in [*range(0, 21), *range(40, 51)]:
        seq += 1
        ingest(state, capture(seq, seconds=second))
    states(
        state, 500, [(2, "READY", "RUNNING"), (8, "RUNNING", "PAUSED"), (12, "PAUSED", "RUNNING")]
    )
    states(state, 501, [(45, "RUNNING", "STOPPING"), (46, "STOPPING", "STOPPED")])
    with state.engine.begin() as db:
        report = read_execution_timeline(
            db, "run", LINE_ID, "source", "stream", now=AT + timedelta(seconds=50)
        )
    shape = [
        (s["from"][17:19], s["to"][17:19], s["class"], s["state"], s.get("unknown_reason"))
        for s in report["segments"]
    ]
    assert shape == [
        ("00", "02", "STOPPED", "STOPPED", None),
        ("02", "08", "RUNNING", "RUNNING", None),
        ("08", "12", "STOPPED", "PAUSED", None),
        ("12", "23", "RUNNING", "RUNNING", None),
        ("23", "40", "UNKNOWN", None, "NO_DATA"),
        ("40", "45", "RUNNING", "RUNNING", None),
        ("45", "46", "STOPPED", "STOPPING", None),
    ]  # the execution ends at its final STOPPED event
    assert report["check_ok"]
    assert report["durations_seconds"] == dict(running=22, stopped=7, unknown=17, total=46)
    assert all(s["stop_reason"] is None for s in report["segments"])


def test_seoul_midnight_quantities_execution_isolation_and_uint64(state):
    midnight = AT + timedelta(hours=15)
    production(state, 10, "run", midnight - timedelta(microseconds=1), UINT64_MAX)
    production(state, 11, "other", midnight, 2)
    production(state, 12, "other", midnight + timedelta(hours=1), 3)
    with state.engine.begin() as db:
        previous = read_daily_report(
            db, LINE_ID, date(2026, 10, 25), now=midnight + timedelta(days=1)
        )
        following = read_daily_report(
            db, LINE_ID, date(2026, 10, 26), now=midnight + timedelta(days=1)
        )
        listing = read_execution_reports(
            db, LINE_ID, AT, AT + timedelta(days=1), now=midnight + timedelta(days=1)
        )
    assert previous["totals"]["good"] == str(UINT64_MAX)
    assert previous["executions"] == ["run"]
    assert following["totals"]["good"] == "5"
    assert following["executions"] == ["other"]
    assert following["hourly"][0]["good"] == "2"
    assert following["hourly"][1]["good"] == "3"
    assert len(following["hourly"]) == 24
    assert following["durations_seconds"] == dict(running=0, stopped=0, unknown=86400, total=86400)
    values = {x["execution_id"]: x for x in listing["items"]}
    assert len(values) == 2
    assert values["run"]["totals"]["good"] == str(UINT64_MAX)
    # Each item carries its own source scope so line-wide lists can open scoped details.
    assert (values["run"]["source_id"], values["run"]["stream_id"]) == ("source", "stream")
    assert values["other"]["totals"]["good"] == "5"
    assert values["run"]["started_at"] is None and values["run"]["ended_at"] is None
    assert values["run"]["provisional"]
    assert "EXECUTION_NOT_SETTLED" in values["run"]["provisional_reasons"]


async def test_asgi_read_permission_strict_queries_csv_and_no_idle_refresh(state):
    with state.engine.begin() as db:
        db.execute(
            text(
                "UPDATE production_executions SET work_order_id='=HYPERLINK(1)' "
                "WHERE execution_id='run'"
            )
        )
        before = (
            db.execute(text("SELECT last_seen_at FROM auth_sessions ORDER BY last_seen_at"))
            .scalars()
            .all()
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=state.app), base_url=ORIGIN
    ) as client:
        params = {
            "line_id": LINE_ID,
            "from": AT.isoformat(),
            "to": (AT + timedelta(days=1)).isoformat(),
        }
        response = await client.get("/api/v1/reports/executions", params=params)
        assert response.status_code == 401
        client.cookies.set("hmi_session", state.session.token)
        response = await client.get("/api/v1/reports/executions", params=params)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        csv = await client.get("/api/v1/reports/executions.csv", params=params)
        assert csv.status_code == 200
        assert csv.content.startswith(b"\xef\xbb\xbf") and "'=HYPERLINK(1)" in csv.text
        assert csv.headers["content-disposition"].startswith("attachment;")
        for invalid in (
            params | {"extra": "x"},
            list(params.items()) + [("line_id", LINE_ID)],
            params | {"to": (AT + timedelta(days=93, microseconds=1)).isoformat()},
            params | {"limit": "101"},
        ):
            assert (
                await client.get("/api/v1/reports/executions", params=invalid)
            ).status_code == 422
        assert (
            await client.get(
                "/api/v1/reports/executions",
                params=params | {"to": (AT + timedelta(days=93)).isoformat()},
            )
        ).status_code == 200
        daily = await client.get(
            "/api/v1/reports/daily", params={"line_id": LINE_ID, "date": "2026-10-26"}
        )
        assert daily.status_code == 200
        assert daily.json()["durations_seconds"]["total"] == 9 * 3600
        assert "DAY_NOT_FINISHED" in daily.json()["provisional_reasons"]
    with state.engine.begin() as db:
        assert (
            db.execute(text("SELECT last_seen_at FROM auth_sessions ORDER BY last_seen_at"))
            .scalars()
            .all()
            == before
        )


def test_midnight_time_split_exact_scope_and_annotator_clock(state):
    midnight = AT + timedelta(hours=15)
    ingest(state, capture(1, seconds=53998))
    ingest(state, capture(2, seconds=54000))
    states(state, 600, [(53998, "READY", "RUNNING")])
    calls = []

    def annotate(db, segments, scope, *, now):
        calls.append((scope, now))
        return segments

    with state.engine.begin() as db:
        before = read_daily_report(
            db, LINE_ID, date(2026, 10, 25), now=midnight + timedelta(days=1)
        )
        after = read_daily_report(
            db,
            LINE_ID,
            date(2026, 10, 26),
            now=midnight + timedelta(days=1),
            scope={"line_id": LINE_ID, "source_id": "source", "stream_id": "stream"},
            annotate=annotate,
        )
        listing = read_execution_reports(
            db, LINE_ID, AT, AT + timedelta(days=1), now=midnight + timedelta(days=1), limit=1
        )
        second = read_execution_reports(
            db,
            LINE_ID,
            AT,
            AT + timedelta(days=1),
            now=midnight + timedelta(days=1),
            limit=1,
            cursor=listing["next_cursor"],
        )
    assert before["durations_seconds"]["running"] == 2
    assert after["durations_seconds"]["running"] == 3
    assert before["durations_seconds"]["total"] == after["durations_seconds"]["total"] == 86400
    assert calls == [
        (
            {"line_id": LINE_ID, "source_id": "source", "stream_id": "stream"},
            midnight + timedelta(days=1),
        )
    ]
    assert listing["next_cursor"] == "run" and second["items"][0]["execution_id"] == "other"
    assert second["next_cursor"] is None


def test_final_settlement_totals_and_state_event_bounds(state):
    from apps.api.settlement import record_final_summary
    from contracts.models import RunSummary

    body = BatchBody(
        schema_version="1.0",
        batch_id="states",
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        capture_generation=1,
        capture_seq=1,
        captured_at=AT + timedelta(seconds=5),
        events=[
            dict(
                kind="STATE_CHANGED",
                event_id=f"state-{seq}",
                seq=seq,
                equipment_id="Line",
                execution_id="run",
                source_id="source",
                stream_id="stream",
                occurred_at=AT + timedelta(seconds=seconds),
                payload=dict(previous_state=previous, state=current, control_version=seq),
            )
            for seq, seconds, previous, current in [
                (1, 1, "READY", "RUNNING"),
                (2, 4, "RUNNING", "STOPPING"),
                (3, 5, "STOPPING", "STOPPED"),
            ]
        ],
    )
    ingest(state, GatewayBatch(body=body, content_sha256=batch_digest(body)))
    summary = RunSummary(
        source_id="source",
        stream_id="stream",
        execution_id="run",
        state="ENDED",
        as_of=AT + timedelta(seconds=5),
        ended_at=AT + timedelta(seconds=5),
        counter_epoch="epoch",
        recipe_version="recipe",
        parameters=PARAMETERS,
        input_quantity=0,
        good_quantity=0,
        reject_quantity=0,
        scrap_quantity=0,
        wip_quantity=0,
        last_event_seq=3,
        final=True,
    )
    with state.engine.begin() as db:
        settled = record_final_summary(db, summary, **GATEWAY, now=AT + timedelta(seconds=5))
        assert settled.status == "SETTLED"
        values = read_execution_reports(
            db, LINE_ID, AT, AT + timedelta(days=1), now=AT + timedelta(seconds=20)
        )
    item = next(i for i in values["items"] if i["execution_id"] == "run")
    assert item["started_at"] == (AT + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    assert item["ended_at"] == (AT + timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    assert item["settlement_status"] == "SETTLED"
    assert item["durations_seconds"]["total"] == 5
    assert item["totals"] == dict(good="0", reject="0", scrap="0")
    assert "EXECUTION_NOT_SETTLED" not in item["provisional_reasons"]


async def test_timeline_query_route_carries_execution_ids_with_slashes(state):
    state.service.register_execution(
        execution_id="lot/../7",
        source_id="source",
        stream_id="stream",
        work_order_id="slash",
        recipe_version="recipe",
        parameters=PARAMETERS,
        counter_epoch="epoch-slash",
        now=AT,
    )
    scope = {"line_id": LINE_ID, "source_id": "source", "stream_id": "stream"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=state.app), base_url=ORIGIN
    ) as client:
        client.cookies.set("hmi_session", state.session.token)
        path = "/api/v1/reports/execution-timeline"
        response = await client.get(path, params=scope | {"execution_id": "lot/../7"})
        assert response.status_code == 200
        assert response.json()["execution_id"] == "lot/../7"
        assert (await client.get(path, params=scope)).status_code == 422
        missing = await client.get(path, params=scope | {"execution_id": "absent"})
        assert missing.status_code == 404
