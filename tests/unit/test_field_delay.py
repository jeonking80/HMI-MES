"""D1 pure queue/plan tests. No storage, sockets, UI, or real sleeps."""

from datetime import UTC, datetime, timedelta

import pytest
from asyncua import ua

from apps.simulator.field_channels import GROUPS, PublishedField
from apps.simulator.field_delay import DelayConflict, DelayPolicy, FieldDelayEngine, RawGroupFrame
from apps.simulator.plc import PLCState
from apps.simulator.tags import project_tags
from contracts.tags.validate import load_contract

NOW = datetime(2026, 10, 21, tzinfo=UTC)
TAGS = load_contract()["tags"]


def policies():
    return tuple(DelayPolicy(group_id=g, revision=0) for g in GROUPS)


def raw(seconds=0, hb=2**64 - 1, code=ua.StatusCodes.Good):
    at = NOW + timedelta(seconds=seconds)
    values = project_tags(PLCState(), None, counter_epoch="epoch", heartbeat=hb)
    return tuple(
        RawGroupFrame(
            group_id=g,
            acquired_at=at,
            fields=tuple(
                PublishedField(
                    tag_id=t["logical_name"],
                    reading={"data_type": t["data_type"], "value": values[t["logical_name"]]},
                    status_code=code,
                    source_timestamp=at,
                )
                for t in TAGS
                if t["logical_name"].startswith(g + ".")
            ),
        )
        for g in GROUPS
    )


def prepare(e, p, mono=0, frames=None):
    return e.prepare(
        policies=p,
        raw_frames=raw(mono, hb=int(mono) + 1) if frames is None else frames,
        monotonic_now=mono,
        published_at=NOW + timedelta(seconds=mono),
    )


def publish(e, plan):
    pointer = [None]
    e.publish(plan, lambda: pointer.__setitem__(0, plan.publications))
    return pointer[0]


def activate(group="Sealer", duration=6000, all_groups=False):
    p = policies()
    e = FieldDelayEngine(p)
    publish(e, prepare(e, p, frames=raw()))
    configured = tuple(
        DelayPolicy(
            group_id=s.policy.group_id,
            revision=1,
            mode="DELAY",
            delay_ms=duration,
            normal_checkpoint=s.last_normal_publication,
        )
        if all_groups or s.policy.group_id == group
        else s.policy
        for s in e.runtime
    )
    plan = prepare(e, configured, frames=raw(hb=99))
    publish(e, plan)
    return e, configured, plan


def output(plan, group="Sealer"):
    return next(v for v in plan.publications if v.group_id == group)


def test_prepare_does_not_mutate_or_certify_normal_publication():
    p = policies()
    e = FieldDelayEngine(p)
    before = e.runtime
    candidate = prepare(e, p)
    assert e.runtime == before and e.pending_count == 0
    normal = output(candidate).normal_checkpoint
    configured = tuple(
        DelayPolicy(group_id=g, revision=1, mode="DELAY", delay_ms=6000, normal_checkpoint=normal)
        if g == "Sealer"
        else policy
        for g, policy in zip(GROUPS, p, strict=True)
    )
    with pytest.raises(DelayConflict):
        prepare(e, configured)
    assert e.runtime == before
    publish(e, candidate)
    assert e.runtime[1].last_normal_publication is not None
    assert output(prepare(e, configured)).waiting_for_first_completion


def test_exact_deadline_initial_waiting_and_no_fresh_timestamp_rewrite():
    e, p, initial = activate()
    waiting = output(initial)
    assert waiting.waiting_for_first_completion and waiting.read_in_flight
    assert all(f.status_code == ua.StatusCodes.BadWaitingForInitialData for f in waiting.fields)
    assert all(f.status_code == 0 for f in waiting.origin.fields)
    assert waiting.origin.bootstrap and waiting.origin.acquired_at is None
    first = next(s for s in e.runtime if s.policy.group_id == "Sealer").pending.acquisition
    before = e.runtime
    almost = prepare(e, p, 5.999)
    assert output(almost).origin == waiting.origin
    assert e.runtime == before
    publish(e, almost)
    ready = prepare(e, p, 6)
    assert not output(ready).waiting_for_first_completion
    assert output(ready).origin == first and output(ready).fields == first.fields
    assert output(ready).published_at == NOW + timedelta(seconds=6)
    assert first.acquired_at == NOW
    assert all(f.source_timestamp == NOW for f in output(ready).fields)
    publish(e, ready)
    repeat = prepare(e, p, 6.1)
    assert output(repeat).origin.acquisition_frame_id == first.acquisition_frame_id
    assert output(repeat).fields == first.fields


def test_no_catchup_and_three_pending_bound_after_long_stall():
    e, p, _ = activate(all_groups=True)
    assert e.pending_count == 3
    previous = {s.policy.group_id: s.pending.acquisition for s in e.runtime}
    late = prepare(e, p, 1000)
    for delivered in late.publications:
        assert delivered.origin == previous[delivered.group_id]
    publish(e, late)
    assert e.pending_count == 3
    assert all(
        s.pending.started_mono == 1000 and s.pending.deadline_mono == 1006 for s in e.runtime
    )
    assert all(
        s.pending.acquisition.acquisition_frame_id != s.delivered.acquisition_frame_id
        for s in e.runtime
    )
    held = prepare(e, p, 1000)
    assert all(v.origin == previous[v.group_id] for v in held.publications)


@pytest.mark.parametrize(
    "code",
    [
        ua.StatusCodes.Good,
        ua.StatusCodes.BadSensorFailure,
        ua.StatusCodes.UncertainSensorNotAccurate,
    ],
)
def test_raw_quality_original_values_uint64_and_source_times_survive_delivery(code):
    p = policies()
    e = FieldDelayEngine(p)
    normal = prepare(e, p, frames=raw(code=code))
    publish(e, normal)
    configured = tuple(
        DelayPolicy(
            group_id=s.policy.group_id,
            revision=1,
            mode="DELAY",
            delay_ms=6000,
            normal_checkpoint=s.last_normal_publication,
        )
        if s.policy.group_id == "Sealer"
        else s.policy
        for s in e.runtime
    )
    origin_raw = raw(seconds=-100, hb=2**64 - 1, code=code)
    publish(e, prepare(e, configured, frames=origin_raw))
    due = prepare(e, configured, 6)
    assert output(due).fields == next(r.fields for r in origin_raw if r.group_id == "Sealer")
    assert (
        next(f.reading.value for f in output(due).fields if f.tag_id.endswith("LinkHeartbeat"))
        == 2**64 - 1
    )
    assert all(
        f.status_code == code and f.source_timestamp == NOW - timedelta(seconds=100)
        for f in output(due).fields
    )
    assert all(
        f.source_timestamp == NOW + timedelta(seconds=6) for f in output(due, "Infeed").fields
    )


def test_normal_flush_and_stale_delayed_plan_cannot_touch_pointer():
    e, p, _ = activate()
    due = prepare(e, p, 6)
    normal = tuple(
        DelayPolicy(group_id=v.group_id, revision=2) if v.group_id == "Sealer" else v for v in p
    )
    fresh = prepare(
        e, normal, 5, frames=raw(5, hb=555, code=ua.StatusCodes.UncertainSensorNotAccurate)
    )
    assert output(fresh).mode == "NORMAL" and not output(fresh).read_in_flight
    publish(e, fresh)
    before = e.runtime
    called = []
    with pytest.raises(DelayConflict):
        e.publish(due, lambda: called.append(True))
    assert not called and e.runtime == before and e.pending_count == 0
    assert all(
        f.status_code == ua.StatusCodes.UncertainSensorNotAccurate for f in output(fresh).fields
    )


def test_restart_discards_queue_and_has_waiting_until_new_deadline():
    e, p, _ = activate()
    publish(e, prepare(e, p, 6))
    old = prepare(e, p, 12)
    restarted = FieldDelayEngine(p)
    assert restarted.pending_count == 0
    with pytest.raises(DelayConflict):
        restarted.publish(old, lambda: None)
    initial = prepare(restarted, p, 100, frames=raw(100, hb=100))
    assert output(initial).waiting_for_first_completion and output(initial).origin.bootstrap
    publish(restarted, initial)
    fresh = prepare(restarted, p, 106)
    assert not output(fresh).waiting_for_first_completion
    assert output(fresh).origin.acquired_at == NOW + timedelta(seconds=100)
    assert (
        next(f.reading.value for f in output(fresh).fields if f.tag_id.endswith("LinkHeartbeat"))
        == 100
    )
    normal = tuple(
        DelayPolicy(group_id=v.group_id, revision=2) if v.group_id == "Sealer" else v for v in p
    )
    unpublished = prepare(restarted, normal, 106)
    stale_checkpoint = output(unpublished).normal_checkpoint
    next_delay = tuple(
        DelayPolicy(
            group_id=v.group_id,
            revision=3,
            mode="DELAY",
            delay_ms=6000,
            normal_checkpoint=stale_checkpoint,
        )
        if v.group_id == "Sealer"
        else v
        for v in normal
    )
    with pytest.raises(DelayConflict):
        prepare(restarted, next_delay, 106)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True, "1", 10**400])
def test_clock_rejected_without_runtime_mutation(bad):
    e, p, _ = activate()
    before = e.runtime
    with pytest.raises(ValueError):
        e.prepare(policies=p, raw_frames=raw(), monotonic_now=bad, published_at=NOW)
    assert e.runtime == before


def test_monotonic_frontier_only_advances_on_adoption():
    e, p, _ = activate()
    candidate = prepare(e, p, 20)
    assert e.runtime[1].pending.deadline_mono == 6
    smaller = prepare(e, p, 1)
    publish(e, smaller)
    with pytest.raises(ValueError):
        prepare(e, p, 0.9)
    with pytest.raises(DelayConflict):
        publish(e, candidate)


def test_duplicate_and_competing_plan_and_foreign_owner_reject_before_swap():
    p = policies()
    e = FieldDelayEngine(p)
    a = prepare(e, p)
    b = prepare(e, p)
    publish(e, a)
    before = e.runtime
    called = []
    for stale in (a, b, prepare(FieldDelayEngine(p), p)):
        with pytest.raises(DelayConflict):
            e.publish(stale, lambda: called.append(True))
    assert not called and e.runtime == before


def test_prepare_failure_and_callback_failure_leave_state_unchanged():
    e, p, _ = activate()
    before = e.runtime
    with pytest.raises(ValueError):
        e.prepare(policies=p, raw_frames=raw()[:2], monotonic_now=1, published_at=NOW)
    assert e.runtime == before
    plan = prepare(e, p, 6)

    def fail_before_pointer():
        raise RuntimeError("encoding/assignment failed before pointer changed")

    with pytest.raises(RuntimeError):
        e.publish(plan, fail_before_pointer)
    assert e.runtime == before
    publish(e, plan)


def test_callback_reentry_and_async_callback_are_rejected():
    p = policies()
    e = FieldDelayEngine(p)
    plan = prepare(e, p)
    before = e.runtime

    def reenter():
        prepare(e, p)

    with pytest.raises(DelayConflict):
        e.publish(plan, reenter)
    assert e.runtime == before

    async def async_swap():
        pass

    with pytest.raises(TypeError):
        e.publish(plan, async_swap)

    class AsyncSwap:
        async def __call__(self):
            pass

    with pytest.raises(TypeError):
        e.publish(plan, AsyncSwap())
    assert e.runtime == before
    publish(e, plan)


@pytest.mark.parametrize("duration", [True, 0, 99, 101, 10001, 1.0, "1000"])
def test_duration_is_only_bounded_integer(duration):
    p = policies()
    e = FieldDelayEngine(p)
    normal = output(prepare(e, p)).normal_checkpoint
    with pytest.raises(ValueError):
        DelayPolicy(
            group_id="Sealer", revision=1, mode="DELAY", delay_ms=duration, normal_checkpoint=normal
        )


def test_direct_duration_revision_change_rejected():
    e, p, _ = activate()
    changed = tuple(
        v.model_copy(
            update={
                "revision": 2,
                "delay_ms": 3000,
                "normal_checkpoint": v.normal_checkpoint.model_copy(update={"revision": 1}),
            }
        )
        if v.group_id == "Sealer"
        else v
        for v in p
    )
    with pytest.raises(DelayConflict):
        prepare(e, changed)


def test_unrepresentable_deadline_cannot_complete_immediately():
    e, p, _ = activate()
    restarted = FieldDelayEngine(p)
    before = restarted.runtime
    with pytest.raises(ValueError):
        restarted.prepare(policies=p, raw_frames=raw(), monotonic_now=1e308, published_at=NOW)
    assert restarted.runtime == before


def test_typed_frame_and_group_integrity_revalidated_before_plan():
    p = policies()
    e = FieldDelayEngine(p)
    before = e.runtime
    invalid = raw()[0].model_copy(update={"fields": raw()[0].fields[:-1]})
    with pytest.raises(ValueError):
        prepare(e, p, frames=(invalid, *raw()[1:]))
    with pytest.raises(ValueError):
        prepare(e, p, frames=(raw()[0], raw()[0], raw()[2]))
    assert e.runtime == before


def test_independent_group_deadlines_and_one_atomic_full_map_boundary():
    p = policies()
    engine = FieldDelayEngine(p)
    publish(engine, prepare(engine, p))
    durations = dict(zip(GROUPS, (100, 3000, 6000), strict=True))
    delayed = tuple(
        DelayPolicy(
            group_id=s.policy.group_id,
            revision=1,
            mode="DELAY",
            delay_ms=durations[s.policy.group_id],
            normal_checkpoint=s.last_normal_publication,
        )
        for s in engine.runtime
    )
    publish(engine, prepare(engine, delayed))
    plan = prepare(engine, delayed, 3)
    assert not output(plan, "Infeed").waiting_for_first_completion
    assert not output(plan, "Sealer").waiting_for_first_completion
    assert output(plan, "Outfeed").waiting_for_first_completion
    old = engine.runtime
    values = project_tags(PLCState(), None, counter_epoch="epoch", heartbeat=300)
    new_map = dict(values)
    for publication in plan.publications:
        new_map.update({f.tag_id: f for f in publication.fields})
    assert len(new_map) == 48
    snapshot = [object()]
    previous = snapshot[0]

    def pointer_swap_only():
        snapshot[0] = new_map

    assert engine.runtime is old and snapshot[0] is previous
    engine.publish(plan, pointer_swap_only)
    assert snapshot[0] is new_map and engine.runtime is not old
    assert snapshot[0]["Line.Heartbeat"] == 300
    assert engine.pending_count == 3
    assert all(s.pending.started_mono == 3 for s in engine.runtime if s.delivered is not None)
    assert (
        next(s for s in engine.runtime if s.policy.group_id == "Outfeed").pending.started_mono == 0
    )


def test_mixed_quality_unchanged_values_get_new_internal_acquisition_ids_per_read():
    engine, p, _ = activate()
    original = raw()
    sealer = original[1]
    codes = (
        ua.StatusCodes.Good,
        ua.StatusCodes.BadSensorFailure,
        ua.StatusCodes.UncertainSensorNotAccurate,
    )
    mixed = sealer.model_copy(
        update={
            "fields": tuple(
                f.model_copy(
                    update={
                        "status_code": codes[i % 3],
                        "source_timestamp": NOW - timedelta(seconds=i),
                    }
                )
                for i, f in enumerate(sealer.fields)
            )
        }
    )
    # Capture the same immutable values on the next read; acquisition IDs still differ.
    frames = (original[0], mixed, original[2])
    publish(engine, prepare(engine, p, 6, frames))
    second = prepare(engine, p, 12, frames)
    acquired = output(second).origin
    assert acquired.fields == mixed.fields
    publish(engine, second)
    third = prepare(engine, p, 18, frames)
    assert output(third).origin.fields == mixed.fields
    assert output(third).origin.acquisition_frame_id != acquired.acquisition_frame_id
    # These IDs never stand in for gateway TagSample.sample_id or a gateway capture ID.
    assert acquired.acquisition_frame_id.startswith("field-acquisition:")


def test_empty_prepare_failure_after_other_group_candidate_does_not_partially_apply():
    engine, p, _ = activate()
    before = engine.runtime
    broken = list(raw())
    broken[2] = broken[2].model_copy(update={"fields": broken[2].fields[:1]})
    with pytest.raises(ValueError):
        prepare(engine, p, 6, broken)
    assert engine.runtime is before
    assert engine.runtime[1].delivered is None
