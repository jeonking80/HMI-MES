"""Pure bounded field-read completion delay engine; no persistence or transport.

Only a trusted publisher may adopt a plan, immediately after its atomic UA map swap.
Acquisition frame IDs are internal provenance, never gateway TagSample.sample_id.
"""

from __future__ import annotations

import inspect
import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Annotated, Literal
from uuid import uuid4

from asyncua import ua
from pydantic import Field, TypeAdapter, model_validator

from apps.simulator.field_channels import GROUPS, Group, GroupFrame, PublishedField
from contracts.models import ContractModel, UInt64, UTCTimestamp

_TIME = TypeAdapter(UTCTimestamp)
Fields = Annotated[tuple[PublishedField, ...], Field(min_length=1, max_length=12)]
Duration = Annotated[int, Field(strict=True, ge=100, le=10_000)]


class DelayConflict(ValueError):
    """Invalid policy transition, missing actual provenance, or stale publication plan."""


class DelayPolicy(ContractModel):
    """Trusted committed config view; not a user request or V3 durable payload."""

    group_id: Group
    revision: UInt64
    mode: Literal["NORMAL", "FROZEN", "BAD", "UNCERTAIN", "READ_FAILURE", "LINK_DOWN", "DELAY"] = (
        "NORMAL"
    )
    delay_ms: Duration | None = None
    normal_checkpoint: GroupFrame | None = None

    @model_validator(mode="after")
    def valid_policy(self):
        if self.mode != "DELAY":
            if self.delay_ms is not None or self.normal_checkpoint is not None:
                raise ValueError("NORMAL has no delay or checkpoint")
        elif (
            self.delay_ms is None
            or self.delay_ms % 100 != 0
            or self.normal_checkpoint is None
            or self.normal_checkpoint.group_id != self.group_id
            or self.normal_checkpoint.mode != "NORMAL"
            or self.normal_checkpoint.revision != self.revision - 1
        ):
            raise ValueError("DELAY needs a bounded duration and exact predecessor NORMAL frame")
        return self


class RawGroupFrame(ContractModel):
    """One trusted acquisition, before publication overlays; raw fields stay immutable."""

    group_id: Group
    acquired_at: UTCTimestamp
    fields: Fields

    @model_validator(mode="after")
    def complete(self):
        GroupFrame(
            group_id=self.group_id,
            frame_id="validation",
            published_at=self.acquired_at,
            revision=0,
            mode="NORMAL",
            fields=self.fields,
        )
        return self


@dataclass(frozen=True)
class Acquisition:
    acquisition_frame_id: str
    fields: tuple[PublishedField, ...]
    acquired_at: datetime | None
    # Bootstrap is genuine previous publication evidence, not a new acquisition.
    bootstrap: bool = False


@dataclass(frozen=True)
class PendingRead:
    generation: str
    acquisition: Acquisition
    started_mono: float
    deadline_mono: float


@dataclass(frozen=True)
class GroupRuntime:
    policy: DelayPolicy
    generation: str
    bootstrap: Acquisition | None = None
    pending: PendingRead | None = None
    delivered: Acquisition | None = None
    last_normal_publication: GroupFrame | None = None


@dataclass(frozen=True)
class Publication:
    group_id: Group
    revision: int
    mode: str
    published_at: datetime
    fields: tuple[PublishedField, ...]
    origin: Acquisition
    waiting_for_first_completion: bool
    read_in_flight: bool
    normal_checkpoint: GroupFrame | None = None


@dataclass(frozen=True)
class PublicationPlan:
    publications: tuple[Publication, ...]
    monotonic_now: float
    _owner: object = field(repr=False)
    _base_version: int = field(repr=False)
    _next_runtime: tuple[GroupRuntime, ...] = field(repr=False)


def _monotonic(value):
    if type(value) not in (int, float):
        raise ValueError("Monotonic time must be a finite nonnegative server number")
    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError("Invalid monotonic time") from error
    if not math.isfinite(result) or result < 0:
        raise ValueError("Invalid monotonic time")
    return result


def _policies(values):
    result = tuple(DelayPolicy.model_validate_json(v.model_dump_json()) for v in values)
    if len(result) != 3 or {v.group_id for v in result} != set(GROUPS):
        raise ValueError("Exactly three unique policies are required")
    return {v.group_id: v for v in result}


def _initial(policy):
    checkpoint = policy.normal_checkpoint
    bootstrap = (
        None
        if checkpoint is None
        else Acquisition(
            "field-acquisition:" + str(uuid4()), checkpoint.fields, None, bootstrap=True
        )
    )
    return GroupRuntime(policy, str(uuid4()), bootstrap=bootstrap)


def _acquisition(raw):
    return Acquisition("field-acquisition:" + str(uuid4()), raw.fields, raw.acquired_at)


def _pending(state, raw, now):
    deadline = now + state.policy.delay_ms / 1000
    if not math.isfinite(deadline) or deadline <= now:
        raise ValueError("Delay deadline cannot be represented by this monotonic clock")
    return PendingRead(state.generation, _acquisition(raw), now, deadline)


class FieldDelayEngine:
    """Three single-flight queues and an adopted monotonic frontier, on one publisher thread.

    Construction may load trusted persisted DELAY policies after restart. This does not
    restore queues or certify their checkpoint as an actual NORMAL publication this boot.
    prepare is side-effect free, including the adopted clock frontier. A discarded plan
    has no authority. The server clock owner must separately supervise clock regressions
    between unadopted readings; this engine rejects readings below its adopted frontier.
    """

    def __init__(self, policies):
        configured = _policies(policies)
        self._owner = object()
        self._version = 0
        self._publishing = False
        self._last_mono = None
        self._runtime = tuple(_initial(configured[group]) for group in GROUPS)

    @property
    def runtime(self):
        return self._runtime

    @property
    def pending_count(self):
        return sum(state.pending is not None for state in self._runtime)

    def prepare(
        self, *, policies, raw_frames, monotonic_now, published_at, pass_through_frames=None
    ) -> PublicationPlan:
        if self._publishing:
            raise DelayConflict("Publisher callback cannot reenter the delay engine")
        now = _monotonic(monotonic_now)
        if self._last_mono is not None and now < self._last_mono:
            raise ValueError("Monotonic time regressed below the adopted publication frontier")
        at = _TIME.validate_python(published_at)
        configured = _policies(policies)
        raws = tuple(RawGroupFrame.model_validate_json(v.model_dump_json()) for v in raw_frames)
        if len(raws) != 3 or {v.group_id for v in raws} != set(GROUPS):
            raise ValueError("Exactly three unique raw group frames are required")
        acquired = {v.group_id: v for v in raws}
        passthrough = (
            {}
            if pass_through_frames is None
            else {
                frame.group_id: RawGroupFrame.model_validate_json(frame.model_dump_json())
                for frame in pass_through_frames
            }
        )
        if pass_through_frames is not None and (
            len(passthrough) != 3 or set(passthrough) != set(GROUPS)
        ):
            raise ValueError("Exactly three pass-through frames are required")
        publications, next_runtime = [], []
        for current in self._runtime:
            policy, raw = configured[current.policy.group_id], acquired[current.policy.group_id]
            state = current
            if policy != current.policy:
                if policy.revision <= current.policy.revision:
                    raise DelayConflict(
                        "Policy changes require the next revision and NORMAL boundary"
                    )
                if policy.mode == "DELAY" and (
                    current.policy.mode != "NORMAL"
                    or current.last_normal_publication is None
                    or policy.normal_checkpoint != current.last_normal_publication
                ):
                    raise DelayConflict(
                        "DELAY requires this boot's actual current NORMAL publication"
                    )
                state = _initial(policy)
            if policy.mode != "DELAY":
                selected_raw = raw if policy.mode == "NORMAL" else passthrough.get(policy.group_id)
                if selected_raw is None:
                    raise DelayConflict("Non-NORMAL modes require their actual pass-through frame")
                origin = _acquisition(selected_raw)
                if policy.mode != "NORMAL":
                    origin = replace(origin, acquired_at=None)
                selected = selected_raw.fields
                normal = None
                if policy.mode == "NORMAL":
                    normal = GroupFrame(
                        group_id=policy.group_id,
                        revision=policy.revision,
                        mode="NORMAL",
                        frame_id="field-publication:" + str(uuid4()),
                        published_at=at,
                        fields=selected,
                    )
                state = replace(state, last_normal_publication=normal)
                output = Publication(
                    policy.group_id,
                    policy.revision,
                    policy.mode,
                    at,
                    selected,
                    origin,
                    False,
                    False,
                    normal,
                )
            else:
                pending, delivered = state.pending, state.delivered
                if pending is None:
                    pending = _pending(state, raw, now)
                elif pending.generation != state.generation:
                    raise DelayConflict("Pending read belongs to another generation")
                elif now >= pending.deadline_mono:
                    delivered = pending.acquisition
                    pending = _pending(state, raw, now)
                waiting = delivered is None
                origin = state.bootstrap if waiting else delivered
                selected = (
                    tuple(
                        v.model_copy(
                            update={"status_code": ua.StatusCodes.BadWaitingForInitialData}
                        )
                        for v in origin.fields
                    )
                    if waiting
                    else origin.fields
                )
                output = Publication(
                    policy.group_id,
                    policy.revision,
                    policy.mode,
                    at,
                    selected,
                    origin,
                    waiting,
                    True,
                )
                state = replace(state, pending=pending, delivered=delivered)
            publications.append(output)
            next_runtime.append(state)
        return PublicationPlan(
            tuple(publications), now, self._owner, self._version, tuple(next_runtime)
        )

    def publish(self, plan, swap_snapshot):
        """Validate, synchronously swap a fully encoded UA map, then adopt without failure.

        The trusted callback must perform only a pointer assignment. Build/encode the
        complete 48-tag map before calling this method; no await, DB read, additional
        validation, or engine reentry belongs in the callback. It must return None.
        The engine cannot undo a callback that swaps an external pointer then raises.
        Under that forbidden callback, queue adoption is withheld but external state
        cannot be guaranteed. Transport integration must preserve this narrow contract.
        """
        if self._publishing:
            raise DelayConflict("Publisher callback cannot reenter the delay engine")
        if (
            not isinstance(plan, PublicationPlan)
            or plan._owner is not self._owner
            or plan._base_version != self._version
        ):
            raise DelayConflict("Publication plan is stale or belongs to another engine")
        if (
            not callable(swap_snapshot)
            or inspect.iscoroutinefunction(swap_snapshot)
            or inspect.iscoroutinefunction(type(swap_snapshot).__call__)
        ):
            raise TypeError("Snapshot swap must be a synchronous pointer-only callback")
        # All candidate selection and version arithmetic precedes the pointer swap.
        next_runtime, next_mono, next_version = (
            plan._next_runtime,
            plan.monotonic_now,
            self._version + 1,
        )
        self._publishing = True
        try:
            swap_snapshot()
            # No validation/callback/DB/await or fallible encoding after the swap.
            self._runtime = next_runtime
            self._last_mono = next_mono
            self._version = next_version
        finally:
            self._publishing = False
