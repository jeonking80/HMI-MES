"""Expired raw provenance coexists with retained minutes, execution totals and clocks."""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from apps.api.bootstrap import LINE_ID
from apps.api.capture_clock import read_capture_clock
from apps.api.ingest_retention import expired_coverage
from apps.api.reports import read_daily_report
from apps.api.trend_history import TrendRetention, cleanup_trends, read_trends
from tests.integration.test_alarm_consumer import capture
from tests.integration.test_alarm_consumer import state as state
from tests.integration.test_capture_clock import modified, same_host
from tests.integration.test_capture_clock import service as service
from tests.integration.test_ingest_retention import OLD, finish_compaction, prune
from tests.integration.test_trend_history import SCOPE, TAG, batch, submit

AT = OLD.replace(hour=0, minute=0, second=0, microsecond=0)


@pytest.fixture(autouse=True)
def clock_policy(service):
    service.trend_clock_policy = replace(
        same_host(),
        valid_from_api_utc=AT - timedelta(days=1),
        valid_until_api_utc=AT + timedelta(days=2),
    )


def read(service, resolution):
    with service.engine.connect() as db:
        return read_trends(
            db,
            scope=SCOPE,
            tags=[TAG],
            start=AT,
            end=AT + timedelta(minutes=2),
            resolution=resolution,
            now=AT + timedelta(minutes=3),
        )


def test_expired_raw_is_explicit_and_minute_values_are_preserved(service):
    submit(service, batch(1, AT), now=OLD)
    before = read(service, "minute")["series"][0]["points"][0]
    assert before["value"] == 150.0
    assert prune(service.engine)["deleted_count"] == 1
    with service.engine.begin() as db:
        cleanup_trends(db, now=AT + timedelta(days=8), retention=TrendRetention())
    second = read(service, "second")
    assert second["raw_evidence"]["availability"] == "RETENTION_EXPIRED"
    assert second["retention_expired"][0]["from_capture_seq"] == "1"
    point = second["series"][0]["points"][0]
    assert point["quality"] == "RETENTION_EXPIRED"
    assert point["value"] is None
    minute = read(service, "minute")["series"][0]["points"][0]
    assert minute["value"] == before["value"]
    assert minute["valid_count"] == before["valid_count"]
    assert minute["raw_evidence"]["availability"] == "RETENTION_EXPIRED"
    with service.engine.connect() as db:
        report = read_daily_report(
            db,
            LINE_ID,
            AT.date(),
            now=AT + timedelta(days=1),
            scope=SCOPE,
        )
    assert report["raw_evidence"]["availability"] == "RETENTION_EXPIRED"


def test_canonical_offset_envelope_is_distinct_from_original_capture(service):
    from tests.integration.test_trend_history import policy

    service.trend_clock_policy = policy().model_copy(
        update={
            "offset_lower_seconds": 0.9,
            "offset_upper_seconds": 1.1,
            "valid_from_api_utc": AT - timedelta(days=1),
            "valid_until_api_utc": AT + timedelta(days=2),
        }
    )
    submit(service, batch(1, AT), now=OLD + timedelta(seconds=2))
    assert prune(service.engine)["deleted_count"] == 1
    with service.engine.connect() as db:
        coverage = db.execute(sa.text("SELECT * FROM ingest_retention_ranges")).mappings().one()
    assert coverage["original_from"] == AT
    assert coverage["analytical_from"] == AT + timedelta(seconds=1)
    assert coverage["time_unknown"] is False
    with service.engine.begin() as db:
        cleanup_trends(db, now=AT + timedelta(days=8), retention=TrendRetention())
    points = read(service, "second")["series"][0]["points"]
    assert points[1]["quality"] == "RETENTION_EXPIRED"


def test_unknown_clock_expiry_never_becomes_normal_missing_data(service):
    service.trend_clock_policy = None
    submit(service, batch(1, AT), now=OLD)
    assert prune(service.engine)["deleted_count"] == 1
    result = read(service, "second")
    assert result["retention_expired"][0]["time_unknown"] is True
    assert all(p["quality"] == "RETENTION_EXPIRED" for p in result["series"][0]["points"])


def test_reversal_and_epoch_bootstrap_use_compacted_frontiers_when_every_raw_is_gone(service):
    submit(service, batch(1, AT + timedelta(seconds=1)), now=AT + timedelta(minutes=2))
    submit(service, batch(3, AT + timedelta(seconds=61)), now=AT + timedelta(minutes=2))
    assert finish_compaction(service.engine, prune(service.engine))["deleted_count"] == 2
    with service.engine.connect() as db:
        assert db.execute(sa.text("SELECT count(*) FROM ingest_batches")).scalar() == 0
        assert db.execute(sa.text("SELECT count(*) FROM capture_clock_evidence")).scalar() == 0
        assert db.execute(sa.text("SELECT count(*) FROM ingest_receipts")).scalar() == 0
        frontiers = (
            db.execute(
                sa.text(
                    "SELECT clock_frontiers FROM ingest_retention_ranges ORDER BY from_capture_seq"
                )
            )
            .scalars()
            .all()
        )
        assert [item["clock-1"]["first"]["capture_seq"] for item in frontiers] == ["1", "3"]
    submit(service, batch(2, AT + timedelta(seconds=62)), now=AT + timedelta(minutes=2))
    with service.engine.connect() as db:
        assert (
            db.execute(
                sa.text(
                    "SELECT reversal_seq FROM analog_history_clock_state "
                    "WHERE clock_epoch='clock-1'",
                )
            ).scalar_one()
            == 3
        )
        coverage = expired_coverage(
            db,
            **SCOPE,
            from_time=AT,
            to_time=AT + timedelta(minutes=2),
        )
    assert any(item["time_unknown"] for item in coverage)
    submit(service, batch(4, AT + timedelta(seconds=63)), now=AT + timedelta(minutes=2))
    with service.engine.connect() as db:
        decision = read_capture_clock(db, "gateway-1", "batch-4")
    assert decision.status == "UNKNOWN"
    assert decision.reason_code == "CLOCK_REVERSAL"
    restarted = modified(
        batch(5, AT + timedelta(seconds=64)),
        lambda body: body["capture_time"].update(
            clock_epoch="clock-after-restart", monotonic_seconds=1.0
        ),
    )
    submit(service, restarted, now=AT + timedelta(minutes=2))
    with service.engine.connect() as db:
        assert read_capture_clock(db, "gateway-1", "batch-5").status == "GOOD"


def test_alarm_decisions_remain_exact_when_parent_raw_is_retired(state):
    value = capture(1)
    state.service.ingest(
        value,
        authenticated_gateway_id="gateway",
        authenticated_source_id="source",
        authenticated_stream_id="stream",
        now=OLD,
    )
    with state.engine.connect() as db:
        before = db.execute(sa.text("SELECT * FROM alarm_capture_decisions")).all()
        assert before
    assert prune(state.engine)["deleted_count"] == 1
    with state.engine.connect() as db:
        assert db.execute(sa.text("SELECT * FROM alarm_capture_decisions")).all() == before
        target = db.execute(
            sa.text("""SELECT confrelid::regclass::text FROM pg_constraint
            WHERE conrelid='alarm_capture_decisions'::regclass
                AND confrelid='ingest_receipts'::regclass AND contype='f'""")
        ).scalar_one()
        assert target == "ingest_receipts"
        assert db.execute(sa.text("SELECT count(*) FROM ingest_receipts")).scalar() == 1
        assert db.execute(sa.text("SELECT count(*) FROM ingest_batches")).scalar() == 0


@pytest.mark.parametrize("coordinate", ["utc", "monotonic"])
def test_merged_compacted_epoch_endpoints_preserve_chronology_and_reversal(service, coordinate):
    for seq in (1, 2, 3):
        submit(service, batch(seq, AT + timedelta(seconds=seq)), now=OLD)
    assert finish_compaction(service.engine, prune(service.engine))["compacted_count"] == 3
    with service.engine.connect() as db:
        assert db.execute(sa.text("SELECT count(*) FROM ingest_receipts")).scalar() == 0
        permanent = db.execute(sa.text("SELECT * FROM ingest_retention_ranges")).mappings().one()
    frontier = permanent["clock_frontiers"]["clock-1"]
    assert frontier["first"]["capture_seq"] == "1"
    assert frontier["last"]["capture_seq"] == "3"
    assert frontier["first"]["monotonic_seconds"] == 101.0
    assert frontier["last"]["monotonic_seconds"] == 103.0
    assert permanent["latest_capture"]["batch_id"] == "batch-3"
    assert (
        permanent["latest_capture"]["content_sha256"]
        == batch(3, AT + timedelta(seconds=3)).content_sha256
    )
    value = batch(4, AT + timedelta(seconds=2 if coordinate == "utc" else 4))
    if coordinate == "monotonic":
        value = modified(value, lambda body: body["capture_time"].update(monotonic_seconds=102.0))
    submit(service, value, now=OLD)
    with service.engine.connect() as db:
        decision = read_capture_clock(db, "gateway-1", "batch-4")
        assert decision.status == "UNKNOWN"
        assert decision.reason_code == "CLOCK_REVERSAL"
        reversal = db.execute(
            sa.text("""SELECT reversal_seq FROM analog_history_clock_state
            WHERE clock_epoch='clock-1'""")
        ).scalar_one()
    assert reversal == 4


def test_unknown_clock_expiry_is_bounded_by_surviving_good_captures(service):
    good = service.trend_clock_policy
    service.trend_clock_policy = None
    submit(service, batch(1, AT), now=OLD)
    assert prune(service.engine)["deleted_count"] == 1
    service.trend_clock_policy = good
    submit(service, batch(2, AT + timedelta(seconds=30)), now=OLD)
    result = read(service, "second")
    entry = result["retention_expired"][0]
    assert entry["time_unknown"] is True
    assert entry["sequence_from"] is None and entry["sequence_until"] is not None

    def at(point):
        return datetime.fromisoformat(point["t"].replace("Z", "+00:00"))

    points = result["series"][0]["points"]
    expired = [at(p) for p in points if p["quality"] == "RETENTION_EXPIRED"]
    # Capture 1 happened before GOOD capture 2: later seconds stay ordinary.
    assert expired and max(expired) < AT + timedelta(seconds=33)
    assert all(
        p["quality"] != "RETENTION_EXPIRED" for p in points if at(p) > AT + timedelta(seconds=33)
    )


def test_inverted_neighbour_bounds_keep_conservative_query_coverage(monkeypatch):
    from apps.api import retention_provenance

    entry = {
        "time_unknown": True,
        "analytical_from": None,
        "analytical_until": None,
        "sequence_from": (AT + timedelta(minutes=5)).isoformat(),
        "sequence_until": (AT + timedelta(minutes=1)).isoformat(),
    }
    monkeypatch.setattr(retention_provenance, "expired_coverage", lambda *a, **k: [entry])
    end = AT + timedelta(minutes=10)
    windows = retention_provenance.expiry_windows(None, scope={"line_id": "x"}, start=AT, end=end)
    assert [(left, right) for left, right, _ in windows] == [(AT, end)]


def test_report_clock_decisions_survive_cache_eviction(service, monkeypatch):
    from apps.api import reports

    for seq in range(1, 6):
        submit(service, batch(seq, AT + timedelta(seconds=seq)), now=OLD)

    def daily():
        with service.engine.connect() as db:
            return read_daily_report(
                db, LINE_ID, AT.date(), now=AT + timedelta(days=1), scope=SCOPE
            )

    reports._CLOCK_MEMO.clear()
    expected = daily()
    assert len(reports._CLOCK_MEMO) >= 2  # canonical clock evidence was validated
    # A tiny cache evicts entries while the same report validates the rest.
    monkeypatch.setattr(reports, "_CLOCK_MEMO_LIMIT", 1)
    assert daily() == expected
    assert daily() == expected
