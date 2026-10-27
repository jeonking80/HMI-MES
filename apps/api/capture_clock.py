"""Durable analytical clock proof; independent of alarm lifecycle decisions."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from pydantic import TypeAdapter

from contracts.models import Identifier, UInt64, UTCTimestamp
from contracts.transport import BatchBody
from contracts.transport import batch_digest as digest_body

CLOCK_EVIDENCE_REVISION = "0018_clock_evidence"
CLOCK_EVIDENCE_REVISIONS = frozenset(
    {
        CLOCK_EVIDENCE_REVISION,
        "0019_registration_ids",
        "0020_line_queue",
        "0021_item_master",
        "0022_ingest_retention",
        "0023_ingest_compaction",
        "0024_ingest_retired_segments",
        "0025_mes_edit_audit",
        "0026_queue_reenroll",
    }
)
NORMALIZATION_VERSION = "api-interval-midpoint-v1"
UNKNOWN_MINUTE = json.dumps(
    {"value": None, "min": None, "max": None, "quality": "TIME_UNKNOWN", "valid_count": 0}
)
_ID = TypeAdapter(Identifier)
_UINT = TypeAdapter(UInt64)
_TIME = TypeAdapter(UTCTimestamp)


def schema_revision(connection):
    """Cache only within this transaction; a pooled connection may see migrations."""
    previous = connection.info.get("analytical_clock_revision")
    transaction = connection.get_transaction()
    if previous is not None and transaction is not None and previous[0] is transaction:
        return previous[1]
    revision = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    connection.info["analytical_clock_revision"] = (connection.get_transaction(), revision)
    return revision


def clock_evidence_available(connection):
    return schema_revision(connection) in CLOCK_EVIDENCE_REVISIONS


def capture_frontier_table(connection, *, alias="capture_history", deduplicate=True):
    """Retained receipts and compact endpoints preserve sequence chronology.

    Compacted ranges are contiguous accepted sequences. A newly admitted capture
    can only fall outside them, so each epoch's two endpoints are sufficient for
    both nearest-neighbor probes. Internal reversals remain in the durable clock
    state. Nearest-neighbor probes may retain identical repeated endpoints to
    avoid sorting/deduplicating the full retained history on every capture.
    """
    revision = schema_revision(connection)
    if revision in {
        "0023_ingest_compaction",
        "0024_ingest_retired_segments",
        "0025_mes_edit_audit",
        "0026_queue_reenroll",
    }:
        union = "UNION" if deduplicate else "UNION ALL"
        return f"""(SELECT gateway_id,source_id,stream_id,capture_generation,capture_seq,
            captured_at,clock_epoch,monotonic_seconds FROM ingest_receipts
        {union} SELECT r.gateway_id,r.source_id,r.stream_id,r.capture_generation,
            (point->>'capture_seq')::numeric,(point->>'captured_at')::timestamptz,
            epoch.key,(point->>'monotonic_seconds')::double precision
            FROM ingest_retention_ranges r
            CROSS JOIN LATERAL jsonb_each(r.clock_frontiers) epoch
            CROSS JOIN LATERAL (VALUES(epoch.value->'first'),(epoch.value->'last')) p(point)
        {union} SELECT r.gateway_id,r.source_id,r.stream_id,r.capture_generation,
            (r.latest_capture->>'capture_seq')::numeric,
            (r.latest_capture->>'captured_at')::timestamptz,
            r.latest_capture->>'clock_epoch',
            (r.latest_capture->>'monotonic_seconds')::double precision
            FROM ingest_retention_ranges r WHERE r.latest_capture IS NOT NULL) AS {alias}"""
    return "ingest_receipts" if revision == "0022_ingest_retention" else "ingest_batches"


@dataclass(frozen=True)
class SameHostClockPolicy:
    """Explicit operator assertion for this registered gateway generation.

    The shared-kernel guarantee covers clock epochs created by gateway restarts
    during this validity interval. It never implies that PLC source clocks share
    the kernel, or bypasses monotonic/UTC reversal and future-capture checks.
    """

    gateway_id: str
    source_id: str
    stream_id: str
    capture_generation: int
    valid_from_api_utc: datetime
    valid_until_api_utc: datetime
    reference_id: str = "same-host-kernel-utc"

    def __post_init__(self):
        for key in ("gateway_id", "source_id", "stream_id", "reference_id"):
            object.__setattr__(self, key, _ID.validate_python(getattr(self, key)))
        object.__setattr__(
            self, "capture_generation", _UINT.validate_python(self.capture_generation)
        )
        for key in ("valid_from_api_utc", "valid_until_api_utc"):
            object.__setattr__(self, key, _TIME.validate_python(getattr(self, key)))
        if self.valid_until_api_utc <= self.valid_from_api_utc:
            raise ValueError("same-host clock assertion requires explicit positive validity")

    def for_capture(self, body):
        from apps.api.alarm_consumer import ConfiguredClockPolicy

        if body.capture_time is None or (
            self.gateway_id,
            self.source_id,
            self.stream_id,
            self.capture_generation,
        ) != (body.gateway_id, body.source_id, body.stream_id, body.capture_generation):
            return None
        return ConfiguredClockPolicy(
            policy_id="same-host-analytics",
            policy_version="v1",
            reference_id=self.reference_id,
            method="CONFIGURED_API_REFERENCE",
            guarantee_kind="SAME_HOST_SHARED_KERNEL_UTC",
            guarantee_basis="Operator explicitly asserts API and gateway share a host kernel UTC",
            gateway_id=self.gateway_id,
            source_id=self.source_id,
            stream_id=self.stream_id,
            capture_generation=self.capture_generation,
            clock_epoch=body.capture_time.clock_epoch,
            valid_from_api_utc=self.valid_from_api_utc,
            valid_until_api_utc=self.valid_until_api_utc,
            offset_lower_seconds=0.0,
            offset_upper_seconds=0.0,
        )


@dataclass(frozen=True)
class CaptureClockDecision:
    status: str
    reason_code: str
    canonical_api_utc: datetime | None
    original_capture_utc: datetime
    interval_lower_utc: datetime | None
    interval_upper_utc: datetime | None
    offset_lower_seconds: float | None
    offset_upper_seconds: float | None
    assessment: Any | None
    batch_sha256: str
    method: str = "NONE"
    normalization_version: str = NORMALIZATION_VERSION
    input_evidence_sha256: str | None = None


def effective_capture_clock(evidence, *, body, batch_sha256, reversal_seq=None):
    """Validate a stored proof binding and apply the current immutable epoch cutoff.

    A missing row alone returns None (legacy fallback). Invalid or explicitly
    UNKNOWN proof never falls back to a current policy or an alarm context.
    Both database mappings and JSON projections are accepted by this pure helper.
    """
    if evidence is None:
        return None
    decision, bound, capture_seq = validated_capture_clock(
        evidence, body=body, batch_sha256=batch_sha256
    )
    return apply_reversal_cutoff(decision, bound, capture_seq, reversal_seq)


def apply_reversal_cutoff(decision, bound, capture_seq, reversal_seq):
    """The epoch cutoff of effective_capture_clock, for an already validated decision."""
    if not bound or reversal_seq is None:
        return decision
    try:
        reversed_clock = capture_seq >= int(reversal_seq)
    except (TypeError, ValueError, OverflowError):
        return CaptureClockDecision(
            "UNKNOWN",
            "EVIDENCE_INVALID",
            None,
            decision.original_capture_utc,
            None,
            None,
            None,
            None,
            None,
            decision.batch_sha256,
        )
    if reversed_clock:
        return replace(
            decision, status="UNKNOWN", reason_code="CLOCK_REVERSAL", canonical_api_utc=None
        )
    return decision


def validated_capture_clock(evidence, *, body, batch_sha256):
    """Return (decision, bound, capture_seq) before any reversal cutoff.

    ``bound`` is False when the proof failed validation (the decision is then
    final and no cutoff applies), exactly as in effective_capture_clock.
    """
    body = BatchBody.model_validate(body)
    original = body.captured_at

    def invalid(reason):
        return (
            CaptureClockDecision(
                "UNKNOWN", reason, None, original, None, None, None, None, None, batch_sha256
            ),
            False,
            body.capture_seq,
        )

    try:
        binding = (
            evidence["gateway_id"],
            evidence["batch_id"],
            evidence["source_id"],
            evidence["stream_id"],
            int(evidence["capture_generation"]),
            int(evidence["capture_seq"]),
            evidence["clock_epoch"],
            evidence["batch_sha256"],
        )
        expected = (
            body.gateway_id,
            body.batch_id,
            body.source_id,
            body.stream_id,
            body.capture_generation,
            body.capture_seq,
            body.capture_time.clock_epoch if body.capture_time else None,
            batch_sha256,
        )
        if (
            binding != expected
            or digest_body(body) != batch_sha256
            or _TIME.validate_python(evidence["original_capture_utc"]) != original
        ):
            return invalid("EVIDENCE_BINDING_MISMATCH")
        lower = evidence["offset_lower_seconds"]
        upper = evidence["offset_upper_seconds"]
        left = (
            _TIME.validate_python(evidence["interval_lower_utc"])
            if evidence["interval_lower_utc"]
            else None
        )
        right = (
            _TIME.validate_python(evidence["interval_upper_utc"])
            if evidence["interval_upper_utc"]
            else None
        )
        canonical = (
            _TIME.validate_python(evidence["canonical_api_utc"])
            if evidence["canonical_api_utc"]
            else None
        )
        decision = CaptureClockDecision(
            evidence["status"],
            evidence["reason_code"],
            canonical,
            original,
            left,
            right,
            lower,
            upper,
            (evidence["proof"] or {}).get("assessment"),
            batch_sha256,
            evidence["method"],
            evidence["normalization_version"],
            evidence.get("input_evidence_sha256"),
        )
        if decision.status == "GOOD":
            if (
                canonical is None
                or lower is None
                or upper is None
                or left is None
                or right is None
                or not lower <= upper
                or not left <= canonical <= right
                or canonical != left + (right - left) / 2
                or decision.method not in {"CONFIGURED_API_REFERENCE", "MEASURED_ROUND_TRIP"}
                or decision.normalization_version != NORMALIZATION_VERSION
            ):
                return invalid("EVIDENCE_INVALID")
        elif decision.status != "UNKNOWN" or canonical is not None:
            return invalid("EVIDENCE_INVALID")
        return decision, True, body.capture_seq
    except (KeyError, TypeError, ValueError, OverflowError):
        return invalid("EVIDENCE_INVALID")


def read_capture_clock(connection, gateway_id, batch_id):
    if not clock_evidence_available(connection):
        return None
    row = (
        connection.execute(
            sa.text("""SELECT e.*,b.body AS original_body,
        b.content_sha256 AS original_digest,c.reversal_seq
        FROM capture_clock_evidence e JOIN ingest_batches b USING(gateway_id,batch_id)
        LEFT JOIN analog_history_clock_state c ON c.gateway_id=e.gateway_id
         AND c.source_id=e.source_id AND c.stream_id=e.stream_id
         AND c.capture_generation=e.capture_generation AND c.clock_epoch=e.clock_epoch
        WHERE e.gateway_id=:gateway AND e.batch_id=:batch"""),
            dict(gateway=gateway_id, batch=batch_id),
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return effective_capture_clock(
        row,
        body=row["original_body"],
        batch_sha256=row["original_digest"],
        reversal_seq=row["reversal_seq"],
    )


def record_capture_clock(
    connection,
    body,
    *,
    now,
    policy=None,
    batch_digest=None,
    source=None,
    measured=None,
    input_evidence_sha256=None,
    newly_inserted=False,
):
    """Persist one capture-specific decision in the intake transaction, on schema18.

    The caller has authenticated and locked the registered source and inserted the
    immutable ingest row. Returning None on old heads never queries a new table.
    """
    if not clock_evidence_available(connection):
        return None
    from apps.api.alarm_consumer import assess_capture_clock

    body = BatchBody.model_validate(body)
    now = _TIME.validate_python(now)
    digest = digest_body(body)
    if batch_digest is not None and batch_digest != digest:
        raise ValueError("capture clock digest must bind the original batch")
    if source is None:
        source = (
            connection.execute(
                sa.text("""SELECT * FROM source_streams
            WHERE source_id=:source AND stream_id=:stream"""),
                dict(source=body.source_id, stream=body.stream_id),
            )
            .mappings()
            .one()
        )
    if (
        source["source_id"],
        source["stream_id"],
        source["gateway_id"],
        int(source["capture_generation"]),
    ) != (body.source_id, body.stream_id, body.gateway_id, body.capture_generation):
        raise ValueError("capture clock source binding does not match registration")
    # The intake caller has just inserted the immutable parent under its identity
    # locks. An evidence row cannot predate that parent (FK), and the insert PK
    # rejects a replay. Other callers retain the full immutable evidence check.
    existing = (
        None if newly_inserted else read_capture_clock(connection, body.gateway_id, body.batch_id)
    )
    if existing is not None:
        if (
            existing.batch_sha256 != digest
            or existing.input_evidence_sha256 != input_evidence_sha256
        ):
            raise ValueError("capture clock identity or input evidence cannot change")
        return existing
    if measured is not None and policy is not None:
        raise ValueError("analytical clock modes must be configured exclusively")
    selected = policy.for_capture(body) if isinstance(policy, SameHostClockPolicy) else policy
    assessment, interval, reason = assess_capture_clock(body, selected, now)
    if isinstance(policy, SameHostClockPolicy) and selected is None:
        reason = "CLOCK_BINDING_MISMATCH"
    lower = assessment.offset_lower_seconds if assessment else None
    upper = assessment.offset_upper_seconds if assessment else None
    proof = dict(
        assessment=assessment.model_dump(mode="json") if assessment else None,
        policy=selected.model_dump(mode="json") if selected else None,
    )
    method = "CONFIGURED_API_REFERENCE" if assessment else "NONE"
    trusted = assessment is not None
    if measured is not None:
        method = "MEASURED_ROUND_TRIP"
        trusted, reason = measured.good, measured.reason_code
        lower, upper = measured.offset_lower_seconds, measured.offset_upper_seconds
        interval = (measured.interval_lower_utc, measured.interval_upper_utc)
        proof = dict(measured.proof)
        proof["assessment"] = None
    clock_valid = (
        capture_clock_valid(connection, body, body.capture_time.clock_epoch)
        if body.capture_time
        else False
    )
    status = "GOOD" if trusted and clock_valid else "UNKNOWN"
    if not clock_valid and body.capture_time is not None:
        reason = "CLOCK_REVERSAL"
    future = (
        interval[1] > now + timedelta(seconds=2)
        if measured is not None and trusted
        else (
            body.captured_at > now + timedelta(seconds=2)
            or (
                interval is not None
                and interval[1] is not None
                and interval[1] > now + timedelta(seconds=2)
            )
        )
    )
    if future:
        status, reason = "UNKNOWN", "FUTURE_CAPTURE"
    canonical = interval[0] + (interval[1] - interval[0]) / 2 if status == "GOOD" else None
    if measured is not None and status == "GOOD" and canonical != measured.canonical_api_utc:
        raise ValueError("measured canonical time does not match its persisted offset interval")
    decision = CaptureClockDecision(
        status,
        reason,
        canonical,
        body.captured_at,
        interval[0] if interval else None,
        interval[1] if interval else None,
        lower,
        upper,
        proof.get("assessment"),
        digest,
        method,
        input_evidence_sha256=input_evidence_sha256,
    )
    connection.execute(
        sa.text("""INSERT INTO capture_clock_evidence
        (gateway_id,batch_id,line_id,source_id,stream_id,capture_generation,capture_seq,
        clock_epoch,batch_sha256,input_evidence_sha256,method,status,reason_code,
        original_capture_utc,canonical_api_utc,
        offset_lower_seconds,offset_upper_seconds,interval_lower_utc,interval_upper_utc,
        assessed_at,normalization_version,proof)
        VALUES (:gateway,:batch,:line,:source,:stream,:generation,:seq,:epoch,:digest,
        :input_digest,:method,
        :status,:reason,:original,:canonical,:lower,:upper,:left,:right,:now,:version,
        CAST(:proof AS jsonb))"""),
        dict(
            gateway=body.gateway_id,
            batch=body.batch_id,
            line=source["line_id"],
            source=body.source_id,
            stream=body.stream_id,
            generation=body.capture_generation,
            seq=body.capture_seq,
            epoch=body.capture_time.clock_epoch if body.capture_time else None,
            digest=digest,
            input_digest=input_evidence_sha256,
            method=decision.method,
            status=status,
            reason=reason,
            original=body.captured_at,
            canonical=canonical,
            lower=lower,
            upper=upper,
            left=decision.interval_lower_utc,
            right=decision.interval_upper_utc,
            now=now,
            version=NORMALIZATION_VERSION,
            proof=json.dumps(proof),
        ),
    )
    return decision


def _fence_expired_reversal_minutes(connection, body, epoch, cutoff):
    """Conservatively fence a source whose deleted membership cannot be reconstructed.

    Receipts retain clock ordering, not sample membership or normalization proof.
    If an expired capture intersects the reversal, fence all existing source/stream
    minutes and their raw rebuild inputs. This intentionally sacrifices precision
    rather than letting an incomplete history replace UNKNOWN with GOOD. Aggregate
    rows and the permanent reversal state are retained.
    """
    revision = schema_revision(connection)
    if revision not in {
        "0022_ingest_retention",
        "0023_ingest_compaction",
        "0024_ingest_retired_segments",
        "0025_mes_edit_audit",
        "0026_queue_reenroll",
    }:
        return
    binding = dict(
        source=body.source_id,
        stream=body.stream_id,
        gateway=body.gateway_id,
        generation=body.capture_generation,
        epoch=epoch,
        cutoff=cutoff,
    )
    compact = ""
    if revision in {
        "0023_ingest_compaction",
        "0024_ingest_retired_segments",
        "0025_mes_edit_audit",
        "0026_queue_reenroll",
    }:
        compact = """ OR EXISTS(SELECT 1 FROM ingest_retention_ranges
        WHERE source_id=:source AND stream_id=:stream AND gateway_id=:gateway
          AND capture_generation=:generation AND :epoch=ANY(clock_epochs)
          AND through_capture_seq>=:cutoff)"""
    expired = connection.execute(
        sa.text(
            """SELECT EXISTS(SELECT 1 FROM ingest_receipts
        WHERE source_id=:source AND stream_id=:stream AND gateway_id=:gateway
          AND capture_generation=:generation AND clock_epoch=:epoch
          AND capture_seq>=:cutoff AND expired_at IS NOT NULL)"""
            + compact
        ),
        binding,
    ).scalar_one()
    if not expired:
        return
    connection.execute(
        sa.text("""UPDATE analog_history_raw SET
        observation=jsonb_set(observation,'{time_quality}','"UNKNOWN"'),aggregated=false
        WHERE source_id=:source AND stream_id=:stream
          AND observation->>'time_quality' IS DISTINCT FROM 'UNKNOWN'"""),
        binding,
    )
    connection.execute(
        sa.text("""UPDATE analog_history_minutes SET point=point || CAST(:unknown AS jsonb)
        WHERE source_id=:source AND stream_id=:stream"""),
        dict(binding, unknown=UNKNOWN_MINUTE),
    )


def fence_incomplete_reversal_minutes(connection, body, epoch, *, buckets=None):
    """Do not rebuild an affected minute from an incomplete raw selection history.

    Run after raw insertion and after bounded cleanup of explicit buckets.
    Missing latest captures
    can otherwise let older GOOD observations win an already superseded second.
    Only derived time quality in the exact pending tag/minute/scope is fenced;
    original samples and immutable capture decisions remain unchanged.
    """
    binding = dict(
        source=body.source_id,
        stream=body.stream_id,
        gateway=body.gateway_id,
        generation=body.capture_generation,
        epoch=epoch,
    )
    cutoff = connection.execute(
        sa.text("""SELECT reversal_seq FROM analog_history_clock_state
        WHERE source_id=:source AND stream_id=:stream AND gateway_id=:gateway
        AND capture_generation=:generation AND clock_epoch=:epoch"""),
        binding,
    ).scalar_one_or_none()
    if cutoff is None:
        return
    _fence_expired_reversal_minutes(connection, body, epoch, cutoff)
    modern = clock_evidence_available(connection)
    raw_at = "COALESCE(canonical_utc,acquired_at)" if modern else "acquired_at"
    remaining_at = (
        "COALESCE(remaining.canonical_utc,remaining.acquired_at)"
        if modern
        else "remaining.acquired_at"
    )
    sample_at = "(sample->>'acquired_at')::timestamptz"
    proof_join = ""
    if modern:
        proof_join = """LEFT JOIN capture_clock_evidence evidence
            ON evidence.gateway_id=b.gateway_id AND evidence.batch_id=b.batch_id
            AND evidence.source_id=b.source_id AND evidence.stream_id=b.stream_id
            AND evidence.capture_generation=b.capture_generation
            AND evidence.capture_seq=b.capture_seq
            AND evidence.clock_epoch=b.body->'capture_time'->>'clock_epoch'
            AND evidence.batch_sha256=b.content_sha256
            AND evidence.original_capture_utc=(b.body->>'captured_at')::timestamptz"""
        sample_at = (
            f"COALESCE(evidence.canonical_api_utc+"
            f"({sample_at}-evidence.original_capture_utc),{sample_at})"
        )
    candidate_filter = "AND NOT aggregated"
    bucket_parameters = {}
    if buckets is not None:
        candidate_filter = f"""AND (tag_id,date_trunc('minute',{raw_at})) IN (
            SELECT * FROM unnest(CAST(:tags AS text[]),CAST(:minutes AS timestamptz[])))"""
        bucket_parameters = dict(
            tags=[tag for tag, _ in buckets], minutes=[minute for _, minute in buckets]
        )
    connection.execute(
        sa.text(f"""WITH pending AS MATERIALIZED (
        SELECT DISTINCT tag_id,date_trunc('minute',{raw_at}) AS minute
        FROM analog_history_raw WHERE source_id=:source AND stream_id=:stream
          AND gateway_id=:gateway AND capture_generation=:generation
          AND observation->'scope'->>'clock_epoch'=:epoch {candidate_filter}
    ), incomplete AS (
        SELECT DISTINCT pending.tag_id,pending.minute
        FROM ingest_batches b {proof_join}
        CROSS JOIN LATERAL jsonb_array_elements(b.body->'samples') sample
        JOIN pending ON pending.tag_id=sample->>'tag_id'
          AND pending.minute=date_trunc('minute',{sample_at})
        LEFT JOIN analog_history_raw retained
          ON retained.source_id=b.source_id AND retained.stream_id=b.stream_id
          AND retained.gateway_id=b.gateway_id AND retained.capture_generation=b.capture_generation
          AND retained.capture_seq=b.capture_seq AND retained.batch_id=b.batch_id
          AND retained.tag_id=sample->>'tag_id' AND retained.sample_id=sample->>'sample_id'
        WHERE b.source_id=:source AND b.stream_id=:stream AND b.gateway_id=:gateway
          AND b.capture_generation=:generation AND b.capture_seq>=:cutoff
          AND b.body->'capture_time'->>'clock_epoch'=:epoch AND retained.sample_id IS NULL
    ), fenced AS (UPDATE analog_history_raw remaining SET
        observation=jsonb_set(remaining.observation,'{{time_quality}}','"UNKNOWN"'),aggregated=false
        FROM incomplete WHERE remaining.source_id=:source AND remaining.stream_id=:stream
          AND remaining.gateway_id=:gateway AND remaining.capture_generation=:generation
          AND remaining.observation->'scope'->>'clock_epoch'=:epoch
          AND remaining.tag_id=incomplete.tag_id
          AND date_trunc('minute',{remaining_at})=incomplete.minute
          RETURNING remaining.tag_id
    ) UPDATE analog_history_minutes cached
        SET point=cached.point || CAST(:unknown AS jsonb)
        FROM incomplete WHERE cached.source_id=:source AND cached.stream_id=:stream
          AND cached.tag_id=incomplete.tag_id AND cached.minute=incomplete.minute"""),
        dict(binding, cutoff=cutoff, unknown=UNKNOWN_MINUTE, **bucket_parameters),
    )


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
    history = capture_frontier_table(connection)
    if history != "ingest_batches":
        captured_at = "captured_at"
        monotonic_seconds = "monotonic_seconds"
        clock_epoch = "clock_epoch"
    else:
        captured_at = "(body->>'captured_at')::timestamptz"
        monotonic_seconds = "(body->'capture_time'->>'monotonic_seconds')::double precision"
        clock_epoch = "body->'capture_time'->>'clock_epoch'"
    if state is None and body.capture_time is not None:
        # A first schema-15 capture or supported downgrade/upgrade can have
        # immutable clock history without a projection registry. Rebuild once
        # in SQL: nearest neighbors alone cannot recover an earlier reversal.
        cutoff = connection.execute(
            sa.text(f"""WITH captures AS (
            SELECT capture_seq,{captured_at} AS at,{monotonic_seconds} AS mono
            FROM {history} WHERE source_id=:source_id AND stream_id=:stream_id
                AND gateway_id=:gateway AND capture_generation=:generation
                AND {clock_epoch}=:epoch
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
    history = capture_frontier_table(connection, deduplicate=False)
    neighbors = connection.execute(
        sa.text(f"""(SELECT capture_seq,{captured_at},{monotonic_seconds}
        FROM {history} WHERE source_id=:source_id AND stream_id=:stream_id
        AND gateway_id=:gateway AND capture_generation=:generation
        AND {clock_epoch}=:epoch AND capture_seq<:seq
        ORDER BY capture_seq DESC LIMIT 1) UNION ALL
        (SELECT capture_seq,{captured_at},{monotonic_seconds} FROM {history}
        WHERE source_id=:source_id AND stream_id=:stream_id AND gateway_id=:gateway
        AND capture_generation=:generation AND {clock_epoch}=:epoch
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
        # Minute projections outlive raw samples. Preserve the canonical coordinate
        # of each originally accepted proof even when its effective quality is now
        # UNKNOWN. The immutable batch provides tag membership and original delta.
        sample_at = "(s->>'acquired_at')::timestamptz"
        evidence_join = ""
        if clock_evidence_available(connection):
            evidence_join = """LEFT JOIN capture_clock_evidence e
                ON e.gateway_id=b.gateway_id AND e.batch_id=b.batch_id
                AND e.source_id=b.source_id AND e.stream_id=b.stream_id
                AND e.capture_generation=b.capture_generation AND e.capture_seq=b.capture_seq
                AND e.clock_epoch=b.body->'capture_time'->>'clock_epoch'
                AND e.batch_sha256=b.content_sha256
                AND e.original_capture_utc=(b.body->>'captured_at')::timestamptz"""
            sample_at = f"""COALESCE(e.canonical_api_utc+
                ({sample_at}-e.original_capture_utc),{sample_at})"""
        connection.execute(
            sa.text(f"""UPDATE analog_history_minutes m
            SET point=m.point || CAST(:unknown AS jsonb)
            FROM (SELECT DISTINCT s->>'tag_id' AS tag_id,
                date_trunc('minute',{sample_at}) AS minute
              FROM ingest_batches b {evidence_join}
              CROSS JOIN LATERAL jsonb_array_elements(b.body->'samples') s
              WHERE b.source_id=:source_id AND b.stream_id=:stream_id AND b.gateway_id=:gateway
              AND b.capture_generation=:generation
              AND b.body->'capture_time'->>'clock_epoch'=:epoch
              AND b.capture_seq>=:reversal) affected
            WHERE m.source_id=:source_id AND m.stream_id=:stream_id
              AND m.tag_id=affected.tag_id AND m.minute=affected.minute"""),
            dict(binding, reversal=reversal, unknown=UNKNOWN_MINUTE),
        )
    if reversal is not None:
        _fence_expired_reversal_minutes(connection, body, epoch, reversal)
    connection.execute(
        sa.text("""INSERT INTO analog_history_clock_state
        (source_id,stream_id,gateway_id,capture_generation,clock_epoch,
         last_seq,last_at,reversal_seq)
        VALUES (:source_id,:stream_id,:gateway,:generation,:epoch,:seq,:at,:reversal)
        ON CONFLICT(source_id,stream_id,gateway_id,capture_generation,clock_epoch)
        DO UPDATE SET last_seq=GREATEST(analog_history_clock_state.last_seq,EXCLUDED.last_seq),
        last_at=CASE WHEN EXCLUDED.last_seq>analog_history_clock_state.last_seq
            THEN EXCLUDED.last_at ELSE analog_history_clock_state.last_at END,
        reversal_seq=EXCLUDED.reversal_seq"""),
        dict(binding, seq=body.capture_seq, at=body.captured_at, reversal=reversal),
    )
    return reversal is None or body.capture_seq < reversal
