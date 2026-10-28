"""Transactional analog history and the existing versioned aggregation projection."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from apps.api.alarm_consumer import assess_capture_clock
from apps.api.capture_clock import (
    capture_clock_valid,
    clock_evidence_available,
    fence_incomplete_reversal_minutes,
    record_capture_clock,
    schema_revision,
)
from apps.api.minute_aggregation import (
    ANALOG_UNITS,
    MINUTE,
    POLICY_VERSION,
    RawObservation,
    aggregate_minutes,
)
from apps.api.retention_provenance import RAW_EXPIRED, expiry_windows, provenance

TREND_REVISIONS = frozenset(
    {
        "0015_analog_history",
        "0016_downtime_reasons",
        "0017_guest_viewer",
        "0018_clock_evidence",
        "0019_registration_ids",
        "0020_line_queue",
        "0021_item_master",
        "0022_ingest_retention",
        "0023_ingest_compaction",
        "0024_ingest_retired_segments",
        "0025_mes_edit_audit",
        "0026_queue_reenroll",
        "0027_recipe_pressure",
        "0028_work_order_reuse",
    }
)


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
    modern = clock_evidence_available(connection)
    at = "COALESCE(r.canonical_utc,r.acquired_at)" if modern else "r.acquired_at"
    select = "observation,canonical_utc" if modern else "observation,NULL::timestamptz"
    source = "analog_history_raw r"
    if buckets is not None:
        source = f"""unnest(CAST(:buckets AS timestamptz[])) b(minute)
            JOIN analog_history_raw r ON {at}>=b.minute
            AND {at}<b.minute+interval '1 minute'"""
    rows = connection.execute(
        sa.text(
            "SELECT "
            + select
            + " FROM "
            + source
            + f"""
        WHERE line_id=:line_id AND source_id=:source_id AND stream_id=:stream_id
        AND tag_id=:tag AND {at}>=:start AND {at}<:end
        ORDER BY capture_seq"""
        ),
        dict(scope, tag=tag, start=start, end=end, buckets=buckets),
    )
    results = []
    for observation, canonical in rows:
        original = RawObservation.model_validate(observation)
        if canonical is not None:
            # The persisted sample remains original; only the aggregation input
            # receives the capture-specific analytical UTC projection.
            original = original.model_copy(
                update={"sample": original.sample.model_copy(update={"acquired_at": canonical})}
            )
        results.append(original)
    return tuple(results)


def capture_trends(
    connection, body, *, source, now, clock_policy=None, retention=None, clock_decision=None
):
    """Called only for a newly accepted batch under the source transaction lock."""
    revision = schema_revision(connection)
    if revision not in TREND_REVISIONS:
        return
    retention = retention or TrendRetention()
    cutoff = floor_minute(now - timedelta(days=retention.raw_days))
    scope = dict(line_id=source["line_id"], source_id=body.source_id, stream_id=body.stream_id)
    modern = clock_evidence_available(connection)
    epoch = body.capture_time.clock_epoch if body.capture_time else "unproved-clock"
    if modern:
        clock_decision = clock_decision or record_capture_clock(
            connection, body, source=source, now=now, policy=clock_policy
        )
        assessment = clock_decision.assessment
        clock_valid = clock_decision.status == "GOOD"
    else:
        assessment, _, _ = assess_capture_clock(body, clock_policy, now)
        has_retained_analog = any(
            sample.tag_id in ANALOG_UNITS and sample.acquired_at >= cutoff
            for sample in body.samples
        )
        clock_valid = (
            capture_clock_valid(connection, body, epoch)
            if body.capture_time is not None or has_retained_analog
            else False
        )
    time_known = (
        clock_valid
        if modern
        else (
            assessment is not None
            and clock_valid
            and body.captured_at <= now + timedelta(seconds=2)
        )
    )
    for sample in body.samples:
        canonical = (
            sample.acquired_at + (clock_decision.canonical_api_utc - body.captured_at)
            if modern and time_known
            else None
        )
        if sample.tag_id not in ANALOG_UNITS or (canonical or sample.acquired_at) < cutoff:
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
                time_quality="GOOD" if time_known else "UNKNOWN",
            )
        )
        extra_column = ",canonical_utc" if modern else ""
        extra_value = ",:canonical" if modern else ""
        connection.execute(
            sa.text(f"""INSERT INTO analog_history_raw
            (line_id,source_id,stream_id,gateway_id,capture_generation,capture_seq,
             tag_id,sample_id,acquired_at,stored_at,batch_id,observation,clock_assessment,
             aggregated{extra_column})
            VALUES (:line_id,:source_id,:stream_id,:gateway,:generation,:seq,
             :tag,:sample,:at,:stored,:batch,CAST(:obs AS jsonb),
             CAST(:assessment AS jsonb),false{extra_value})"""),
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
                assessment=json.dumps(
                    assessment.model_dump(mode="json")
                    if hasattr(assessment, "model_dump")
                    else assessment
                ),
                canonical=canonical,
            ),
        )
    fence_incomplete_reversal_minutes(connection, body, epoch)
    at = "COALESCE(canonical_utc,acquired_at)" if modern else "acquired_at"
    # One unfinished bucket per tag normally; the bound also limits delayed backlogs.
    pending = connection.execute(
        sa.text(f"""SELECT DISTINCT tag_id,
        date_trunc('minute',{at}) AS minute FROM analog_history_raw
        WHERE line_id=:line_id AND source_id=:source_id AND stream_id=:stream_id
        AND NOT aggregated AND {at}<:complete
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
            sa.text(f"""UPDATE analog_history_raw SET aggregated=true
            WHERE line_id=:line_id AND source_id=:source_id AND stream_id=:stream_id
            AND tag_id=:tag AND {at}>=:minute AND {at}<:end"""),
            dict(scope, tag=tag, minute=minute, end=minute + MINUTE),
        )
    cleanup_trends(connection, now=now, retention=retention)


def cleanup_trends(connection, *, now, retention):
    counts = []
    raw_time = (
        "COALESCE(canonical_utc,acquired_at)"
        if clock_evidence_available(connection)
        else "acquired_at"
    )
    for table, column, cutoff, predicate in (
        (
            "analog_history_raw",
            f"LEAST({raw_time},stored_at)",
            floor_minute(now - timedelta(days=retention.raw_days)),
            f"AND (aggregated OR {raw_time}>stored_at+interval '2 seconds')",
        ),
        ("analog_history_minutes", "minute", now - timedelta(days=retention.minute_days), ""),
    ):
        returning = (
            f""" RETURNING source_id,stream_id,gateway_id,capture_generation,
                observation->'scope'->>'clock_epoch' AS epoch,
                tag_id,date_trunc('minute',{raw_time}) AS minute"""
            if table == "analog_history_raw"
            else ""
        )
        result = connection.execute(
            sa.text(f"""DELETE FROM {table} WHERE ctid IN
            (SELECT ctid FROM {table} WHERE {column}<:cutoff {predicate}
             ORDER BY {column} LIMIT :bound FOR UPDATE SKIP LOCKED){returning}"""),
            dict(cutoff=cutoff, bound=retention.delete_limit),
        )
        if table == "analog_history_raw":
            removed = result.fetchall()
            counts.append(len(removed))
            touched = {}
            for row in removed:
                scope_key = (
                    row.source_id,
                    row.stream_id,
                    row.gateway_id,
                    row.capture_generation,
                    row.epoch,
                )
                representative, buckets = touched.setdefault(scope_key, (row, set()))
                buckets.add((row.tag_id, row.minute))
            for representative, buckets in touched.values():
                fence_incomplete_reversal_minutes(
                    connection, representative, representative.epoch, buckets=buckets
                )
        else:
            counts.append(result.rowcount)
    return tuple(counts)


def read_trends(connection, *, scope, tags, start, end, resolution, now):
    modern = clock_evidence_available(connection)
    at = "COALESCE(canonical_utc,acquired_at)" if modern else "acquired_at"
    result = []
    windows = expiry_windows(connection, scope=scope, start=start, end=end)
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
                    sa.text(f"""SELECT DISTINCT
                date_trunc('minute',{at}) FROM analog_history_raw
                WHERE line_id=:line_id AND source_id=:source_id AND stream_id=:stream_id
                AND tag_id=:tag AND {at}>=:start AND {at}<:end
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
        for index, point in enumerate(points):
            point_at = datetime.fromisoformat(point["t"].replace("Z", "+00:00"))
            affected = any(
                left < point_at + step
                and right > point_at
                and (entry.get("tag_ids") is None or tag in entry["tag_ids"])
                for left, right, entry in windows
            )
            if affected:
                point = dict(point, raw_evidence=dict(RAW_EXPIRED))
                if point["value"] is None:
                    point["quality"] = "RETENTION_EXPIRED"
                points[index] = point
        result.append(dict(tag_id=tag, unit=ANALOG_UNITS[tag], points=points))
    return dict(
        scope=scope,
        **{"from": z(start), "to": z(end)},
        resolution=resolution,
        policy_version=POLICY_VERSION,
        series=result,
        generated_at=z(now),
        **provenance([entry for _, _, entry in windows]),
    )
