"""Actual SQLite field publication configuration and encrypted 48-tag overlays.

Pure prepare/published helpers model the publication boundary; socket tests below
exercise real OPC encoding, group isolation and the unchanged physical controller.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from asyncua import ua

from apps.gateway.opcua_client import GatewayOPCUAClient
from apps.simulator.console import create_app
from apps.simulator.field_channels import FieldChannels, FieldFaultConflict, FieldFaultInput
from apps.simulator.opcua_server import NAMESPACE_URI
from apps.simulator.plc import PLCState
from apps.simulator.storage import SimulatorStorage, StorageCorruption, encode_uint64
from apps.simulator.tags import project_tags
from contracts.models import ReadEventsRequest
from tests.integration.test_gateway_opcua import Clock, command, config_for
from tests.integration.test_simulator_console import ORIGIN, post, sign_in
from tests.integration.test_simulator_console import environment as environment
from tests.integration.test_simulator_opcua import TAG_CONTRACT, client_for
from tests.integration.test_simulator_opcua import runtime as runtime

NOW = datetime(2026, 10, 21, tzinfo=UTC)


def mutate(component, mode, revision=0, group="Sealer"):
    return component.set_fault(
        group,
        FieldFaultInput(mode=mode, expected_revision=revision),
        actor="field-operator",
        at=NOW,
    )


def prepare(component, heartbeat=2**53 + 1, status=ua.StatusCodes.Good, at=NOW):
    values = project_tags(PLCState(), None, counter_epoch="field-epoch", heartbeat=heartbeat)
    return component.prepare(values, acquired_at=at, published_at=at, status_code=status)


@pytest.fixture
def store(tmp_path):
    with SimulatorStorage(tmp_path / "field.sqlite") as storage:
        yield storage


def test_fresh_initialization_and_repeat(store):
    a = FieldChannels(store)
    b = FieldChannels(store)
    assert a.states() == b.states()
    assert len(a.states()) == 3
    assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 3


def test_component_init_rollback(store):
    class Broken(FieldChannels):
        def _before_initialize_commit(self):
            raise RuntimeError("injected field failure")

    with pytest.raises(RuntimeError):
        Broken(store)
    assert (
        store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'field_channel_%'"
        ).fetchone()[0]
        == 0
    )
    FieldChannels(store)


def test_config_audit_cas_and_failure_rollback(store):
    component = FieldChannels(store)
    fields, frames = prepare(component)
    component.published(frames)
    baseline = component.describe()

    def fail():
        raise RuntimeError("injected field failure")

    component._before_fault_commit = fail
    with pytest.raises(RuntimeError):
        mutate(component, "BAD")
    assert component.describe() == baseline
    assert store.connection.execute("SELECT COUNT(*) FROM field_channel_audit").fetchone()[0] == 0
    component._before_fault_commit = lambda: None
    mutate(component, "BAD")
    with pytest.raises(FieldFaultConflict):
        mutate(component, "UNCERTAIN")
    assert store.connection.execute("SELECT COUNT(*) FROM field_channel_audit").fetchone()[0] == 1
    assert component.describe()[2].configured.mode == "BAD"


def test_outer_rollback_and_no_publish_uncommitted(store):
    component = FieldChannels(store)
    with pytest.raises(RuntimeError):
        with store.transaction():
            mutate(component, "BAD")
            with pytest.raises(FieldFaultConflict):
                prepare(component)
            raise RuntimeError("rollback outer")
    assert all(s.revision == 0 for s in component.states())
    assert store.connection.execute("SELECT COUNT(*) FROM field_channel_audit").fetchone()[0] == 0


def test_frozen_preserves_exact_published_group_and_other_groups(store):
    component = FieldChannels(store)
    _, original = prepare(component)
    component.published(original)
    mutate(component, "FROZEN")
    fields, frames = prepare(component, heartbeat=2**53 + 2, at=NOW + timedelta(seconds=1))
    assert frames["Sealer"].fields == original["Sealer"].fields
    assert (
        next(v for v in frames["Sealer"].fields if v.tag_id == "Sealer.LinkHeartbeat").reading.value
        == 2**53 + 1
    )
    for group in ("Infeed", "Outfeed"):
        assert (
            next(
                v for v in frames[group].fields if v.tag_id == group + ".LinkHeartbeat"
            ).reading.value
            == 2**53 + 2
        )
    assert len(fields) == 48
    assert all(f.source_timestamp == NOW for f in frames["Sealer"].fields)
    assert component.describe()[2].pending
    component.published(frames)
    assert not component.describe()[2].pending


@pytest.mark.parametrize(
    "status", [ua.StatusCodes.BadSensorFailure, ua.StatusCodes.UncertainSensorNotAccurate]
)
def test_frozen_rejects_prior_bad_uncertain(store, status):
    component = FieldChannels(store)
    _, frames = prepare(component, status=status)
    component.published(frames)
    with pytest.raises(FieldFaultConflict):
        mutate(component, "FROZEN")


def test_restart_persists_fault_without_publication(tmp_path):
    path = tmp_path / "restart.sqlite"
    with SimulatorStorage(path) as store:
        component = FieldChannels(store)
        _, frames = prepare(component)
        component.published(frames)
        mutate(component, "FROZEN")
    with SimulatorStorage(path) as store:
        component = FieldChannels(store)
        assert next(s for s in component.states() if s.group_id == "Sealer").mode == "FROZEN"
        assert all(s.published is None and s.pending for s in component.describe())
        assert store.recovery_required


@pytest.mark.parametrize("damage", ["partial", "unknown_version", "malformed_json"])
def test_existing_component_rejects_damage(store, damage):
    FieldChannels(store)
    if damage == "partial":
        store.connection.execute("DROP TABLE field_channel_audit")
    elif damage == "unknown_version":
        store.connection.execute("PRAGMA ignore_check_constraints=ON")
        store.connection.execute("UPDATE field_channel_schema SET version=9")
    else:
        store.connection.execute(
            "UPDATE field_channel_faults SET state_json='{}' WHERE group_id='Sealer'"
        )
    with pytest.raises(StorageCorruption):
        FieldChannels(store)


def test_live_component_deletion_cannot_bootstrap_normal_state(store):
    component = FieldChannels(store)
    mutate(component, "BAD")
    for table in ("field_channel_audit", "field_channel_faults", "field_channel_schema"):
        store.connection.execute(f"DROP TABLE {table}")
    with pytest.raises(StorageCorruption):
        component.states()
    assert (
        store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name GLOB 'field_channel_*'"
        ).fetchone()[0]
        == 0
    )


def test_failed_live_history_check_cannot_cache_corrupt_schema(store):
    component = FieldChannels(store)
    mutate(component, "BAD")
    mutate(component, "NORMAL", 1)
    store.connection.execute("DROP TRIGGER field_channel_audit_update")
    store.connection.execute(
        "UPDATE field_channel_audit SET before_json='{}' WHERE revision=?", (encode_uint64(1),)
    )
    store.connection.execute(
        "CREATE TRIGGER field_channel_audit_update BEFORE UPDATE ON field_channel_audit "
        "BEGIN SELECT RAISE(ABORT,'Field fault audit is append only'); END"
    )
    for _ in range(2):
        with pytest.raises(StorageCorruption):
            component.states()
    with pytest.raises(StorageCorruption):
        prepare(component)


@pytest.mark.parametrize(
    "damage", ["noop_trigger", "unknown_table", "malformed_old_audit", "orphan_future_audit"]
)
def test_schema_and_history_fail_closed(store, damage):
    component = FieldChannels(store)
    if damage == "noop_trigger":
        store.connection.execute("DROP TRIGGER field_channel_audit_delete")
        store.connection.execute(
            "CREATE TRIGGER field_channel_audit_delete BEFORE DELETE ON field_channel_audit "
            "BEGIN SELECT 1; END"
        )
    elif damage == "unknown_table":
        store.connection.execute("CREATE TABLE field_channel_unknown(value TEXT)")
    elif damage == "malformed_old_audit":
        mutate(component, "BAD")
        mutate(component, "NORMAL", 1)
        store.connection.execute("DROP TRIGGER field_channel_audit_update")
        store.connection.execute(
            "UPDATE field_channel_audit SET before_json='{}' WHERE revision=?", (encode_uint64(1),)
        )
        store.connection.execute(
            "CREATE TRIGGER field_channel_audit_update BEFORE UPDATE ON field_channel_audit "
            "BEGIN SELECT RAISE(ABORT,'Field fault audit is append only'); END"
        )
    else:
        state = next(s for s in component.states() if s.group_id == "Sealer")
        store.connection.execute(
            "INSERT INTO field_channel_audit(group_id,revision,actor,changed_at,"
            "before_json,after_json,before_payload_version,after_payload_version) "
            "VALUES(?,?,?,?,?,?,3,3)",
            (
                "Sealer",
                encode_uint64(3),
                "field-operator",
                NOW.isoformat(),
                state.model_dump_json(),
                state.model_dump_json(),
            ),
        )
    with pytest.raises(StorageCorruption):
        FieldChannels(store)


def test_nonprefix_trigger_cannot_mutate_process_recovery(store):
    FieldChannels(store)
    store.connection.execute(
        "CREATE TRIGGER unexpected_overlay_side_effect AFTER UPDATE ON field_channel_faults "
        "BEGIN UPDATE simulator_identity SET recovery_required=0 WHERE singleton=1; END"
    )
    with pytest.raises(StorageCorruption):
        FieldChannels(store)
    assert store.recovery_required


def test_frozen_audit_requires_matching_previous_normal_provenance(store):
    from apps.simulator.field_channels import FieldFaultState

    component = FieldChannels(store)
    _, frames = prepare(component)
    component.published(frames)
    previous = mutate(component, "BAD")
    forged = FieldFaultState(
        group_id="Sealer",
        revision=2,
        mode="FROZEN",
        frozen_checkpoint=frames["Sealer"].model_copy(update={"revision": 1}),
    )
    with store.transaction():
        store.connection.execute(
            "UPDATE field_channel_faults SET revision=?,state_json=?,state_payload_version=3 "
            "WHERE group_id=?",
            (encode_uint64(2), forged.model_dump_json(), "Sealer"),
        )
        store.connection.execute(
            "INSERT INTO field_channel_audit(group_id,revision,actor,changed_at,"
            "before_json,after_json,before_payload_version,after_payload_version) "
            "VALUES(?,?,?,?,?,?,3,3)",
            (
                "Sealer",
                encode_uint64(2),
                "field-operator",
                NOW.isoformat(),
                previous.model_dump_json(),
                forged.model_dump_json(),
            ),
        )
    with pytest.raises(StorageCorruption):
        FieldChannels(store)


async def test_real_secure_48tag_overlay_group_isolation_and_physical_state(runtime):
    adapter, directory = runtime
    component = FieldChannels(adapter.controller.storage)
    adapter.field_channels = component
    before_state = adapter.controller.state
    before_process = adapter.controller.storage.load_execution()
    before_runtime = adapter.controller.storage.connection.execute(
        "SELECT snapshot_json FROM runtime_state WHERE singleton=1"
    ).fetchone()[0]
    first = datetime.now(UTC)
    await adapter.publish_snapshot(acquired_at=first, heartbeat=2**53 + 1)
    component.set_fault(
        "Sealer",
        FieldFaultInput(mode="FROZEN", expected_revision=0),
        actor="field-operator",
        at=datetime.now(UTC),
    )
    async with await client_for(adapter, directory) as client:
        index = await client.get_namespace_index(NAMESPACE_URI)
        tags = TAG_CONTRACT["tags"]
        names = [t["logical_name"] for t in tags]
        nodes = [client.get_node(ua.NodeId(name, index)) for name in names]
        second = first + timedelta(seconds=1)
        await adapter.publish_snapshot(acquired_at=second, heartbeat=2**53 + 2)
        frozen = await client.read_attributes(nodes, ua.AttributeIds.Value)
        assert len(frozen) == 48 and len({v.ServerTimestamp for v in frozen}) == 1
        for tag, name, value in zip(tags, names, frozen, strict=True):
            assert value.Value.VariantType == getattr(ua.VariantType, tag["data_type"])
            assert value.StatusCode.value == ua.StatusCodes.Good
            assert value.SourceTimestamp == (first if name.startswith("Sealer.") else second)
            if name.endswith("Heartbeat"):
                assert value.Value.Value == (2**53 + 1 if name.startswith("Sealer.") else 2**53 + 2)
        for mode, revision, expected_code in [
            ("NORMAL", 1, ua.StatusCodes.Good),
            ("BAD", 2, ua.StatusCodes.BadSensorFailure),
            ("UNCERTAIN", 3, ua.StatusCodes.UncertainSensorNotAccurate),
        ]:
            component.set_fault(
                "Sealer",
                FieldFaultInput(mode=mode, expected_revision=revision),
                actor="field-operator",
                at=datetime.now(UTC),
            )
            await adapter.publish_snapshot(acquired_at=second, heartbeat=2**53 + 3)
            snapshot = await client.read_attributes(nodes, ua.AttributeIds.Value)
            assert len(snapshot) == 48 and len({v.ServerTimestamp for v in snapshot}) == 1
            for name, value in zip(names, snapshot, strict=True):
                assert value.StatusCode.value == (
                    expected_code if name.startswith("Sealer.") else ua.StatusCodes.Good
                )
                assert value.SourceTimestamp == second
            if mode in ("BAD", "UNCERTAIN"):
                with pytest.raises(FieldFaultConflict):
                    component.set_fault(
                        "Sealer",
                        FieldFaultInput(mode="FROZEN", expected_revision=revision + 1),
                        actor="field-operator",
                        at=datetime.now(UTC),
                    )
    assert adapter.controller.state == before_state
    assert adapter.controller.storage.load_execution() == before_process
    assert (
        adapter.controller.storage.connection.execute(
            "SELECT snapshot_json FROM runtime_state WHERE singleton=1"
        ).fetchone()[0]
        == before_runtime
    )


@pytest.mark.parametrize("mode", ["FROZEN", "READ_FAILURE", "LINK_DOWN"])
async def test_real_gateway_field_freeze_age_3_5_and_normal_recovery_with_live_opc(runtime, mode):
    adapter, directory = runtime
    fields = FieldChannels(adapter.controller.storage)
    adapter.field_channels = fields
    clock = Clock()
    gateway = GatewayOPCUAClient(
        config_for(adapter, directory), utc_clock=clock.utc, monotonic_clock=clock.monotonic
    )
    old = datetime.now(UTC) - timedelta(days=100)
    await adapter.publish_snapshot(acquired_at=old, heartbeat=100)
    try:
        assert (await gateway.read_tags()).health.state == "GOOD"
        fields.set_fault(
            "Sealer", FieldFaultInput(mode=mode, expected_revision=0), actor="operator", at=NOW
        )
        for elapsed, expected in ((3.0, "DELAYED"), (5.0, "ALARM")):
            clock.elapsed = elapsed
            await adapter.publish_snapshot(
                acquired_at=old + timedelta(seconds=elapsed), heartbeat=int(100 + elapsed)
            )
            observation = await gateway.read_tags()
            assert observation.health.state == expected
            assert observation.health.connected and observation.health.read_age_seconds == 0.0
            by_name = {sample.tag_id: sample for sample in observation.samples}
            assert by_name["Sealer.LinkHeartbeat"].reading.value == 100
            assert by_name["Sealer.LinkHeartbeat"].source_timestamp == old
            assert by_name["Line.Heartbeat"].reading.value == 100 + elapsed
            assert len({sample.acquired_at for sample in observation.samples}) == 1
        identity = adapter.controller.storage.identity
        page = await gateway.read_events(
            ReadEventsRequest(
                source_id=identity.source_id, stream_id=identity.stream_id, after_seq=0, limit=1
            )
        )
        assert (
            page.source_id == identity.source_id
        )  # UA methods remain live during a field-only fault.
        fields.set_fault(
            "Sealer", FieldFaultInput(mode="NORMAL", expected_revision=1), actor="operator", at=NOW
        )
        clock.elapsed = 5.1
        await adapter.publish_snapshot(acquired_at=old + timedelta(seconds=6), heartbeat=106)
        recovered = await gateway.read_tags()
        assert recovered.health.state == "GOOD"
        assert all(sample.status_code == 0 for sample in recovered.samples)
    finally:
        await gateway.disconnect()


@pytest.mark.parametrize("mode", ["FROZEN", "READ_FAILURE", "LINK_DOWN"])
async def test_field_overlay_does_not_stop_actual_running_process_or_change_event_identity(
    runtime, mode
):
    adapter, directory = runtime
    controller = adapter.controller
    fields = FieldChannels(controller.storage)
    adapter.field_channels = fields
    load = command(
        controller,
        "LOAD_JOB",
        "field-load",
        work_order_id="field-work",
        recipe_version="r1",
        target_quantity=100,
        parameters={"temperature_c": 150.0, "speed_m_per_min": 6.0},
    )
    assert controller.submit(load, now=datetime.now(UTC)).submission.disposition == "ACCEPTED"
    controller.tick(now=datetime.now(UTC))
    start = command(controller, "START", "field-start")
    assert controller.submit(start, now=datetime.now(UTC)).submission.disposition == "ACCEPTED"
    controller.tick(now=datetime.now(UTC))
    baseline = controller.storage.load_execution()
    await adapter.publish_snapshot(acquired_at=datetime.now(UTC), heartbeat=1)
    frozen = fields.set_fault(
        "Sealer", FieldFaultInput(mode=mode, expected_revision=0), actor="operator", at=NOW
    )
    first = controller.storage.load_execution().tick
    for tick in range(1, 66):
        controller.tick(now=datetime.now(UTC))
        await adapter.publish_snapshot(acquired_at=datetime.now(UTC), heartbeat=tick + 1)
        assert controller.state.state.value == "RUNNING" and controller.state.run_output
        assert controller.state.inputs.run_feedback
    process = controller.storage.load_execution()
    assert process.tick - first == 65 and process.good_quantity > baseline.good_quantity
    assert process.execution_id == baseline.execution_id == "run-gateway"
    assert fields.states()[2] == frozen
    async with await client_for(adapter, directory) as client:
        index = await client.get_namespace_index(NAMESPACE_URI)
        field = await client.get_node(ua.NodeId("Sealer.LinkHeartbeat", index)).read_data_value(
            raise_on_bad_status=False
        )
        line = await client.get_node(ua.NodeId("Line.Heartbeat", index)).read_data_value()
        assert field.Value.Value == 1 and line.Value.Value == 66
        assert (
            field.StatusCode.value
            == {
                "FROZEN": ua.StatusCodes.Good,
                "READ_FAILURE": ua.StatusCodes.BadCommunicationError,
                "LINK_DOWN": ua.StatusCodes.BadNotConnected,
            }[mode]
        )
    for mode, revision in (("BAD", 1), ("UNCERTAIN", 2), ("NORMAL", 3)):
        fields.set_fault(
            "Sealer",
            FieldFaultInput(mode=mode, expected_revision=revision),
            actor="operator",
            at=NOW,
        )
        controller.tick(now=datetime.now(UTC))
        await adapter.publish_snapshot(acquired_at=datetime.now(UTC), heartbeat=100 + revision)
        assert controller.state.state.value == "RUNNING" and controller.state.run_output
        assert controller.state.latched_fault_code == 0


@pytest.fixture
async def console(environment):
    env = environment
    fields = FieldChannels(env.store)
    app = create_app(env.controller, env.auth, env.config, field_channels=fields)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        yield SimpleNamespace(
            fields=fields, client=client, store=env.store, controller=env.controller
        )


@pytest.mark.parametrize("mode", ["BAD", "READ_FAILURE", "LINK_DOWN"])
async def test_field_http_exact_group_cas_actor_and_passive_result_recovery(console, mode):
    env = console
    session = await sign_in(env.client)
    csrf = session.json()["csrf_token"]
    baseline = env.controller.state
    _, frames = prepare(env.fields)
    env.fields.published(frames)
    response = await post(
        env.client, "/field-faults/Sealer", csrf, {"mode": mode, "expected_revision": "0"}
    )
    assert response.status_code == 200 and response.json()["revision"] == "1"
    for _ in range(3):
        observed = (await env.client.get("/field-faults")).json()
        sealer = next(item for item in observed if item["configured"]["group_id"] == "Sealer")
        assert sealer["configured"]["revision"] == "1" and sealer["configured"]["mode"] == mode
        assert sealer["pending"]
    assert (
        env.store.connection.execute("SELECT count(*) FROM field_channel_audit").fetchone()[0] == 1
    )
    assert (
        env.store.connection.execute("SELECT actor FROM field_channel_audit").fetchone()[0]
        == "operator"
    )
    stale = await post(
        env.client, "/field-faults/Sealer", csrf, {"mode": "UNCERTAIN", "expected_revision": "0"}
    )
    assert stale.status_code == 409
    forged = await post(
        env.client,
        "/field-faults/Infeed",
        csrf,
        {"mode": "BAD", "expected_revision": "0", "actor": "other"},
    )
    assert forged.status_code == 422
    assert (
        await post(
            env.client, "/field-faults/Line", csrf, {"mode": "BAD", "expected_revision": "0"}
        )
    ).status_code == 422
    assert all(
        state.mode == "NORMAL" for state in env.fields.states() if state.group_id != "Sealer"
    )
    assert env.controller.state == baseline


@pytest.mark.parametrize("kind", ["viewer", "initial", "csrf", "origin"])
async def test_field_http_permission_and_mutation_protection(console, kind):
    env = console
    session = await sign_in(
        env.client, login="viewer" if kind == "viewer" else "operator", change=kind != "initial"
    )
    csrf = session.json()["csrf_token"]
    response = await post(
        env.client,
        "/field-faults/Sealer",
        "wrong" if kind == "csrf" else csrf,
        {"mode": "BAD", "expected_revision": "0"},
        origin="https://invalid.example" if kind == "origin" else ORIGIN,
    )
    assert response.status_code == 403
    assert all(state.mode == "NORMAL" and state.revision == 0 for state in env.fields.states())
    assert (
        env.store.connection.execute("SELECT count(*) FROM field_channel_audit").fetchone()[0] == 0
    )


async def test_field_http_competing_revision_commits_only_one_group_mutation(console):
    env = console
    csrf = (await sign_in(env.client)).json()["csrf_token"]
    responses = await asyncio.gather(
        *(
            post(env.client, "/field-faults/Sealer", csrf, {"mode": mode, "expected_revision": "0"})
            for mode in ("BAD", "UNCERTAIN")
        )
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert (
        env.store.connection.execute("SELECT count(*) FROM field_channel_audit").fetchone()[0] == 1
    )


@pytest.mark.parametrize(
    "mode,code",
    [
        ("READ_FAILURE", ua.StatusCodes.BadCommunicationError),
        ("LINK_DOWN", ua.StatusCodes.BadNotConnected),
    ],
)
@pytest.mark.parametrize(
    "raw",
    [
        ua.StatusCodes.Good,
        ua.StatusCodes.BadSensorFailure,
        ua.StatusCodes.UncertainSensorNotAccurate,
    ],
)
async def test_secure_failure_preserves_raw_frame_and_uint64_then_fresh_normal(
    runtime, mode, code, raw
):
    adapter, directory = runtime
    fields = FieldChannels(adapter.controller.storage)
    adapter.field_channels = fields
    first = datetime.now(UTC)
    await adapter.publish_snapshot(acquired_at=first, heartbeat=2**64 - 1, status_code=raw)
    state = fields.set_fault(
        "Sealer", FieldFaultInput(mode=mode, expected_revision=0), actor="operator", at=first
    )
    original = {field.tag_id: field for field in state.normal_checkpoint.fields}
    before = adapter.controller.state
    async with await client_for(adapter, directory) as client:
        index = await client.get_namespace_index(NAMESPACE_URI)
        tags = TAG_CONTRACT["tags"]
        nodes = [client.get_node(ua.NodeId(tag["logical_name"], index)) for tag in tags]
        later = first + timedelta(seconds=10)
        await adapter.publish_snapshot(acquired_at=later, heartbeat=10)
        observed = await client.read_attributes(nodes, ua.AttributeIds.Value)
        assert len(observed) == 48 and len({v.ServerTimestamp for v in observed}) == 1
        for tag, value in zip(tags, observed, strict=True):
            name = tag["logical_name"]
            assert value.Value.VariantType == getattr(ua.VariantType, tag["data_type"])
            if name.startswith("Sealer."):
                source = original[name]
                assert (value.Value.Value, value.SourceTimestamp) == (
                    source.reading.value,
                    source.source_timestamp,
                )
                assert value.StatusCode.value == code and source.status_code == raw
            else:
                assert value.SourceTimestamp == later and value.StatusCode.value == 0
        assert (
            next(
                v
                for t, v in zip(tags, observed, strict=True)
                if t["logical_name"] == "Sealer.LinkHeartbeat"
            ).Value.Value
            == 2**64 - 1
        )
        restarted = FieldChannels(adapter.controller.storage)
        assert all(s.pending for s in restarted.describe())
        adapter.field_channels = restarted
        await adapter.publish_snapshot(acquired_at=later, heartbeat=11)
        assert not any(s.pending for s in restarted.describe())
        restored = next(s for s in restarted.states() if s.group_id == "Sealer")
        assert restored == state
        restarted.set_fault(
            "Sealer",
            FieldFaultInput(mode="NORMAL", expected_revision=1),
            actor="operator",
            at=later,
        )
        with pytest.raises(FieldFaultConflict):
            restarted.set_fault(
                "Sealer",
                FieldFaultInput(mode=mode, expected_revision=2),
                actor="operator",
                at=later,
            )
        fresh = later + timedelta(seconds=1)
        await adapter.publish_snapshot(
            acquired_at=fresh, heartbeat=12, status_code=ua.StatusCodes.UncertainSensorNotAccurate
        )
        recovered = await client.read_attributes(nodes, ua.AttributeIds.Value)
        assert all(
            v.SourceTimestamp == fresh
            and v.StatusCode.value == ua.StatusCodes.UncertainSensorNotAccurate
            for v in recovered
        )
        assert (
            next(
                v
                for t, v in zip(tags, recovered, strict=True)
                if t["logical_name"] == "Sealer.LinkHeartbeat"
            ).Value.Value
            == 12
        )
    assert adapter.controller.state == before
