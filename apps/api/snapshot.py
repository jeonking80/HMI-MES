"""Read durable capture evidence inside a caller-owned, authorized transaction."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import uuid4

import sqlalchemy as sa
from pydantic import TypeAdapter

from apps.api.ingestion import ingest_batches, source_streams
from apps.api.models import TagDefinition
from contracts.models import ContractModel, Identifier, UInt64, UTCTimestamp
from contracts.tags.validate import load_contract
from contracts.transport import GatewayCommunication, LineView, ScreenSnapshot, TagSample

_CONTRACT = load_contract()
_TIME = TypeAdapter(UTCTimestamp)
_ID = TypeAdapter(Identifier)


class SampleEvidence(ContractModel):
    tag_id: Identifier
    batch_id: Identifier
    capture_generation: UInt64
    capture_seq: UInt64
    captured_at: UTCTimestamp
    stored_at: UTCTimestamp
    fresh: bool


class SnapshotHealth(ContractModel):
    state: Literal["NEVER_OBSERVED", "STALE", "DEGRADED", "CAPTURE_FRESH"]
    heartbeat: Literal["UNKNOWN", "GOOD", "DELAYED", "ALARM"] = "UNKNOWN"
    communication: GatewayCommunication | None = None
    communication_captured_at: UTCTimestamp | None = None
    communication_stored_at: UTCTimestamp | None = None
    source_conflicted: bool
    reconciliation_required: bool


class SnapshotRead(ContractModel):
    source_id: Identifier
    stream_id: Identifier
    screen: ScreenSnapshot
    missing_tag_ids: tuple[Identifier, ...]
    evidence: tuple[SampleEvidence, ...]
    health: SnapshotHealth


class SnapshotNotFound(LookupError):
    """The requested source/stream is not bound to the requested line."""


def read_snapshot(
    connection: sa.Connection,
    *,
    line_id: str,
    source_id: str,
    stream_id: str,
    now: datetime,
    prefer_complete_capture: bool = False,
) -> SnapshotRead:
    """Return latest observations, never open/commit a separate DB transaction.

    Capture order, not source timestamp or arrival order, determines each tag's
    value. Only the currently configured gateway/generation is eligible. A fresh
    capture does not prove a live or progressing PLC heartbeat; control remains
    restricted until a later service implements that independent evidence.
    """
    line_id, source_id, stream_id = (
        _ID.validate_python(value) for value in (line_id, source_id, stream_id)
    )
    now = _TIME.validate_python(now)
    source = (
        connection.execute(
            sa.select(source_streams).where(
                source_streams.c.line_id == line_id,
                source_streams.c.source_id == source_id,
                source_streams.c.stream_id == stream_id,
            )
        )
        .mappings()
        .first()
    )
    if source is None:
        raise SnapshotNotFound("Source/stream is not bound to this line")
    expected = set(
        connection.execute(
            sa.select(TagDefinition.logical_name).where(
                TagDefinition.line_id == line_id, TagDefinition.enabled.is_(True)
            )
        ).scalars()
    )

    def latest_sample_capture():
        return (
            connection.execute(
                sa.select(
                    ingest_batches.c.body,
                    ingest_batches.c.stored_at,
                    ingest_batches.c.batch_id,
                    ingest_batches.c.capture_generation,
                    ingest_batches.c.capture_seq,
                )
                .where(
                    ingest_batches.c.source_id == source_id,
                    ingest_batches.c.stream_id == stream_id,
                    ingest_batches.c.gateway_id == source["gateway_id"],
                    ingest_batches.c.capture_generation == source["capture_generation"],
                    sa.func.jsonb_array_length(ingest_batches.c.body["samples"]) > 0,
                )
                .order_by(ingest_batches.c.capture_seq.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )

    latest = latest_sample_capture() if prefer_complete_capture else None
    complete_rows = None
    if latest is not None:
        observations = latest["body"]["samples"]
        names = [item["tag_id"] for item in observations]
        # A complete newest capture dominates every older observation. Partial
        # captures must retain the historical per-tag lookup, including BAD tags.
        if expected.issubset(names) and len(names) == len(set(names)):
            complete_rows = [
                dict(
                    sample=item,
                    batch_id=latest["batch_id"],
                    capture_generation=latest["capture_generation"],
                    capture_seq=latest["capture_seq"],
                    captured_at=latest["body"]["captured_at"],
                    stored_at=latest["stored_at"],
                )
                for item in sorted(observations, key=lambda item: item["tag_id"])
            ]
    # Limit each tag's ordered capture probe BEFORE expanding its sample array.
    # This expands at most one batch per enabled tag, rather than every retained
    # batch. The containment probe keeps all history: even an arbitrarily old BAD
    # observation must survive unrelated partial captures. No quality predicate
    # may skip a newer BAD/null/uncertain sample in favor of older GOOD evidence.
    rows = (
        complete_rows
        if complete_rows is not None
        else connection.execute(
            sa.text("""
        SELECT sample, b.batch_id,
               b.capture_generation, b.capture_seq,
               b.body->>'captured_at' AS captured_at, b.stored_at
        FROM unnest(CAST(:expected_tags AS text[])) AS wanted(tag_id)
        CROSS JOIN LATERAL (
            SELECT body, batch_id, capture_generation, capture_seq, stored_at
            FROM ingest_batches
            WHERE source_id=:source_id AND stream_id=:stream_id
              AND gateway_id=:gateway_id AND capture_generation=:capture_generation
              AND body @> jsonb_build_object(
                  'samples', jsonb_build_array(jsonb_build_object('tag_id', wanted.tag_id))
              )
            ORDER BY capture_seq DESC
            LIMIT 1
        ) AS b
        CROSS JOIN LATERAL jsonb_array_elements(b.body->'samples') AS sample
        WHERE sample->>'tag_id'=wanted.tag_id
        ORDER BY wanted.tag_id
    """),
            {
                "expected_tags": sorted(expected),
                "source_id": source_id,
                "stream_id": stream_id,
                "gateway_id": source["gateway_id"],
                "capture_generation": source["capture_generation"],
            },
        ).mappings()
    )
    samples, evidence = [], []
    stale_after = _CONTRACT["quality"]["stale_after_ms"] / 1000
    for row in rows:
        sample = TagSample.model_validate(row["sample"])
        if sample.tag_id not in expected:
            continue
        captured = _TIME.validate_python(row["captured_at"])
        # Replayed old captures cannot become fresh merely by arriving now.
        # Future wall-clock evidence is unknown rather than clamped to age zero.
        fresh = all(
            0 <= (now - timestamp).total_seconds() < stale_after
            for timestamp in (captured, row["stored_at"], sample.acquired_at)
        )
        samples.append(sample)
        evidence.append(
            SampleEvidence(
                tag_id=sample.tag_id,
                batch_id=row["batch_id"],
                capture_generation=int(row["capture_generation"]),
                capture_seq=int(row["capture_seq"]),
                captured_at=captured,
                stored_at=row["stored_at"],
                fresh=fresh,
            )
        )
    # Use the newest sample capture, including legacy captures lacking health.
    # Never borrow older GOOD evidence to fill a newer capture's unknown health.
    if not prefer_complete_capture:
        latest = latest_sample_capture()
    communication = None
    communication_captured_at = None
    heartbeat = "UNKNOWN"
    if latest is not None and latest["body"].get("communication") is not None:
        communication = GatewayCommunication.model_validate(latest["body"]["communication"])
        communication_captured_at = _TIME.validate_python(latest["body"]["captured_at"])
        elapsed = (now - communication_captured_at).total_seconds()
        if elapsed >= 0 and now >= latest["stored_at"]:
            elapsed = max(elapsed, (now - latest["stored_at"]).total_seconds())
            ages = [
                communication.read_age_seconds,
                *communication.heartbeat_ages_seconds.model_dump().values(),
            ]
            oldest = max((age + elapsed for age in ages if age is not None), default=elapsed)
            if oldest >= 5:
                heartbeat = "ALARM"
            elif oldest >= 3:
                heartbeat = "DELAYED"
            elif communication.state == "GOOD":
                heartbeat = "GOOD"
    missing = tuple(sorted(expected - {sample.tag_id for sample in samples}))
    if not samples:
        health = "NEVER_OBSERVED"
    elif any(not item.fresh for item in evidence):
        health = "STALE"
    elif missing or any(sample.status_code >> 30 != 0 for sample in samples):
        health = "DEGRADED"
    else:
        health = "CAPTURE_FRESH"
    if samples and (source["conflicted"] or source["reconciliation_required"]):
        health = "DEGRADED"
    usable = {
        sample.tag_id: sample.reading.value
        for sample, item in zip(samples, evidence, strict=True)
        if item.fresh and sample.status_code >> 30 == 0
    }
    state = _CONTRACT["enums"]["EquipmentState"].get(str(usable.get("Line.State")), "UNKNOWN")
    if heartbeat in {"UNKNOWN", "ALARM"}:
        state = "UNKNOWN"
    execution = usable.get("Line.ActiveExecutionId") or None
    # Invalid external execution strings must not defeat the read: the raw
    # observation is retained, while the derived identifier remains unknown.
    try:
        execution = _ID.validate_python(execution) if execution is not None else None
    except ValueError:
        execution = None
    return SnapshotRead(
        source_id=source_id,
        stream_id=stream_id,
        screen=ScreenSnapshot(
            kind="SNAPSHOT",
            sync_id=str(uuid4()),
            revision=0,
            emitted_at=now,
            view=LineView(
                line_id=line_id,
                state=state,
                execution_id=execution,
                control_restricted=True,
                samples=tuple(samples),
            ),
        ),
        missing_tag_ids=missing,
        evidence=tuple(evidence),
        health=SnapshotHealth(
            state=health,
            heartbeat=heartbeat,
            communication=communication,
            communication_captured_at=communication_captured_at,
            communication_stored_at=latest["stored_at"] if communication is not None else None,
            source_conflicted=source["conflicted"],
            reconciliation_required=source["reconciliation_required"],
        ),
    )
