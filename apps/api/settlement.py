"""Execution settlement on the caller's transaction; no transport or cursor writes.

The caller must supply gateway-authenticated scope. Results become durable only
when that caller commits. A final PLC summary never resolves unknown commands.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import sqlalchemy as sa
from pydantic import TypeAdapter

from apps.api.database import SCHEMA_LOCK_ID
from apps.api.ingestion import (
    event_inbox,
    execution_totals,
    production_executions,
    source_streams,
    stream_gaps,
    stream_progress,
)
from apps.api.models import Line
from contracts.models import Identifier, RunSummary, UTCTimestamp

_ID = TypeAdapter(Identifier)
_TIME = TypeAdapter(UTCTimestamp)


class SettlementScopeError(ValueError):
    """Authenticated gateway does not own the original execution."""


@dataclass(frozen=True)
class SettlementResult:
    execution_id: str
    status: Literal["PENDING", "SETTLED", "MISMATCH", "CONFLICT"]
    reason_code: str
    summary: RunSummary | None
    checked_at: datetime | None


def _scope(connection, execution_id, gateway, source, stream):
    connection.execute(
        sa.text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": SCHEMA_LOCK_ID}
    )
    execution_id, gateway, source, stream = (
        _ID.validate_python(value) for value in (execution_id, gateway, source, stream)
    )
    predicate = sa.and_(source_streams.c.source_id == source, source_streams.c.stream_id == stream)
    binding = connection.execute(sa.select(source_streams).where(predicate)).mappings().first()
    if binding is None:
        raise SettlementScopeError("Authenticated source/stream is not registered")
    connection.execute(sa.select(Line.id).where(Line.id == binding["line_id"]).with_for_update())
    binding = (
        connection.execute(sa.select(source_streams).where(predicate).with_for_update())
        .mappings()
        .one()
    )
    if binding["gateway_id"] != gateway:
        raise SettlementScopeError("Authenticated gateway does not own source/stream")
    execution = (
        connection.execute(
            sa.select(production_executions)
            .where(
                production_executions.c.execution_id == execution_id,
                production_executions.c.source_id == source,
                production_executions.c.stream_id == stream,
            )
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if execution is None:
        raise SettlementScopeError("Original execution is not registered to authenticated scope")
    return binding, execution


def _canonical(summary):
    # Poll time can grow while an ended execution's fixed boundary stays equal.
    body = summary.model_dump(mode="json")
    body.pop("as_of")
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _row(connection, execution_id):
    return (
        connection.execute(
            sa.text("SELECT * FROM run_reconciliations WHERE execution_id=:id"),
            {"id": execution_id},
        )
        .mappings()
        .first()
    )


def _result(execution_id, row):
    if row is None:
        return SettlementResult(execution_id, "PENDING", "FINAL_SUMMARY_REQUIRED", None, None)
    return SettlementResult(
        execution_id,
        row["status"],
        row["reason_code"],
        RunSummary.model_validate(row["final_summary"]),
        row["checked_at"],
    )


def record_final_summary(
    connection: sa.Connection,
    summary: RunSummary,
    *,
    authenticated_gateway_id: str,
    authenticated_source_id: str,
    authenticated_stream_id: str,
    now: datetime,
) -> SettlementResult:
    """Retain actual final source evidence, then check durable ingestion progress."""
    summary = RunSummary.model_validate_json(summary.model_dump_json())
    now = _TIME.validate_python(now)
    if not summary.final:
        raise ValueError("Settlement requires an actual final RunSummary")
    if (summary.source_id, summary.stream_id) != (authenticated_source_id, authenticated_stream_id):
        raise SettlementScopeError("Summary differs from authenticated source/stream")
    binding, execution = _scope(
        connection,
        summary.execution_id,
        authenticated_gateway_id,
        authenticated_source_id,
        authenticated_stream_id,
    )
    canonical = _canonical(summary)
    existing = _row(connection, summary.execution_id)
    if existing is None:
        connection.execute(
            sa.text(
                "INSERT INTO run_reconciliations(execution_id,final_summary,canonical_final,"
                "status,reason_code,received_at,checked_at) VALUES "
                "(:id,CAST(:summary AS jsonb),:canonical,'PENDING','EVENTS_PENDING',:now,:now)"
            ),
            {
                "id": summary.execution_id,
                "summary": summary.model_dump_json(),
                "canonical": canonical,
                "now": now,
            },
        )
    elif existing["canonical_final"] != canonical:
        connection.execute(
            sa.text(
                "INSERT INTO run_summary_conflicts"
                "(execution_id,summary,canonical_final,received_at) "
                "VALUES (:id,CAST(:summary AS jsonb),:canonical,:now) "
                "ON CONFLICT(execution_id,canonical_final) DO NOTHING"
            ),
            {
                "id": summary.execution_id,
                "summary": summary.model_dump_json(),
                "canonical": canonical,
                "now": now,
            },
        )
        _update(connection, summary.execution_id, "CONFLICT", "FINAL_SUMMARY_CONFLICT", now)
    return _reconcile(connection, binding, execution, now)


def reconcile_execution(
    connection: sa.Connection,
    execution_id: str,
    *,
    authenticated_gateway_id: str,
    authenticated_source_id: str,
    authenticated_stream_id: str,
    now: datetime,
) -> SettlementResult:
    """Recheck after committed backfill without replacing final source evidence."""
    binding, execution = _scope(
        connection,
        execution_id,
        authenticated_gateway_id,
        authenticated_source_id,
        authenticated_stream_id,
    )
    return _reconcile(connection, binding, execution, _TIME.validate_python(now))


def get_settlement(
    connection: sa.Connection,
    execution_id: str,
    *,
    authenticated_gateway_id: str,
    authenticated_source_id: str,
    authenticated_stream_id: str,
) -> SettlementResult:
    """Read scoped last check; next-job callers should reconcile before gating."""
    _scope(
        connection,
        execution_id,
        authenticated_gateway_id,
        authenticated_source_id,
        authenticated_stream_id,
    )
    return _result(execution_id, _row(connection, execution_id))


def _update(connection, execution_id, status, reason, now):
    connection.execute(
        sa.text(
            "UPDATE run_reconciliations SET status=:status,reason_code=:reason,checked_at=:now "
            "WHERE execution_id=:id"
        ),
        {"id": execution_id, "status": status, "reason": reason, "now": now},
    )


def _reconcile(connection, binding, execution, now):
    execution_id = execution["execution_id"]
    row = _row(connection, execution_id)
    if row is None or row["status"] == "CONFLICT":
        return _result(execution_id, row)
    summary = RunSummary.model_validate(row["final_summary"])
    status, reason = "PENDING", "EVENTS_PENDING"
    scope = sa.and_(
        event_inbox.c.source_id == summary.source_id, event_inbox.c.stream_id == summary.stream_id
    )
    gap_scope = sa.and_(
        stream_gaps.c.source_id == summary.source_id, stream_gaps.c.stream_id == summary.stream_id
    )
    progress = connection.execute(
        sa.select(stream_progress.c.verified_through_seq).where(
            stream_progress.c.source_id == summary.source_id,
            stream_progress.c.stream_id == summary.stream_id,
        )
    ).scalar_one()
    if (
        execution["recipe_version"] != summary.recipe_version
        or execution["parameters"] != summary.parameters.model_dump(mode="json")
        or execution["counter_epoch"] != summary.counter_epoch
    ):
        status, reason = "MISMATCH", "EXECUTION_CONFIGURATION_MISMATCH"
    elif binding["conflicted"]:
        status, reason = "CONFLICT", "SOURCE_IDENTITY_CONFLICT"
    elif binding["reconciliation_required"]:
        reason = "SOURCE_RECONCILIATION_REQUIRED"
    elif connection.execute(
        sa.select(stream_gaps.c.from_seq)
        .where(
            gap_scope,
            stream_gaps.c.from_seq <= summary.last_event_seq,
        )
        .limit(1)
    ).first():
        reason = "STREAM_GAP"
    elif connection.execute(
        sa.select(event_inbox.c.event_id)
        .where(
            scope,
            event_inbox.c.seq <= summary.last_event_seq,
            event_inbox.c.applied_at.is_(None),
        )
        .limit(1)
    ).first():
        reason = "QUARANTINED_EVENTS"
    elif progress < summary.last_event_seq:
        reason = "EVENTS_PENDING"
    elif connection.execute(
        sa.text(
            "SELECT command_id FROM command_requests WHERE execution_id=:id "
            "AND (status IN "
            "('REQUESTED','DELIVERED','ACCEPTED','OUTCOME_UNKNOWN') OR collision_latched) LIMIT 1"
        ),
        {"id": execution_id},
    ).first():
        reason = "EXECUTION_COMMAND_UNRESOLVED"
    else:
        totals = (
            connection.execute(
                sa.select(execution_totals).where(
                    execution_totals.c.execution_id == execution_id,
                )
            )
            .mappings()
            .one()
        )
        after_boundary = connection.execute(
            sa.select(event_inbox.c.event_id)
            .where(
                scope,
                event_inbox.c.execution_id == execution_id,
                event_inbox.c.seq > summary.last_event_seq,
            )
            .limit(1)
        ).first()
        if after_boundary:
            status, reason = "MISMATCH", "EVENT_AFTER_FINAL_BOUNDARY"
        elif any(
            totals[key] != getattr(summary, key)
            for key in ("good_quantity", "reject_quantity", "scrap_quantity")
        ):
            status, reason = "MISMATCH", "QUANTITY_MISMATCH"
        else:
            status, reason = "SETTLED", "MATCHED"
    _update(connection, execution_id, status, reason, now)
    return _result(execution_id, _row(connection, execution_id))
