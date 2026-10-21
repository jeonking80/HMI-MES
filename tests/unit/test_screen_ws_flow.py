"""Deterministic screen ordering/backpressure; no live transport claims here."""

from datetime import UTC, datetime

import pytest

from apps.api.screen_ws import ScreenWSConfig, _Outbound, _Pressure, _RevisionExhausted, _Sequence
from contracts.models import UINT64_MAX
from contracts.transport import LineView, validate_screen_update

AT = datetime(2026, 10, 21, tzinfo=UTC)
VIEW = LineView(line_id="line", state="UNKNOWN", execution_id=None, control_restricted=True)


def test_bounded_32_frames_invalidate_without_dropping_a_revision():
    queue = _Outbound(ScreenWSConfig())
    sequence = _Sequence("line")
    queue.put(sequence.initial(VIEW, AT))
    for _ in range(31):
        queue.put(sequence.update(VIEW, AT))
    assert queue.queue.qsize() == 32
    with pytest.raises(_Pressure) as caught:
        queue.put(sequence.update(VIEW, AT))
    assert caught.value.can_notify
    assert queue.queue.qsize() == 32
    assert '"kind":"SNAPSHOT"' in queue.queue.get_nowait()


def test_full_replacement_order_gap_reconnect_and_uint64_exhaustion():
    sequence = _Sequence("line")
    initial = sequence.initial(VIEW, AT)
    update = sequence.update(VIEW, AT)
    validate_screen_update(initial, update)
    assert update.view == VIEW and '"revision":"1"' in update.model_dump_json()
    skipped = sequence.update(VIEW, AT)
    with pytest.raises(ValueError):
        validate_screen_update(initial, skipped)
    reconnect = _Sequence("line").initial(VIEW, AT)
    assert reconnect.sync_id != initial.sync_id
    with pytest.raises(ValueError):
        validate_screen_update(reconnect, update)
    sequence.revision = UINT64_MAX
    with pytest.raises(_RevisionExhausted):
        sequence.update(VIEW, AT)


def test_frame_bound_rejects_without_enqueuing():
    queue = _Outbound(ScreenWSConfig(max_outbound_bytes=1))
    with pytest.raises(_Pressure) as caught:
        queue.put(_Sequence("line").initial(VIEW, AT))
    assert not caught.value.can_notify and queue.queue.empty()


@pytest.mark.parametrize(
    "options",
    [
        {"queue_size": 33},
        {"queue_size": True},
        {"poll_interval_seconds": float("nan")},
        {"send_timeout_seconds": 0},
        {"max_inbound_bytes": 4097},
        {"max_outbound_bytes": 131073},
    ],
)
def test_runtime_bounds_are_explicit(options):
    with pytest.raises(ValueError):
        ScreenWSConfig(**options)
