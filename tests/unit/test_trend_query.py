"""Strict trend bounds and conservative quality preserve the wire contract."""

from datetime import timedelta

import pytest
from starlette.datastructures import QueryParams

from apps.api.minute_aggregation import RawObservation
from apps.api.trend_history import TrendRetention, points_for
from apps.api.trend_http import parse_query
from tests.integration.test_trend_history import AT, SCOPE, TAG, batch


def query(delta, resolution):
    return QueryParams(
        list(SCOPE.items())
        + [
            ("tag", TAG),
            ("from", AT.isoformat()),
            ("to", (AT + delta).isoformat()),
            ("resolution", resolution),
        ]
    )


def test_exact_bounds_auto_and_invalid_values():
    assert parse_query(query(timedelta(hours=1), "auto"))["resolution"] == "second"
    assert parse_query(query(timedelta(hours=1, seconds=1), "auto"))["resolution"] == "minute"
    assert parse_query(query(timedelta(days=7), "minute"))["resolution"] == "minute"
    for delta, resolution in [
        (timedelta(hours=1, microseconds=1), "second"),
        (timedelta(days=7, microseconds=1), "minute"),
        (timedelta(0), "minute"),
        (timedelta(minutes=1), "invalid"),
    ]:
        with pytest.raises(ValueError):
            parse_query(query(delta, resolution))
    for changes in [
        {"delete_limit": 0},
        {"raw_days": 0},
        {"minute_days": 91},
        {"raw_days": float("nan")},
        {"delete_limit": True},
    ]:
        with pytest.raises(ValueError):
            TrendRetention(**changes)


def observation(seq=1, status=0, epoch="clock-1"):
    body = batch(seq, status=status).body
    return RawObservation(
        scope=dict(
            source_id=body.source_id,
            stream_id=body.stream_id,
            gateway_id=body.gateway_id,
            capture_generation=body.capture_generation,
            clock_epoch=epoch,
        ),
        capture_seq=seq,
        sample=body.samples[0],
        time_quality="GOOD",
    )


def test_latest_bad_and_uncertain_do_not_fallback_to_old_good():
    for status, expected in [(0x80000000, "BAD"), (0x40000000, "UNCERTAIN")]:
        points = points_for(
            (observation(), observation(2, status)), TAG, AT, AT + timedelta(seconds=1), "second"
        )
        assert points[0]["quality"] == expected
        assert points[0]["value"] is None


def test_mixed_epoch_never_becomes_good_and_partial_minute_expected_is_60():
    points = points_for(
        (observation(), observation(2, epoch="new-clock")),
        TAG,
        AT,
        AT + timedelta(minutes=1),
        "minute",
    )
    assert points[0]["quality"] == "TIME_UNKNOWN"
    assert points[0]["value"] is None
    points = points_for((observation(),), TAG, AT, AT + timedelta(seconds=1), "minute")
    assert points[0]["quality"] == "PARTIAL"
    assert points[0]["expected_count"] == 60
