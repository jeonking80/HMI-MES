"""Shared same-host binding and persisted analytical UTC interpretation."""

from dataclasses import replace
from datetime import timedelta

import pytest

from apps.api.capture_clock import NORMALIZATION_VERSION, effective_capture_clock
from contracts.transport import batch_digest
from tests.integration.test_capture_clock import same_host
from tests.integration.test_trend_history import AT, batch


def evidence(body):
    left, right = AT - timedelta(seconds=0.2), AT + timedelta(seconds=0.1)
    return dict(
        gateway_id=body.gateway_id,
        batch_id=body.batch_id,
        source_id=body.source_id,
        stream_id=body.stream_id,
        capture_generation=body.capture_generation,
        capture_seq=body.capture_seq,
        clock_epoch=body.capture_time.clock_epoch,
        batch_sha256=batch_digest(body),
        original_capture_utc=body.captured_at,
        status="GOOD",
        reason_code="MEASURED",
        canonical_api_utc=left + (right - left) / 2,
        offset_lower_seconds=-0.2,
        offset_upper_seconds=0.2,
        interval_lower_utc=left,
        interval_upper_utc=right,
        method="MEASURED_ROUND_TRIP",
        normalization_version=NORMALIZATION_VERSION,
        proof={"assessment": None},
    )


def test_final_interval_midpoint_allows_clipped_measurement_and_applies_cutoff():
    body = batch(1).body
    row = evidence(body)
    result = effective_capture_clock(row, body=body, batch_sha256=batch_digest(body))
    assert result.status == "GOOD"
    assert result.canonical_api_utc == AT - timedelta(seconds=0.05)
    assert result.canonical_api_utc != body.captured_at
    result = effective_capture_clock(
        row, body=body, batch_sha256=batch_digest(body), reversal_seq=1
    )
    assert result.status == "UNKNOWN" and result.canonical_api_utc is None
    assert row["status"] == "GOOD"


@pytest.mark.parametrize(
    "field,value",
    [
        ("gateway_id", "other"),
        ("source_id", "other"),
        ("stream_id", "other"),
        ("capture_generation", 2),
        ("capture_seq", 2),
        ("clock_epoch", "other"),
        ("batch_sha256", "0" * 64),
        ("batch_id", "other"),
    ],
)
def test_binding_failure_never_returns_missing_fallback(field, value):
    body = batch(1).body
    row = evidence(body)
    row[field] = value
    result = effective_capture_clock(row, body=body, batch_sha256=batch_digest(body))
    assert result is not None and result.status == "UNKNOWN"
    assert result.canonical_api_utc is None


def test_missing_and_explicit_unknown_are_distinct():
    body = batch(1).body
    assert effective_capture_clock(None, body=body, batch_sha256=batch_digest(body)) is None
    row = evidence(body)
    row.update(status="UNKNOWN", canonical_api_utc=None)
    assert (
        effective_capture_clock(row, body=body, batch_sha256=batch_digest(body)).status == "UNKNOWN"
    )


def test_shared_kernel_assertion_exact_generation_and_dynamic_epoch():
    policy = same_host()
    body = batch(1).body
    assert policy.for_capture(body).clock_epoch == "clock-1"
    changed = body.model_copy(
        update={"capture_time": body.capture_time.model_copy(update={"clock_epoch": "restarted"})}
    )
    assert policy.for_capture(changed).clock_epoch == "restarted"
    assert replace(policy, capture_generation=2).for_capture(body) is None
    with pytest.raises(ValueError):
        replace(policy, valid_until_api_utc=policy.valid_from_api_utc)
