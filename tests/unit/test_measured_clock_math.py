"""Signed interval mathematics and monotonic continuity boundaries."""

from datetime import UTC, datetime, timedelta

import pytest

from apps.api.measured_clock import evaluate_measurement
from contracts.clock_evidence import MeasuredClockEvidence
from contracts.transport import BatchBody, GatewayBatch, batch_digest

AT = datetime(2026, 10, 23, 12, tzinfo=UTC)


def measured_batch(
    *, nonce="nonce-1", seq=1, epoch="clock-1", rtt=0.2, age=0.3, utc_delta=0.0, capture_extra=0.0
):
    t0 = AT + timedelta(seconds=3.7)
    t1 = t0 + timedelta(seconds=rtt + utc_delta)
    captured = t1 + timedelta(seconds=age + capture_extra)
    body = BatchBody(
        schema_version="1.2",
        batch_id=f"measured-{seq}",
        gateway_id="gateway-1",
        source_id="simulator-1",
        stream_id="stream-1",
        capture_generation=1,
        capture_seq=seq,
        captured_at=captured,
        capture_time=dict(
            clock_epoch=epoch, monotonic_seconds=100.0 + rtt + age, utc_read_span_seconds=0.0
        ),
        communication=dict(
            state="GOOD",
            connected=True,
            read_age_seconds=0.0,
            heartbeat_ages_seconds={
                key: 0.0
                for key in (
                    "Line.Heartbeat",
                    "Infeed.LinkHeartbeat",
                    "Sealer.LinkHeartbeat",
                    "Outfeed.LinkHeartbeat",
                )
            },
        ),
        samples=[
            dict(
                sample_id=f"sample-{seq}",
                tag_id="Sealer.TemperaturePV",
                source_timestamp=captured,
                server_timestamp=None,
                acquired_at=captured,
                status_code=0,
                reading=dict(data_type="Double", value=150.0),
            )
        ],
    )
    e = MeasuredClockEvidence(
        gateway_id="gateway-1",
        source_id="simulator-1",
        stream_id="stream-1",
        capture_generation=1,
        clock_epoch="clock-1",
        t0_utc=t0,
        t0_monotonic_seconds=100.0,
        nonce=nonce,
        api_utc=AT + timedelta(seconds=0.1),
        t1_utc=t1,
        t1_monotonic_seconds=100.0 + rtt,
    )
    return GatewayBatch(body=body, content_sha256=batch_digest(body), clock_evidence=e)


def assess(value, receipt=AT + timedelta(seconds=1)):
    return evaluate_measurement(
        value, api_received_at=receipt, probe_expires_at=AT + timedelta(seconds=60.1)
    )


def test_positive_gateway_skew_yields_negative_signed_offset_and_good_capture():
    result = assess(measured_batch())
    assert result.good
    assert result.offset_lower_seconds == pytest.approx(-3.8)
    assert result.offset_upper_seconds == pytest.approx(-3.6)
    assert AT < result.canonical_api_utc < AT + timedelta(seconds=1)
    assert result.proof["measurement_age_seconds"] == pytest.approx(0.3)


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"rtt": 2.001}, "MEASURED_ROUND_TRIP_INVALID"),
        ({"utc_delta": 0.1}, "MEASURED_ROUND_TRIP_INVALID"),
        ({"epoch": "other-clock"}, "MEASURED_EPOCH_MISMATCH"),
        ({"age": 60.001}, "MEASURED_EVIDENCE_STALE"),
        ({"age": -0.01}, "MEASURED_EVIDENCE_STALE"),
        ({"capture_extra": 0.2}, "MEASURED_MONOTONIC_DISCONTINUITY"),
        ({"age": 10.0}, "MEASURED_CAPTURE_FUTURE"),
    ],
)
def test_invalid_measurement_remains_unknown(kwargs, reason):
    result = assess(measured_batch(**kwargs))
    assert not result.good and result.reason_code == reason
    assert result.canonical_api_utc is None


def test_api_age_also_expires_even_with_fresh_gateway_coordinates():
    assert assess(measured_batch(), receipt=AT + timedelta(seconds=61)).reason_code == (
        "MEASURED_EVIDENCE_STALE"
    )


def test_large_capture_bracket_is_unknown_and_cannot_hide_clock_jump():
    value = measured_batch(capture_extra=1.0)
    capture = value.body.capture_time.model_copy(update={"utc_read_span_seconds": 2.0})
    body = value.body.model_copy(update={"capture_time": capture})
    value = value.model_copy(update={"body": body})
    assert assess(value).reason_code == "MEASURED_UTC_READ_SPAN_EXCEEDED"


def test_capture_monotonic_reset_is_unknown():
    value = measured_batch()
    capture = value.body.capture_time.model_copy(update={"monotonic_seconds": 0.0})
    body = value.body.model_copy(update={"capture_time": capture})
    value = value.model_copy(update={"body": body})
    assert assess(value).reason_code == "MEASURED_EVIDENCE_STALE"


@pytest.mark.parametrize("field", ["t0_monotonic_seconds", "t1_monotonic_seconds"])
@pytest.mark.parametrize("number", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_probe_coordinates_are_rejected(field, number):
    from pydantic import ValidationError

    raw = measured_batch().clock_evidence.model_dump(mode="json")
    raw[field] = number
    with pytest.raises(ValidationError):
        MeasuredClockEvidence.model_validate(raw)
