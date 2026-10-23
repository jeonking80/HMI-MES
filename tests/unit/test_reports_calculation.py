"""Pure spreadsheet safety and exact coverage partition edge cases."""

from datetime import UTC, datetime, timedelta

import pytest

from apps.api.reports import build_timeline, evaluate_segments
from apps.api.reports_http import csv_cell


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_csv_formula_prefixes_are_escaped(prefix):
    assert csv_cell(prefix + "payload") == "'" + prefix + "payload"
    assert csv_cell("한글") == "한글"


def test_unobserved_periods_and_same_source_state_changes():
    at = datetime(2026, 10, 23, tzinfo=UTC)

    def evidence(offset, state):
        return {
            "from": at + timedelta(seconds=offset),
            "to": at + timedelta(seconds=offset + 3),
            "class": "RUNNING" if state == "RUNNING" else "STOPPED",
            "state": state,
            "fault_code": None,
            "stop_reason": None,
            "source_id": "source",
            "stream_id": "stream",
            "execution_id": "original",
        }

    segments = build_timeline(
        at - timedelta(seconds=1),
        at + timedelta(seconds=6),
        [evidence(0, "RUNNING"), evidence(2, "PAUSED")],
    )
    total, stopped = evaluate_segments(segments)
    assert total == dict(running=2, stopped=3, unknown=2, total=7)
    assert stopped == {"PAUSED": 3}
    assert segments[0]["unknown_reason"] == "NO_EVIDENCE"
    assert segments[1]["execution_id"] == "original"
