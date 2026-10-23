"""Hand-computed SPC fixtures, independent WE windows and future-only regression."""

import math
from datetime import UTC, datetime, timedelta

import pytest

from apps.api.spc import OFFSETS, SPCSlot, calculate_spc, regression_warning, western_electric
from apps.api.spc_http import historical_spec

AT = datetime(2026, 10, 23, tzinfo=UTC)
SPEC = dict(usl=160.0, lsl=140.0, target=150.0, source="recipe+alarm_rule")


def slots(count, *, offset=0):
    return [
        SPCSlot(AT + timedelta(minutes=m, seconds=s), 148.0 + i + offset, epoch="epoch")
        for m in range(count)
        for i, s in enumerate(OFFSETS)
    ]


def calculate(values, count=25, **kwargs):
    return calculate_spc(
        slots=values,
        start=AT,
        end=AT + timedelta(minutes=count),
        now=AT + timedelta(minutes=count),
        tag_id="Sealer.TemperaturePV",
        unit="°C",
        spec=SPEC,
        **kwargs,
    )


def test_hand_calculated_limits_capability_and_first20():
    result = calculate(slots(25))
    assert result["status"] == "OK"
    assert result["limits"]["xbar"] == pytest.approx(dict(cl=150, ucl=152.308, lcl=147.692))
    assert result["limits"]["range"] == pytest.approx(dict(cl=4, ucl=8.456, lcl=0))
    # sigma=4/2.326, Cp=Cpk=20/(6*sigma)=1.9383333.
    assert result["capability"]["cp"] == pytest.approx(1.9383333333333)
    assert result["capability"]["cpk"] == pytest.approx(1.9383333333333)
    shifted = slots(20) + [
        SPCSlot(s.at + timedelta(minutes=20), s.value + 10, epoch=s.epoch) for s in slots(5)
    ]
    revised = calculate(shifted)
    assert revised["limits"] == result["limits"]
    assert revised["baseline"]["subgroups"] == 20
    assert revised["subgroups"][-1]["violations"] == ["WE1", "WE2", "WE3"]


@pytest.mark.parametrize("sign", [-1, 1])
@pytest.mark.parametrize(
    "rule,positive,negative",
    [
        ("WE1", [3.01], [3.0]),
        ("WE2", [2.1, 0, 2.1], [2.0, 0, 2.1]),
        ("WE3", [1.1, 1.1, 0, 1.1, 1.1], [1.0, 1.1, 0, 1.1, 1.1]),
        ("WE4", [0.1] * 8, [0.1] * 7 + [0.0]),
    ],
)
def test_we_positives_negatives(rule, positive, negative, sign):
    assert rule in western_electric([sign * x for x in positive], 0, 1)[-1]
    assert rule not in western_electric([sign * x for x in negative], 0, 1)[-1]


def test_missing_bad_unknown_and_insufficient():
    values = slots(20)
    values.pop(0)
    values[4] = SPCSlot(values[4].at, None, "BAD", "epoch")
    values[9] = SPCSlot(values[9].at, None, "TIME_UNKNOWN", "epoch")
    result = calculate(values, count=20)
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["limits"] is None
    assert result["capability"] is None
    assert result["excluded"]["count"] == 3
    assert result["excluded"]["reasons"] == dict(MISSING=1, BAD=1, TIME_UNKNOWN=1, UNCERTAIN=0)
    assert calculate([], count=20)["excluded"]["count"] == 20


def test_no_pooling_epochs_and_zero_variance():
    values = slots(25)
    values[-1] = SPCSlot(values[-1].at, values[-1].value, epoch="other")
    assert calculate(values)["excluded"]["reasons"]["TIME_UNKNOWN"] == 1
    constant = [SPCSlot(s.at, 150.0, epoch=s.epoch) for s in slots(25)]
    result = calculate(constant)
    assert result["capability"] is None
    assert result["trend_warning"]["projected_cross"] is None
    assert math.isfinite(result["limits"]["xbar"]["ucl"])


def test_regression_gates_and_future_cross():
    now = AT + timedelta(minutes=15)
    points = [(AT + timedelta(minutes=m), 140.0 + m) for m in range(15)]
    warning = regression_warning(points, now=now, spec=SPEC)
    assert warning["slope_per_minute"] == pytest.approx(1)
    assert warning["r2"] == pytest.approx(1)
    assert warning["projected_cross"] == dict(limit="USL", at="2026-10-23T00:20:00Z")
    assert regression_warning(points[:9], now=now, spec=SPEC)["projected_cross"] is None
    assert regression_warning(points, now=now, spec=dict(SPEC, usl=150))["projected_cross"] is None
    noisy = [(at, 150.0 + (-1) ** m) for m, (at, _) in enumerate(points)]
    assert regression_warning(noisy, now=now, spec=SPEC)["projected_cross"] is None
    falling = [(at, 160.0 - m) for m, (at, _) in enumerate(points)]
    assert regression_warning(falling, now=now, spec=SPEC)["projected_cross"]["limit"] == "LSL"
    assert regression_warning(points, now=now, spec=None)["projected_cross"] is None


def test_capability_threshold_and_historical_spec():
    assert calculate(slots(24), count=24)["capability"] is None
    config = dict(kind="TEMPERATURE_DEVIATION", setpoint_c=150, activation_delta_c=10)
    assert historical_spec({"temperature_c": 150}, "Sealer.TemperaturePV", [config]) == SPEC
    for configs in ([], [None], [config, None], [config, dict(config, activation_delta_c=20)]):
        assert historical_spec({"temperature_c": 150}, "Sealer.TemperaturePV", configs) is None
    assert historical_spec({"temperature_c": 100}, "Sealer.TemperaturePV", [config]) is None
    assert historical_spec({"temperature_c": 150}, "Sealer.AirPressure", [config]) is None


def test_missing_after_valid_group_is_missing_and_nonfinite_is_excluded():
    result = calculate(slots(1), count=2)
    assert result["excluded"]["reasons"]["MISSING"] == 1
    values = slots(25)
    values[0] = SPCSlot(values[0].at, float("nan"), epoch="epoch")
    result = calculate(values)
    assert result["excluded"]["reasons"]["BAD"] == 1
    assert result["capability"] is None


def test_regression_constant_and_extreme_finite_values_serialize():
    import json

    points = [(AT + timedelta(minutes=m), 1e308 if m % 2 else -1e308) for m in range(15)]
    warning = regression_warning(points, now=AT + timedelta(minutes=15), spec=SPEC)
    json.dumps(warning, allow_nan=False)
    assert warning["projected_cross"] is None


def test_regression_uses_minute_means_without_complete_offset_groups():
    values = [
        SPCSlot(AT + timedelta(minutes=m, seconds=1), 140.0 + m, epoch="epoch") for m in range(15)
    ]
    result = calculate(values, count=15)
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["trend_warning"]["projected_cross"]["at"] == "2026-10-23T00:20:00Z"
