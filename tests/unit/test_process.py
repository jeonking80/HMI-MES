"""Concrete material flow and restart replay checks for the pure process model."""

import json
from dataclasses import FrozenInstanceError, replace
from fractions import Fraction

import pytest

from apps.simulator.process import (
    UINT64_MAX,
    Item,
    ProcessConfig,
    ProcessState,
    fresh_execution,
    scrap,
    step,
)


def fast_state(*, pattern=("GOOD", "REJECT", "GOOD"), seed=1):
    return fresh_execution(
        None,
        "execution-A",
        config=ProcessConfig(speed_m_per_min=60, pattern=pattern, reject_probability=0.5),
        seed=seed,
    )


def assert_balance(state):
    assert state.input_quantity == (
        state.good_quantity + state.reject_quantity + state.scrap_quantity + state.wip_quantity
    )


def test_three_stages_and_known_production_sequence():
    state = fast_state()
    observed = []
    for tick in range(1, 7):
        before = state
        result = step(state, run=True, admit=tick <= 3)
        state = result.state
        assert state.tick == tick
        assert before.tick == tick - 1  # Original snapshots remain unchanged.
        assert_balance(state)
        observed.extend((tick, d.item_id, d.execution_id, d.outcome) for d in result.deltas)
        if tick == 1:
            assert state.slots == (Item(1, "execution-A"), None, None)
        if tick == 2:
            assert state.slots == (Item(2, "execution-A"), Item(1, "execution-A"), None)
        if tick == 3:
            assert [item.item_id for item in state.slots] == [3, 2, 1]
    assert observed == [
        (4, 1, "execution-A", "GOOD"),
        (5, 2, "execution-A", "REJECT"),
        (6, 3, "execution-A", "GOOD"),
    ]
    assert (state.input_quantity, state.good_quantity, state.reject_quantity) == (3, 2, 1)
    assert state.wip_quantity == 0


def test_pause_retains_items_positions_cursor_and_resumes_same_execution():
    state = fresh_execution(None, "execution-A")
    for _ in range(5):
        state = step(state, run=True, admit=True).state
    paused = state
    for _ in range(20):
        result = step(state, run=False, admit=True, heater_on=True)
        state = result.state
        assert state.slots == paused.slots
        assert state.input_quantity == paused.input_quantity
        assert state.decision_cursor == paused.decision_cursor
        assert state.rng_state == paused.rng_state
        assert result.deltas == ()
    assert state.temperature_c > paused.temperature_c
    outputs = []
    for _ in range(40):
        result = step(state, run=True, admit=False)
        state = result.state
        outputs.extend(result.deltas)
        assert_balance(state)
    assert [(d.item_id, d.execution_id) for d in outputs] == [(1, "execution-A")]
    assert state.wip_quantity == 0


@pytest.mark.parametrize("run,enabled", [(False, True), (True, False), (False, False)])
def test_external_fault_stop_or_disabled_permission_prevents_all_production(run, enabled):
    state = fast_state(pattern=())
    for _ in range(3):
        state = step(state, run=True, admit=True).state
    held = state
    for _ in range(10):
        result = step(state, run=run, enabled=enabled, admit=True)
        state = result.state
        assert result.deltas == ()
        assert state.slots == held.slots
        assert state.input_quantity == 3
        assert state.good_quantity == state.reject_quantity == state.decision_cursor == 0
        assert state.rng_state == held.rng_state


@pytest.mark.parametrize("pattern", [("REJECT", "GOOD"), ()])
def test_json_snapshot_resumes_identical_state_and_deltas(pattern):
    state = fast_state(pattern=pattern, seed=7)
    for _ in range(7):
        state = step(state, run=True, admit=True).state
    snapshot = state.to_json()
    restored = ProcessState.from_json(snapshot)
    assert restored == state
    assert restored.to_json() == snapshot
    for tick in range(25):
        kwargs = {"run": tick % 4 != 0, "admit": tick < 12, "heater_on": tick < 8}
        original_result = step(state, **kwargs)
        replay_result = step(restored, **kwargs)
        assert replay_result == original_result
        state, restored = original_result.state, replay_result.state
        assert_balance(state)


def test_seeded_inspection_has_known_portable_sequence_and_no_hidden_rng():
    state = fast_state(pattern=(), seed=1)
    observed = []
    for tick in range(1, 9):
        result = step(state, run=True, admit=tick <= 5)
        state = result.state
        observed.extend(d.outcome for d in result.deltas)
    assert observed == ["REJECT", "REJECT", "GOOD", "GOOD", "REJECT"]
    assert state.rng_state == 217083232
    assert state.decision_cursor == 5


def test_admission_is_explicit_and_backpressure_never_overwrites_wip():
    state = fresh_execution(
        None,
        "execution-A",
        config=ProcessConfig(stage_lengths_mm=(10, 1000, 10)),
    )
    for _ in range(20):
        state = step(state, run=True, admit=True).state
        assert_balance(state)
    assert state.input_quantity == 2
    assert [item.item_id if item else None for item in state.slots] == [2, 1, None]
    for _ in range(250):
        result = step(state, run=True, admit=False)
        state = result.state
        assert_balance(state)
    assert state.input_quantity == 2
    assert state.wip_quantity == 0


def test_default_speed_means_ten_100ms_ticks_per_100mm_stage():
    state = fresh_execution(None, "execution-A")
    state = step(state, run=True, admit=True).state
    for _ in range(9):
        result = step(state, run=True, admit=False)
        state = result.state
        assert result.deltas == ()
    assert state.slots[0] == Item(1, "execution-A", progress_mm=90)
    state = step(state, run=True, admit=False).state
    assert state.slots == (None, Item(1, "execution-A"), None)
    for _ in range(19):
        result = step(state, run=True, admit=False)
        state = result.state
        assert result.deltas == ()
    result = step(state, run=True, admit=False)
    assert result.state.tick == 31
    assert [(d.item_id, d.outcome) for d in result.deltas] == [(1, "GOOD")]


@pytest.mark.parametrize("speed,length,ticks", [(0.06, 1, 10), (0.18, 3, 10), (0.2, 1, 3)])
def test_exact_fractional_travel_reaches_stage_boundary_on_expected_tick(speed, length, ticks):
    config = ProcessConfig(speed_m_per_min=speed, stage_lengths_mm=(length,) * 3)
    state = fresh_execution(None, "fractional", config=config)
    state = step(state, run=True, admit=True).state
    for movement in range(1, 3 * ticks + 1):
        result = step(state, run=True, admit=False)
        state = result.state
        assert_balance(state)
        if movement < 3 * ticks:
            assert result.deltas == ()
            assert state.slots[movement // ticks] is not None
        else:
            assert [(d.item_id, d.outcome) for d in result.deltas] == [(1, "GOOD")]
            assert state.wip_quantity == 0


def test_genuinely_below_boundary_does_not_move_early_and_fraction_snapshot_replays():
    config = ProcessConfig(speed_m_per_min=0.05999999999999999, stage_lengths_mm=(1, 1, 1))
    state = fresh_execution(None, "below-boundary", config=config)
    state = step(state, run=True, admit=True).state
    for _ in range(10):
        state = step(state, run=True, admit=False).state
    assert state.slots[0] is not None
    assert state.slots[0].progress_mm == Fraction("0.05999999999999999") * Fraction(50, 3)
    assert state.slots[0].progress_mm < 1
    restored = ProcessState.from_json(state.to_json())
    expected = step(state, run=True, admit=False)
    assert step(restored, run=True, admit=False) == expected
    assert expected.state.slots == (None, Item(1, "below-boundary"), None)


def test_rational_progress_snapshot_is_canonical_and_rejects_invalid_representations():
    state = fresh_execution(
        None,
        "fraction",
        config=ProcessConfig(speed_m_per_min=0.06, stage_lengths_mm=(1, 1, 1)),
    )
    state = step(state, run=True, admit=True).state
    state = step(state, run=True, admit=False).state
    assert state.slots[0].progress_mm == Fraction(1, 10)
    snapshot = json.loads(state.to_json())
    assert snapshot["slots"][0]["progress_mm"] == "1/10"
    assert ProcessState.from_json(state.to_json()) == state
    for value in ("2/20", "0.1", "1/0", "NaN", -1, "-1/10"):
        snapshot["slots"][0]["progress_mm"] = value
        with pytest.raises(ValueError):
            ProcessState.from_json(json.dumps(snapshot))


@pytest.mark.parametrize(
    "field",
    [
        "input_quantity",
        "good_quantity",
        "reject_quantity",
        "scrap_quantity",
        "decision_cursor",
        "tick",
    ],
)
def test_uint64_state_fields_reject_upper_bound_overflow_in_memory_and_snapshots(field):
    state = fast_state()
    with pytest.raises(ValueError, match=field):
        replace(state, **{field: UINT64_MAX + 1})
    snapshot = json.loads(state.to_json())
    snapshot[field] = UINT64_MAX + 1
    with pytest.raises(ValueError, match=field):
        ProcessState.from_json(json.dumps(snapshot))


def test_uint64_maximum_item_can_be_classified_without_overflow():
    state = ProcessState(
        "maximum",
        config=ProcessConfig(speed_m_per_min=60, pattern=("GOOD",)),
        slots=(None, None, Item(UINT64_MAX, "maximum")),
        input_quantity=UINT64_MAX,
        good_quantity=UINT64_MAX - 1,
        decision_cursor=UINT64_MAX - 1,
    )
    result = step(state, run=True, admit=False)
    assert result.state.good_quantity == result.state.decision_cursor == UINT64_MAX
    assert result.deltas[0].item_id == UINT64_MAX
    assert ProcessState.from_json(result.state.to_json()) == result.state
    with pytest.raises(ValueError, match="item_id"):
        Item(UINT64_MAX + 1, "maximum")


def test_admission_and_tick_exhaustion_raise_without_modifying_input_state():
    state = ProcessState(
        "maximum",
        config=ProcessConfig(speed_m_per_min=60),
        input_quantity=UINT64_MAX - 1,
        scrap_quantity=UINT64_MAX - 1,
    )
    last_admission = step(state, run=True, admit=True).state
    assert last_admission.input_quantity == UINT64_MAX
    assert last_admission.slots[0].item_id == UINT64_MAX
    snapshot = last_admission.to_json()
    with pytest.raises(ValueError, match="exhausted"):
        step(last_admission, run=True, admit=True)
    assert last_admission.to_json() == snapshot
    drained = scrap(last_admission, (UINT64_MAX,), reason="explicit final disposal").state
    assert drained.scrap_quantity == UINT64_MAX
    assert_balance(drained)
    last_tick = step(replace(drained, tick=UINT64_MAX - 1), run=False, admit=False).state
    assert last_tick.tick == UINT64_MAX
    assert ProcessState.from_json(last_tick.to_json()) == last_tick
    with pytest.raises(ValueError, match="tick exhausted"):
        step(last_tick, run=False, admit=False)


def test_execution_identifier_length_matches_protocol_boundary():
    identifier = "A" * 128
    state = fresh_execution(None, identifier)
    assert ProcessState.from_json(state.to_json()) == state
    assert Item(1, identifier).execution_id == identifier
    for invalid in ("A" * 129, "", "has whitespace"):
        with pytest.raises(ValueError, match="execution_id"):
            fresh_execution(None, invalid)
        with pytest.raises(ValueError, match="execution_id"):
            Item(1, invalid)


def test_explicit_scrap_keeps_other_items_and_reason_without_consuming_inspection():
    state = fast_state()
    for _ in range(3):
        state = step(state, run=True, admit=True).state
    result = scrap(state, (2,), reason="operator removed damaged package")
    after = result.state
    assert [item.item_id if item else None for item in after.slots] == [3, None, 1]
    assert after.scrap_quantity == 1
    assert after.tick == state.tick
    assert after.rng_state == state.rng_state
    assert after.decision_cursor == 0
    assert [(d.item_id, d.execution_id, d.outcome, d.reason) for d in result.deltas] == [
        (2, "execution-A", "SCRAP", "operator removed damaged package"),
    ]
    assert_balance(after)
    with pytest.raises(ValueError, match="reason"):
        scrap(state, (1,), reason=" ")
    for selected in ((), (1, 1), (99,), (True,)):
        with pytest.raises(ValueError):
            scrap(state, selected, reason="explicit disposal")


def test_execution_change_requires_no_wip_and_cannot_relabel_items():
    state = step(fast_state(), run=True, admit=True).state
    with pytest.raises(ValueError, match="WIP"):
        fresh_execution(state, "execution-B")
    with pytest.raises(ValueError, match="execution_id mismatch"):
        replace(state, execution_id="execution-B")
    with pytest.raises(ValueError, match="execution_id mismatch"):
        replace(state, slots=(Item(1, "execution-B"), None, None))
    emptied = scrap(state, (1,), reason="job ended with damaged WIP").state
    next_run = fresh_execution(emptied, "execution-B")
    assert emptied.scrap_quantity == 1
    assert next_run.input_quantity == next_run.wip_quantity == 0
    assert step(next_run, run=True, admit=True).state.slots[0].execution_id == "execution-B"


def test_temperature_moves_toward_setpoint_or_ambient_without_granting_motion():
    config = ProcessConfig(
        temperature_sp_c=26,
        ambient_temperature_c=25,
        air_pressure_bar=0,
        heating_c_per_s=5,
        cooling_c_per_s=1,
    )
    state = fresh_execution(None, "execution-A", config=config)
    for _ in range(10):
        state = step(state, run=False, admit=True, heater_on=True).state
    assert state.temperature_c == 26  # No overshoot.
    assert state.wip_quantity == state.input_quantity == 0
    for _ in range(15):
        state = step(state, run=False, admit=False).state
    assert state.temperature_c == 25
    assert state.config.air_pressure_bar == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"speed_m_per_min": -1},
        {"speed_m_per_min": 0},
        {"speed_m_per_min": True},
        {"temperature_sp_c": -1},
        {"air_pressure_bar": -1},
        {"heating_c_per_s": -1},
        {"cooling_c_per_s": float("nan")},
        {"speed_m_per_min": float("inf")},
        {"speed_m_per_min": 10**1000},
        {"reject_probability": 1.1},
        {"reject_probability": -0.1},
        {"stage_lengths_mm": (1, -1, 1)},
        {"stage_lengths_mm": (1, 1)},
        {"pattern": ("UNKNOWN",)},
        {"pattern": ["GOOD"]},
    ],
)
def test_invalid_numeric_configuration_and_patterns_are_rejected(kwargs):
    with pytest.raises(ValueError):
        ProcessConfig(**kwargs)


def test_state_validation_and_snapshot_validation():
    state = fast_state()
    for kwargs in (
        {"input_quantity": -1},
        {"input_quantity": 1},
        {"rng_state": 2**32},
        {"rng_state": True},
        {"tick": -1},
        {"temperature_c": float("nan")},
        {"decision_cursor": 1},
        {"slots": (None,)},
        {"execution_id": ""},
    ):
        with pytest.raises(ValueError):
            replace(state, **kwargs)
    for field, value in (("input_quantity", -1), ("rng_state", -1), ("slots", [])):
        snapshot = json.loads(state.to_json())
        snapshot[field] = value
        with pytest.raises(ValueError):
            ProcessState.from_json(json.dumps(snapshot))
    snapshot = json.loads(state.to_json())
    del snapshot["rng_state"]
    with pytest.raises(ValueError, match="snapshot fields"):
        ProcessState.from_json(json.dumps(snapshot))
    with pytest.raises(ValueError, match="boolean"):
        step(state, run=1, admit=True)
    with pytest.raises(FrozenInstanceError):
        state.input_quantity = 10
