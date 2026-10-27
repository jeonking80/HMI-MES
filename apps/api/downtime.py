"""Immutable downtime annotations tied to exact, current report evidence."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, TypeAdapter
from sqlalchemy import text

from apps.api.auth import PERMISSIONS, AuthorizationError
from contracts.models import ContractModel, Identifier, UTCTimestamp

ReasonCode = Literal["MATERIAL_WAIT", "EQUIPMENT", "CHANGEOVER", "PLANNED", "UNCLASSIFIED"]


class DowntimeConflict(ValueError):
    pass


class DowntimeUnknown(ValueError):
    pass


class DowntimeIntent(ContractModel):
    request_id: UUID
    execution_id: Identifier
    line_id: Identifier
    source_id: Identifier
    stream_id: Identifier
    segment_from: UTCTimestamp
    reason_code: ReasonCode
    note: Annotated[str, Field(strict=True, max_length=200)] = ""


def _iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _time(value):
    return TypeAdapter(UTCTimestamp).validate_python(value)


def _receipt(row):
    return {
        "request_id": str(row["request_id"]),
        "execution_id": row["execution_id"],
        "line_id": row["line_id"],
        "source_id": row["source_id"],
        "stream_id": row["stream_id"],
        "segment_from": _iso(row["segment_from"]),
        "segment_to": _iso(row["segment_to"]),
        "code": row["reason_code"],
        "note": row["note"],
        "recorded_by": row["actor"],
        "recorded_at": _iso(row["recorded_at"]),
    }


def annotate_downtime(db, definition, *, principal, now):
    """Caller owns the authenticated transaction; no commit or external I/O."""
    if principal.must_change_password or not principal.roles & PERMISSIONS["downtime_annotate"]:
        raise AuthorizationError("permission denied")
    definition = DowntimeIntent.model_validate_json(definition.model_dump_json())
    intent = definition.model_dump(mode="json") | {"actor": principal.login}
    key = int.from_bytes(
        hashlib.sha256(str(definition.request_id).encode()).digest()[:8], "big", signed=True
    )
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    prior = (
        db.execute(
            text("SELECT * FROM downtime_reason_history WHERE request_id=:id"),
            {"id": definition.request_id},
        )
        .mappings()
        .first()
    )
    if prior:
        if prior["original_intent"] != intent:
            raise DowntimeConflict("request identity belongs to another actor or intent")
        if prior["receipt"] != _receipt(prior):
            raise DowntimeConflict("original receipt is incoherent")
        return prior["receipt"]
    # Same lock order as ingestion: line then source stream. Evidence cannot change
    # between checking the segment and committing its original boundaries.
    db.execute(text("SELECT id FROM lines WHERE id=:line FOR UPDATE"), {"line": definition.line_id})
    stream = db.execute(
        text(
            "SELECT line_id FROM source_streams "
            "WHERE source_id=:source AND stream_id=:stream FOR UPDATE"
        ),
        {"source": definition.source_id, "stream": definition.stream_id},
    ).scalar_one_or_none()
    if stream != definition.line_id:
        raise DowntimeConflict("scope does not match source stream")
    from apps.api.reports import ReportNotFound, read_execution_timeline

    try:
        timeline = read_execution_timeline(
            db,
            definition.execution_id,
            definition.line_id,
            definition.source_id,
            definition.stream_id,
            now=now,
        )
    except ReportNotFound:
        raise DowntimeConflict("execution does not match scope") from None
    segment = next(
        (s for s in timeline["segments"] if _time(s["from"]) == definition.segment_from), None
    )
    if segment is None:
        raise DowntimeConflict("segment start does not match current timeline")
    if segment["class"] == "UNKNOWN":
        raise DowntimeUnknown("unknown time cannot receive a stop reason")
    if segment["class"] != "STOPPED":
        raise DowntimeConflict("segment is not stopped")
    row = dict(
        intent,
        request_id=definition.request_id,
        segment_from=definition.segment_from,
        segment_to=_time(segment["to"]),
        reason_code=definition.reason_code,
        recorded_at=now,
    )
    receipt = _receipt(row)
    db.execute(
        text("""INSERT INTO downtime_reason_history
        (request_id,execution_id,line_id,source_id,stream_id,segment_from,segment_to,
         reason_code,note,actor,recorded_at,original_intent,receipt)
        VALUES(:request_id,:execution_id,:line_id,:source_id,:stream_id,:segment_from,:segment_to,
         :reason_code,:note,:actor,:recorded_at,
         CAST(:original_intent AS jsonb),CAST(:receipt AS jsonb))"""),
        row | {"original_intent": json.dumps(intent), "receipt": json.dumps(receipt)},
    )
    return receipt


def read_downtime_history(db, execution_id, segment_from):
    # execution_id is globally unique in production_executions. The public query
    # contract requires only execution_id + segment_from; rows retain full scope.
    rows = (
        db.execute(
            text("""SELECT * FROM downtime_reason_history
        WHERE execution_id=:execution AND segment_from=:start ORDER BY id DESC"""),
            {"execution": execution_id, "start": segment_from},
        )
        .mappings()
        .all()
    )
    return {
        "execution_id": execution_id,
        "segment_from": _iso(segment_from),
        "items": [_receipt(row) for row in rows],
    }


def annotate_report_segments(db, segments, scope, *, now=None):
    """Attach only annotations whose full original STOPPED boundaries still exist.

    Daily segments may be clipped at midnight, but must be contained within that
    verified original interval. No changed or UNKNOWN boundary receives a reason.
    """
    from apps.api.reports import ReportNotFound, read_execution_timeline

    now = now or datetime.now(UTC)
    cache = {}
    result = []
    for source in segments:
        segment = dict(source)
        segment["stop_reason"] = None
        execution = segment.get("execution_id")
        source_id = segment.get("source_id", scope.get("source_id"))
        stream_id = segment.get("stream_id", scope.get("stream_id"))
        if segment["class"] != "STOPPED" or not all((execution, source_id, stream_id)):
            result.append(segment)
            continue
        key = (execution, scope["line_id"], source_id, stream_id)
        if key not in cache:
            rows = (
                db.execute(
                    text("""SELECT * FROM downtime_reason_history
                WHERE execution_id=:execution AND line_id=:line AND source_id=:source
                AND stream_id=:stream ORDER BY id DESC"""),
                    dict(
                        execution=execution,
                        line=scope["line_id"],
                        source=source_id,
                        stream=stream_id,
                    ),
                )
                .mappings()
                .all()
            )
            # Only an execution with recorded reasons needs its verified original timeline.
            canonical = set()
            if rows:
                try:
                    timeline = read_execution_timeline(db, *key, now=now, annotate=None)
                    canonical = {
                        (s["from"], s["to"])
                        for s in timeline["segments"]
                        if s["class"] == "STOPPED"
                    }
                except ReportNotFound:
                    pass
            cache[key] = [
                r for r in rows if (_iso(r["segment_from"]), _iso(r["segment_to"])) in canonical
            ]
        row = next(
            (
                r
                for r in cache[key]
                if r["segment_from"] <= _time(segment["from"])
                and r["segment_to"] >= _time(segment["to"])
            ),
            None,
        )
        if row:
            segment["stop_reason"] = {
                k: v
                for k, v in _receipt(row).items()
                if k in {"code", "note", "recorded_by", "recorded_at"}
            }
        result.append(segment)
    return result
