"""D3 actual SQLite + encrypted OPC publication; no HTTP/UI/browser or PostgreSQL."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from asyncua import ua

from apps.gateway.opcua_client import GatewayOPCUAClient
from apps.simulator.controller import SimulatorController
from apps.simulator.field_channels import (
    FieldChannels,
    FieldFaultConflict,
    FieldFaultInput,
    PublishedField,
)
from apps.simulator.field_delay import DelayConflict
from apps.simulator.field_publication import FieldPublication, get_publication_runtime
from apps.simulator.opcua_server import NAMESPACE_URI
from apps.simulator.plc import PLCState
from apps.simulator.storage import SimulatorStorage
from apps.simulator.tags import project_tags
from contracts.models import ReadEventsRequest
from contracts.tags.validate import load_contract
from tests.integration.test_gateway_opcua import Clock, command, config_for
from tests.integration.test_simulator_opcua import client_for
from tests.integration.test_simulator_opcua import runtime as runtime

NOW = datetime(2026, 10, 21, tzinfo=UTC)
TAGS = load_contract()["tags"]
MODES = ("NORMAL", "FROZEN", "BAD", "UNCERTAIN", "READ_FAILURE", "LINK_DOWN", "DELAY")


def values(hb=1):
    return project_tags(PLCState(), None, counter_epoch="epoch", heartbeat=hb)


def change(store, mode, delay=6000):
    state = next(s for s in store.states() if s.group_id == "Sealer")
    return store.set_fault(
        "Sealer",
        FieldFaultInput(
            mode=mode, expected_revision=state.revision, delay_ms=delay if mode == "DELAY" else None
        ),
        actor="operator",
        at=NOW,
    )


@pytest.fixture
def case(tmp_path):
    with SimulatorStorage(tmp_path / "d3.sqlite") as storage:
        SimulatorController(storage, now=NOW)
        store = FieldChannels(storage)
        mono = [0.0]
        adapter = FieldPublication(store, monotonic_clock=lambda: mono[0])
        yield SimpleNamespace(store=store, adapter=adapter, mono=mono, pointer=[None])


def prepared(c, hb=1, status=0):
    return c.adapter.prepare(
        values(hb),
        acquired_at=NOW + timedelta(seconds=c.mono[0]),
        published_at=NOW + timedelta(seconds=c.mono[0]),
        status_code=status,
    )


def adopt(c, plan):
    frames = plan.frames
    snapshot = plan.fields

    def swap():
        c.pointer[0] = snapshot
        c.store._published = frames
        c.store._field_publication_runtime = plan.runtime_status

    c.adapter.publish(plan, swap)


@pytest.mark.parametrize("mode", MODES[:-1])
@pytest.mark.parametrize(
    "raw", [0, ua.StatusCodes.BadSensorFailure, ua.StatusCodes.UncertainSensorNotAccurate]
)
def test_all_legacy_modes_match_store_overlay_and_only_normal_certifies(case, mode, raw):
    c = case
    adopt(c, prepared(c))
    if mode != "NORMAL":
        change(c.store, mode)
    c.mono[0] = 1
    expected, _ = c.store.prepare(
        values(2),
        acquired_at=NOW + timedelta(seconds=1),
        published_at=NOW + timedelta(seconds=1),
        status_code=raw,
    )
    plan = prepared(c, 2, raw)
    assert dict(plan.fields) == expected
    adopt(c, plan)
    sealer = next(s for s in c.adapter._engine.runtime if s.policy.group_id == "Sealer")
    assert (sealer.last_normal_publication is not None) == (mode == "NORMAL")
    assert c.store.describe()[2].published.mode == mode


def test_revision_jumps_pass_through_and_no_fake_normal_checkpoint(case):
    c = case
    adopt(c, prepared(c))
    change(c.store, "BAD")
    change(c.store, "UNCERTAIN")
    change(c.store, "NORMAL")
    with pytest.raises(FieldFaultConflict):
        change(c.store, "DELAY")
    adopt(c, prepared(c))
    assert c.store.describe()[2].published.revision == 3
    change(c.store, "DELAY")
    adopt(c, prepared(c, 2))
    assert c.adapter._engine.pending_count == 1
    change(c.store, "BAD")
    change(c.store, "UNCERTAIN")
    adopt(c, prepared(c, 3))
    assert c.adapter._engine.pending_count == 0
    assert (
        next(
            s for s in c.adapter._engine.runtime if s.policy.group_id == "Sealer"
        ).last_normal_publication
        is None
    )


def test_prepare_discard_stale_config_and_duplicate_swap_fail_before_pointer(case):
    c = case
    plan = prepared(c)
    with pytest.raises(FieldFaultConflict):
        change(c.store, "DELAY")
    adopt(c, plan)
    change(c.store, "DELAY")
    plan = prepared(c, 2)
    before = c.store._published
    runtime = c.adapter._engine.runtime
    pointer = c.pointer[0]
    change(c.store, "NORMAL")
    with pytest.raises(DelayConflict):
        adopt(c, plan)
    assert (
        c.store._published is before
        and c.pointer[0] is pointer
        and c.adapter._engine.runtime is runtime
    )
    fresh = prepared(c, 3)
    adopt(c, fresh)
    with pytest.raises(DelayConflict):
        adopt(c, fresh)


def test_mixed_raw_quality_delay_delivery_and_restart_waiting(case):
    c = case
    at = NOW
    fields = {
        tag["logical_name"]: PublishedField(
            tag_id=tag["logical_name"],
            reading={
                "data_type": tag["data_type"],
                "value": values(2**64 - 1)[tag["logical_name"]],
            },
            status_code=(
                0,
                ua.StatusCodes.BadSensorFailure,
                ua.StatusCodes.UncertainSensorNotAccurate,
            )[i % 3],
            source_timestamp=at - timedelta(seconds=i),
        )
        for i, tag in enumerate(TAGS)
    }
    plan = c.adapter.prepare_fields(fields, acquired_at=NOW, published_at=NOW)
    adopt(c, plan)
    state = change(c.store, "DELAY")
    assert len({f.status_code for f in state.normal_checkpoint.fields}) == 3
    plan = c.adapter.prepare_fields(fields, acquired_at=NOW, published_at=NOW)
    adopt(c, plan)
    c.mono[0] = 6
    due = prepared(c, 5)
    assert all(due.fields[f.tag_id] == f for f in state.normal_checkpoint.fields)
    adopt(c, due)
    restarted = FieldPublication(c.store, monotonic_clock=lambda: c.mono[0])
    plan = restarted.prepare(
        values(99),
        acquired_at=NOW + timedelta(seconds=6),
        published_at=NOW + timedelta(seconds=6),
        status_code=0,
    )
    assert all(
        f.status_code == ua.StatusCodes.BadWaitingForInitialData
        for f in plan.frames["Sealer"].fields
    )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True])
def test_server_clock_fault_stays_closed_until_new_adapter(case, bad):
    c = case
    adopt(c, prepared(c))
    before = c.store._published
    c.mono[0] = bad
    with pytest.raises(ValueError):
        c.adapter.prepare(values(), acquired_at=NOW, published_at=NOW, status_code=0)
    c.mono[0] = 10
    with pytest.raises(ValueError):
        prepared(c)
    assert c.store._published is before


async def test_real_tls_delay_deadlines_unique_gateway_samples_and_fresh_normal(runtime):
    server, directory = runtime
    store = FieldChannels(server.controller.storage)
    server.field_channels = store
    mono = [0.0]
    server._field_monotonic_clock = lambda: mono[0]
    gateway_clock = Clock()
    gateway = GatewayOPCUAClient(
        config_for(server, directory),
        utc_clock=gateway_clock.utc,
        monotonic_clock=gateway_clock.monotonic,
    )
    source = datetime.now(UTC)
    await server.publish_snapshot(acquired_at=source, heartbeat=2**64 - 1)
    try:
        first = await gateway.read_tags()
        assert first.health.state == "GOOD"
        change(store, "DELAY")
        await server.publish_snapshot(acquired_at=source, heartbeat=2**64 - 2)
        for seconds, expected in [(3, "DELAYED"), (5, "ALARM")]:
            mono[0] = gateway_clock.elapsed = seconds
            await server.publish_snapshot(
                acquired_at=source + timedelta(seconds=seconds), heartbeat=seconds
            )
            read = await gateway.read_tags()
            assert (
                read.health.state == expected
                and read.health.connected
                and read.health.read_age_seconds == 0
            )
            assert all(
                s.status_code == ua.StatusCodes.BadWaitingForInitialData
                for s in read.samples
                if s.tag_id.startswith("Sealer.")
            )
        mono[0] = gateway_clock.elapsed = 6
        await server.publish_snapshot(acquired_at=source + timedelta(seconds=6), heartbeat=6)
        complete = await gateway.read_tags()
        sealer = {s.tag_id: s for s in complete.samples if s.tag_id.startswith("Sealer.")}
        assert complete.health.state == "GOOD"
        assert sealer["Sealer.LinkHeartbeat"].reading.value == 2**64 - 2
        assert all(s.source_timestamp == source and s.status_code == 0 for s in sealer.values())
        again = await gateway.read_tags()
        assert {s.sample_id for s in again.samples}.isdisjoint(
            s.sample_id for s in complete.samples
        )
        assert (
            next(s for s in again.samples if s.tag_id == "Sealer.LinkHeartbeat").reading
            == sealer["Sealer.LinkHeartbeat"].reading
        )
        identity = server.controller.storage.identity
        assert (
            await gateway.read_events(
                ReadEventsRequest(
                    source_id=identity.source_id, stream_id=identity.stream_id, after_seq=0, limit=1
                )
            )
        ).source_id == identity.source_id
        change(store, "NORMAL")
        mono[0] = gateway_clock.elapsed = 6.1
        await server.publish_snapshot(acquired_at=source + timedelta(seconds=7), heartbeat=7)
        fresh = await gateway.read_tags()
        assert all(
            s.source_timestamp == source + timedelta(seconds=7) and s.status_code == 0
            for s in fresh.samples
        )
        assert server._field_publication._engine.pending_count == 0
    finally:
        await gateway.disconnect()


async def test_late_encoding_failure_retains_pointer_store_proof_and_queue(runtime, monkeypatch):
    import apps.simulator.opcua_server as module

    server, _ = runtime
    store = FieldChannels(server.controller.storage)
    server.field_channels = store
    mono = [0.0]
    server._field_monotonic_clock = lambda: mono[0]
    await server.publish_snapshot(acquired_at=NOW, heartbeat=1)
    change(store, "DELAY")
    await server.publish_snapshot(acquired_at=NOW, heartbeat=2)
    before = server._snapshot
    observed = store._published
    queue = server._field_publication._engine.runtime
    variant = module.ua.Variant
    calls = []

    def broken(*args, **kwargs):
        calls.append(True)
        if len(calls) == 47:
            raise RuntimeError("late encoding failure")
        return variant(*args, **kwargs)

    mono[0] = 6
    monkeypatch.setattr(module.ua, "Variant", broken)
    with pytest.raises(RuntimeError):
        await server.publish_snapshot(acquired_at=NOW + timedelta(seconds=6), heartbeat=6)
    assert len(calls) == 47 and server._snapshot is before and store._published is observed
    assert server._field_publication._engine.runtime is queue
    monkeypatch.setattr(module.ua, "Variant", variant)
    mono[0] = 5
    with pytest.raises(ValueError):
        await server.publish_snapshot(acquired_at=NOW + timedelta(seconds=5), heartbeat=5)
    assert server._snapshot is before and store._published is observed
    mono[0] = 7
    with pytest.raises(ValueError):
        await server.publish_snapshot(acquired_at=NOW + timedelta(seconds=7), heartbeat=7)


def test_runtime_view_is_readonly_and_clock_latch_rejects_previously_prepared_plan(case):
    c = case
    assert get_publication_runtime(c.store) is None
    adopt(c, prepared(c))
    first = get_publication_runtime(c.store)
    assert all(not v.waiting_for_first_completion and not v.read_in_flight for v in first)
    change(c.store, "DELAY")
    plan = prepared(c, 2)
    assert get_publication_runtime(c.store) is first
    adopt(c, plan)
    waiting = get_publication_runtime(c.store)
    sealer = next(v for v in waiting if v.group_id == "Sealer")
    assert sealer.published_mode == "DELAY" and sealer.published_revision == 1
    assert sealer.waiting_for_first_completion and sealer.read_in_flight
    assert not c.store.describe()[2].pending
    c.mono[0] = 6
    valid = prepared(c, 6)
    c.mono[0] = 5
    with pytest.raises(ValueError):
        prepared(c, 5)
    before = c.pointer[0]
    queue = c.adapter._engine.runtime
    with pytest.raises(ValueError):
        adopt(c, valid)
    assert c.pointer[0] is before and c.adapter._engine.runtime is queue
    assert get_publication_runtime(c.store) is waiting
    assert get_publication_runtime(c.store) is waiting


async def test_actual_running_ticks_and_production_continue_during_delayed_field(runtime):
    server, directory = runtime
    controller = server.controller
    store = FieldChannels(controller.storage)
    server.field_channels = store
    mono = [0.0]
    server._field_monotonic_clock = lambda: mono[0]
    load = command(
        controller,
        "LOAD_JOB",
        "delay-load",
        work_order_id="delay-work",
        recipe_version="recipe",
        target_quantity=100,
        parameters={"temperature_c": 150.0, "speed_m_per_min": 6.0},
    )
    assert controller.submit(load, now=datetime.now(UTC)).submission.disposition == "ACCEPTED"
    controller.tick(now=datetime.now(UTC))
    assert (
        controller.submit(
            command(controller, "START", "delay-start"), now=datetime.now(UTC)
        ).submission.disposition
        == "ACCEPTED"
    )
    controller.tick(now=datetime.now(UTC))
    baseline = controller.storage.load_execution()
    source = datetime.now(UTC)
    await server.publish_snapshot(acquired_at=source, heartbeat=1)
    change(store, "DELAY")
    await server.publish_snapshot(acquired_at=source, heartbeat=2)
    for tick in range(1, 66):
        mono[0] = tick / 10
        controller.tick(now=datetime.now(UTC))
        await server.publish_snapshot(acquired_at=datetime.now(UTC), heartbeat=tick + 2)
        assert controller.state.state.value == "RUNNING" and controller.state.run_output
        assert controller.state.latched_fault_code == 0
    finished = controller.storage.load_execution()
    assert finished.tick - baseline.tick == 65 and finished.good_quantity > baseline.good_quantity
    async with await client_for(server, directory) as client:
        index = await client.get_namespace_index(NAMESPACE_URI)
        nodes = [client.get_node(ua.NodeId(t["logical_name"], index)) for t in TAGS]
        fields = await client.read_attributes(nodes, ua.AttributeIds.Value)
        assert len(fields) == 48 and len({f.ServerTimestamp for f in fields}) == 1
        for tag, field in zip(TAGS, fields, strict=True):
            assert field.Value.VariantType == getattr(ua.VariantType, tag["data_type"])
        sealer = fields[
            next(i for i, t in enumerate(TAGS) if t["logical_name"] == "Sealer.LinkHeartbeat")
        ]
        line = fields[next(i for i, t in enumerate(TAGS) if t["logical_name"] == "Line.Heartbeat")]
        assert sealer.Value.Value == 2 and sealer.SourceTimestamp == source
        assert line.Value.Value == 67
    runtime = next(s for s in get_publication_runtime(store) if s.group_id == "Sealer")
    assert not runtime.waiting_for_first_completion and runtime.read_in_flight


async def test_actual_tls_all_known_modes_pass_through_and_delay_completion(runtime):
    server, directory = runtime
    store = FieldChannels(server.controller.storage)
    server.field_channels = store
    mono = [0.0]
    server._field_monotonic_clock = lambda: mono[0]
    async with await client_for(server, directory) as client:
        index = await client.get_namespace_index(NAMESPACE_URI)
        nodes = [client.get_node(ua.NodeId(t["logical_name"], index)) for t in TAGS]
        for number, mode in enumerate(MODES):
            if store.states()[2].mode != "NORMAL":
                change(store, "NORMAL")
            mono[0] += 1
            source = NOW + timedelta(seconds=mono[0])
            await server.publish_snapshot(acquired_at=source, heartbeat=100 + number)
            if mode != "NORMAL":
                change(store, mode, delay=100)
            await server.publish_snapshot(acquired_at=source, heartbeat=200 + number)
            read = await client.read_attributes(nodes, ua.AttributeIds.Value)
            expected = {
                "NORMAL": 0,
                "FROZEN": 0,
                "BAD": ua.StatusCodes.BadSensorFailure,
                "UNCERTAIN": ua.StatusCodes.UncertainSensorNotAccurate,
                "READ_FAILURE": ua.StatusCodes.BadCommunicationError,
                "LINK_DOWN": ua.StatusCodes.BadNotConnected,
                "DELAY": ua.StatusCodes.BadWaitingForInitialData,
            }[mode]
            assert all(
                v.StatusCode.value == (expected if t["logical_name"].startswith("Sealer.") else 0)
                for t, v in zip(TAGS, read, strict=True)
            )
            view = next(v for v in get_publication_runtime(store) if v.group_id == "Sealer")
            assert view.published_mode == mode and view.waiting_for_first_completion == (
                mode == "DELAY"
            )
            if mode == "DELAY":
                mono[0] += 0.101
                await server.publish_snapshot(
                    acquired_at=source + timedelta(seconds=1), heartbeat=500
                )
                due = await client.read_attributes(nodes, ua.AttributeIds.Value)
                held = due[
                    next(
                        i for i, t in enumerate(TAGS) if t["logical_name"] == "Sealer.LinkHeartbeat"
                    )
                ]
                assert held.Value.Value == 200 + number and held.SourceTimestamp == source
                assert not next(
                    v for v in get_publication_runtime(store) if v.group_id == "Sealer"
                ).waiting_for_first_completion


async def test_same_server_stop_start_retires_delivered_queue_and_old_normal_proof(runtime):
    server, directory = runtime
    store = FieldChannels(server.controller.storage)
    server.field_channels = store
    mono = [0.0]
    server._field_monotonic_clock = lambda: mono[0]
    source = NOW
    await server.publish_snapshot(
        acquired_at=source,
        heartbeat=2**64 - 1,
        status_code=ua.StatusCodes.UncertainSensorNotAccurate,
    )
    state = change(store, "DELAY")
    checkpoint = state.normal_checkpoint
    await server.publish_snapshot(acquired_at=source, heartbeat=2**64 - 2)
    mono[0] = 6
    await server.publish_snapshot(acquired_at=source + timedelta(seconds=6), heartbeat=6)
    assert not next(
        v for v in get_publication_runtime(store) if v.group_id == "Sealer"
    ).waiting_for_first_completion
    old_adapter = server._field_publication
    await server.stop()
    # Retired transport cannot certify NORMAL or claim an in-flight read is still live.
    assert get_publication_runtime(store) is None and not store._published
    mono[0] = 7
    await server.start()
    assert server._field_publication is not old_adapter
    assert next(s for s in store.states() if s.group_id == "Sealer").normal_checkpoint == checkpoint
    async with await client_for(server, directory) as client:
        index = await client.get_namespace_index(NAMESPACE_URI)
        node = client.get_node(ua.NodeId("Sealer.LinkHeartbeat", index))
        waiting = await node.read_data_value(raise_on_bad_status=False)
        assert waiting.StatusCode.value == ua.StatusCodes.BadWaitingForInitialData
        assert waiting.Value.Value == 2**64 - 1 and waiting.SourceTimestamp == source
        assert all(
            f.status_code == ua.StatusCodes.UncertainSensorNotAccurate for f in checkpoint.fields
        )
        view = next(v for v in get_publication_runtime(store) if v.group_id == "Sealer")
        assert view.waiting_for_first_completion and view.read_in_flight
        mono[0] = 12.999
        await server.publish_snapshot(acquired_at=source + timedelta(seconds=13), heartbeat=13)
        assert (
            await node.read_data_value(raise_on_bad_status=False)
        ).StatusCode.value == ua.StatusCodes.BadWaitingForInitialData
        mono[0] = 13
        await server.publish_snapshot(acquired_at=source + timedelta(seconds=14), heartbeat=14)
        due = await node.read_data_value()
        assert (
            due.Value.Value == 0
        )  # New start's initial virtual acquisition, not old delivered HB.
        assert due.SourceTimestamp != source
    change(store, "NORMAL")
    await server.publish_snapshot(acquired_at=source + timedelta(seconds=15), heartbeat=15)
    await server.stop()
    with pytest.raises(FieldFaultConflict):
        change(store, "DELAY")
    await server.start()


async def test_failed_listener_start_clears_prelisten_normal_proof_and_runtime(
    runtime, monkeypatch
):
    import apps.simulator.opcua_server as module

    server, _ = runtime
    store = FieldChannels(server.controller.storage)
    server.field_channels = store
    await server.publish_snapshot(acquired_at=NOW, heartbeat=1)
    await server.stop()
    original_start = module.Server.start
    observed = []

    async def fail_after_prelisten_publication(inner):
        observed.append(get_publication_runtime(store))
        assert store._published and get_publication_runtime(store) is not None
        raise RuntimeError("listener start failure after initial map preparation")

    monkeypatch.setattr(module.Server, "start", fail_after_prelisten_publication)
    with pytest.raises(RuntimeError):
        await server.start()
    assert observed and server._server is None and server._field_publication is None
    assert get_publication_runtime(store) is None and not store._published
    with pytest.raises(FieldFaultConflict):
        change(store, "DELAY")
    monkeypatch.setattr(module.Server, "start", original_start)
    await server.start()
    assert store._published and get_publication_runtime(store) is not None


@pytest.mark.parametrize("mode", ["READ_FAILURE", "LINK_DOWN"])
def test_live_v3_failure_noop_and_config_rollback_keep_proof_and_audit(case, mode):
    c = case
    adopt(c, prepared(c))
    state = change(c.store, mode)
    adopt(c, prepared(c, 2))
    original = c.store._published
    queue = c.adapter._engine.runtime
    rows = [
        tuple(r)
        for r in c.store.storage.connection.execute("SELECT * FROM main.field_channel_audit")
    ]
    assert change(c.store, mode) == state
    assert [
        tuple(r)
        for r in c.store.storage.connection.execute("SELECT * FROM main.field_channel_audit")
    ] == rows

    def fail():
        raise RuntimeError("current V3 config commit failure")

    c.store._before_fault_commit = fail
    with pytest.raises(RuntimeError):
        change(c.store, "NORMAL")
    assert c.store._published is original and c.adapter._engine.runtime is queue
    assert next(s for s in c.store.states() if s.group_id == "Sealer") == state
    assert [
        tuple(r)
        for r in c.store.storage.connection.execute("SELECT * FROM main.field_channel_audit")
    ] == rows


@pytest.mark.parametrize("before_version,after_version", [(2, 1), (3, 1), (3, 2)])
def test_live_v3_parser_rejects_valid_payloads_in_downgrade_audit_order(
    case, before_version, after_version
):
    from apps.simulator import _field_channels_v1 as legacy1
    from apps.simulator import _field_channels_v2 as legacy2
    from apps.simulator import field_channels as current

    modules = {1: legacy1, 2: legacy2, 3: current}
    before = modules[before_version].FieldFaultState(group_id="Sealer").model_dump_json()
    after = (
        modules[after_version]
        .FieldFaultState(group_id="Sealer", revision=1, mode="BAD")
        .model_dump_json()
    )
    with pytest.raises(ValueError):
        case.store._audit_states(
            (
                "Sealer",
                "00000000000000000001",
                "operator",
                NOW.isoformat(),
                before,
                after,
                before_version,
                after_version,
            )
        )
