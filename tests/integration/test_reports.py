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
    service = IngestionService(migrated, alarm_consumer=consumer)
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


def test_coverage_states_communication_time_unknown_and_no_carry(state):
    ingest(state, capture(1, seconds=0))
    ingest(
        state,
        capture(
            2, seconds=2, values={"Line.State": 3, "Sealer.State": 3, "Sealer.RunFeedback": False}
        ),
    )
    ingest(state, capture(3, seconds=4, communication_age=5))
    # Unknown epoch has no configured clock assessment.
    ingest(state, capture(4, seconds=6, epoch="unknown-clock"))
    with state.engine.begin() as db:
        report = read_execution_timeline(
            db, "run", LINE_ID, "source", "stream", now=AT + timedelta(seconds=20)
        )
    assert report["check_ok"]
    assert report["durations_seconds"] == dict(running=2, stopped=2, unknown=16, total=20)
    assert {s.get("unknown_reason") for s in report["segments"]} >= {
        "COMMUNICATION_LOST",
        "TIME_UNKNOWN",
    }
    assert report["segments"][-1]["class"] == "UNKNOWN"
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


def test_permanent_gap_overrides_apparently_healthy_captures(state):
    for seq, seconds in enumerate((0, 2, 4, 6, 8), start=1):
        ingest(state, capture(seq, seconds=seconds))
    production(state, 10, "run", AT + timedelta(seconds=1))
    production(state, 12, "run", AT + timedelta(seconds=8))
    ingest(
        state,
        capture(
            13,
            seconds=10,
            gaps=[
                dict(
                    from_seq=2,
                    through_seq=2,
                    irrecoverable=True,
                    detected_at=AT + timedelta(seconds=10),
                    reason_code="RETENTION_LOST",
                    reason="test permanent gap",
                    affected_execution_ids=["run"],
                )
            ],
        ),
    )
    with state.engine.begin() as db:
        value = read_execution_timeline(
            db, "run", LINE_ID, "source", "stream", now=AT + timedelta(seconds=10)
        )
    assert value["check_ok"]
    assert value["durations_seconds"] == dict(running=3, stopped=0, unknown=7, total=10)
    assert any(
        s["from"] == (AT + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        and s.get("unknown_reason") == "COMMUNICATION_LOST"
        for s in value["segments"]
    )


def test_midnight_time_split_exact_scope_and_annotator_clock(state):
    midnight = AT + timedelta(hours=15)
    ingest(state, capture(1, seconds=53998))
    ingest(state, capture(2, seconds=54000))
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


def test_two_live_sources_are_ambiguous_but_exact_scope_is_preserved(state):
    from apps.api.alarm_consumer import AlarmConsumerConfig
    from apps.api.alarm_lifecycle import AlarmScope

    second_scope = AlarmScope(
        line_id=LINE_ID, source_id="second", stream_id="stream", asset_id="Sealer"
    )
    service = IngestionService(state.engine)
    service.configure_source(
        line_id=LINE_ID,
        source_id="second",
        stream_id="stream",
        gateway_id="second-gateway",
        capture_generation=1,
        now=AT,
    )
    with state.engine.begin() as db:
        install_rule(
            db,
            second_scope,
            PRESSURE,
            input_id="second-install",
            principal=ADMIN,
            at=AT - timedelta(minutes=1),
        )
    original = config(rules=(PRESSURE,))
    second_policy = original.clock_policy.model_copy(
        update={"source_id": "second", "gateway_id": "second-gateway"}
    )
    second_config = AlarmConsumerConfig(
        scope=second_scope,
        gateway_id="second-gateway",
        capture_generation=1,
        rules=(PRESSURE,),
        clock_policy=second_policy,
    )
    consumer = AlarmConsumer(second_config, consumer_epoch="second-epoch")
    start(state.engine, consumer)
    service = IngestionService(state.engine, alarm_consumer=consumer)
    ingest(state, capture(1, seconds=0))
    body = capture(1, seconds=0).body.model_dump(mode="python")
    body.update(source_id="second", gateway_id="second-gateway", batch_id="second-batch")
    body = BatchBody.model_validate(body)
    service.ingest(
        GatewayBatch(body=body, content_sha256=batch_digest(body)),
        authenticated_gateway_id="second-gateway",
        authenticated_source_id="second",
        authenticated_stream_id="stream",
        now=AT + timedelta(seconds=0.1),
    )
    with state.engine.begin() as db:
        broad = read_daily_report(db, LINE_ID, date(2026, 10, 25), now=AT + timedelta(seconds=3))
        exact = read_daily_report(
            db,
            LINE_ID,
            date(2026, 10, 25),
            now=AT + timedelta(seconds=3),
            scope={"source_id": "source", "stream_id": "stream"},
        )
        timeline = read_execution_timeline(
            db, "run", LINE_ID, "source", "stream", now=AT + timedelta(seconds=3)
        )
    assert broad["durations_seconds"]["running"] == 0
    assert exact["durations_seconds"]["running"] == 3
    assert timeline["durations_seconds"]["running"] == 3
    assert timeline["segments"][0]["source_id"] == "source"


def test_clock_reversal_stays_unknown_after_apparent_recovery_and_day_clip(state):
    stopped = {"Line.State": 3, "Sealer.State": 3, "Sealer.RunFeedback": False}
    for seq, seconds in ((1, 2), (2, 1), (3, 4)):
        ingest(state, capture(seq, seconds=seconds, values=stopped))
    with state.engine.begin() as db:
        timeline = read_execution_timeline(
            db, "run", LINE_ID, "source", "stream", now=AT + timedelta(seconds=5)
        )
    assert timeline["durations_seconds"]["stopped"] == 0
    assert any(s.get("unknown_reason") == "TIME_UNKNOWN" for s in timeline["segments"])
    # Clip far beyond the bad samples: old reversal in this epoch still applies.
    ingest(state, capture(4, seconds=54000, values=stopped))
    with state.engine.begin() as db:
        daily = read_daily_report(
            db, LINE_ID, date(2026, 10, 26), now=AT + timedelta(hours=15, seconds=3)
        )
    assert daily["durations_seconds"]["stopped"] == 0
    assert daily["durations_seconds"]["unknown"] == 3


@pytest.mark.parametrize("monotonic,event_only", [(999.0, False), (1000.0, False), (999.0, True)])
def test_monotonic_clock_reversal_invalidates_later_utc_captures(state, monotonic, event_only):
    ingest(state, capture(1, seconds=0))
    body = capture(2, seconds=1).body.model_dump(mode="python")
    body["capture_time"]["monotonic_seconds"] = monotonic
    if event_only:
        body["samples"] = []
        body["events"] = [
            dict(
                kind="STATE_CHANGED",
                event_id="event-only-clock",
                seq=1,
                equipment_id="Line",
                execution_id="run",
                source_id="source",
                stream_id="stream",
                occurred_at=AT + timedelta(seconds=1),
                payload=dict(previous_state="READY", state="RUNNING", control_version=1),
            )
        ]
    body = BatchBody.model_validate(body)
    ingest(state, GatewayBatch(body=body, content_sha256=batch_digest(body)))
    ingest(state, capture(3, seconds=2))
    with state.engine.begin() as db:
        value = read_execution_timeline(
            db, "run", LINE_ID, "source", "stream", now=AT + timedelta(seconds=3)
        )
    assert value["durations_seconds"] == dict(running=1, stopped=0, unknown=2, total=3)
    assert value["segments"][-1]["unknown_reason"] == "TIME_UNKNOWN"


def test_digital_only_reversal_breaks_prior_execution_coverage(state):
    ingest(state, capture(1, seconds=0))
    body = capture(2, seconds=1).body.model_dump(mode="python")
    body["capture_time"]["monotonic_seconds"] = 999.0
    body["samples"] = [s for s in body["samples"] if s["tag_id"] == "Line.State"]
    body = BatchBody.model_validate(body)
    ingest(state, GatewayBatch(body=body, content_sha256=batch_digest(body)))
    with state.engine.begin() as db:
        value = read_execution_timeline(
            db, "run", LINE_ID, "source", "stream", now=AT + timedelta(seconds=3)
        )
    assert value["durations_seconds"] == dict(running=1, stopped=0, unknown=2, total=3)
    assert value["segments"][-1]["unknown_reason"] == "TIME_UNKNOWN"


def test_changed_active_execution_cuts_off_prior_owner_immediately(state):
    ingest(state, capture(1, seconds=0))
    ingest(state, capture(2, seconds=1, values={"Line.ActiveExecutionId": "other"}))
    with state.engine.begin() as db:
        value = read_execution_timeline(
            db, "run", LINE_ID, "source", "stream", now=AT + timedelta(seconds=3)
        )
    assert value["durations_seconds"] == dict(running=1, stopped=0, unknown=2, total=3)
    assert value["segments"][-1]["unknown_reason"] == "NO_EVIDENCE"
    assert value["segments"][-1]["execution_id"] is None
    assert value["segments"][-1]["observed_execution_id"] == "other"
