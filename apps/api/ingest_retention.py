"""Compact expiry evidence and bounded post-commit raw ingestion maintenance."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import UTC, datetime
from weakref import WeakKeyDictionary

import sqlalchemy as sa

from apps.api.capture_clock import schema_revision
from apps.api.database import SCHEMA_LOCK_ID

RETENTION_REVISION = "0022_ingest_retention"
COMPACTION_REVISION = "0023_ingest_compaction"
SEGMENTS_REVISION = "0024_ingest_retired_segments"
COMPACTION_REVISIONS = frozenset({COMPACTION_REVISION, SEGMENTS_REVISION})
RETENTION_REVISIONS = COMPACTION_REVISIONS | {RETENTION_REVISION}
_LOG = logging.getLogger(__name__)
_MAINTENANCE_LOCK = threading.Lock()
_MAINTENANCE_STATE = WeakKeyDictionary()


class IngestRetentionExpired(ValueError):
    """A permanent capture identity has outlived its original raw evidence."""

    reason_code = "RETENTION_EXPIRED"


def validate_retention_hours(value: int) -> int:
    if type(value) is not int or value < 3:
        raise ValueError("ingest retention must be an integer of at least three hours")
    return value


def reject_legacy_retention_setting() -> None:
    if "HMI_INGEST_RETENTION_DAYS" in os.environ:
        raise ValueError("HMI_INGEST_RETENTION_DAYS is unsupported; use HMI_INGEST_RETENTION_HOURS")


def retention_hours_from_env() -> int:
    reject_legacy_retention_setting()
    value = os.environ.get("HMI_INGEST_RETENTION_HOURS", "168")
    if re.fullmatch(r"[0-9]+", value) is None:
        raise ValueError("HMI_INGEST_RETENTION_HOURS must be an integer of at least three hours")
    return validate_retention_hours(int(value))


def retention_available(connection) -> bool:
    return schema_revision(connection) in RETENTION_REVISIONS


def latest_capture_table(connection) -> str:
    """Trusted SQL relation retaining the full metadata of each scope's frontier."""
    revision = schema_revision(connection)
    if revision in COMPACTION_REVISIONS:
        # PostgreSQL restores timestamptz/numeric types from the receipt JSON.
        # Only the latest compacted capture in each range is needed for this
        # context; analytical clock neighbors use their separate epoch endpoints.
        return """(SELECT * FROM public.ingest_receipts
            UNION ALL
            SELECT receipt.* FROM public.ingest_retention_ranges r
            CROSS JOIN LATERAL jsonb_populate_record(
                NULL::public.ingest_receipts,r.latest_capture) AS receipt
            WHERE r.latest_capture IS NOT NULL) AS capture_frontier"""
    if revision == RETENTION_REVISION:
        return "ingest_receipts"
    return "ingest_batches"


def check_retention_expired(connection, body) -> None:
    """Check both immutable identities before ACK replay or collision recording."""
    revision = schema_revision(connection)
    if revision not in RETENTION_REVISIONS:
        return
    query = """SELECT 1 FROM public.ingest_receipts
        WHERE gateway_id=:gateway AND expired_at IS NOT NULL
          AND (batch_id=:batch OR
               (capture_generation=:generation AND capture_seq=:capture))"""
    if revision in COMPACTION_REVISIONS:
        # Capture identity is gateway-global across source/stream rebindings;
        # batch identity is gateway-global across capture generations as well.
        query += """ UNION ALL
            SELECT 1 FROM public.ingest_retention_ranges
            WHERE gateway_id=:gateway AND capture_generation=:generation
                AND :capture BETWEEN from_capture_seq AND through_capture_seq"""
        if revision == SEGMENTS_REVISION:
            query += """ UNION ALL
                SELECT 1 WHERE public.retired_ingest_batch_exists(:gateway,:batch)"""
        else:
            query += """ UNION ALL
            SELECT 1 FROM public.ingest_retired_batch_chunks
            WHERE gateway_id=:gateway AND batch_ids @> ARRAY[:batch]::text[]"""
    expired = connection.execute(
        sa.text(query + " LIMIT 1"),
        {
            "gateway": body.gateway_id,
            "batch": body.batch_id,
            "generation": body.capture_generation,
            "capture": body.capture_seq,
        },
    ).first()
    if expired is not None:
        raise IngestRetentionExpired("Original capture retention has expired")


def schedule_ingest_maintenance(engine, retention_hours=168) -> None:
    """Admit one post-response maintenance attempt per engine per minute.

    Run as an invocation-owned response background task: the ACK body is already
    sent, but ASGI completion still waits for this function. Admit only when the
    task actually starts so a failed response send cannot strand an in-flight
    reservation. The process cooldown reduces traffic; SQL still owns the global
    lock, durable interval, cutoff clock and transactional safety checks.
    """
    validate_retention_hours(retention_hours)
    with _MAINTENANCE_LOCK:
        now = time.monotonic()
        state = _MAINTENANCE_STATE.get(engine)
        if state is not None and (state[0] or now < state[1]):
            return
        _MAINTENANCE_STATE[engine] = (True, now + 60)

    try:
        maybe_prune_ingest(engine, retention_hours)
    finally:
        with _MAINTENANCE_LOCK:
            _MAINTENANCE_STATE[engine] = (False, time.monotonic() + 60)


def maybe_prune_ingest(engine, retention_hours=168, max_batches=200, max_candidates=400) -> dict:
    """Run after intake commits; SQL owns the durable global scheduler and DB clock.

    A failed attempt rolls its scheduler state and deletions back together. A later
    successful intake may retry it. Failure diagnostics never include SQL or payloads.
    """
    validate_retention_hours(retention_hours)
    for value, maximum in ((max_batches, 1000), (max_candidates, 2000)):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError("ingest maintenance bounds exceed the supported range")
    try:
        with engine.begin() as connection:
            # Bound execution and secondary lock waits after the ingestion commit.
            connection.execute(sa.text("SET LOCAL statement_timeout = '1500ms'"))
            connection.execute(sa.text("SET LOCAL lock_timeout = '100ms'"))
            # Skip a concurrent migration instead of waiting on its exclusive lock.
            locked = connection.execute(
                sa.text("SELECT pg_try_advisory_xact_lock_shared(:key)"),
                {"key": SCHEMA_LOCK_ID},
            ).scalar_one()
            if not locked:
                return {"status": "unavailable"}
            # Revision22's SQL takes days; revision23 and later accept hours.
            if schema_revision(connection) not in COMPACTION_REVISIONS:
                return {"status": "unavailable"}
            return connection.execute(
                sa.text("SELECT public.prune_ingest_batches_hours(:hours,:batches,:candidates)"),
                {"hours": retention_hours, "batches": max_batches, "candidates": max_candidates},
            ).scalar_one()
    except Exception as error:
        _LOG.warning("Ingest retention maintenance failed: %s", type(error).__name__)
        return {"status": "failed", "error_class": type(error).__name__}


def expired_coverage(
    connection, *, line_id, source_id, stream_id, from_time, to_time
) -> list[dict]:
    """Return conservative scoped envelopes, including intervals with unknown time."""
    if not retention_available(connection):
        return []
    rows = connection.execute(
        sa.text("""WITH coverage AS (
          SELECT r.*, (r.time_unknown OR EXISTS (
            SELECT 1 FROM public.analog_history_clock_state c
            WHERE c.gateway_id=r.gateway_id AND c.source_id=r.source_id
              AND c.stream_id=r.stream_id AND c.capture_generation=r.capture_generation
              AND c.clock_epoch=ANY(r.clock_epochs)
              AND c.reversal_seq<=r.through_capture_seq
          )) AS effective_time_unknown
          FROM public.ingest_retention_ranges r
        ) SELECT * FROM coverage
        WHERE (CAST(:line AS text) IS NULL OR line_id=:line)
          AND (CAST(:source AS text) IS NULL OR source_id=:source)
          AND (CAST(:stream AS text) IS NULL OR stream_id=:stream)
          AND (effective_time_unknown
            OR (analytical_from<=:until AND analytical_until>=:start)
            OR (original_from<=:until AND original_until>=:start)
            OR (stored_from<=:until AND stored_until>=:start))
        ORDER BY gateway_id,source_id,stream_id,capture_generation,from_capture_seq"""),
        {
            "line": line_id,
            "source": source_id,
            "stream": stream_id,
            "start": from_time,
            "until": to_time,
        },
    ).mappings()
    result = []
    for row in rows:
        value = dict(row)
        value.pop("clock_frontiers", None)
        value.pop("latest_capture", None)
        # Late reversal evidence invalidates the old analytical envelope without
        # mutating the permanent expiry record. Unknown scope covers this query.
        value["time_unknown"] = value.pop("effective_time_unknown")
        for key, item in value.items():
            if isinstance(item, datetime):
                value[key] = item.astimezone(UTC).isoformat().replace("+00:00", "Z")
        for key in (
            "capture_generation",
            "from_capture_seq",
            "through_capture_seq",
            "deleted_count",
        ):
            value[key] = str(value[key])
        value["reason_code"] = "RETENTION_EXPIRED"
        result.append(value)
    return result
