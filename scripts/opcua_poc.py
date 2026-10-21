"""DEV-03a secure, separate-process OPC UA compatibility probe, not a PLC."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from asyncua import Client, Server, ua, uamethod
from asyncua.common.utils import ServiceError
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
from asyncua.crypto.validator import CertificateValidator, CertificateValidatorOptions
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pydantic import ValidationError

# Running this file directly must also resolve the shared contracts package.
REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from contracts.models import (  # noqa: E402
    CommandNotFound,
    CommandRequest,
    CommandResult,
    CommandResultRequest,
    CommandSubmission,
    EventPage,
    EventReadError,
    ReadEventsRequest,
    RunNotFound,
    RunSummary,
    RunSummaryRequest,
)

SERVER_URI = "urn:hmi-mes:opcua-poc:server"
CLIENT_URI = "urn:hmi-mes:opcua-poc:client"
METHOD_MODELS = {
    "SubmitCommand": (CommandRequest, "command-request.json"),
    "GetCommandResult": (CommandResultRequest, "command-result-request.json"),
    "ReadEvents": (ReadEventsRequest, "read-events-request.json"),
    "GetRunSummary": (RunSummaryRequest, "run-summary-request.json"),
}


def tag_contract() -> dict:
    return json.loads((REPOSITORY / "contracts/tags/tags.v1.json").read_text())


def representative_value(tag: dict) -> bool | str | float | int:
    """Static compatibility samples, never live process measurements."""
    return {
        "Boolean": False,
        "String": "POC_ONLY",
        "Double": 12.5,
        "Int32": 0,
        "UInt32": 42,
        "UInt64": 2**64 - 1,
    }[tag["data_type"]]


def create_identity(directory: Path, name: str, uri: str, *, server: bool) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"HMI MES PoC {name}")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
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
            x509.KeyUsage(True, True, True, True, False, False, False, None, None),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    (directory / f"{name}.der").write_bytes(cert.public_bytes(serialization.Encoding.DER))
    key_path = directory / f"{name}.pem"
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )


def trusted_validator(certificate: Path, *, peer_server: bool):
    """Direct certificate trust plus validity, URI, usage, and server host checks."""
    trusted = x509.load_der_x509_certificate(certificate.read_bytes())
    options = CertificateValidatorOptions.EXT_VALIDATION | (
        CertificateValidatorOptions.PEER_SERVER
        if peer_server
        else CertificateValidatorOptions.PEER_CLIENT
    )
    basic = CertificateValidator(options)

    async def validate(cert: x509.Certificate, application: ua.ApplicationDescription) -> None:
        await basic(cert, application)
        if cert.fingerprint(hashes.SHA256()) != trusted.fingerprint(hashes.SHA256()):
            raise ServiceError(ua.StatusCodes.BadCertificateUntrusted)
        if peer_server:
            names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            if ipaddress.ip_address("127.0.0.1") not in names.get_values_for_type(x509.IPAddress):
                raise ServiceError(ua.StatusCodes.BadCertificateHostNameInvalid)

    return validate


def example_model(model, name: str):
    return model.model_validate_json((REPOSITORY / "contracts/examples" / name).read_text())


def method_response(name: str, request):
    """Fixed compatibility fixtures, no command log or simulator business behavior."""
    if name == "SubmitCommand":
        return CommandSubmission(
            command_id=request.command_id,
            disposition="REJECTED",
            received_at=datetime.now(UTC),
            reason_code="POC_ONLY",
            reason="Transport probe never accepts or executes commands.",
        )
    if name == "GetCommandResult":
        result = example_model(CommandResult, "command-result.json")
        if request.command_id != result.command_id:
            return CommandNotFound(
                code="COMMAND_NOT_FOUND",
                command_id=request.command_id,
                reason="No such PoC fixture.",
            )
        return result
    if name == "GetRunSummary":
        summary = example_model(RunSummary, "run-summary.json")
        if request.execution_id != summary.execution_id:
            return RunNotFound(
                code="RUN_NOT_FOUND",
                execution_id=request.execution_id,
                reason="No such PoC fixture.",
            )
        return summary
    page = example_model(EventPage, "event-page.json")
    if (request.source_id, request.stream_id) != (page.source_id, page.stream_id):
        return EventReadError(
            source_id=request.source_id,
            stream_id=request.stream_id,
            after_seq=request.after_seq,
            code="STREAM_MISMATCH",
            retained_from_seq=page.retained_from_seq,
            retained_through_seq=page.retained_through_seq,
            reason="No such PoC fixture stream.",
        )
    if request.after_seq > page.retained_through_seq:
        return EventReadError(
            source_id=request.source_id,
            stream_id=request.stream_id,
            after_seq=request.after_seq,
            code="CURSOR_AHEAD",
            retained_from_seq=page.retained_from_seq,
            retained_through_seq=page.retained_through_seq,
            reason="Beyond PoC fixture stream.",
        )
    events = tuple(event for event in page.events if event.seq > request.after_seq)[: request.limit]
    last = events[-1].seq if events else request.after_seq
    return EventPage(
        source_id=page.source_id,
        stream_id=page.stream_id,
        after_seq=request.after_seq,
        retained_from_seq=page.retained_from_seq,
        retained_through_seq=page.retained_through_seq,
        events=events,
        page_last_seq=last,
        has_more=last < page.retained_through_seq,
    )


def application_method(name: str, model):
    @uamethod
    def invoke(_parent, payload):
        try:
            request = model.model_validate_json(payload)
        except (ValidationError, ValueError, TypeError):
            return ua.CallMethodResult(StatusCode=ua.StatusCode(ua.StatusCodes.BadInvalidArgument))
        return method_response(name, request).model_dump_json()

    return invoke


async def serve(directory: Path, endpoint: str) -> None:
    server = Server()
    await server.init()
    server.set_endpoint(endpoint)
    await server.set_application_uri(SERVER_URI)
    server.set_server_name("HMI MES transport PoC; no process control")
    await server.load_certificate(directory / "server.der")
    await server.load_private_key(directory / "server.pem")
    server.set_security_policy([ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt])
    # Application certificates authenticate this transport-only probe. No user login/control.
    server.set_identity_tokens([ua.AnonymousIdentityToken])
    server.set_certificate_validator(trusted_validator(directory / "client.der", peer_server=False))
    contract = tag_contract()
    index = await server.register_namespace(contract["namespace_uri"])
    root = await server.nodes.objects.add_object(ua.NodeId("PoC", index), "PoC")
    for tag in contract["tags"]:
        await root.add_variable(
            ua.NodeId(tag["node_id"]["identifier"], index),
            tag["logical_name"],
            representative_value(tag),
            getattr(ua.VariantType, tag["data_type"]),
        )
        # Deliberately omit set_writable: every feedback tag stays read-only.
    for name, (model, _) in METHOD_MODELS.items():
        await root.add_method(
            ua.NodeId(name, index),
            name,
            application_method(name, model),
            [ua.VariantType.String],
            [ua.VariantType.String],
        )
    async with server:
        (directory / "ready").write_text("ready")
        await asyncio.Event().wait()


@dataclass(frozen=True)
class Probe:
    directory: Path
    endpoint: str
    process: subprocess.Popen

    async def client(self, *, identity: str = "client", server_pin: str = "server") -> Client:
        client = Client(self.endpoint, timeout=2)
        client.application_uri = CLIENT_URI
        client.certificate_validator = trusted_validator(
            self.directory / f"{server_pin}.der",
            peer_server=True,
        )
        # Supply the expected server certificate explicitly; never discover/trust it dynamically.
        await client.set_security(
            SecurityPolicyBasic256Sha256,
            self.directory / f"{identity}.der",
            self.directory / f"{identity}.pem",
            server_certificate=str(self.directory / f"{server_pin}.der"),
            mode=ua.MessageSecurityMode.SignAndEncrypt,
        )
        return client


@contextmanager
def launch_server() -> Iterator[Probe]:
    with tempfile.TemporaryDirectory(prefix="hmi-mes-opcua-") as temporary:
        directory = Path(temporary)
        for name, uri, is_server in (
            ("server", SERVER_URI, True),
            ("client", CLIENT_URI, False),
            ("unknown", CLIENT_URI, False),
            ("wrong-server", SERVER_URI, True),
        ):
            create_identity(directory, name, uri, server=is_server)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        endpoint = f"opc.tcp://127.0.0.1:{port}/hmi-mes-poc/"
        with (directory / "server.log").open("w+") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--serve",
                    str(directory),
                    "--endpoint",
                    endpoint,
                ],
                cwd=REPOSITORY,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 15
                while not (directory / "ready").exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        raise RuntimeError(f"OPC UA subprocess failed to start: {log.read()}")
                    time.sleep(0.05)
                yield Probe(directory, endpoint, process)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


async def read_tags(client: Client) -> None:
    contract = tag_contract()
    index = await client.get_namespace_index(contract["namespace_uri"])
    nodes = [
        client.get_node(ua.NodeId(tag["node_id"]["identifier"], index)) for tag in contract["tags"]
    ]
    values = await client.read_attributes(nodes, ua.AttributeIds.Value)
    for tag, node, value in zip(contract["tags"], nodes, values, strict=True):
        value.StatusCode.check()
        assert value.Value.VariantType == getattr(ua.VariantType, tag["data_type"])
        assert value.Value.Value == representative_value(tag)
        assert value.SourceTimestamp is not None and value.ServerTimestamp is not None
        assert await node.read_data_type_as_variant_type() == getattr(
            ua.VariantType,
            tag["data_type"],
        )


async def roundtrip_methods(client: Client) -> None:
    index = await client.get_namespace_index(tag_contract()["namespace_uri"])
    root = client.get_node(ua.NodeId("PoC", index))
    response_models = {
        "SubmitCommand": CommandSubmission,
        "GetCommandResult": CommandResult,
        "ReadEvents": EventPage,
        "GetRunSummary": RunSummary,
    }
    for name, (model, example) in METHOD_MODELS.items():
        payload = (REPOSITORY / "contracts/examples" / example).read_text()
        request = model.model_validate_json(payload)
        actual = await root.call_method(ua.NodeId(name, index), payload)
        response = response_models[name].model_validate_json(actual)
        if name == "SubmitCommand":
            assert response.command_id == request.command_id
            assert response.disposition == "REJECTED" and response.reason_code == "POC_ONLY"
        else:
            assert response == method_response(name, request)


async def run_probe() -> None:
    with launch_server() as probe:
        client = await probe.client()
        async with client:
            await read_tags(client)
            await roundtrip_methods(client)
        # A new secure session after disconnect must rediscover the namespace URI.
        async with await probe.client() as reconnected:
            await read_tags(reconnected)
    print("PASS: 48 typed tag samples, 4 request/response method bindings, secure reconnect")
    print("Transport compatibility only; run pytest for certificate/write rejection checks.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--endpoint", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    if arguments.serve:
        if not arguments.endpoint:
            parser.error("--endpoint is required with --serve")
        asyncio.run(serve(arguments.serve, arguments.endpoint))
    else:
        asyncio.run(run_probe())


if __name__ == "__main__":
    main()
