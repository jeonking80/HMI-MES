"""Real encrypted socket sessions against controller-backed OPC UA methods/tags."""

from __future__ import annotations

import ipaddress
import json
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from asyncua import Client, ua
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from apps.simulator.controller import SimulatorController
from apps.simulator.opcua_server import (
    NAMESPACE_URI,
    ROOT_NODE_ID,
    OPCUAServerConfig,
    SimulatorOPCUAServer,
    pinned_certificate_validator,
)
from apps.simulator.plc import PLCInputs, RecoveryChecks
from apps.simulator.process import ProcessConfig
from apps.simulator.storage import SimulatorStorage
from apps.simulator.tags import project_tags
from contracts.models import (
    CommandNotFound,
    CommandRequest,
    CommandResult,
    CommandResultRequest,
    CommandSubmission,
    EventPage,
    ReadEventsRequest,
    RunSummary,
    RunSummaryRequest,
)

SERVER_URI = "urn:hmi-mes:test:simulator"
CLIENT_URI = "urn:hmi-mes:test:gateway"
TAG_CONTRACT = json.loads(
    (Path(__file__).resolve().parents[2] / "contracts/tags/tags.v1.json").read_text()
)


def certificate(directory, name, uri, *, server=False, expired=False):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now + timedelta(days=-1 if expired else 2))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(uri),
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(True, True, True, True, False, False, False, None, None), critical=True
        )
        .add_extension(
            x509.ExtendedKeyUsage(
                [
                    ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH,
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    (directory / f"{name}.der").write_bytes(cert.public_bytes(serialization.Encoding.DER))
    (directory / f"{name}.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (directory / f"{name}.pem").chmod(0o600)


@pytest.fixture
async def runtime(tmp_path):
    for name, uri, server in (
        ("server", SERVER_URI, True),
        ("client", CLIENT_URI, False),
        ("unknown", CLIENT_URI, False),
    ):
        certificate(tmp_path, name, uri, server=server)
    certificate(tmp_path, "expired", CLIENT_URI, expired=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config = OPCUAServerConfig(
        endpoint=f"opc.tcp://127.0.0.1:{port}/simulator/",
        application_uri=SERVER_URI,
        server_certificate=tmp_path / "server.der",
        server_private_key=tmp_path / "server.pem",
        trusted_client_certificate=tmp_path / "client.der",
        gateway_principal="gateway-test",
    )
    with SimulatorStorage(tmp_path / "opcua.sqlite3") as store:
        controller = SimulatorController(
            store,
            now=datetime.now(UTC),
            process_config=ProcessConfig(
                stage_lengths_mm=(1, 1, 1),
                ambient_temperature_c=150.0,
            ),
        )
        controller.recover(
            now=datetime.now(UTC),
            operator_id="local-operator",
            checks=RecoveryChecks(
                outputs_stopped=True,
                positions_verified=True,
                storage_reconciled=True,
                commands_reconciled=True,
                operator_confirmed=True,
            ),
        )
        controller.set_inputs(
            PLCInputs(temperature_c=150.0),
            now=datetime.now(UTC),
            control_mode="REMOTE",
            operation_mode="AUTO",
        )
        adapter = SimulatorOPCUAServer(controller, config)
        await adapter.start()
        try:
            yield adapter, tmp_path
        finally:
            await adapter.stop()


async def client_for(adapter, directory, *, identity="client", application_uri=CLIENT_URI):
    client = Client(adapter.config.endpoint, timeout=2)
    client.application_uri = application_uri
    client.certificate_validator = pinned_certificate_validator(
        directory / "server.der",
        peer_server=True,
    )
    await client.set_security(
        SecurityPolicyBasic256Sha256,
        directory / f"{identity}.der",
        directory / f"{identity}.pem",
        server_certificate=str(directory / "server.der"),
        mode=ua.MessageSecurityMode.SignAndEncrypt,
    )
    return client


def command(controller, kind, command_id, **args):
    now = datetime.now(UTC)
    return CommandRequest(
        command_id=command_id,
        target_id="Line",
        execution_id="run-A",
        requested_by="mes-operator",
        requested_at=now,
        expires_at=now + timedelta(seconds=5),
        expected_control_version=controller.state.control_version,
        command={"kind": kind, "args": args},
    )


async def call(root, index, method, request, response_model):
    response = await root.call_method(ua.NodeId(method, index), request.model_dump_json())
    return response_model.model_validate_json(response)


async def test_secure_methods_produce_real_causal_results_events_and_final_summary(runtime):
    adapter, directory = runtime
    controller = adapter.controller
    async with await client_for(adapter, directory) as client:
        index = await client.get_namespace_index(NAMESPACE_URI)
        root = client.get_node(ua.NodeId(ROOT_NODE_ID, index))
        load = command(
            controller,
            "LOAD_JOB",
            "load",
            work_order_id="work-A",
            recipe_version="recipe-1",
            target_quantity=10,
            parameters={"temperature_c": 150.0, "speed_m_per_min": 6.0},
        )
        accepted = await call(root, index, "SubmitCommand", load, CommandSubmission)
        assert accepted.disposition == "ACCEPTED"
        result_req = CommandResultRequest(command_id="load")
        assert (await call(root, index, "GetCommandResult", result_req, CommandResult)).status == (
            "ACCEPTED"
        )
        assert controller.storage.load_execution() is None
        controller.tick(now=datetime.now(UTC))
        loaded = await call(root, index, "GetCommandResult", result_req, CommandResult)
        assert loaded.status == "SUCCEEDED" and loaded.evidence.execution_id == "run-A"
        version, head = controller.state.control_version, controller.state.last_event_seq
        assert await call(root, index, "SubmitCommand", load, CommandSubmission) == accepted
        assert (
            controller.state.control_version == version and controller.state.last_event_seq == head
        )
        changed = CommandRequest.model_validate({**load.model_dump(), "requested_by": "different"})
        conflict = await call(root, index, "SubmitCommand", changed, CommandSubmission)
        assert conflict.disposition == "REJECTED" and conflict.reason_code == "COMMAND_ID_CONFLICT"
        start = command(controller, "START", "start")
        assert (await call(root, index, "SubmitCommand", start, CommandSubmission)).disposition == (
            "ACCEPTED"
        )
        for _ in range(6):
            controller.tick(now=datetime.now(UTC))
        started = await call(
            root, index, "GetCommandResult", CommandResultRequest(command_id="start"), CommandResult
        )
        assert started.status == "SUCCEEDED" and started.evidence.state == "RUNNING"
        before_end = controller.storage.load_execution()
        assert before_end.input_quantity > 0 and before_end.wip_quantity > 0
        end = command(controller, "END_RUN", "end")
        assert (await call(root, index, "SubmitCommand", end, CommandSubmission)).disposition == (
            "ACCEPTED"
        )
        for _ in range(10):
            controller.tick(now=datetime.now(UTC))
            if controller.get_result("end").status == "SUCCEEDED":
                break
        ended = await call(
            root, index, "GetCommandResult", CommandResultRequest(command_id="end"), CommandResult
        )
        summary = await call(
            root, index, "GetRunSummary", RunSummaryRequest(execution_id="run-A"), RunSummary
        )
        assert ended.status == "SUCCEEDED" and summary.final and summary.wip_quantity == 0
        assert summary.input_quantity == summary.good_quantity + summary.reject_quantity
        assert summary.input_quantity == before_end.input_quantity
        identity = controller.storage.identity
        events = await call(
            root,
            index,
            "ReadEvents",
            ReadEventsRequest(
                source_id=identity.source_id,
                stream_id=identity.stream_id,
                after_seq=0,
                limit=1000,
            ),
            EventPage,
        )
        assert events.page_last_seq == summary.last_event_seq == ended.evidence.last_event_seq
        assert [event.seq for event in events.events] == list(range(1, len(events.events) + 1))
        assert (
            sum(event.kind == "PRODUCTION_GOOD" for event in events.events) == summary.good_quantity
        )
        assert (
            sum(event.kind == "PRODUCTION_REJECT" for event in events.events)
            == summary.reject_quantity
        )
        assert all(
            event.execution_id == "run-A"
            for event in events.events
            if event.kind.startswith("PRODUCTION_")
        )
        absent = await call(
            root,
            index,
            "GetCommandResult",
            CommandResultRequest(command_id="missing"),
            CommandNotFound,
        )
        assert absent.code == "COMMAND_NOT_FOUND"
        await adapter.publish_snapshot(acquired_at=datetime.now(UTC), heartbeat=20)
        for name, expected in (
            ("Line.WipCount", 0),
            ("Line.GoodTotal", summary.good_quantity),
            ("Line.RejectTotal", summary.reject_quantity),
            ("Line.EventSeq", summary.last_event_seq),
            ("Line.ActiveExecutionId", "run-A"),
        ):
            assert await client.get_node(ua.NodeId(name, index)).read_value() == expected


async def test_all_48_readonly_typed_tags_share_atomic_timestamp_quality_snapshot(runtime):
    adapter, directory = runtime
    controller = adapter.controller
    acquired = datetime.now(UTC)
    await adapter.publish_snapshot(acquired_at=acquired, heartbeat=2**53 + 1)
    expected = project_tags(
        controller.state,
        controller.storage.load_execution(),
        counter_epoch=controller.storage.identity.counter_epoch,
        heartbeat=2**53 + 1,
    )
    async with await client_for(adapter, directory) as client:
        index = await client.get_namespace_index(NAMESPACE_URI)
        nodes = [client.get_node(ua.NodeId(name, index)) for name in expected]
        values = await client.read_attributes(nodes, ua.AttributeIds.Value)
        assert len(values) == 48
        types = {tag["logical_name"]: tag["data_type"] for tag in TAG_CONTRACT["tags"]}
        for (name, expected_value), node, value in zip(
            expected.items(), nodes, values, strict=True
        ):
            value.StatusCode.check()
            assert value.Value.Value == expected_value, name
            assert value.Value.VariantType == getattr(ua.VariantType, types[name])
            assert value.Value.VariantType == await node.read_data_type_as_variant_type()
            assert value.SourceTimestamp == acquired
            with pytest.raises(ua.UaStatusCodeError) as denied:
                await node.write_value(ua.Variant(expected_value, value.Value.VariantType))
            assert denied.value.code == ua.StatusCodes.BadUserAccessDenied
        assert len({value.ServerTimestamp for value in values}) == 1
        await adapter.publish_snapshot(
            acquired_at=acquired, heartbeat=10, status_code=ua.StatusCodes.UncertainLastUsableValue
        )
        uncertain = await client.read_attributes(nodes, ua.AttributeIds.Value)
        assert all(
            value.StatusCode.value == ua.StatusCodes.UncertainLastUsableValue for value in uncertain
        )
        assert all(value.SourceTimestamp == acquired for value in uncertain)
        assert controller.state.control_version == expected["Line.ControlVersion"]


async def test_unknown_certificate_plaintext_uri_mismatch_and_malformed_requests_rejected(runtime):
    adapter, directory = runtime
    for identity, application_uri, expected_code in (
        ("unknown", CLIENT_URI, ua.StatusCodes.BadCertificateUntrusted),
        ("client", "urn:wrong:uri", ua.StatusCodes.BadCertificateUriInvalid),
        ("expired", CLIENT_URI, ua.StatusCodes.BadCertificateTimeInvalid),
    ):
        with pytest.raises(ua.UaStatusCodeError) as denied:
            async with await client_for(
                adapter, directory, identity=identity, application_uri=application_uri
            ):
                pytest.fail("Untrusted application established an operational session")
        assert denied.value.code == expected_code
    with pytest.raises(ua.UaError):
        async with Client(adapter.config.endpoint, timeout=2):
            pytest.fail("Plaintext established an operational session")
    async with await client_for(adapter, directory) as client:
        index = await client.get_namespace_index(NAMESPACE_URI)
        root = client.get_node(ua.NodeId(ROOT_NODE_ID, index))
        for payload in ("{broken", '{"command_id":"bad"}'):
            with pytest.raises(ua.UaStatusCodeError) as denied:
                await root.call_method(ua.NodeId("SubmitCommand", index), payload)
            assert denied.value.code == ua.StatusCodes.BadInvalidArgument
        assert adapter.controller.commands.lookup("bad") is None


async def test_certificate_gateway_authority_does_not_bypass_local_plc_mode(runtime):
    adapter, directory = runtime
    controller = adapter.controller
    controller.set_inputs(controller.state.inputs, now=datetime.now(UTC), control_mode="LOCAL")
    async with await client_for(adapter, directory) as client:
        index = await client.get_namespace_index(NAMESPACE_URI)
        root = client.get_node(ua.NodeId(ROOT_NODE_ID, index))
        req = command(
            controller,
            "LOAD_JOB",
            "local-bypass",
            work_order_id="work-A",
            recipe_version="recipe-1",
            target_quantity=10,
            parameters={"temperature_c": 150.0, "speed_m_per_min": 6.0},
        )
        rejected = await call(root, index, "SubmitCommand", req, CommandSubmission)
        assert rejected.disposition == "REJECTED" and rejected.reason_code == "LOCAL_MODE"
        assert (
            controller.state.control_mode == "LOCAL" and controller.storage.load_execution() is None
        )
        payload = json.loads(req.model_dump_json())
        payload["authority"] = "LOCAL"
        with pytest.raises(ua.UaStatusCodeError) as denied:
            await root.call_method(ua.NodeId("SubmitCommand", index), json.dumps(payload))
        assert denied.value.code == ua.StatusCodes.BadInvalidArgument


def test_configuration_requires_preprovisioned_certificates_and_explicit_endpoint(tmp_path):
    with pytest.raises(ValueError, match="existing file"):
        OPCUAServerConfig(
            endpoint="opc.tcp://127.0.0.1:4840/",
            application_uri=SERVER_URI,
            server_certificate=tmp_path / "missing.der",
            server_private_key=tmp_path / "missing.pem",
            trusted_client_certificate=tmp_path / "client.der",
        )
    with pytest.raises(ValueError, match="endpoint"):
        OPCUAServerConfig(
            endpoint="http://127.0.0.1:4840/",
            application_uri=SERVER_URI,
            server_certificate=tmp_path / "missing.der",
            server_private_key=tmp_path / "missing.pem",
            trusted_client_certificate=tmp_path / "client.der",
        )
