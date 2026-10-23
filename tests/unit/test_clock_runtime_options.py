"""Operator opt-ins stay explicit and are never inferred from platform names."""

import pytest

from apps.api.__main__ import parser


def _serve(*extra):
    return parser().parse_args(
        [
            "serve",
            "--source-id",
            "source-A",
            "--stream-id",
            "main",
            "--gateway-id",
            "gateway-A",
            "--capture-generation",
            "1",
            "--key-id",
            "key-A",
            "--token-file",
            "/tmp/not-read",
            "--expires-at",
            "2027-01-01T00:00:00Z",
            *extra,
        ]
    )


def test_runtime_clock_options_default_off():
    args = _serve()
    assert args.trend_clock_policy is None
    assert args.clock_measured_round_trip is False


def test_same_host_and_measured_are_independent_explicit_options():
    local = _serve("--trend-clock-policy", "same-host")
    assert local.trend_clock_policy == "same-host"
    assert local.clock_measured_round_trip is False
    remote = _serve("--clock-measured-round-trip")
    assert remote.trend_clock_policy is None
    assert remote.clock_measured_round_trip is True


def test_incompatible_clock_guarantees_are_configuration_errors():
    with pytest.raises(SystemExit):
        _serve("--trend-clock-policy", "same-host", "--clock-measured-round-trip")
