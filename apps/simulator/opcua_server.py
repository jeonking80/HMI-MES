"""Certificate-pinned OPC UA adapter for the real simulator controller.

The gateway application certificate authorizes these four application methods.
End-user requested_by is downstream audit data, never a simulator login claim.
Controller calls run synchronously on the owning asyncio event-loop thread.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from asyncua import Server, ua, uamethod
from asyncua.common.utils import ServiceError
from asyncua.crypto.validator import CertificateValidator, CertificateValidatorOptions
from asyncua.server.user_managers import CertificateUserManager
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from pydantic import TypeAdapter, ValidationError

from apps.simulator.controller import SimulatorController
from apps.simulator.field_channels import FieldChannels
from apps.simulator.tags import project_tags
from contracts.models import (
    CommandRequest,
    CommandResultRequest,
    Identifier,
    ReadEventsRequest,
    RunSummaryRequest,
    UTCTimestamp,
)

_CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contracts/tags/tags.v1.json"
_CONTRACT = json.loads(_CONTRACT_PATH.read_text())
NAMESPACE_URI = _CONTRACT["namespace_uri"]
ROOT_NODE_ID = "Simulator"
_TIME = TypeAdapter(UTCTimestamp)
_ID = TypeAdapter(Identifier)
_METHOD_MODELS = {
    "SubmitCommand": CommandRequest,
    "GetCommandResult": CommandResultRequest,
    "ReadEvents": ReadEventsRequest,
    "GetRunSummary": RunSummaryRequest,
}


@dataclass(frozen=True)
class OPCUAServerConfig:
    endpoint: str
    application_uri: str
    server_certificate: Path
    server_private_key: Path
    trusted_client_certificate: Path
    gateway_principal: str = "gateway"

    def __post_init__(self) -> None:
        endpoint = urlsplit(self.endpoint)
        if (
            endpoint.scheme != "opc.tcp"
            or not endpoint.hostname
            or not endpoint.port
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("An explicit opc.tcp endpoint with hostname and port is required")
        if not self.application_uri or not urlsplit(self.application_uri).scheme:
            raise ValueError("An explicit server application URI is required")
        _ID.validate_python(self.gateway_principal)
        for name in ("server_certificate", "server_private_key", "trusted_client_certificate"):
            path = Path(getattr(self, name))
            if not path.is_file():
                raise ValueError(f"Configured {name} must be an existing file")
            object.__setattr__(self, name, path)


def pinned_certificate_validator(certificate_path: Path, *, peer_server: bool = False):
    """Validate time, URI, usage, role, and exact pre-provisioned leaf certificate."""
    trusted = x509.load_der_x509_certificate(certificate_path.read_bytes())
    options = CertificateValidatorOptions.EXT_VALIDATION | (
        CertificateValidatorOptions.PEER_SERVER
        if peer_server
        else CertificateValidatorOptions.PEER_CLIENT
    )
    basic = CertificateValidator(options)
    fingerprint = trusted.fingerprint(hashes.SHA256())

    async def validate(cert: x509.Certificate, application: ua.ApplicationDescription) -> None:
        await basic(cert, application)
        if cert.fingerprint(hashes.SHA256()) != fingerprint:
            raise ServiceError(ua.StatusCodes.BadCertificateUntrusted)

    return validate


class SimulatorOPCUAServer:
    """One real secure server; no certificate provisioning or simulation timer.

    All public calls and controller mutations must use the same event-loop thread.
    Tags use an immutable snapshot pointer; read-service callbacks never yield.
    """

    def __init__(
        self,
        controller: SimulatorController,
        config: OPCUAServerConfig,
        *,
        field_channels: FieldChannels | None = None,
        field_monotonic_clock=None,
    ) -> None:
        self.controller = controller
        self.config = config
        if field_channels is not None and field_channels.storage is not controller.storage:
            raise ValueError("Field channels must share the simulator storage owner")
        self.field_channels = field_channels
        self._field_publication = None
        self._field_monotonic_clock = field_monotonic_clock
        self._server: Server | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._snapshot: dict[str, ua.DataValue] = {}
        self.namespace_index: int | None = None

    def _owning_loop(self) -> None:
        if self._loop is not asyncio.get_running_loop():
            raise RuntimeError("OPC adapter must run on its owning event loop")

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("OPC server is already started")
        self._loop = asyncio.get_running_loop()
        self._retire_field_publication()
        config = self.config
        cert = x509.load_der_x509_certificate(config.server_certificate.read_bytes())
        key = serialization.load_pem_private_key(
            config.server_private_key.read_bytes(), password=None
        )
        public_format = serialization.PublicFormat.SubjectPublicKeyInfo
        if cert.public_key().public_bytes(serialization.Encoding.DER, public_format) != (
            key.public_key().public_bytes(serialization.Encoding.DER, public_format)
        ):
            raise ValueError("Server certificate and private key do not match")
        # Reject misconfigured server identity before opening the listener.
        own_validator = CertificateValidator(
            CertificateValidatorOptions.EXT_VALIDATION | CertificateValidatorOptions.PEER_SERVER
        )
        await own_validator(
            cert,
            ua.ApplicationDescription(
                ApplicationUri=config.application_uri,
                ApplicationType=ua.ApplicationType.Server,
            ),
        )
        manager = CertificateUserManager()
        await manager.add_user(config.trusted_client_certificate, config.gateway_principal)
        server = Server(user_manager=manager)
        await server.init()
        server.set_endpoint(config.endpoint)
        server.set_server_name("HMI MES simulator")
        await server.set_application_uri(config.application_uri)
        await server.load_certificate(config.server_certificate)
        await server.load_private_key(config.server_private_key)
        server.set_security_policy([ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt])
        # Anonymous is only the UA user-token form. CertificateUserManager still
        # requires the validated peer application certificate and maps it to User.
        server.set_identity_tokens([ua.AnonymousIdentityToken])
        server.set_certificate_validator(
            pinned_certificate_validator(
                config.trusted_client_certificate,
            )
        )
        index = await server.register_namespace(NAMESPACE_URI)
        root = await server.nodes.objects.add_object(ua.NodeId(ROOT_NODE_ID, index), ROOT_NODE_ID)
        for tag in _CONTRACT["tags"]:
            variant_type = getattr(ua.VariantType, tag["data_type"])
            default = {"Boolean": False, "String": "", "Double": 0.0}.get(tag["data_type"], 0)
            nodeid = ua.NodeId(tag["node_id"]["identifier"], index)
            await root.add_variable(nodeid, tag["logical_name"], default, variant_type)
            # No set_writable: even setpoint/mode feedback stays read-only.
            server.set_attribute_value_callback(nodeid, self._read_value)
        for name in _METHOD_MODELS:
            await root.add_method(
                ua.NodeId(name, index),
                name,
                self._method(name),
                [ua.VariantType.String],
                [ua.VariantType.String],
            )
        self.namespace_index = index
        self._server = server
        try:
            await self.publish_snapshot(acquired_at=datetime.now(UTC), heartbeat=0)
            await server.start()
        except BaseException:
            try:
                await server.stop()
            finally:
                self._server = None
                self._retire_field_publication()
            raise

    async def stop(self) -> None:
        if self._server is not None:
            self._owning_loop()
            server, self._server = self._server, None
            try:
                await server.stop()
            finally:
                self._retire_field_publication()

    def _retire_field_publication(self):
        """Transport lifecycle boundary, separate from constructing a prepare adapter."""
        self._field_publication = None
        if self.field_channels is not None:
            self.field_channels._published = {}
            self.field_channels._field_publication_runtime = None

    def _read_value(self, nodeid: ua.NodeId, _attribute: ua.AttributeIds) -> ua.DataValue:
        return self._snapshot[nodeid.Identifier]

    def _method(self, name: str):
        # An async handler is essential: asyncua dispatches synchronous handlers
        # in a worker thread, which would break SQLite/thread/transaction ownership.
        @uamethod
        async def invoke(_parent, *arguments):
            self._owning_loop()
            if len(arguments) != 1 or not isinstance(arguments[0], str):
                return ua.CallMethodResult(
                    StatusCode=ua.StatusCode(ua.StatusCodes.BadInvalidArgument)
                )
            try:
                request = _METHOD_MODELS[name].model_validate_json(arguments[0])
            except (ValidationError, ValueError, TypeError):
                return ua.CallMethodResult(
                    StatusCode=ua.StatusCode(ua.StatusCodes.BadInvalidArgument)
                )
            # There is no await from request validation through durable controller return.
            if name == "SubmitCommand":
                response = self.controller.submit(
                    request,
                    now=datetime.now(UTC),
                    authority="REMOTE",
                    authenticated_gateway_principal=self.config.gateway_principal,
                ).submission
            elif name == "GetCommandResult":
                response = self.controller.get_result(request.command_id)
            elif name == "GetRunSummary":
                response = self.controller.get_summary(request.execution_id)
            else:
                response = self.controller.read_events(request)
            return response.model_dump_json()

        return invoke

    async def publish_snapshot(
        self,
        *,
        acquired_at: datetime,
        heartbeat: int,
        last_outcome: str | None = None,
        status_code: int = ua.StatusCodes.Good,
    ) -> None:
        """Atomically replace all 48 values with one acquisition/status boundary.

        No await occurs while capturing controller/process identity or swapping
        values. Callback reads preserve this boundary across a batch read. This
        polling adapter does not currently send subscription datachange events.
        """
        self._owning_loop()
        if self._server is None:
            raise RuntimeError("Start the OPC server before publishing")
        acquired_at = _TIME.validate_python(acquired_at)
        if type(status_code) is not int or not 0 <= status_code <= 2**32 - 1:
            raise ValueError("StatusCode must be an exact UInt32")
        plc = self.controller.state
        process = self.controller.storage.load_execution()
        values = project_tags(
            plc,
            process,
            counter_epoch=self.controller.storage.identity.counter_epoch,
            heartbeat=heartbeat,
            last_outcome=last_outcome,
        )
        expected = {tag["logical_name"] for tag in _CONTRACT["tags"]}
        if set(values) != expected:
            raise ValueError("Tag projection must match all 48 dictionary tags exactly")
        published_at = datetime.now(UTC)
        fields, prepared = None, None
        store = self.field_channels
        if store is not None:
            from apps.simulator.field_publication import FieldPublication

            if self._field_publication is None or self._field_publication.store is not store:
                kwargs = (
                    {}
                    if self._field_monotonic_clock is None
                    else {"monotonic_clock": self._field_monotonic_clock}
                )
                self._field_publication = FieldPublication(store, **kwargs)
            prepared = self._field_publication.prepare(
                values, acquired_at=acquired_at, published_at=published_at, status_code=status_code
            )
            fields = prepared.fields
        snapshot = {}
        for tag in _CONTRACT["tags"]:
            snapshot[tag["node_id"]["identifier"]] = ua.DataValue(
                Value=ua.Variant(
                    fields[tag["logical_name"]].reading.value
                    if fields
                    else values[tag["logical_name"]],
                    getattr(ua.VariantType, tag["data_type"]),
                ),
                StatusCode=ua.StatusCode(
                    fields[tag["logical_name"]].status_code if fields else status_code
                ),
                SourceTimestamp=fields[tag["logical_name"]].source_timestamp
                if fields
                else acquired_at,
                ServerTimestamp=published_at,
            )
        if prepared is None:
            self._snapshot = snapshot
        else:
            # All models/encoding/DB checks precede this trusted no-await seam.
            # Only already-existing reference attributes change after the first swap.
            frames = prepared.frames
            runtime_status = prepared.runtime_status

            def swap_snapshot_and_observations():
                self._snapshot = snapshot
                store._published = frames
                store._field_publication_runtime = runtime_status

            self._field_publication.publish(prepared, swap_snapshot_and_observations)
