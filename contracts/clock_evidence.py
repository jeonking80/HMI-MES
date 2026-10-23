"""Measured clock wire evidence; the API authenticates and assesses every assertion."""

from typing import Annotated, Literal

from pydantic import Field

from contracts.models import ContractModel, Identifier, UInt64, UTCTimestamp

MonotonicSeconds = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]


class ClockProbeRequest(ContractModel):
    gateway_id: Identifier
    source_id: Identifier
    stream_id: Identifier
    capture_generation: UInt64
    clock_epoch: Identifier
    t0_utc: UTCTimestamp
    t0_monotonic_seconds: MonotonicSeconds


class ClockProbeResponse(ContractModel):
    nonce: Identifier
    api_utc: UTCTimestamp


class MeasuredClockEvidence(ClockProbeRequest):
    method: Literal["MEASURED_ROUND_TRIP"] = "MEASURED_ROUND_TRIP"
    nonce: Identifier
    api_utc: UTCTimestamp
    t1_utc: UTCTimestamp
    t1_monotonic_seconds: MonotonicSeconds
