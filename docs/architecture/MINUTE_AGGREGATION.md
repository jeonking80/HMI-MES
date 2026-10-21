# DEV15 pure minute aggregation policy

`apps/api/minute_aggregation.py` implements `utc-second-latest-capture-v1` only.
It does not implement durable raw projection, a scheduler, HTTP, browser trends,
retention deletion, or aggregate finalization. No database schema changes apply.
The specification and development plan require nine 1 Hz analog tags, raw history
for seven days and minute aggregates for ninety days. Those retention periods are
requirements for later storage work, not behavior provided by this pure unit.

## Input and provenance

`aggregate_minutes(observations, *, scope, tag_ids, start, end)` accepts validated
`RawObservation` objects wrapping original `TagSample` values. The explicit scope
binds source, stream, gateway, capture generation and clock epoch. Mixed scopes
are rejected; independent gateway generations/clock epochs cannot be merged by
comparing their sequence numbers. A 24-hour history spanning collector epochs must
currently be segmented. Cross-epoch selection and merging require a future explicit
policy: callers must not silently drop another epoch or sum segment expected counts
when their wall-clock slots overlap. The future ingestion adapter must establish
this provenance and clock-quality evidence. The function performs no external I/O.

The dictionary's nine `analog_history` tags are authoritative:

| Unit | Tags |
|---|---|
| A | Infeed.MotorCurrent, Sealer.MotorCurrent, Outfeed.MotorCurrent |
| m/min | Infeed.ActualSpeed, Sealer.ActualSpeed, Outfeed.ActualSpeed |
| ℃ | Sealer.TemperaturePV, Sealer.TemperatureSP |
| bar | Sealer.AirPressure |

All are Double. Original values, quality/status bits, nullable source/server
clocks, acquisition time and sample identity remain in slot evidence. API storage
time and batch identity remain the responsibility of the future durable adapter.
An identical observation repeated with the same sample ID contributes once.
Changed sample identity/content or different samples claiming one capture/tag
position raise `AggregationConflict`. Tags in one capture must agree on acquisition
time. No readings are coerced into another dictionary type.

## Versioned one-second slots

UTC acquisition time, not arrival time or SourceTimestamp, locates a sample.
Each UTC floor-second is one expected slot. The selected observation is the
highest capture sequence in that second. A later Bad/Uncertain/null observation
therefore invalidates the slot even if an earlier observation was Good. All
superseded raw observations remain available in that slot's `raw_observations`.
This prevents jitter or repeated reads from weighting one second more heavily.
It does not relabel raw sample count as valid count.

For every returned tag/minute:

- `raw_count` counts unique in-range raw observations before slot selection.
- `observed_slot_count` counts slots having an observation.
- `valid_slot_count` counts selected Good, nonnull, time-GOOD observations.
- `expected_slot_count` counts one-second intervals intersecting the query.
- `missing_slot_count` and `invalid_slot_count` distinguish absent observations
  from observed slots excluded from statistics.

Thus `valid <= observed <= expected`; raw count can exceed expected. A complete
minute expects sixty slots. Samples are filtered by the exact `[start,end)` query,
then assigned to slots. A partial query such as `[00:00:00.2,00:00:01.2)` expects
two intersecting slots, although its elapsed length is one second. It excludes
samples before `.2` and at/after `1.2`. `partial`, `covered_start`, and `covered_end`
make this policy explicit; partial results must not overwrite full-minute durable
aggregates in a future adapter.

Min/max/average use equally weighted valid selected slots. No valid slots means
null statistics, including for an entirely absent minute. `last_value` belongs
to the latest observed selected slot and is null when that observation is invalid.
`last_observed_at` gives its actual acquisition time. `last_good_value` and
`last_good_at` are separate evidence. Neither field is carried into another slot
or minute; trailing missing slots remain explicit and must break a plotted line.
Source-event sequence gaps do not identify analog missing intervals.

## Time quality and query boundaries

Caller-proven time quality is GOOD, UNKNOWN, or BAD; the default is UNKNOWN.
UNKNOWN/BAD observations are preserved and excluded from statistics. Capture-order
acquisition-time reversal latches CLOCK_REVERSAL invalidity from that capture onward
within the supplied epoch, including context supplied before the query start.
Unordered input is sorted by capture sequence. No fixed SourceTimestamp is treated
as proof of a faulty clock: unchanged OPC source time is permitted by the contract.
The adapter must establish backward-clock and greater-than-two-second-skew evidence;
this function cannot discover clock history absent from its inputs.

A result is provisional when it has missing slots, explicit unknown/bad time
(including superseded observations), or reversal evidence. OPC invalidity remains
explicit in the slot evidence and counts even when time quality is known. A minute
with all sixty observed, time-GOOD but OPC-Bad slots can be complete and
non-provisional while having zero valid slots and null statistics. Non-provisional
is not a claim of normal measurement quality; consumers must inspect invalid-slot
counts and evidence. This
unit does not certify immutable completeness or perform late-arrival finalization.

Queries require timezone-aware positive half-open boundaries, at most 24 hours,
and one to four distinct supported tags. Boundaries are normalized to UTC; minute
buckets remain UTC-aligned. `seoul_day_bounds(date)` converts an Asia/Seoul calendar
day to UTC: 2026-10-21 is `[2026-10-20T15:00Z,2026-10-21T15:00Z)`. There is no
server-local timezone inference. This bounded pure calculation does not establish
the later four-tag/24-hour database query p95 performance acceptance criterion.

## Verification

```sh
.venv/bin/pytest -q tests/unit/test_minute_aggregation.py
.venv/bin/ruff check apps/api/minute_aggregation.py tests/unit/test_minute_aggregation.py
```

Focused tests cover complete and partial minutes, duplicate-slot jitter,
Bad/Uncertain/null, absent minutes, time UNKNOWN/BAD, reversal and prior context,
replay/collision, mixed provenance, tag boundaries, exact half-open filtering and
Seoul midnight conversion. Durable backfill/idempotency, database reads, late
aggregate revisions, retention and real-network trend behavior remain separate
DEV15 units.
