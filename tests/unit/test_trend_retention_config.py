"""Deployment retention parsing without external services or secret reflection."""

from dataclasses import replace

import pytest

from apps.api.serverless import VercelConfig
from apps.api.trend_history import TrendRetention

DSN = "postgresql://hmi_app:fixture-only@neon.example.test/hmi_mes_demo?sslmode=require"


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("HMI_PUBLIC_ORIGIN", "https://hmi.example.test")
    monkeypatch.setenv("HMI_DATABASE_DSN", DSN)
    monkeypatch.setenv("HMI_GATEWAY_CREDENTIALS_JSON", "[]")
    for key in ("HMI_TREND_RAW_HOURS", "HMI_TREND_MINUTE_DAYS", "HMI_GUEST_VIEWER_ENABLED"):
        monkeypatch.delenv(key, raising=False)


def test_absent_defaults_and_explicit_factory_default():
    assert VercelConfig.from_environment().trend_retention is None
    assert VercelConfig("https://hmi.example.test", DSN).trend_retention is None


@pytest.mark.parametrize("hours,days", [("6", "14"), ("168", "90"), ("1", "1"), ("3", "5")])
def test_valid_integer_deployment_profiles(monkeypatch, hours, days):
    monkeypatch.setenv("HMI_TREND_RAW_HOURS", hours)
    monkeypatch.setenv("HMI_TREND_MINUTE_DAYS", days)
    config = VercelConfig.from_environment()
    assert config.trend_retention.raw_days == float(hours) / 24
    assert config.trend_retention.minute_days == float(days)
    assert config.trend_retention.delete_limit == 1000


@pytest.mark.parametrize("key", ["HMI_TREND_RAW_HOURS", "HMI_TREND_MINUTE_DAYS"])
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "0",
        "-1",
        "NaN",
        "nan",
        "Inf",
        "-Infinity",
        "1e999",
        "true",
        "false",
        "null",
        "{}",
        "[]",
        '"6"',
        "secret-sentinel",
        "9" * 100,
    ],
)
def test_invalid_values_fail_closed_without_reflection(monkeypatch, key, raw):
    monkeypatch.setenv(key, raw)
    with pytest.raises(ValueError) as error:
        VercelConfig.from_environment()
    assert (
        str(error.value)
        == f"{key} must be a whole number from 1 to {168 if key.endswith('HOURS') else 90}"
    )


@pytest.mark.parametrize(
    "key,raw",
    [
        ("HMI_TREND_RAW_HOURS", "168.01"),
        ("HMI_TREND_MINUTE_DAYS", "90.01"),
        ("HMI_TREND_RAW_HOURS", "5e-324"),
    ],
)
def test_outside_baseline_or_unrepresentable_profile(monkeypatch, key, raw):
    monkeypatch.setenv(key, raw)
    with pytest.raises(ValueError):
        VercelConfig.from_environment()


def test_partial_profile_preserves_other_default(monkeypatch):
    monkeypatch.setenv("HMI_TREND_RAW_HOURS", "6")
    assert VercelConfig.from_environment().trend_retention == TrendRetention(raw_days=0.25)
    monkeypatch.delenv("HMI_TREND_RAW_HOURS")
    monkeypatch.setenv("HMI_TREND_MINUTE_DAYS", "14")
    assert VercelConfig.from_environment().trend_retention == TrendRetention(minute_days=14)


def test_direct_config_requires_typed_nonboolean_retention():
    config = VercelConfig.from_environment()
    for retention in ({"raw_days": 0.25}, TrendRetention(raw_days=True)):
        with pytest.raises(ValueError):
            replace(config, trend_retention=retention)
