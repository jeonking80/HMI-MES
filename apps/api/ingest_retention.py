"""Compact expiry evidence and bounded post-commit raw ingestion maintenance."""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime

import sqlalchemy as sa

from apps.api.database import SCHEMA_LOCK_ID

RETENTION_REVISION = "0022_ingest_retention"
_LOG = logging.getLogger(__name__)


class IngestRetentionExpired(ValueError):
    """A permanent capture identity has outlived its original raw evidence."""

    reason_code = "RETENTION_EXPIRED"


def validate_retention_days(value: int) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("ingest retention must be an integer of at least one day")
    return value


def retention_days_from_env() -> int:
    value = os.environ.get("HMI_INGEST_RETENTION_DAYS", "7")
    if re.fullmatch(r"[0-9]+", value) is None:
        raise ValueError("HMI_INGEST_RETENTION_DAYS must be a positive integer")
    return validate_retention_days(int(value))


def retention_available(connection) -> bool:
    revisions = connection.execute(sa.text("SELECT version_num FROM alembic_version"))
    return revisions.scalars().all() == [RETENTION_REVISION]


def check_retention_expired(connection, body) -> None:
    """Check both immutable identities before ACK replay or collision recording."""
    if not retention_available(connection):
        return
    expired = connection.execute(
        sa.text("""SELECT 1 FROM public.ingest_receipts
        WHERE gateway_id=:gateway AND expired_at IS NOT NULL
          AND (batch_id=:batch OR
               (capture_generation=:generation AND capture_seq=:capture)) LIMIT 1"""),
        {
            "gateway": body.gateway_id,
            "batch": body.batch_id,
            "generation": body.capture_generation,
            "capture": body.capture_seq,
        },
    ).first()
    if expired is not None:
        raise IngestRetentionExpired("Original capture retention has expired")


def maybe_prune_ingest(engine, retention_days=7, max_batches=200, max_candidates=400) -> dict:
    """Run after intake commits; SQL owns the durable global scheduler and DB clock.

    A failed attempt rolls its scheduler state and deletions back together. A later
    successful intake may retry it. Failure diagnostics never include SQL or payloads.
    """
    validate_retention_days(retention_days)
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
            if not locked or not retention_available(connection):
                return {"status": "unavailable"}
            return connection.execute(
                sa.text("SELECT public.prune_ingest_batches(:days,:batches,:candidates)"),
                {"days": retention_days, "batches": max_batches, "candidates": max_candidates},
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
