"""Trusted V3 field-store/queue publication adapter. Persistence remains in field_channels.

Prepare validates all models/committed policy; the OPC owner encodes the complete map,
then supplies a synchronous reference-only snapshot + observation adoption closure.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from types import MappingProxyType
from uuid import uuid4

from asyncua import ua

from apps.simulator.field_channels import (
    GROUPS,
    FieldFaultConflict,
    Group,
    GroupFrame,
    Mode,
    PublishedField,
)
from apps.simulator.field_delay import DelayConflict, DelayPolicy, FieldDelayEngine, RawGroupFrame
from contracts.models import ContractModel, UInt64
from contracts.tags.validate import load_contract

_TAGS = {t["logical_name"]: t for t in load_contract()["tags"]}
_GROUP_TAGS = {g: tuple(n for n in _TAGS if n.startswith(g + ".")) for g in GROUPS}
_FAILURES = {
    "READ_FAILURE": ua.StatusCodes.BadCommunicationError,
    "LINK_DOWN": ua.StatusCodes.BadNotConnected,
}


class PublicationRuntime(ContractModel):
    group_id: Group
    published_revision: UInt64
    published_mode: Mode
    waiting_for_first_completion: bool
    read_in_flight: bool


def get_publication_runtime(store) -> tuple[PublicationRuntime, ...] | None:
    """Read only the last atomically adopted publication view; None means unknown."""
    return getattr(store, "_field_publication_runtime", None)


@dataclass(frozen=True)
class PreparedPublication:
    fields: object
    frames: object
    policy_fingerprint: tuple
    engine_plan: object
    owner: object
    runtime_status: tuple[PublicationRuntime, ...]


class FieldPublication:
    def __init__(self, store, *, monotonic_clock=time.monotonic):
        self.store = store
        if not hasattr(store, "_field_publication_runtime"):
            store._field_publication_runtime = None
        self._clock = monotonic_clock
        self._last_clock = None
        self._clock_failed = False
        self._engine = None
        self._owner = object()

    def _monotonic(self):
        if self._clock_failed:
            raise ValueError("Field clock continuity requires a new publisher generation")
        try:
            value = self._clock()
            if type(value) not in (int, float):
                raise ValueError("Invalid field publication monotonic clock")
            value = float(value)
            if (
                not math.isfinite(value)
                or value < 0
                or (self._last_clock is not None and value < self._last_clock)
            ):
                raise ValueError("Invalid or regressed field publication monotonic clock")
        except Exception:
            self._clock_failed = True
            raise
        # Clock observation is independent from queue/UA adoption. A failed prepare
        # cannot make a later regressed server clock reading look trustworthy.
        self._last_clock = value
        return value

    @staticmethod
    def _policies(states):
        return tuple(
            DelayPolicy(
                group_id=s.group_id,
                revision=s.revision,
                mode=s.mode,
                delay_ms=s.delay_ms,
                normal_checkpoint=s.normal_checkpoint if s.mode == "DELAY" else None,
            )
            for s in states
        )

    def prepare(self, values, *, acquired_at, published_at, status_code):
        if set(values) != set(_TAGS):
            raise ValueError("Complete 48-tag projection is required")
        raw = {
            name: PublishedField(
                tag_id=name,
                reading={"data_type": tag["data_type"], "value": values[name]},
                status_code=status_code,
                source_timestamp=acquired_at,
            )
            for name, tag in _TAGS.items()
        }
        return self.prepare_fields(raw, acquired_at=acquired_at, published_at=published_at)

    def prepare_fields(self, fields, *, acquired_at, published_at):
        """Trusted acquisition seam, including field-specific raw quality; no user input."""
        if self.store.storage.connection.in_transaction:
            raise FieldFaultConflict("Uncommitted field config cannot be published")
        if set(fields) != set(_TAGS):
            raise ValueError("Complete raw dictionary is required")
        raw = {
            name: PublishedField.model_validate_json(value.model_dump_json())
            for name, value in fields.items()
        }
        if any(name != value.tag_id for name, value in raw.items()):
            raise ValueError("Raw field identity mismatch")
        states = self.store.states()
        policies = self._policies(states)
        now = self._monotonic()
        if self._engine is None:
            self._engine = FieldDelayEngine(policies)
        selected = dict(raw)
        for state in states:
            original = tuple(raw[name] for name in _GROUP_TAGS[state.group_id])
            if state.mode == "FROZEN" and all(f.status_code >> 30 == 0 for f in original):
                group = state.frozen_checkpoint.fields
            elif state.mode in _FAILURES:
                group = tuple(
                    f.model_copy(update={"status_code": _FAILURES[state.mode]})
                    for f in state.normal_checkpoint.fields
                )
            elif state.mode in {"BAD", "UNCERTAIN"}:
                code = (
                    ua.StatusCodes.BadSensorFailure
                    if state.mode == "BAD"
                    else ua.StatusCodes.UncertainSensorNotAccurate
                )
                group = tuple(
                    f.model_copy(update={"status_code": code})
                    if f.status_code >> 30 < code >> 30
                    else f
                    for f in original
                )
            else:
                group = original
            selected.update({f.tag_id: f for f in group})
        raw_frames = tuple(
            RawGroupFrame(
                group_id=g, acquired_at=acquired_at, fields=tuple(raw[n] for n in _GROUP_TAGS[g])
            )
            for g in GROUPS
        )
        pass_frames = tuple(
            RawGroupFrame(
                group_id=g,
                acquired_at=acquired_at,
                fields=tuple(selected[n] for n in _GROUP_TAGS[g]),
            )
            for g in GROUPS
        )
        plan = self._engine.prepare(
            policies=policies,
            raw_frames=raw_frames,
            pass_through_frames=pass_frames,
            monotonic_now=now,
            published_at=published_at,
        )
        frames = {}
        for output in plan.publications:
            frame = output.normal_checkpoint or GroupFrame(
                group_id=output.group_id,
                revision=output.revision,
                mode=output.mode,
                frame_id="field-publication:" + str(uuid4()),
                published_at=published_at,
                fields=output.fields,
            )
            frames[output.group_id] = frame
            selected.update({f.tag_id: f for f in output.fields})
        return PreparedPublication(
            MappingProxyType(selected),
            MappingProxyType(frames),
            policies,
            plan,
            self._owner,
            tuple(
                PublicationRuntime(
                    group_id=p.group_id,
                    published_revision=p.revision,
                    published_mode=p.mode,
                    waiting_for_first_completion=p.waiting_for_first_completion,
                    read_in_flight=p.read_in_flight,
                )
                for p in plan.publications
            ),
        )

    def publish(self, plan, swap_snapshot_and_observations):
        if self._clock_failed:
            raise ValueError("Field clock continuity requires a new publisher generation")
        if not isinstance(plan, PreparedPublication) or plan.owner is not self._owner:
            raise DelayConflict("Publication plan belongs to another adapter")
        if self.store.storage.connection.in_transaction:
            raise FieldFaultConflict("Uncommitted field config cannot be published")
        if self._policies(self.store.states()) != plan.policy_fingerprint:
            raise DelayConflict("Committed field config changed after preparation")
        self._engine.publish(plan.engine_plan, swap_snapshot_and_observations)
