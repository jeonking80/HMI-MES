"""Real PostgreSQL audit, idempotency, authorization and immutable segment checks."""

import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from apps.api.auth import UserAuth
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.database import migration_config
from apps.api.downtime import DowntimeIntent, annotate_downtime, annotate_report_segments
from apps.api.downtime_http import register_downtime_routes
from apps.api.ingestion import IngestionService
from apps.api.user_http import UserHTTPConfig, create_user_app

AT = datetime(2026, 10, 23, tzinfo=UTC)
ORIGIN = "https://downtime.example.test"
SCOPE = dict(line_id=LINE_ID, source_id="source", stream_id="stream")


def account(auth, role, name=None):
    name = name or role
    password = secrets.token_urlsafe(24)
    auth.bootstrap_user(name, password, roles={role})
    challenge = auth.begin_login()
    result = auth.login(
        name,
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=ORIGIN,
    )
    return auth.change_password(
        result.token, secrets.token_urlsafe(24), csrf_token=result.csrf_token, origin=ORIGIN
    )


@pytest.fixture
def state(migrated, monkeypatch):
    bootstrap_reference_data(migrated)
    auth = UserAuth(migrated, allowed_origins={ORIGIN}, clock=lambda: AT + timedelta(minutes=1))
    session = account(auth, "operator")
    IngestionService(migrated).configure_source(
        **SCOPE, gateway_id="gateway", capture_generation=1, now=AT
    )
    segments = [
        {
            "from": AT.isoformat().replace("+00:00", "Z"),
            "to": (AT + timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
            "class": "STOPPED",
            "state": "FAULT",
            "fault_code": 3,
            "stop_reason": None,
            "execution_id": "run",
            "source_id": "source",
            "stream_id": "stream",
        }
    ]

    def timeline(*args, **kwargs):
        return {"segments": segments}

    monkeypatch.setattr("apps.api.reports.read_execution_timeline", timeline)
    app = create_user_app(auth, UserHTTPConfig(origin=ORIGIN))
    if not any(r.path == "/api/v1/downtime-reasons" for r in app.routes):
        register_downtime_routes(app, auth)
    return SimpleNamespace(engine=migrated, auth=auth, session=session, segments=segments, app=app)


def intent(**changes):
    return {
        "request_id": str(uuid4()),
        "execution_id": "run",
        **SCOPE,
        "segment_from": "2026-10-23T00:00:00Z",
        "reason_code": "EQUIPMENT",
        "note": "히터 점검",
    } | changes


def protection(session):
    return {"Origin": ORIGIN, "X-CSRF-Token": session.csrf_token}


@pytest.fixture
async def client(state):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=state.app), base_url=ORIGIN
    ) as client:
        client.cookies.set("hmi_session", state.session.token)
        yield client


async def test_history_replay_conflict_latest_and_daily_clipping(state, client):
    body = intent()
    first = await client.post(
        "/api/v1/downtime-reasons", json=body, headers=protection(state.session)
    )
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    assert (
        await client.post("/api/v1/downtime-reasons", json=body, headers=protection(state.session))
    ).json() == first.json()
    assert (
        await client.post(
            "/api/v1/downtime-reasons",
            json=body | {"note": "other"},
            headers=protection(state.session),
        )
    ).status_code == 409
    second = await client.post(
        "/api/v1/downtime-reasons",
        json=intent(reason_code="PLANNED"),
        headers=protection(state.session),
    )
    assert second.status_code == 200
    history = await client.get(
        "/api/v1/downtime-reasons/history",
        params={"execution_id": "run", "segment_from": body["segment_from"]},
    )
    assert [r["code"] for r in history.json()["items"]] == ["PLANNED", "EQUIPMENT"]
    clipped = state.segments[0] | {"from": "2026-10-23T00:00:10Z", "to": "2026-10-23T00:00:20Z"}
    with state.engine.begin() as db:
        assert (
            annotate_report_segments(db, [clipped], SCOPE, now=AT)[0]["stop_reason"]["code"]
            == "PLANNED"
        )
        assert (
            annotate_report_segments(db, [clipped | {"class": "UNKNOWN"}], SCOPE, now=AT)[0][
                "stop_reason"
            ]
            is None
        )
    state.segments[0]["to"] = "2026-10-23T00:00:40Z"
    with state.engine.begin() as db:
        assert annotate_report_segments(db, [clipped], SCOPE, now=AT)[0]["stop_reason"] is None
    # Replay preserves original receipt after evidence changes.
    assert (
        await client.post("/api/v1/downtime-reasons", json=body, headers=protection(state.session))
    ).json() == first.json()


@pytest.mark.parametrize(
    "role,expected",
    [("operator", 200), ("production_manager", 200), ("viewer", 403), ("equipment_admin", 403)],
)
async def test_permissions_and_csrf(state, client, role, expected):
    session = account(state.auth, role, name="new-" + role)
    client.cookies.set("hmi_session", session.token)
    assert (
        await client.post("/api/v1/downtime-reasons", json=intent(), headers=protection(session))
    ).status_code == expected
    assert (
        await client.post("/api/v1/downtime-reasons", json=intent(), headers={"Origin": ORIGIN})
    ).status_code == 403


async def test_unknown_mismatch_queries_and_cross_actor_scope_replay(state, client):
    body = intent()
    assert (
        await client.post("/api/v1/downtime-reasons", json=body, headers=protection(state.session))
    ).status_code == 200
    for changes in ({"source_id": "other"}, {"stream_id": "other"}, {"execution_id": "other"}):
        assert (
            await client.post(
                "/api/v1/downtime-reasons", json=body | changes, headers=protection(state.session)
            )
        ).status_code == 409
    other = account(state.auth, "operator", name="other")
    client.cookies.set("hmi_session", other.token)
    assert (
        await client.post("/api/v1/downtime-reasons", json=body, headers=protection(other))
    ).status_code == 409
    state.segments[0]["class"] = "UNKNOWN"
    assert (
        await client.post("/api/v1/downtime-reasons", json=intent(), headers=protection(other))
    ).status_code == 422
    assert (
        await client.post(
            "/api/v1/downtime-reasons",
            json=intent(segment_from="2026-10-23T00:00:01Z"),
            headers=protection(other),
        )
    ).status_code == 409
    query = "execution_id=run&segment_from=2026-10-23T00:00:00Z"
    for extra in ("&extra=1", "&execution_id=run"):
        assert (
            await client.get("/api/v1/downtime-reasons/history?" + query + extra)
        ).status_code == 422
    client.cookies.clear()
    assert (await client.get("/api/v1/downtime-reasons/history?" + query)).status_code == 401


def test_concurrent_replay_atomic_failure_immutability_and_migration(state):
    body = DowntimeIntent.model_validate(intent())

    def write():
        with state.engine.begin() as db:
            return annotate_downtime(db, body, principal=state.session.principal, now=AT)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: write(), range(4)))
    assert results == [results[0]] * 4
    with state.engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM downtime_reason_history")).scalar_one() == 1
    for sql in (
        "UPDATE downtime_reason_history SET note='tampered'",
        "DELETE FROM downtime_reason_history",
        "TRUNCATE downtime_reason_history",
    ):
        with pytest.raises(DBAPIError), state.engine.begin() as db:
            db.execute(text(sql))
    with pytest.raises(RuntimeError), state.engine.begin() as db:
        annotate_downtime(
            db, DowntimeIntent.model_validate(intent()), principal=state.session.principal, now=AT
        )
        raise RuntimeError("force rollback")
    with state.engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM downtime_reason_history")).scalar_one() == 1
    with state.engine.connect() as db, pytest.raises(DBAPIError):
        command.downgrade(migration_config(db), "0015_analog_history")


def test_empty_migration_roundtrip(engine):
    with engine.connect() as db:
        command.upgrade(migration_config(db), "0016_downtime_reasons")
        command.downgrade(migration_config(db), "0015_analog_history")
        command.upgrade(migration_config(db), "head")


def test_real_evidence_timeline_daily_and_unknown(migrated):
    """Use the same actual capture/clock/coordinator setup as reporting, without a mock."""
    from test_alarm_consumer import AT as report_at
    from test_alarm_consumer import capture
    from test_reports import ingest, states
    from test_reports import state as report_fixture

    state = report_fixture.__wrapped__(migrated)
    operator = account(
        UserAuth(migrated, allowed_origins={ORIGIN}, clock=lambda: report_at),
        "operator",
        "downtime-operator",
    )
    for seq, seconds in ((1, 0), (2, 2)):
        ingest(state, capture(seq, seconds=seconds))
    # The PLC reports the run paused from the start; the reason annotates that stop.
    states(state, 500, [(0, "READY", "PAUSED")])
    from apps.api.reports import read_daily_report, read_execution_timeline

    now = report_at + timedelta(seconds=3)
    with migrated.begin() as db:
        timeline = read_execution_timeline(db, "run", **SCOPE, now=now)
        stopped = next(s for s in timeline["segments"] if s["class"] == "STOPPED")
        definition = DowntimeIntent.model_validate(intent(segment_from=stopped["from"]))
        receipt = annotate_downtime(db, definition, principal=operator.principal, now=now)
        assert receipt["code"] == "EQUIPMENT"
        annotated = read_execution_timeline(
            db, "run", **SCOPE, now=now, annotate=annotate_report_segments
        )
        assert (
            next(s for s in annotated["segments"] if s["class"] == "STOPPED")["stop_reason"]["code"]
            == "EQUIPMENT"
        )
        report = read_daily_report(
            db,
            LINE_ID,
            (report_at + timedelta(hours=9)).date(),
            now=now,
            annotate=annotate_report_segments,
        )
        assert report["stopped_by_reason"]["EQUIPMENT"] == 3
        assert sum(report["stopped_by_reason"].values()) == report["durations_seconds"]["stopped"]
