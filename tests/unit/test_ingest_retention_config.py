"""Retention uses whole hours and rejects ambiguous legacy configuration."""

import pytest

from apps.api.ingest_retention import retention_hours_from_env, validate_retention_hours
from apps.api.internal import InternalConfig


@pytest.fixture(autouse=True)
def clear_retention_environment(monkeypatch):
    monkeypatch.delenv("HMI_INGEST_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("HMI_INGEST_RETENTION_HOURS", raising=False)


def test_default_and_explicit_minimum(monkeypatch):
    assert retention_hours_from_env() == 168
    assert InternalConfig().ingest_retention_hours == 168
    monkeypatch.setenv("HMI_INGEST_RETENTION_HOURS", "3")
    assert retention_hours_from_env() == 3
    assert InternalConfig().ingest_retention_hours == 3
    assert InternalConfig(ingest_retention_hours=24).ingest_retention_hours == 24


@pytest.mark.parametrize("value", [True, False, 0, 1, 2, -1, 3.0, "168", None])
def test_direct_values_cannot_bypass_whole_hour_validation(value):
    with pytest.raises(ValueError):
        validate_retention_hours(value)
    with pytest.raises(ValueError):
        InternalConfig(ingest_retention_hours=value)


@pytest.mark.parametrize(
    "value", ["", "0", "1", "2", "-1", "2.5", "3.0", "1e2", "NaN", " 3", "3\n"]
)
def test_environment_rejects_ambiguous_values_without_reflection(monkeypatch, value):
    monkeypatch.setenv("HMI_INGEST_RETENTION_HOURS", value)
    with pytest.raises(ValueError) as error:
        retention_hours_from_env()
    assert value not in str(error.value) if value else str(error.value)
    with pytest.raises(ValueError):
        InternalConfig()


@pytest.mark.parametrize("legacy", ["", "1", "7", "invalid"])
@pytest.mark.parametrize("hours", [None, "3", "168"])
def test_legacy_days_is_rejected_alone_and_with_hours(monkeypatch, legacy, hours):
    monkeypatch.setenv("HMI_INGEST_RETENTION_DAYS", legacy)
    if hours is not None:
        monkeypatch.setenv("HMI_INGEST_RETENTION_HOURS", hours)
    with pytest.raises(ValueError, match="HMI_INGEST_RETENTION_DAYS is unsupported"):
        retention_hours_from_env()
    with pytest.raises(ValueError, match="HMI_INGEST_RETENTION_DAYS is unsupported"):
        InternalConfig()
    with pytest.raises(ValueError, match="HMI_INGEST_RETENTION_DAYS is unsupported"):
        InternalConfig(ingest_retention_hours=3)
