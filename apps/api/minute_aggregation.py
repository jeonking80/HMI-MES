"""Pure, versioned 1 Hz minute aggregation; no persistence, clocks or interpolation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from math import fsum
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import model_validator

from contracts.models import ContractModel, Identifier, PositiveUInt64, UInt64
from contracts.tags.validate import load_contract
from contracts.transport import TagSample

POLICY_VERSION = "utc-second-latest-capture-v1"
ANALOG_UNITS = {
    tag["logical_name"]: tag["unit"] for tag in load_contract()["tags"] if tag["analog_history"]
}
SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)


class AggregationScope(ContractModel):
    source_id: Identifier
    stream_id: Identifier
    gateway_id: Identifier
    capture_generation: UInt64
    clock_epoch: Identifier


class RawObservation(ContractModel):
    scope: AggregationScope
    capture_seq: PositiveUInt64
    sample: TagSample
    time_quality: Literal["GOOD", "UNKNOWN", "BAD"] = "UNKNOWN"

    @model_validator(mode="after")
    def analog_only(self):
        if self.sample.tag_id not in ANALOG_UNITS:
            raise ValueError("only the nine dictionary analog-history tags are supported")
        return self


class AggregationConflict(ValueError):
    """Identity, capture provenance or clock epoch cannot be safely combined."""


@dataclass(frozen=True)
class SlotEvidence:
    start: datetime
    selected: RawObservation | None
    invalid_reason: str | None
    raw_count: int
    raw_observations: tuple[RawObservation, ...]

    @property
    def valid(self) -> bool:
        return self.selected is not None and self.invalid_reason is None


@dataclass(frozen=True)
class MinuteAggregate:
    scope: AggregationScope
    tag_id: str
    unit: str
    bucket_start: datetime
    covered_start: datetime
    covered_end: datetime
    partial: bool
    raw_count: int
    observed_slot_count: int
    valid_slot_count: int
    expected_slot_count: int
    missing_slot_count: int
    invalid_slot_count: int
    minimum: float | None
    maximum: float | None
    average: float | None
    last_value: float | None
    last_observed_at: datetime | None
    last_good_value: float | None
    last_good_at: datetime | None
    provisional: bool
    slots: tuple[SlotEvidence, ...]
    policy_version: str = POLICY_VERSION


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("query boundaries must be timezone-aware")
    return value.astimezone(UTC)


def seoul_day_bounds(day: date) -> tuple[datetime, datetime]:
    """One Seoul calendar day as an exact UTC half-open interval."""
    zone = ZoneInfo("Asia/Seoul")
    return (
        datetime.combine(day, time.min, zone).astimezone(UTC),
        datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC),
    )


def aggregate_minutes(
    observations: tuple[RawObservation, ...],
    *,
    scope: AggregationScope,
    tag_ids: tuple[str, ...],
    start: datetime,
    end: datetime,
) -> tuple[MinuteAggregate, ...]:
    """Aggregate at most four tags over at most 24 h, ordered tag then UTC minute.

    The caller supplies an explicit clock epoch and time-quality proof. Values
    never establish their own clock proof. All inputs must belong to that scope.
    """
    start, end = _utc(start), _utc(end)
    if not start < end or end - start > timedelta(days=1):
        raise ValueError("query must be positive and at most 24 hours")
    if not 1 <= len(tag_ids) <= 4 or len(set(tag_ids)) != len(tag_ids):
        raise ValueError("query requires one to four distinct analog tags")
    if any(tag not in ANALOG_UNITS for tag in tag_ids):
        raise ValueError("unsupported analog tag")
    unique: dict[str, RawObservation] = {}
    positions: dict[tuple[int, str], RawObservation] = {}
    capture_times: dict[int, datetime] = {}
    for observation in observations:
        if observation.scope != scope:
            raise AggregationConflict("mixed scope, generation or clock epoch")
        sample = observation.sample
        previous = unique.get(sample.sample_id)
        if previous is not None and previous != observation:
            raise AggregationConflict("sample identity has changed")
        position = (observation.capture_seq, sample.tag_id)
        if position in positions and positions[position] != observation:
            raise AggregationConflict("capture/tag identity has changed")
        at = sample.acquired_at
        if (
            observation.capture_seq in capture_times
            and capture_times[observation.capture_seq] != at
        ):
            raise AggregationConflict("one capture has inconsistent acquisition times")
        capture_times[observation.capture_seq] = at
        unique[sample.sample_id] = observation
        positions[position] = observation

    reversal_at = None
    previous_at = None
    for seq, at in sorted(capture_times.items()):
        if previous_at is not None and at < previous_at:
            reversal_at = seq
            break
        previous_at = at

    selected: dict[tuple[str, datetime], RawObservation] = {}
    raw: dict[tuple[str, datetime], list[RawObservation]] = {}
    for observation in sorted(unique.values(), key=lambda item: item.capture_seq):
        sample = observation.sample
        if sample.tag_id not in tag_ids or not start <= sample.acquired_at < end:
            continue
        key = (sample.tag_id, sample.acquired_at.replace(microsecond=0))
        selected[key] = observation
        raw.setdefault(key, []).append(observation)

    results = []
    for tag in tag_ids:
        bucket = start.replace(second=0, microsecond=0)
        while bucket < end:
            covered_start, covered_end = max(start, bucket), min(end, bucket + MINUTE)
            cursor = covered_start.replace(microsecond=0)
            slots = []
            while cursor < covered_end:
                key = (tag, cursor)
                observation = selected.get(key)
                reason = None
                if observation is None:
                    reason = "MISSING"
                elif reversal_at is not None and observation.capture_seq >= reversal_at:
                    reason = "CLOCK_REVERSAL"
                elif observation.time_quality != "GOOD":
                    reason = "TIME_" + observation.time_quality
                elif observation.sample.status_code >> 30 != 0:
                    reason = "OPC_NON_GOOD"
                elif observation.sample.reading.value is None:
                    reason = "NULL_VALUE"
                slots.append(
                    SlotEvidence(
                        cursor, observation, reason, len(raw.get(key, ())), tuple(raw.get(key, ()))
                    )
                )
                cursor += SECOND
            good = [slot for slot in slots if slot.valid]
            values = [float(slot.selected.sample.reading.value) for slot in good]
            observed = [slot for slot in slots if slot.selected is not None]
            last = observed[-1] if observed else None
            last_good = good[-1] if good else None
            results.append(
                MinuteAggregate(
                    scope=scope,
                    tag_id=tag,
                    unit=ANALOG_UNITS[tag],
                    bucket_start=bucket,
                    covered_start=covered_start,
                    covered_end=covered_end,
                    partial=covered_start != bucket or covered_end != bucket + MINUTE,
                    raw_count=sum(slot.raw_count for slot in slots),
                    observed_slot_count=len(observed),
                    valid_slot_count=len(good),
                    expected_slot_count=len(slots),
                    missing_slot_count=len(slots) - len(observed),
                    invalid_slot_count=len(observed) - len(good),
                    minimum=min(values) if values else None,
                    maximum=max(values) if values else None,
                    average=fsum(value / len(values) for value in values) if values else None,
                    last_value=float(last.selected.sample.reading.value)
                    if last is not None and last.valid
                    else None,
                    last_observed_at=last.selected.sample.acquired_at if last else None,
                    last_good_value=float(last_good.selected.sample.reading.value)
                    if last_good
                    else None,
                    last_good_at=last_good.selected.sample.acquired_at if last_good else None,
                    provisional=any(
                        slot.selected is None
                        or any(
                            item.time_quality != "GOOD"
                            or (reversal_at is not None and item.capture_seq >= reversal_at)
                            for item in slot.raw_observations
                        )
                        for slot in slots
                    ),
                    slots=tuple(slots),
                )
            )
            bucket += MINUTE
    return tuple(results)
