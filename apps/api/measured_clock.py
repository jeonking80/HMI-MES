"""Durable single-use round-trip evidence. Call inside the intake transaction."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from sqlalchemy import text

from apps.api.ingest_retention import retention_available
from contracts.clock_evidence import ClockProbeRequest, ClockProbeResponse
from contracts.transport import GatewayBatch, gateway_batch_digest

MAX_AGE_SECONDS = 60.0
MAX_RTT_SECONDS = 2.0
MAX_CLOCK_RESIDUAL_SECONDS = 0.05
MAX_UTC_READ_SPAN_SECONDS = 0.05
PROBE_SCOPE_CAP = 120
PROBE_GLOBAL_CAP = 10000
PROBE_CLEANUP_LIMIT = 100
_CAP_LOCK = 4725328694253421


class MeasuredClockConflict(Exception):
    """Nonce was changed, reused or presented by another credential/scope."""


class ProbeScopeError(Exception):
    """Persisted binding does not permit issuing this measurement."""


class ProbeCapacityError(Exception):
    """The bounded durable probe store is full."""


@dataclass(frozen=True)
class MeasuredClockResult:
    good: bool
    reason_code: str
    epoch: str | None
    offset_lower_seconds: float | None
    offset_upper_seconds: float | None
    interval_lower_utc: datetime | None
    interval_upper_utc: datetime | None
    canonical_api_utc: datetime | None
    proof: dict


def issue_probe(
    connection,
    request: ClockProbeRequest,
    *,
    credential_key_id: str,
    api_received_at: datetime,
    now: datetime,
) -> ClockProbeResponse:
    # All issuers serialize the cleanup/count/insert operation across API instances.
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CAP_LOCK})
    scope = dict(
        gateway=request.gateway_id,
        source=request.source_id,
        stream=request.stream_id,
        generation=request.capture_generation,
        epoch=request.clock_epoch,
    )
    # Prioritize the issuing scope so its expired cap cannot repeatedly roll
    # back unrelated cleanup while leaving that scope permanently saturated.
    retained = retention_available(connection)
    if retained:
        # Owner function clamps the virtual-test cutoff to DB wall time and
        # removes only expired unconsumed probes. Consumed proofs live with raw.
        connection.execute(
            text(
                "SELECT public.prune_expired_clock_probes("
                ":cutoff,:gateway,:source,:stream,:generation,:epoch)"
            ),
            dict(scope, cutoff=now),
        )
    else:
        connection.execute(
            text("""DELETE FROM capture_clock_probes WHERE nonce_sha256 IN
            (SELECT nonce_sha256 FROM capture_clock_probes WHERE expires_at<=:now
             ORDER BY CASE WHEN gateway_id=:gateway AND source_id=:source
              AND stream_id=:stream AND capture_generation=:generation AND clock_epoch=:epoch
              THEN 0 ELSE 1 END,expires_at,nonce_sha256 LIMIT :limit)"""),
            dict(scope, now=now, limit=PROBE_CLEANUP_LIMIT),
        )
    # Consumed proofs now live with the seven-day raw batch; capacity limits
    # bound outstanding single-use nonces, rather than stopping issuance after
    # the first 10,000 successful measurements.
    outstanding = " WHERE consumed_batch_id IS NULL" if retained else ""
    count = connection.execute(
        text("SELECT count(*) FROM capture_clock_probes" + outstanding)
    ).scalar_one()
    scoped = connection.execute(
        text(
            """SELECT count(*) FROM capture_clock_probes
        WHERE gateway_id=:gateway AND source_id=:source AND stream_id=:stream
          AND capture_generation=:generation AND clock_epoch=:epoch"""
            + (" AND consumed_batch_id IS NULL" if retained else "")
        ),
        scope,
    ).scalar_one()
    if count >= PROBE_GLOBAL_CAP or scoped >= PROBE_SCOPE_CAP:
        raise ProbeCapacityError("clock probe storage limit reached")
    if not 0 <= (now - api_received_at).total_seconds() < MAX_AGE_SECONDS:
        raise ProbeCapacityError("clock probe expired while being issued")
    nonce = secrets.token_urlsafe(32)
    connection.execute(
        text("""INSERT INTO capture_clock_probes
        (nonce_sha256,credential_key_id,gateway_id,source_id,stream_id,capture_generation,
         clock_epoch,api_received_at,expires_at,gateway_send_utc,gateway_send_monotonic)
        VALUES (:nonce,:key,:gateway,:source,:stream,:generation,:epoch,:received,:expires,
                :t0,:mono)"""),
        dict(
            scope,
            nonce=hashlib.sha256(nonce.encode()).hexdigest(),
            key=credential_key_id,
            received=api_received_at,
            expires=api_received_at + timedelta(seconds=MAX_AGE_SECONDS),
            t0=request.t0_utc,
            mono=request.t0_monotonic_seconds,
        ),
    )
    return ClockProbeResponse(nonce=nonce, api_utc=api_received_at)


def assess_measured_clock(
    connection, batch: GatewayBatch, *, credential_key_id: str | None, api_received_at: datetime
) -> MeasuredClockResult:
    evidence = batch.clock_evidence
    if evidence is None:
        return MeasuredClockResult(
            False, "NO_MEASURED_EVIDENCE", None, None, None, None, None, None, {}
        )
    body = batch.body
    scope = (body.gateway_id, body.source_id, body.stream_id, body.capture_generation)
    expected = (
        evidence.gateway_id,
        evidence.source_id,
        evidence.stream_id,
        evidence.capture_generation,
    )
    if scope != expected or credential_key_id is None:
        raise MeasuredClockConflict("measured clock scope mismatch")
    nonce_hash = hashlib.sha256(evidence.nonce.encode()).hexdigest()
    row = (
        connection.execute(
            text("""SELECT * FROM capture_clock_probes
        WHERE nonce_sha256=:nonce FOR UPDATE"""),
            {"nonce": nonce_hash},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise MeasuredClockConflict("unknown clock probe")
    if (
        row["gateway_id"],
        row["source_id"],
        row["stream_id"],
        int(row["capture_generation"]),
        row["clock_epoch"],
        row["credential_key_id"],
    ) != (*expected, evidence.clock_epoch, credential_key_id):
        raise MeasuredClockConflict("clock probe belongs to another scope")
    if (
        row["api_received_at"] != evidence.api_utc
        or row["gateway_send_utc"] != evidence.t0_utc
        or row["gateway_send_monotonic"] != evidence.t0_monotonic_seconds
    ):
        raise MeasuredClockConflict("clock probe facts were changed")
    input_sha = gateway_batch_digest(batch)
    if row["consumed_batch_id"] is not None:
        if (row["consumed_batch_id"], row["consumed_input_sha256"]) != (body.batch_id, input_sha):
            raise MeasuredClockConflict("clock probe already consumed")
    else:
        connection.execute(
            text("""UPDATE capture_clock_probes SET consumed_batch_id=:batch,
            consumed_input_sha256=:sha,consumed_at=:now WHERE nonce_sha256=:nonce"""),
            {"batch": body.batch_id, "sha": input_sha, "now": api_received_at, "nonce": nonce_hash},
        )
    result = evaluate_measurement(
        batch, api_received_at=api_received_at, probe_expires_at=row["expires_at"]
    )
    # This authenticated fact survives nonce cleanup with the immutable proof.
    # It is deliberately excluded from the gateway's wire/envelope digest.
    return replace(result, proof=dict(result.proof, credential_key_id=credential_key_id))


def evaluate_measurement(
    batch: GatewayBatch, *, api_received_at: datetime, probe_expires_at: datetime
) -> MeasuredClockResult:
    """Pure bounded interval assessment, allowing signed offsets such as -3.7 s."""
    e = batch.clock_evidence
    b = batch.body
    capture = b.capture_time
    rtt = e.t1_monotonic_seconds - e.t0_monotonic_seconds
    utc_rtt = (e.t1_utc - e.t0_utc).total_seconds()
    lower = (e.api_utc - e.t1_utc).total_seconds()
    upper = (e.api_utc - e.t0_utc).total_seconds()
    age = None if capture is None else capture.monotonic_seconds - e.t1_monotonic_seconds
    proof = dict(
        e.model_dump(mode="json"),
        round_trip_seconds=rtt,
        utc_round_trip_seconds=utc_rtt,
        measurement_age_seconds=age,
        api_measurement_age_seconds=(api_received_at - e.api_utc).total_seconds(),
        offset_lower_seconds=lower,
        offset_upper_seconds=upper,
    )

    def unknown(reason):
        return MeasuredClockResult(
            False, reason, e.clock_epoch, None, None, None, None, None, proof
        )

    if capture is None or capture.clock_epoch != e.clock_epoch:
        return unknown("MEASURED_EPOCH_MISMATCH")
    if capture.utc_read_span_seconds > MAX_UTC_READ_SPAN_SECONDS:
        return unknown("MEASURED_UTC_READ_SPAN_EXCEEDED")
    if (
        not 0 <= rtt <= MAX_RTT_SECONDS
        or utc_rtt < 0
        or abs(utc_rtt - rtt) > MAX_CLOCK_RESIDUAL_SECONDS
    ):
        return unknown("MEASURED_ROUND_TRIP_INVALID")
    if (
        not 0 <= age <= MAX_AGE_SECONDS
        or api_received_at > probe_expires_at
        or not 0 <= (api_received_at - e.api_utc).total_seconds() <= MAX_AGE_SECONDS
    ):
        return unknown("MEASURED_EVIDENCE_STALE")
    capture_delta = (b.captured_at - e.t1_utc).total_seconds()
    if (
        capture_delta < 0
        or abs(capture_delta - age) > MAX_CLOCK_RESIDUAL_SECONDS + capture.utc_read_span_seconds
    ):
        return unknown("MEASURED_MONOTONIC_DISCONTINUITY")
    # A 50 ms maximum residual/drift over the <=60 s validity window is an
    # explicit operating assumption; preserve it and the capture read bracket
    # as uncertainty instead of silently reporting a point estimate.
    uncertainty = MAX_CLOCK_RESIDUAL_SECONDS + capture.utc_read_span_seconds
    proof["max_clock_residual_seconds"] = MAX_CLOCK_RESIDUAL_SECONDS
    proof["capture_utc_read_span_seconds"] = capture.utc_read_span_seconds
    interval_lower = b.captured_at + timedelta(seconds=lower - uncertainty)
    interval_upper = b.captured_at + timedelta(seconds=upper + uncertainty)
    # The intersection contains only possible capture instants before API receipt.
    if interval_lower > api_received_at:
        return unknown("MEASURED_CAPTURE_FUTURE")
    interval_upper = min(interval_upper, api_received_at)
    canonical = interval_lower + (interval_upper - interval_lower) / 2
    return MeasuredClockResult(
        True,
        "MEASURED_ROUND_TRIP",
        e.clock_epoch,
        lower,
        upper,
        interval_lower,
        interval_upper,
        canonical,
        proof,
    )
