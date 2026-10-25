"""Retention is measured in whole days, always longer than gateway ledger retention."""

import pytest

from apps.api.ingest_retention import retention_days_from_env, validate_retention_days
from apps.api.internal import InternalConfig


def test_default_and_explicit_minimum(monkeypatch):
    monkeypatch.delenv("HMI_INGEST_RETENTION_DAYS", raising=False)
    assert retention_days_from_env() == 7
    assert InternalConfig().ingest_retention_days == 7
    monkeypatch.setenv("HMI_INGEST_RETENTION_DAYS", "1")
    assert retention_days_from_env() == 1
    assert InternalConfig().ingest_retention_days == 1


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, "7", None])
def test_direct_values_cannot_bypass_whole_day_validation(value):
    with pytest.raises(ValueError):
        validate_retention_days(value)
    with pytest.raises(ValueError):
        InternalConfig(ingest_retention_days=value)


@pytest.mark.parametrize("value", ["", "0", "-1", "0.0833", "1.0", "1e2", "NaN", " 7", "7\n"])
def test_environment_rejects_ambiguous_values_without_reflection(monkeypatch, value):
    monkeypatch.setenv("HMI_INGEST_RETENTION_DAYS", value)
    with pytest.raises(ValueError) as error:
        retention_days_from_env()
    assert value not in str(error.value) if value else str(error.value)
