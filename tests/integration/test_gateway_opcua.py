"""Gateway reads and commands over actual pinned encrypted simulator sockets."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest
from asyncua import Server, ua, uamethod
from asyncua.common.utils import ServiceError
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from apps.gateway.opcua_client import (
    NAMESPACE_URI,
    GatewayMethodUncertain,
    GatewayOPCUAClient,
    GatewayOPCUAConfig,
    GatewayReadError,
)
from contracts.models import (
    UINT64_MAX,
    CommandRequest,
    CommandResultRequest,
    ReadEventsRequest,
    RunSummaryRequest,
)
from tests.integration.test_simulator_opcua import (
    CLIENT_URI,
    SERVER_URI,
    TAG_CONTRACT,
    certificate,
)
from tests.integration.test_simulator_opcua import runtime as runtime


@dataclass
class Clock:
    elapsed: float = 0.0
    wall: datetime = datetime(2026, 10, 20, tzinfo=UTC)

    def monotonic(self):
        return self.elapsed

    def utc(self):
        return self.wall


def config_for(adapter, directory, **changes):
    return GatewayOPCUAConfig(
        **{
            "endpoint": adapter.config.endpoint,
            "application_uri": CLIENT_URI,
            "client_certificate": directory / "client.der",
            "client_private_key": directory / "client.pem",
            "trusted_server_certificate": directory / "server.der",
            "server_application_uri": SERVER_URI,
            **changes,
        }
    )


def command(controller, kind, command_id, **args):
    now = datetime.now(UTC)
    return CommandRequest(
        command_id=command_id,
        target_id="Line",
        execution_id="run-gateway",
        requested_by="operator-audit",
        requested_at=now,
        expires_at=now + timedelta(seconds=5),
        expected_control_version=controller.state.control_version,
        command={"kind": kind, "args": args},
    )


async def test_dictionary_batch_preserves_raw_quality_timestamps_and_exact_uint64(runtime):
    server, directory = runtime
    clock = Clock()
    gateway = GatewayOPCUAClient(
        config_for(server, directory), utc_clock=clock.utc, monotonic_clock=clock.monotonic
    )
    source_time = datetime.now(UTC) - timedelta(days=100)
    await server.publish_snapshot(acquired_at=source_time, heartbeat=UINT64_MAX)
    try:
        observation = await gateway.read_tags()
        assert observation.acquired_at == clock.wall and observation.health.state == "GOOD"
        assert len(observation.samples) == 48
        assert len({sample.sample_id for sample in observation.samples}) == 48
        expected_types = {tag["logical_name"]: tag["data_type"] for tag in TAG_CONTRACT["tags"]}
        assert {
            sample.tag_id: sample.reading.data_type for sample in observation.samples
        } == expected_types
        assert all(sample.source_timestamp == source_time for sample in observation.samples)
        assert len({sample.server_timestamp for sample in observation.samples}) == 1
        assert all(
            sample.acquired_at == clock.wall and sample.status_code == 0
            for sample in observation.samples
        )
        heartbeat = next(s for s in observation.samples if s.tag_id == "Line.Heartbeat")
        assert heartbeat.reading.value == UINT64_MAX
        assert f'"value":"{UINT64_MAX}"' in heartbeat.model_dump_json()
        # Inject raw field quality at the source; collect actual encoded OPC DataValues.
        snapshot = dict(server._snapshot)
        snapshot["Sealer.TemperaturePV"] = ua.DataValue(
            Value=ua.Variant(71.25, ua.VariantType.Double),
            StatusCode=ua.StatusCode(ua.StatusCodes.UncertainLastUsableValue),
            SourceTimestamp=None,
            ServerTimestamp=None,
        )
        snapshot["Sealer.AirPressure"] = ua.DataValue(
            Value=ua.Variant(None, ua.VariantType.Null),
            StatusCode=ua.StatusCode(ua.StatusCodes.BadNoData),
        )
        server._snapshot = snapshot
        raw = await gateway.read_tags()
        temperature = next(s for s in raw.samples if s.tag_id == "Sealer.TemperaturePV")
        pressure = next(s for s in raw.samples if s.tag_id == "Sealer.AirPressure")
        assert temperature.reading.value == 71.25
        assert temperature.status_code == ua.StatusCodes.UncertainLastUsableValue
        assert temperature.source_timestamp is None and temperature.server_timestamp is None
        assert pressure.reading.value is None and pressure.status_code == ua.StatusCodes.BadNoData
        assert {s.sample_id for s in observation.samples}.isdisjoint(
            s.sample_id for s in raw.samples
        )
    finally:
        await gateway.disconnect()


async def test_freshness_uses_good_heartbeat_changes_and_monotonic_receipts_not_source_time(
    runtime,
):
    server, directory = runtime
    clock = Clock()
    gateway = GatewayOPCUAClient(
        config_for(server, directory), utc_clock=clock.utc, monotonic_clock=clock.monotonic
    )
    old_source = datetime.now(UTC) - timedelta(days=100)
    try:
        await server.publish_snapshot(acquired_at=old_source, heartbeat=7)
        assert (await gateway.read_tags()).health.state == "GOOD"
        clock.elapsed = 1.0
        clock.wall -= timedelta(days=1000)  # Wall-clock adjustment cannot rewind freshness.
        await server.publish_snapshot(acquired_at=old_source, heartbeat=8)
        assert (await gateway.read_tags()).health.state == "GOOD"
        clock.elapsed = 4.0
        assert (await gateway.read_tags()).health.state == "DELAYED"
        assert gateway.health().read_age_seconds == 0.0  # Reads succeed but heartbeat is frozen.
        clock.elapsed = 6.0
        assert (await gateway.read_tags()).health.state == "ALARM"
        clock.elapsed = 7.0
        await server.publish_snapshot(
            acquired_at=old_source, heartbeat=9, status_code=ua.StatusCodes.UncertainLastUsableValue
        )
        uncertain = await gateway.read_tags()
        assert uncertain.health.state == "ALARM"
        assert all(
            s.status_code == ua.StatusCodes.UncertainLastUsableValue for s in uncertain.samples
        )
        clock.elapsed = 8.0
        await server.publish_snapshot(acquired_at=old_source, heartbeat=10)
        assert (await gateway.read_tags()).health.state == "GOOD"
        clock.elapsed = 11.0
        assert gateway.health().state == "DELAYED"  # No read attempted also ages the link.
        clock.elapsed = 13.0
        assert gateway.health().state == "ALARM"
    finally:
        await gateway.disconnect()


async def test_reconnect_rediscovers_namespace_index_and_never_sends_an_operational_method(
    runtime, monkeypatch
):
    server, directory = runtime
    gateway = GatewayOPCUAClient(config_for(server, directory))
    try:
        first = await gateway.read_tags()
        old_index = gateway.namespace_index
        await gateway.disconnect()
        assert not gateway.health().connected
        await server.stop()
        original = Server.register_namespace

        async def shifted_namespace(instance, uri):
            if uri == NAMESPACE_URI:
                await original(instance, "urn:gateway:test:namespace-shift")
            return await original(instance, uri)

        monkeypatch.setattr(Server, "register_namespace", shifted_namespace)
        await server.start()
        second = await gateway.read_tags()  # Read-only automatic connection.
        assert gateway.namespace_index != old_index
        assert len(first.samples) == len(second.samples) == 48
        assert not server.controller.commands.pending_records()
        assert server.controller.storage.load_execution() is None
    finally:
        await gateway.disconnect()


async def test_typed_methods_keep_original_ids_and_return_real_events_and_summary(runtime):
    server, directory = runtime
    controller = server.controller
    gateway = GatewayOPCUAClient(config_for(server, directory))
    load = command(
        controller,
        "LOAD_JOB",
        "load-gateway",
        work_order_id="work-gateway",
        recipe_version="recipe-1",
        target_quantity=8,
        parameters={"temperature_c": 150.0, "speed_m_per_min": 6.0},
    )
    with pytest.raises(GatewayMethodUncertain) as disconnected:
        await gateway.submit_command(load)
    assert (
        not disconnected.value.delivery_possible
        and disconnected.value.command_id == load.command_id
    )
    assert controller.commands.lookup(load.command_id) is None
    try:
        await gateway.connect()
        first = await gateway.submit_command(load)
        assert first.disposition == "ACCEPTED"
        controller.tick(now=datetime.now(UTC))
        loaded = await gateway.get_command_result(CommandResultRequest(command_id=load.command_id))
        assert loaded.status == "SUCCEEDED" and loaded.evidence.execution_id == "run-gateway"
        assert await gateway.submit_command(load) == first
        await gateway.submit_command(command(controller, "START", "start-gateway"))
        for _ in range(6):
            controller.tick(now=datetime.now(UTC))
        await gateway.submit_command(command(controller, "END_RUN", "end-gateway"))
        for _ in range(10):
            controller.tick(now=datetime.now(UTC))
            if controller.get_result("end-gateway").status == "SUCCEEDED":
                break
        summary = await gateway.get_run_summary(RunSummaryRequest(execution_id="run-gateway"))
        assert summary.final and summary.wip_quantity == 0 and summary.good_quantity > 0
        identity = controller.storage.identity
        page = await gateway.read_events(
            ReadEventsRequest(
                source_id=identity.source_id,
                stream_id=identity.stream_id,
                after_seq=0,
                limit=1000,
            )
        )
        assert page.page_last_seq == summary.last_event_seq
        absent = await gateway.get_command_result(CommandResultRequest(command_id="absent"))
        assert absent.code == "COMMAND_NOT_FOUND"
        missing = await gateway.get_run_summary(RunSummaryRequest(execution_id="absent"))
        assert missing.code == "RUN_NOT_FOUND"
        mismatch = await gateway.read_events(
            ReadEventsRequest(source_id="wrong", stream_id="wrong", after_seq=0)
        )
        assert mismatch.code == "STREAM_MISMATCH"
    finally:
        await gateway.disconnect()


async def test_response_loss_exposes_uncertainty_without_retry_or_invented_plc_failure(runtime):
    server, directory = runtime
    gateway = GatewayOPCUAClient(config_for(server, directory))
    try:
        await gateway.connect()
        await server.stop()
        req = command(server.controller, "START", "original-command-id")
        with pytest.raises(GatewayMethodUncertain) as lost:
            await gateway.submit_command(req)
        assert lost.value.delivery_possible and lost.value.command_id == "original-command-id"
        assert req.command_id == "original-command-id"
        assert server.controller.commands.lookup(req.command_id) is None
        with pytest.raises(GatewayReadError):
            await gateway.read_tags()
        assert not gateway.health().connected
    finally:
        await gateway.disconnect()


async def test_wrong_server_pin_uri_and_mismatched_client_key_fail_closed(runtime):
    server, directory = runtime
    certificate(directory, "other-server", SERVER_URI, server=True)
    cases = (
        {"trusted_server_certificate": directory / "other-server.der"},
        {"server_application_uri": "urn:wrong:server"},
        {"client_private_key": directory / "unknown.pem"},
    )
    for changes in cases:
        gateway = GatewayOPCUAClient(config_for(server, directory, **changes))
        try:
            with pytest.raises(
                (ua.UaError, ServiceError, ValueError, TimeoutError, ConnectionError)
            ):
                await gateway.connect()
            assert not gateway.health().connected
        finally:
            await gateway.disconnect()


async def test_contract_violations_are_explicit_capture_errors_and_never_normal_values(runtime):
    server, directory = runtime
    gateway = GatewayOPCUAClient(config_for(server, directory))
    snapshot = dict(server._snapshot)
    snapshot["Line.State"] = replace(
        snapshot["Line.State"], Value=ua.Variant(999, ua.VariantType.Int32)
    )
    server._snapshot = snapshot
    try:
        with pytest.raises(GatewayReadError) as rejected:
            await gateway.read_tags()
        assert rejected.value.__cause__ is not None
        assert not gateway.health().connected
        assert gateway.health().state == "UNKNOWN"
    finally:
        await gateway.disconnect()


@pytest.mark.parametrize("reply_kind", ["MALFORMED_JSON", "WRONG_COMMAND_ID"])
async def test_malformed_reply_after_real_admission_stays_unknown_until_original_id_query(
    runtime,
    reply_kind,
):
    server, directory = runtime
    gateway = GatewayOPCUAClient(config_for(server, directory))
    req = command(
        server.controller,
        "LOAD_JOB",
        "reply-lost",
        work_order_id="work-gateway",
        recipe_version="recipe-1",
        target_quantity=8,
        parameters={"temperature_c": 150.0, "speed_m_per_min": 6.0},
    )

    @uamethod
    async def admitted_but_malformed(_parent, payload):
        submission = server.controller.submit(
            CommandRequest.model_validate_json(payload),
            now=datetime.now(UTC),
        ).submission
        if reply_kind == "WRONG_COMMAND_ID":
            return submission.model_copy(update={"command_id": "another-command"}).model_dump_json()
        return "{not-a-contract-response"

    method = server._server.iserver.aspace[ua.NodeId("SubmitCommand", server.namespace_index)]
    original = method.call
    method.call = admitted_but_malformed
    try:
        await gateway.connect()
        with pytest.raises(GatewayMethodUncertain) as unknown:
            await gateway.submit_command(req)
        assert unknown.value.command_id == req.command_id and unknown.value.delivery_possible
        result = await gateway.get_command_result(CommandResultRequest(command_id=req.command_id))
        assert result.status == "ACCEPTED"
        assert len(server.controller.commands.pending_records()) == 1
        server.controller.tick(now=datetime.now(UTC))
        completed = await gateway.get_command_result(
            CommandResultRequest(command_id=req.command_id)
        )
        assert completed.status == "SUCCEEDED"
    finally:
        method.call = original
        await gateway.disconnect()


async def test_pinned_server_still_requires_configured_endpoint_hostname(runtime):
    server, directory = runtime
    await server.stop()
    old = x509.load_der_x509_certificate((directory / "server.der").read_bytes())
    key = serialization.load_pem_private_key((directory / "server.pem").read_bytes(), password=None)
    builder = (
        x509.CertificateBuilder()
        .subject_name(old.subject)
        .issuer_name(old.issuer)
        .public_key(old.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(old.not_valid_before_utc)
        .not_valid_after(old.not_valid_after_utc)
    )
    for extension in old.extensions:
        value = extension.value
        if isinstance(value, x509.SubjectAlternativeName):
            value = x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(SERVER_URI),
                    x509.DNSName("wrong-host.invalid"),
                ]
            )
        builder = builder.add_extension(value, extension.critical)
    cert = builder.sign(key, hashes.SHA256())
    (directory / "server.der").write_bytes(cert.public_bytes(serialization.Encoding.DER))
    await server.start()
    gateway = GatewayOPCUAClient(config_for(server, directory))
    try:
        with pytest.raises(ua.UaStatusCodeError) as rejected:
            await gateway.connect()
        assert rejected.value.code == ua.StatusCodes.BadCertificateHostNameInvalid
        assert not gateway.health().connected
    finally:
        await gateway.disconnect()


async def test_capture_brackets_utc_and_monotonic_regression_resets_epoch(runtime):
    server, directory = runtime
    clock = Clock(elapsed=10.0)

    def measured_utc():
        clock.elapsed += 0.125
        return clock.wall

    gateway = GatewayOPCUAClient(
        config_for(server, directory), utc_clock=measured_utc, monotonic_clock=clock.monotonic
    )
    await server.publish_snapshot(acquired_at=clock.wall, heartbeat=1)
    try:
        first = await gateway.read_tags()
        assert first.capture_time.monotonic_seconds == 10.125
        assert first.capture_time.utc_read_span_seconds == 0.125
        clock.elapsed = 2.0
        with pytest.raises(GatewayReadError):
            await gateway.read_tags()
        assert gateway.health().state == "UNKNOWN"
        assert gateway._heartbeat == {}
        recovered = await gateway.read_tags()
        assert recovered.capture_time.clock_epoch != first.capture_time.clock_epoch
        assert recovered.capture_time.monotonic_seconds == 2.125
        # A regression observed through health must not resurrect old GOOD evidence.
        clock.elapsed = 1.0
        assert gateway.health().state == "UNKNOWN"
        assert gateway.health().state == "UNKNOWN"
        clock.elapsed = float("nan")
        assert gateway.health().state == "UNKNOWN"
        clock.elapsed = 3.0
        after_invalid_clock = await gateway.read_tags()
        assert after_invalid_clock.capture_time.clock_epoch != recovered.capture_time.clock_epoch
    finally:
        await gateway.disconnect()


async def test_pump_persists_capture_before_slow_event_query(runtime, tmp_path):
    from apps.gateway.buffer import GatewayBuffer
    from apps.gateway.worker import GatewayPump

    server, directory = runtime
    clock = Clock(elapsed=10.0)
    gateway = GatewayOPCUAClient(
        config_for(server, directory), utc_clock=clock.utc, monotonic_clock=clock.monotonic
    )

    async def slow_events(request):
        clock.elapsed += 60.0
        clock.wall += timedelta(seconds=60)
        raise GatewayMethodUncertain("ReadEvents", None, delivery_possible=True)

    gateway.read_events = slow_events
    await server.publish_snapshot(acquired_at=clock.wall, heartbeat=1)
    try:
        with GatewayBuffer(tmp_path / "pump-capture.db") as buffer:
            pump = GatewayPump("source", "stream", opc=gateway, buffer=buffer, intake=None)
            result = await pump.capture_once()
            batch = buffer.lease_next().batch
            assert result.event_query_uncertain
            assert batch.body.schema_version == "1.2"
            assert batch.body.capture_time.monotonic_seconds == 10.0
            assert batch.body.captured_at == clock.wall - timedelta(seconds=60)
            assert all(s.acquired_at == batch.body.captured_at for s in batch.body.samples)
    finally:
        await gateway.disconnect()
