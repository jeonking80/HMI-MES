"""Serverless remote-time measurement requires exact deployment opt-in."""

import pytest

from apps.api.serverless import VercelConfig


@pytest.fixture(autouse=True)
def config_env(monkeypatch):
    monkeypatch.setenv("HMI_PUBLIC_ORIGIN", "https://hmi.example.test")
    monkeypatch.setenv(
        "HMI_DATABASE_DSN",
        "postgresql://hmi_app:test-only@neon.example.test/hmi_mes_demo?sslmode=require",
    )
    monkeypatch.setenv("HMI_GATEWAY_CREDENTIALS_JSON", "[]")
    monkeypatch.delenv("HMI_CLOCK_MEASURED_ROUND_TRIP", raising=False)


def test_remote_clock_default_off_and_explicit_values(monkeypatch):
    assert VercelConfig.from_environment().measured_clock_enabled is False
    monkeypatch.setenv("HMI_CLOCK_MEASURED_ROUND_TRIP", "true")
    assert VercelConfig.from_environment().measured_clock_enabled is True
    monkeypatch.setenv("HMI_CLOCK_MEASURED_ROUND_TRIP", "false")
    assert VercelConfig.from_environment().measured_clock_enabled is False


@pytest.mark.parametrize("value", ["", "True", "1", "yes", "secret-not-reflected"])
def test_remote_clock_invalid_values_fail_without_echo(monkeypatch, value):
    monkeypatch.setenv("HMI_CLOCK_MEASURED_ROUND_TRIP", value)
    with pytest.raises(ValueError) as error:
        VercelConfig.from_environment()
    assert str(error.value) == "HMI_CLOCK_MEASURED_ROUND_TRIP must be true or false"
