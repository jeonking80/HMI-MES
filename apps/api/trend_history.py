"""Transactional analog history and the existing versioned aggregation projection."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, timedelta

import sqlalchemy as sa

from apps.api.alarm_consumer import assess_capture_clock
from apps.api.minute_aggregation import (
    ANALOG_UNITS,
    MINUTE,
    POLICY_VERSION,
    RawObservation,
    aggregate_minutes,
)

TREND_REVISIONS = frozenset({"0015_analog_history", "0016_downtime_reasons", "0017_guest_viewer"})


@dataclass(frozen=True)
class TrendRetention:
    raw_days: float = 7
    minute_days: float = 90
    delete_limit: int = 1000

    def __post_init__(self):
        if not 0 < self.raw_days <= 7 or not 0 < self.minute_days <= 90:
            raise ValueError("retention must be positive and within baseline retention")
        if type(self.delete_limit) is not int or not 1 <= self.delete_limit <= 10000:
            raise ValueError("cleanup requires a bounded positive delete limit")


def z(at):
    return at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def floor_minute(at):
    return at.replace(second=0, microsecond=0)


def quality(slot):
    if slot.valid:
        return "GOOD"
    if slot.selected is None:
        return "MISSING"
    if slot.invalid_reason in {"CLOCK_REVERSAL", "TIME_UNKNOWN", "TIME_BAD"}:
        return "TIME_UNKNOWN"
    return "UNCERTAIN" if slot.selected.sample.status_code >> 30 == 1 else "BAD"


def points_for(observations, tag, start, end, resolution):
    """Never combine incompatible capture generations or clock epochs as good data."""
    groups = defaultdict(list)
    for obs in observations:
        groups[obs.scope.model_dump_json()].append(obs)
    if len(groups) > 1:
        step = timedelta(seconds=1) if resolution == "second" else MINUTE
        cursor = start
        result = []
        while cursor < end:
            result.append(
                dict(
                    t=z(cursor),
                    value=None,
                    min=None,
                    max=None,
                    quality="TIME_UNKNOWN",
                    valid_count=0,
                    expected_count=1 if resolution == "second" else 60,
                )
            )
            cursor += step
        return result
    if not groups:
        return []
    values = next(iter(groups.values()))
    aggregates = aggregate_minutes(
        tuple(values), scope=values[0].scope, tag_ids=(tag,), start=start, end=end
    )
    result = []
    for aggregate in aggregates:
        if resolution == "second":
            for slot in aggregate.slots:
                value = float(slot.selected.sample.reading.value) if slot.valid else None
                result.append(
                    dict(
                        t=z(slot.start),
                        value=value,
                        min=value,
                        max=value,
                        quality=quality(slot),
                        valid_count=int(slot.valid),
                        expected_count=1,
                    )
                )
        else:
            qualities = {quality(slot) for slot in aggregate.slots}
            count = aggregate.valid_slot_count
            q = (
                ("GOOD" if count == 60 else "PARTIAL")
                if count
                else (
                    next(
                        (q for q in ("TIME_UNKNOWN", "BAD", "UNCERTAIN") if q in qualities),
                        "MISSING",
                    )
                )
            )
            result.append(
                dict(
                    t=z(aggregate.bucket_start),
                    value=aggregate.average,
                    min=aggregate.minimum,
                    max=aggregate.maximum,
                    quality=q,
                    valid_count=count,
                    expected_count=60,
                )
            )
    return result


def raw_observations(connection, scope, tag, start, end, *, buckets=None):
    # Indexed range probes load only absent/pending/boundary buckets, rather than
    # deserializing seven days of raw evidence when minute projections exist.
    if buckets == []:
        return ()
    source = "analog_history_raw r"
    if buckets is not None:
        source = """unnest(CAST(:buckets AS timestamptz[])) b(minute)
            JOIN analog_history_raw r ON r.acquired_at>=b.minute
            AND r.acquired_at<b.minute+interval '1 minute'"""
    rows = connection.execute(
        sa.text(
            "SELECT observation FROM "
            + source
            + """
        WHERE line_id=:line_id AND source_id=:source_id AND stream_id=:stream_id
        AND tag_id=:tag AND acquired_at>=:start AND acquired_at<:end
        ORDER BY capture_seq"""
        ),
        dict(scope, tag=tag, start=start, end=end, buckets=buckets),
    )
    return tuple(RawObservation.model_validate(row[0]) for row in rows)


def capture_clock_valid(connection, body, epoch):
    """Remember clock reversal across minute/retention boundaries under the source lock."""
    binding = dict(
        source_id=body.source_id,
        stream_id=body.stream_id,
        gateway=body.gateway_id,
        generation=body.capture_generation,
        epoch=epoch,
    )
    where = """source_id=:source_id AND stream_id=:stream_id AND gateway_id=:gateway
        AND capture_generation=:generation AND clock_epoch=:epoch"""
    state = (
        connection.execute(
            sa.text("SELECT * FROM analog_history_clock_state WHERE " + where), binding
        )
        .mappings()
        .first()
    )
    reversal = int(state["reversal_seq"]) if state and state["reversal_seq"] is not None else None
    if state is None and body.capture_time is not None:
        # A first schema-15 capture or supported downgrade/upgrade can have
        # immutable clock history without a projection registry. Rebuild once
        # in SQL: nearest neighbors alone cannot recover an earlier reversal.
        cutoff = connection.execute(
            sa.text("""WITH captures AS (
            SELECT capture_seq,(body->>'captured_at')::timestamptz AS at,
                (body->'capture_time'->>'monotonic_seconds')::double precision AS mono
            FROM ingest_batches WHERE source_id=:source_id AND stream_id=:stream_id
                AND gateway_id=:gateway AND capture_generation=:generation
                AND body->'capture_time'->>'clock_epoch'=:epoch
        ), ordered AS (
            SELECT capture_seq,at,mono,lag(at) OVER w AS previous_at,
                lag(mono) OVER w AS previous_mono
            FROM captures WINDOW w AS (ORDER BY capture_seq)
        ) SELECT min(capture_seq) FROM ordered
            WHERE at<previous_at OR mono<=previous_mono"""),
            binding,
        ).scalar_one()
        reversal = int(cutoff) if cutoff is not None else None
    # The immutable intake includes digital-only and expired analog captures.
    # Those captures still constrain the clock, even without retained trend rows.
    # The gateway/generation/sequence unique index supports both neighbor probes.
    neighbors = connection.execute(
        sa.text("""(SELECT capture_seq,(body->>'captured_at')::timestamptz,
        (body->'capture_time'->>'monotonic_seconds')::double precision FROM
        ingest_batches WHERE source_id=:source_id AND stream_id=:stream_id
        AND gateway_id=:gateway AND capture_generation=:generation
        AND body->'capture_time'->>'clock_epoch'=:epoch AND capture_seq<:seq
        ORDER BY capture_seq DESC LIMIT 1) UNION ALL
        (SELECT capture_seq,(body->>'captured_at')::timestamptz,
        (body->'capture_time'->>'monotonic_seconds')::double precision FROM ingest_batches
        WHERE source_id=:source_id AND stream_id=:stream_id AND gateway_id=:gateway
        AND capture_generation=:generation AND body->'capture_time'->>'clock_epoch'=:epoch
        AND capture_seq>:seq ORDER BY capture_seq LIMIT 1)"""),
        dict(binding, seq=body.capture_seq),
    ).all()
    monotonic = body.capture_time.monotonic_seconds if body.capture_time else None
    for seq, at, neighbor_monotonic in neighbors:
        candidate = None
        reversed_monotonic = (
            monotonic is not None
            and neighbor_monotonic is not None
            and (
                (seq < body.capture_seq and neighbor_monotonic >= monotonic)
                or (seq > body.capture_seq and neighbor_monotonic <= monotonic)
            )
        )
        if seq < body.capture_seq and (at > body.captured_at or reversed_monotonic):
            candidate = body.capture_seq
        elif seq > body.capture_seq and (at < body.captured_at or reversed_monotonic):
            candidate = int(seq)
        if candidate is not None:
            reversal = min(reversal, candidate) if reversal is not None else candidate
    if reversal is not None and (not state or state["reversal_seq"] != reversal):
        # Invalidate retained evidence before any cached minute can be served as good.
        connection.execute(
            sa.text("""UPDATE analog_history_raw SET
            observation=jsonb_set(observation,'{time_quality}','"UNKNOWN"'),aggregated=false
            WHERE source_id=:source_id AND stream_id=:stream_id AND gateway_id=:gateway
            AND capture_generation=:generation
            AND observation->'scope'->>'clock_epoch'=:epoch AND capture_seq>=:reversal"""),
            dict(binding, reversal=reversal),
        )
    connection.execute(
        sa.text("""INSERT INTO analog_history_clock_state
        (source_id,stream_id,gateway_id,capture_generation,clock_epoch,last_seq,last_at,reversal_seq)
        VALUES (:source_id,:stream_id,:gateway,:generation,:epoch,:seq,:at,:reversal)
        ON CONFLICT(source_id,stream_id,gateway_id,capture_generation,clock_epoch)
        DO UPDATE SET last_seq=GREATEST(analog_history_clock_state.last_seq,EXCLUDED.last_seq),
        last_at=CASE WHEN EXCLUDED.last_seq>analog_history_clock_state.last_seq
            THEN EXCLUDED.last_at ELSE analog_history_clock_state.last_at END,
        reversal_seq=EXCLUDED.reversal_seq"""),
        dict(binding, seq=body.capture_seq, at=body.captured_at, reversal=reversal),
    )
    return reversal is None or body.capture_seq < reversal


def capture_trends(connection, body, *, source, now, clock_policy=None, retention=None):
    """Called only for a newly accepted batch under the source transaction lock."""
    revision = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    if revision not in TREND_REVISIONS:
        return
    retention = retention or TrendRetention()
    cutoff = floor_minute(now - timedelta(days=retention.raw_days))
    scope = dict(line_id=source["line_id"], source_id=body.source_id, stream_id=body.stream_id)
    assessment, _, _ = assess_capture_clock(body, clock_policy, now)
    epoch = body.capture_time.clock_epoch if body.capture_time else "unproved-clock"
    has_retained_analog = any(
        sample.tag_id in ANALOG_UNITS and sample.acquired_at >= cutoff for sample in body.samples
    )
    clock_valid = (
        capture_clock_valid(connection, body, epoch)
        if body.capture_time is not None or has_retained_analog
        else False
    )
    for sample in body.samples:
        if sample.tag_id not in ANALOG_UNITS or sample.acquired_at < cutoff:
            continue
        observation = RawObservation.model_validate(
            dict(
                scope=dict(
                    source_id=body.source_id,
                    stream_id=body.stream_id,
                    gateway_id=body.gateway_id,
                    capture_generation=body.capture_generation,
                    clock_epoch=epoch,
                ),
                capture_seq=body.capture_seq,
                sample=sample,
                time_quality="GOOD"
                if assessment and clock_valid and body.captured_at <= now + timedelta(seconds=2)
                else "UNKNOWN",
            )
        )
        connection.execute(
            sa.text("""INSERT INTO analog_history_raw
            (line_id,source_id,stream_id,gateway_id,capture_generation,capture_seq,
             tag_id,sample_id,acquired_at,stored_at,batch_id,observation,clock_assessment,aggregated)
            VALUES (:line_id,:source_id,:stream_id,:gateway,:generation,:seq,
             :tag,:sample,:at,:stored,:batch,CAST(:obs AS jsonb),
             CAST(:assessment AS jsonb),false)"""),
            dict(
                scope,
                gateway=body.gateway_id,
                generation=body.capture_generation,
                seq=body.capture_seq,
                tag=sample.tag_id,
                sample=sample.sample_id,
                at=sample.acquired_at,
                stored=now,
                batch=body.batch_id,
                obs=observation.model_dump_json(),
                assessment=assessment.model_dump_json() if assessment else "null",
            ),
        )
    # One unfinished bucket per tag normally; the bound also limits delayed backlogs.
    pending = connection.execute(
        sa.text("""SELECT DISTINCT tag_id,
        date_trunc('minute',acquired_at) AS minute FROM analog_history_raw
        WHERE line_id=:line_id AND source_id=:source_id AND stream_id=:stream_id
        AND NOT aggregated AND acquired_at<:complete
        ORDER BY minute,tag_id LIMIT 32"""),
        dict(scope, complete=floor_minute(now)),
    ).all()
    for tag, minute in pending:
        observations = raw_observations(connection, scope, tag, minute, minute + MINUTE)
        point = points_for(observations, tag, minute, minute + MINUTE, "minute")[0]
        connection.execute(
            sa.text("""INSERT INTO analog_history_minutes
            (line_id,source_id,stream_id,tag_id,minute,policy_version,point)
            VALUES (:line_id,:source_id,:stream_id,:tag,:minute,:policy,CAST(:point AS jsonb))
            ON CONFLICT (line_id,source_id,stream_id,tag_id,minute,policy_version)
            DO UPDATE SET point=EXCLUDED.point"""),
            dict(scope, tag=tag, minute=minute, policy=POLICY_VERSION, point=json.dumps(point)),
        )
        connection.execute(
            sa.text("""UPDATE analog_history_raw SET aggregated=true
            WHERE line_id=:line_id AND source_id=:source_id AND stream_id=:stream_id
            AND tag_id=:tag AND acquired_at>=:minute AND acquired_at<:end"""),
            dict(scope, tag=tag, minute=minute, end=minute + MINUTE),
        )
    cleanup_trends(connection, now=now, retention=retention)


def cleanup_trends(connection, *, now, retention):
    counts = []
    for table, column, cutoff, predicate in (
        (
            "analog_history_raw",
            "LEAST(acquired_at,stored_at)",
            floor_minute(now - timedelta(days=retention.raw_days)),
            "AND (aggregated OR acquired_at>stored_at+interval '2 seconds')",
        ),
        ("analog_history_minutes", "minute", now - timedelta(days=retention.minute_days), ""),
    ):
        result = connection.execute(
            sa.text(f"""DELETE FROM {table} WHERE ctid IN
            (SELECT ctid FROM {table} WHERE {column}<:cutoff {predicate}
             ORDER BY {column} LIMIT :bound FOR UPDATE SKIP LOCKED)"""),
            dict(cutoff=cutoff, bound=retention.delete_limit),
        )
        counts.append(result.rowcount)
    return tuple(counts)


def read_trends(connection, *, scope, tags, start, end, resolution, now):
    result = []
    for tag in tags:
        step = timedelta(seconds=1) if resolution == "second" else MINUTE
        cursor = start.replace(microsecond=0) if resolution == "second" else floor_minute(start)
        points = []
        stored = {}
        if resolution == "minute":
            stored = {
                row[0]: row[1]
                for row in connection.execute(
                    sa.text("""SELECT minute,point
                FROM analog_history_minutes WHERE line_id=:line_id AND source_id=:source_id
                AND stream_id=:stream_id AND tag_id=:tag AND minute>=:start AND minute<:end
                AND policy_version=:policy"""),
                    dict(scope, tag=tag, start=cursor, end=end, policy=POLICY_VERSION),
                )
            }
        buckets = None
        if resolution == "minute":
            pending = set(
                connection.execute(
                    sa.text("""SELECT DISTINCT
                date_trunc('minute',acquired_at) FROM analog_history_raw
                WHERE line_id=:line_id AND source_id=:source_id AND stream_id=:stream_id
                AND tag_id=:tag AND acquired_at>=:start AND acquired_at<:end
                AND NOT aggregated"""),
                    dict(scope, tag=tag, start=start, end=end),
                ).scalars()
            )
            buckets = []
            bucket = cursor
            while bucket < end:
                if (
                    bucket not in stored
                    or bucket in pending
                    or bucket < start
                    or bucket + MINUTE > end
                ):
                    buckets.append(bucket)
                bucket += MINUTE
        observations = raw_observations(connection, scope, tag, start, end, buckets=buckets)
        by_bucket = defaultdict(list)
        for observation in observations:
            by_bucket[floor_minute(observation.sample.acquired_at)].append(observation)
        while cursor < end:
            if resolution == "second":
                minute = floor_minute(cursor)
                bucket_end = min(end, minute + MINUTE)
                computed = points_for(
                    by_bucket[minute], tag, max(start, minute), bucket_end, resolution
                )
                if computed:
                    points.extend(computed)
                else:
                    while cursor < bucket_end:
                        points.append(
                            dict(
                                t=z(cursor),
                                value=None,
                                min=None,
                                max=None,
                                quality="MISSING",
                                valid_count=0,
                                expected_count=1,
                            )
                        )
                        cursor += step
                cursor = bucket_end
                continue
            point = (
                stored.get(cursor)
                if cursor not in by_bucket and start <= cursor and cursor + MINUTE <= end
                else None
            )
            if point is None:
                computed = points_for(
                    by_bucket[cursor],
                    tag,
                    max(start, cursor),
                    min(end, cursor + MINUTE),
                    resolution,
                )
                point = (
                    computed[0]
                    if computed
                    else dict(
                        t=z(cursor),
                        value=None,
                        min=None,
                        max=None,
                        quality="MISSING",
                        valid_count=0,
                        expected_count=60,
                    )
                )
            points.append(point)
            cursor += step
        result.append(dict(tag_id=tag, unit=ANALOG_UNITS[tag], points=points))
    return dict(
        scope=scope,
        **{"from": z(start), "to": z(end)},
        resolution=resolution,
        policy_version=POLICY_VERSION,
        series=result,
        generated_at=z(now),
    )
