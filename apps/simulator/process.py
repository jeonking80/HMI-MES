"""Pure packaging simulation, one 100ms tick per ``step`` call.

Initial simulation choices: three single-capacity stages (infeed, sealer,
outfeed), 100mm travel each, 6m/min speed, 25C ambient, 150C setpoint,
6bar pressure, 5C/s heating and 1C/s cooling. These are test values, not
equipment operating limits. Every item moves at most one stage per tick;
admission occurs after movement and never automatically ends/changes a job.
Travel uses exact fractions from the configured speed's decimal text, without
rounding a genuinely subthreshold distance into a stage transition. Item progress
is a Fraction; JSON snapshots encode it as a canonical rational string.
Quantities, item IDs, cursor and tick fit UInt64; exhaustion raises ValueError
without changing the supplied state. Execution identifiers have at most 128 chars.

``step(state, run=..., admit=...)`` returns an immutable state and item deltas.
The caller supplies final run permission after PLC/fault/estop checks. Disabled
or stopped motion retains WIP and inspection cursor; heat evolves independently.
Temperature and pressure do not grant permission here. ``scrap`` requires
explicit item IDs and a reason. ``fresh_execution`` requires previous WIP zero.

Pattern decisions repeat in order; an empty pattern selects a specified 32-bit
LCG (1664525*x + 1013904223 mod 2**32). Cursor and generator state live in
``ProcessState.to_json()`` snapshots, making replay independent of Python's RNG.
Deltas have no durable event IDs or timestamps: later persistence must commit
state, counters and generated events atomically before publishing anything.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, fields, replace
from fractions import Fraction

TICK_MS = 100
STAGES = ("infeed", "sealer", "outfeed")
UINT64_MAX = 2**64 - 1
_RNG_MODULUS = 2**32


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _number(value: object, name: str, minimum: float = 0) -> None:
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= minimum
    except OverflowError:
        valid = False
    _require(
        valid,
        f"{name} must be a finite number >= {minimum}",
    )


def _integer(value: object, name: str, minimum: int = 0) -> None:
    _require(
        type(value) is int and minimum <= value <= UINT64_MAX,
        f"{name} must be an integer in [{minimum}, {UINT64_MAX}]",
    )


def _identifier(value: object) -> None:
    _require(
        isinstance(value, str) and 1 <= len(value) <= 128 and not any(c.isspace() for c in value),
        "execution_id must have 1..128 characters without whitespace",
    )


def _object_fields(value: object, cls: type) -> None:
    _require(
        isinstance(value, dict) and set(value) == {field.name for field in fields(cls)},
        f"invalid {cls.__name__} snapshot fields",
    )


@dataclass(frozen=True)
class ProcessConfig:
    speed_m_per_min: float = 6.0
    stage_lengths_mm: tuple[int, int, int] = (100, 100, 100)
    temperature_sp_c: float = 150.0
    ambient_temperature_c: float = 25.0
    air_pressure_bar: float = 6.0
    heating_c_per_s: float = 5.0
    cooling_c_per_s: float = 1.0
    pattern: tuple[str, ...] = ("GOOD", "GOOD", "REJECT")
    reject_probability: float = 0.1

    def __post_init__(self) -> None:
        for name in (
            "speed_m_per_min",
            "temperature_sp_c",
            "ambient_temperature_c",
            "air_pressure_bar",
            "heating_c_per_s",
            "cooling_c_per_s",
            "reject_probability",
        ):
            _number(getattr(self, name), name)
        _require(self.speed_m_per_min > 0, "speed_m_per_min must be positive")
        _require(self.reject_probability <= 1, "reject_probability must be <= 1")
        _require(
            type(self.stage_lengths_mm) is tuple and len(self.stage_lengths_mm) == 3,
            "exactly three immutable stage lengths required",
        )
        for length in self.stage_lengths_mm:
            _integer(length, "stage length", 1)
        _require(type(self.pattern) is tuple, "pattern must be an immutable tuple")
        _require(all(p in ("GOOD", "REJECT") for p in self.pattern), "invalid inspection pattern")


# Operating line: random inspection outcomes at a plausible reject rate. The
# fixed GOOD, GOOD, REJECT pattern stays the deterministic default for tests.
OPERATING_PROCESS = ProcessConfig(pattern=(), reject_probability=0.03)


@dataclass(frozen=True)
class Item:
    item_id: int
    execution_id: str
    progress_mm: Fraction = Fraction(0)

    def __post_init__(self) -> None:
        _integer(self.item_id, "item_id", 1)
        _identifier(self.execution_id)
        progress = self.progress_mm
        if not isinstance(progress, Fraction):
            _number(progress, "progress_mm")
            progress = Fraction(str(progress))
        _require(progress >= 0, "progress_mm must be nonnegative")
        object.__setattr__(self, "progress_mm", progress)


@dataclass(frozen=True)
class ProcessState:
    execution_id: str
    config: ProcessConfig = ProcessConfig()
    slots: tuple[Item | None, Item | None, Item | None] = (None, None, None)
    input_quantity: int = 0
    good_quantity: int = 0
    reject_quantity: int = 0
    scrap_quantity: int = 0
    decision_cursor: int = 0
    rng_state: int = 1
    temperature_c: float = 25.0
    tick: int = 0
    schema_version: int = 1

    def __post_init__(self) -> None:
        _identifier(self.execution_id)
        _require(isinstance(self.config, ProcessConfig), "config must be ProcessConfig")
        _require(
            type(self.slots) is tuple and len(self.slots) == 3, "three immutable slots required"
        )
        for name in (
            "input_quantity",
            "good_quantity",
            "reject_quantity",
            "scrap_quantity",
            "decision_cursor",
            "rng_state",
            "tick",
            "schema_version",
        ):
            _integer(getattr(self, name), name)
        _require(self.schema_version == 1, "unsupported state schema_version")
        _require(self.rng_state < _RNG_MODULUS, "rng_state must fit UInt32")
        _number(self.temperature_c, "temperature_c")
        ids = []
        for index, item in enumerate(self.slots):
            if item is not None:
                _require(isinstance(item, Item), "slots must contain Item or None")
                _require(item.execution_id == self.execution_id, "WIP execution_id mismatch")
                _require(item.item_id <= self.input_quantity, "item_id exceeds admitted quantity")
                _require(
                    item.progress_mm <= self.config.stage_lengths_mm[index], "invalid progress"
                )
                ids.append(item.item_id)
        _require(len(set(ids)) == len(ids), "duplicate WIP item_id")
        _require(
            self.input_quantity
            == self.good_quantity + self.reject_quantity + self.scrap_quantity + self.wip_quantity,
            "input must equal good + reject + scrap + WIP",
        )
        _require(
            self.decision_cursor == self.good_quantity + self.reject_quantity,
            "decision_cursor must equal classified quantity",
        )

    @property
    def wip_quantity(self) -> int:
        return sum(item is not None for item in self.slots)

    def to_json(self) -> str:
        data = asdict(self)
        for item in data["slots"]:
            if item is not None:
                item["progress_mm"] = str(item["progress_mm"])
        return json.dumps(data, allow_nan=False, sort_keys=True)

    @classmethod
    def from_json(cls, snapshot: str) -> ProcessState:
        data = json.loads(snapshot)
        _object_fields(data, cls)
        config = data["config"]
        _object_fields(config, ProcessConfig)
        _require(type(config["stage_lengths_mm"]) is list, "stage lengths must be a JSON array")
        _require(type(config["pattern"]) is list, "pattern must be a JSON array")
        config["stage_lengths_mm"] = tuple(config["stage_lengths_mm"])
        config["pattern"] = tuple(config["pattern"])
        data["config"] = ProcessConfig(**config)
        _require(type(data["slots"]) is list, "slots must be a JSON array")
        items = []
        for item in data["slots"]:
            if item is not None:
                _object_fields(item, Item)
                progress = item["progress_mm"]
                _require(isinstance(progress, str), "snapshot progress must be a rational string")
                try:
                    fraction = Fraction(progress)
                except (ValueError, ZeroDivisionError) as exc:
                    raise ValueError("snapshot progress must be a rational string") from exc
                _require(str(fraction) == progress, "snapshot progress must be canonical")
                item["progress_mm"] = fraction
                item = Item(**item)
            items.append(item)
        data["slots"] = tuple(items)
        return cls(**data)


@dataclass(frozen=True)
class ItemDelta:
    item_id: int
    execution_id: str
    outcome: str  # GOOD / REJECT / SCRAP; exactly one item per delta
    reason: str | None = None


@dataclass(frozen=True)
class StepResult:
    state: ProcessState
    deltas: tuple[ItemDelta, ...] = ()


def fresh_execution(
    previous: ProcessState | None,
    execution_id: str,
    *,
    config: ProcessConfig | None = None,
    seed: int = 1,
) -> ProcessState:
    """Explicitly create a fresh run; caller retains the previous run's record."""
    if previous is not None:
        _require(isinstance(previous, ProcessState), "previous must be ProcessState")
        _require(previous.wip_quantity == 0, "cannot replace an execution with WIP")
        _require(previous.execution_id != execution_id, "fresh execution needs a new execution_id")
    if config is None:
        config = ProcessConfig()
    _require(isinstance(config, ProcessConfig), "config must be ProcessConfig")
    return ProcessState(
        execution_id=execution_id,
        config=config,
        rng_state=seed,
        temperature_c=previous.temperature_c
        if previous is not None
        else config.ambient_temperature_c,
    )


def step(
    state: ProcessState,
    *,
    run: bool,
    admit: bool,
    enabled: bool = True,
    heater_on: bool = False,
) -> StepResult:
    """Advance exactly 100ms; no mutation, wall clock, IO, job selection or PLC logic."""
    _require(isinstance(state, ProcessState), "state must be ProcessState")
    _require(state.tick < UINT64_MAX, "tick exhausted UInt64")
    for flag in (run, admit, enabled, heater_on):
        _require(type(flag) is bool, "step flags must be boolean")
    cfg = state.config
    temperature = state.temperature_c
    target = cfg.temperature_sp_c if heater_on else cfg.ambient_temperature_c
    rate = cfg.heating_c_per_s if heater_on and temperature < target else cfg.cooling_c_per_s
    temperature += max(-rate / 10, min(rate / 10, target - temperature))
    if not run or not enabled:
        return StepResult(replace(state, tick=state.tick + 1, temperature_c=temperature))

    slots = list(state.slots)
    good, reject = state.good_quantity, state.reject_quantity
    cursor, rng = state.decision_cursor, state.rng_state
    deltas = []
    distance = Fraction(str(cfg.speed_m_per_min)) * Fraction(1000 * TICK_MS, 60000)
    for index in (2, 1, 0):
        item = slots[index]
        if item is None:
            continue
        progress = min(cfg.stage_lengths_mm[index], item.progress_mm + distance)
        item = replace(item, progress_mm=progress)
        slots[index] = item
        if progress < cfg.stage_lengths_mm[index]:
            continue
        if index < 2:
            if slots[index + 1] is None:
                slots[index + 1] = replace(item, progress_mm=0)
                slots[index] = None
        else:
            if cfg.pattern:
                outcome = cfg.pattern[cursor % len(cfg.pattern)]
            else:
                rng = (1664525 * rng + 1013904223) % _RNG_MODULUS
                outcome = "REJECT" if rng / _RNG_MODULUS < cfg.reject_probability else "GOOD"
            cursor += 1
            good += outcome == "GOOD"
            reject += outcome == "REJECT"
            deltas.append(ItemDelta(item.item_id, item.execution_id, outcome))
            slots[index] = None
    admitted = state.input_quantity
    if admit and slots[0] is None:
        _require(admitted < UINT64_MAX, "input_quantity and item identity exhausted UInt64")
        admitted += 1
        slots[0] = Item(admitted, state.execution_id)
    return StepResult(
        replace(
            state,
            slots=tuple(slots),
            input_quantity=admitted,
            good_quantity=good,
            reject_quantity=reject,
            decision_cursor=cursor,
            rng_state=rng,
            tick=state.tick + 1,
            temperature_c=temperature,
        ),
        tuple(deltas),
    )


def scrap(state: ProcessState, item_ids: tuple[int, ...], *, reason: str) -> StepResult:
    """Remove only explicitly selected WIP; caller checks safe physical disposal."""
    _require(isinstance(state, ProcessState), "state must be ProcessState")
    _require(isinstance(reason, str) and bool(reason.strip()), "scrap requires a reason")
    _require(type(item_ids) is tuple and bool(item_ids), "select at least one item to scrap")
    for item_id in item_ids:
        _integer(item_id, "item_id", 1)
    _require(len(set(item_ids)) == len(item_ids), "duplicate scrap selection")
    selected = set(item_ids)
    present = {item.item_id for item in state.slots if item is not None}
    _require(selected <= present, "scrap selection includes an unknown item")
    deltas = tuple(
        ItemDelta(item.item_id, item.execution_id, "SCRAP", reason)
        for item in state.slots
        if item is not None and item.item_id in selected
    )
    return StepResult(
        replace(
            state,
            slots=tuple(
                None if item and item.item_id in selected else item for item in state.slots
            ),
            scrap_quantity=state.scrap_quantity + len(selected),
        ),
        deltas,
    )
