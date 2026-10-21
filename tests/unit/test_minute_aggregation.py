"""Pure DEV15 slot policy: counts, immutable evidence, quality and local boundaries."""

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from apps.api.minute_aggregation import (
    ANALOG_UNITS,
    POLICY_VERSION,
    AggregationConflict,
    AggregationScope,
    RawObservation,
    aggregate_minutes,
    seoul_day_bounds,
)
from contracts.transport import TagSample

AT = datetime(2026, 10, 21, tzinfo=UTC)
TAG = "Sealer.TemperaturePV"
SCOPE = AggregationScope(
    source_id="source",
    stream_id="stream",
    gateway_id="gateway",
    capture_generation=1,
    clock_epoch="epoch",
)


def observation(seq, seconds, value=10.0, *, status=0, quality="GOOD", tag=TAG, **changes):
    return RawObservation(
        scope=SCOPE,
        capture_seq=seq,
        time_quality=quality,
        sample=TagSample(
            sample_id=f"sample-{seq}-{tag}",
            tag_id=tag,
            acquired_at=AT + timedelta(seconds=seconds),
            source_timestamp=AT - timedelta(days=30),
            server_timestamp=None,
            status_code=status,
            reading={"data_type": "Double", "value": value},
        ),
        **changes,
    )


def aggregate(*items, start=AT, end=AT + timedelta(minutes=1), tags=(TAG,)):
    return aggregate_minutes(items, scope=SCOPE, tag_ids=tags, start=start, end=end)


def test_complete_minute_and_exact_nine_units():
    (result,) = aggregate(*(observation(i + 1, i, float(i)) for i in range(60)))
    assert len(ANALOG_UNITS) == 9
    assert result.policy_version == POLICY_VERSION
    assert (
        result.raw_count,
        result.observed_slot_count,
        result.valid_slot_count,
        result.expected_slot_count,
    ) == (60, 60, 60, 60)
    assert (result.minimum, result.maximum, result.average) == (0, 59, 29.5)
    assert (result.last_value, result.last_observed_at) == (59, AT + timedelta(seconds=59))
    assert not result.partial and not result.provisional


def test_jitter_chooses_latest_capture_without_weighting_extra_observations():
    (result,) = aggregate(
        observation(1, 0.1, 100.0), observation(2, 0.9, 2.0), observation(3, 1.1, 4.0)
    )
    assert (result.raw_count, result.valid_slot_count, result.average) == (3, 2, 3)
    assert len(result.slots[0].raw_observations) == 2
    assert result.slots[0].selected.capture_seq == 2
    assert result.missing_slot_count == 58 and result.provisional


@pytest.mark.parametrize("status,value", [(0x80000000, None), (0x40000000, 99.0)])
def test_latest_non_good_replaces_earlier_good_and_last_does_not_resurrect(status, value):
    (result,) = aggregate(
        observation(1, 0, 7.0), observation(2, 1.1, 8.0), observation(3, 1.9, value, status=status)
    )
    assert result.average == 7
    assert result.valid_slot_count == 1 and result.invalid_slot_count == 1
    assert result.last_value is None and result.last_good_value == 7
    assert result.last_observed_at == AT + timedelta(seconds=1.9)
    assert result.slots[1].selected.sample.reading.value == value
    assert result.slots[1].invalid_reason == "OPC_NON_GOOD"


def test_empty_minute_and_no_zero_or_carry_forward():
    first, second = aggregate(observation(1, 59, 12.0), end=AT + timedelta(minutes=2))
    assert first.last_value == 12 and first.last_observed_at == AT + timedelta(seconds=59)
    assert second.raw_count == second.observed_slot_count == second.valid_slot_count == 0
    assert second.expected_slot_count == second.missing_slot_count == 60
    assert second.minimum is second.maximum is second.average is second.last_value is None
    assert second.last_good_value is second.last_observed_at is None


def test_partial_half_open_intersecting_slots_and_exact_sample_filter():
    (result,) = aggregate(
        observation(1, 0.1, 100.0),
        observation(2, 0.2, 2.0),
        observation(3, 1, 4.0),
        observation(4, 1.2, 100.0),
        start=AT + timedelta(seconds=0.2),
        end=AT + timedelta(seconds=1.2),
    )
    assert result.partial and result.expected_slot_count == 2
    assert result.raw_count == 2 and result.average == 3
    assert result.covered_start == AT + timedelta(seconds=0.2)
    assert result.covered_end == AT + timedelta(seconds=1.2)


@pytest.mark.parametrize("quality", ["UNKNOWN", "BAD"])
def test_time_quality_retained_but_excluded_from_statistics(quality):
    (result,) = aggregate(observation(1, 0, 15.0, quality=quality))
    assert result.provisional and result.valid_slot_count == 0
    assert result.average is None and result.raw_count == 1
    assert result.slots[0].invalid_reason == "TIME_" + quality


def test_reversal_uses_capture_order_not_input_order_and_latches_time_uncertainty():
    a, b, c = observation(1, 2), observation(2, 1), observation(3, 3)
    assert aggregate(c, a, b) == aggregate(a, b, c)
    (result,) = aggregate(c, a, b)
    assert result.slots[1].invalid_reason == "CLOCK_REVERSAL"
    assert result.slots[3].invalid_reason == "CLOCK_REVERSAL"
    assert result.slots[2].valid
    assert result.valid_slot_count == 1


def test_identical_replay_idempotence_and_identity_collision():
    item = observation(1, 0)
    assert aggregate(item, item) == aggregate(item)
    changed = item.model_copy(update={"time_quality": "BAD"})
    with pytest.raises(AggregationConflict, match="sample identity"):
        aggregate(item, changed)
    different_id = item.model_copy(
        update={"sample": item.sample.model_copy(update={"sample_id": "different"})}
    )
    with pytest.raises(AggregationConflict, match="capture/tag"):
        aggregate(item, different_id)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_id", "other"),
        ("stream_id", "other"),
        ("gateway_id", "other"),
        ("capture_generation", 2),
        ("clock_epoch", "other"),
    ],
)
def test_ambiguous_provenance_rejected(field, value):
    item = observation(1, 0)
    item = item.model_copy(update={"scope": SCOPE.model_copy(update={field: value})})
    with pytest.raises(AggregationConflict, match="mixed scope"):
        aggregate(item)


def test_inconsistent_same_capture_time_rejected():
    with pytest.raises(AggregationConflict, match="inconsistent acquisition"):
        aggregate(observation(1, 0), observation(1, 1, tag="Sealer.TemperatureSP"))


def test_seoul_midnight_boundaries_and_utc_minute_buckets():
    start, end = seoul_day_bounds(date(2026, 10, 21))
    assert start == datetime(2026, 10, 20, 15, tzinfo=UTC)
    assert end == datetime(2026, 10, 21, 15, tzinfo=UTC)
    (result,) = aggregate(start=start, end=start + timedelta(minutes=1))
    assert result.bucket_start == start and result.expected_slot_count == 60


@pytest.mark.parametrize(
    "changes",
    [
        {"start": AT.replace(tzinfo=None)},
        {"end": AT},
        {"end": AT + timedelta(days=1, seconds=1)},
        {"tags": ()},
        {"tags": (TAG, TAG)},
        {"tags": ("Line.State",)},
        {"tags": tuple(ANALOG_UNITS)[:5]},
    ],
)
def test_query_bounds_and_tags(changes):
    with pytest.raises(ValueError):
        aggregate(**changes)


def test_unknown_time_default_and_nonanalog_input_rejected():
    item = observation(1, 0).model_dump()
    item.pop("time_quality")
    assert RawObservation.model_validate(item).time_quality == "UNKNOWN"
    item["sample"] = TagSample(
        sample_id="state",
        tag_id="Line.State",
        source_timestamp=None,
        server_timestamp=None,
        acquired_at=AT,
        status_code=0,
        reading={"data_type": "Int32", "value": 0},
    )
    with pytest.raises(ValidationError, match="nine dictionary"):
        RawObservation.model_validate(item)


def test_superseded_unknown_evidence_remains_visible_and_provisional():
    (result,) = aggregate(observation(1, 0.1, quality="UNKNOWN"), observation(2, 0.9, 20.0))
    assert result.valid_slot_count == 1 and result.average == 20
    assert result.provisional
    assert result.slots[0].raw_observations[0].time_quality == "UNKNOWN"


def test_independent_tags_same_capture_and_boundary_exclusion():
    other = "Sealer.TemperatureSP"
    rows = aggregate(
        observation(1, 0, 10.0),
        observation(1, 0, 20.0, tag=other),
        observation(2, 60, 500.0),
        tags=(TAG, other),
    )
    assert [row.average for row in rows] == [10, 20]
    assert [row.raw_count for row in rows] == [1, 1]


def test_reversal_context_before_query_is_not_discarded():
    (result,) = aggregate(observation(1, -0.1), observation(2, -0.2), observation(3, 1))
    assert result.raw_count == 1 and result.valid_slot_count == 0
    assert result.slots[1].invalid_reason == "CLOCK_REVERSAL"
