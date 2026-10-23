"""Pinned OPC UA gateway collection and typed methods; no buffer or command worker.

One explicit read performs a 48-node batch. Reads may establish a new secure
session; operational method calls never reconnect or retry themselves. Timers,
first-send expiry, durable command delivery and ingestion ACKs belong to callers.
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from asyncua import Client, ua
from asyncua.common.utils import ServiceError
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
from asyncua.crypto.validator import CertificateValidator, CertificateValidatorOptions
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from pydantic import TypeAdapter

from contracts.models import (
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
    UTCTimestamp,
)
from contracts.tags.validate import load_contract
from contracts.transport import CaptureTime, TagSample

_DICTIONARY = load_contract()
NAMESPACE_URI = _DICTIONARY["namespace_uri"]
_TAGS = tuple(_DICTIONARY["tags"])
_HEARTBEATS = (
    "Line.Heartbeat",
    "Infeed.LinkHeartbeat",
    "Sealer.LinkHeartbeat",
    "Outfeed.LinkHeartbeat",
)
_TIME = TypeAdapter(UTCTimestamp)
_METHODS = {
    "SubmitCommand": TypeAdapter(CommandSubmission),
    "GetCommandResult": TypeAdapter(CommandResult | CommandNotFound),
    "ReadEvents": TypeAdapter(EventPage | EventReadError),
    "GetRunSummary": TypeAdapter(RunSummary | RunNotFound),
}


@dataclass(frozen=True)
class GatewayOPCUAConfig:
    endpoint: str
    application_uri: str
    client_certificate: Path
    client_private_key: Path
    trusted_server_certificate: Path
    server_application_uri: str
    poll_interval_seconds: float = 1.0
    timeout_seconds: float = 5.0

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
        for uri in (self.application_uri, self.server_application_uri):
            if not uri or not urlsplit(uri).scheme:
                raise ValueError("Explicit client and server application URIs are required")
        for value in (self.poll_interval_seconds, self.timeout_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("Polling interval and request timeout must be finite and positive")
        for name in ("client_certificate", "client_private_key", "trusted_server_certificate"):
            path = Path(getattr(self, name))
            if not path.is_file():
                raise ValueError(f"Configured {name} must be an existing file")
            object.__setattr__(self, name, path)


@dataclass(frozen=True)
class GatewayHealth:
    state: Literal["UNKNOWN", "GOOD", "DELAYED", "ALARM"]
    connected: bool
    read_age_seconds: float
    heartbeat_ages_seconds: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class GatewayObservation:
    acquired_at: datetime
    samples: tuple[TagSample, ...]
    health: GatewayHealth
    capture_time: CaptureTime | None = None


class GatewayReadError(RuntimeError):
    """No complete valid capture; never substitute a synthetic normal sample."""


class GatewayMethodUncertain(RuntimeError):
    """RPC/response failed; inspect/query the original ID instead of inventing FAILED."""

    def __init__(self, method: str, command_id: str | None, *, delivery_possible: bool) -> None:
        self.method = method
        self.command_id = command_id
        self.delivery_possible = delivery_possible
        super().__init__(f"{method} has no validated response; reconcile its original request")


class GatewayOPCUAClient:
    """Caller-driven collector. Use one owning loop; no tasks or retry queue created."""

    def __init__(
        self,
        config: GatewayOPCUAConfig,
        *,
        utc_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._utc_clock = utc_clock
        self._monotonic_clock = monotonic_clock
        self._clock_epoch = str(uuid4())
        self._last_monotonic: float | None = None
        self._started = self._monotonic()
        self._last_read: float | None = None
        self._heartbeat: dict[str, tuple[int, float]] = {}
        self._client: Client | None = None
        self._nodes = ()
        self._root = None
        self.namespace_index: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connection_lock = asyncio.Lock()

    def _monotonic(self) -> float:
        value = self._monotonic_clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            self._clock_epoch = str(uuid4())
            self._last_monotonic = None
            self._last_read = None
            self._heartbeat = {}
            raise ValueError("Monotonic clock must supply a finite number")
        previous = self._last_monotonic
        self._last_monotonic = value
        if previous is None:
            self._started = value
        if previous is not None and value < previous:
            self._clock_epoch = str(uuid4())
            self._started = value
            self._last_read = None
            self._heartbeat.clear()
            raise ValueError("Monotonic clock regressed; capture continuity reset")
        return value

    def clock_reading(self) -> tuple[str, datetime, float]:
        """Read the same injected clocks and epoch used by OPC capture, without I/O."""
        self._monotonic()
        utc = _TIME.validate_python(self._utc_clock())
        monotonic = self._monotonic()
        return self._clock_epoch, utc, monotonic

    def _owning_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("Gateway client must use its owning event loop")

    def _server_validator(self):
        config = self.config
        trusted = x509.load_der_x509_certificate(config.trusted_server_certificate.read_bytes())
        fingerprint = trusted.fingerprint(hashes.SHA256())
        basic = CertificateValidator(
            CertificateValidatorOptions.EXT_VALIDATION | CertificateValidatorOptions.PEER_SERVER
        )
        hostname = urlsplit(config.endpoint).hostname

        async def validate(cert: x509.Certificate, application: ua.ApplicationDescription) -> None:
            await basic(cert, application)
            if cert.fingerprint(hashes.SHA256()) != fingerprint:
                raise ServiceError(ua.StatusCodes.BadCertificateUntrusted)
            if application.ApplicationUri != config.server_application_uri:
                raise ServiceError(ua.StatusCodes.BadCertificateUriInvalid)
            names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            try:
                address = ipaddress.ip_address(hostname)
            except ValueError:
                matched = hostname.casefold() in {
                    name.casefold() for name in names.get_values_for_type(x509.DNSName)
                }
            else:
                matched = address in names.get_values_for_type(x509.IPAddress)
            if not matched:
                raise ServiceError(ua.StatusCodes.BadCertificateHostNameInvalid)

        return validate

    async def connect(self) -> None:
        """Explicit secure connection and namespace discovery; no operational request."""
        self._owning_loop()
        async with self._connection_lock:
            await self._connect()

    async def _connect(self) -> None:
        if self._client is not None:
            return
        config = self.config
        cert = x509.load_der_x509_certificate(config.client_certificate.read_bytes())
        key = serialization.load_pem_private_key(
            config.client_private_key.read_bytes(), password=None
        )
        public_format = serialization.PublicFormat.SubjectPublicKeyInfo
        if cert.public_key().public_bytes(serialization.Encoding.DER, public_format) != (
            key.public_key().public_bytes(serialization.Encoding.DER, public_format)
        ):
            raise ValueError("Client certificate and private key do not match")
        own_validator = CertificateValidator(
            CertificateValidatorOptions.EXT_VALIDATION | CertificateValidatorOptions.PEER_CLIENT
        )
        await own_validator(
            cert,
            ua.ApplicationDescription(
                ApplicationUri=config.application_uri,
                ApplicationType=ua.ApplicationType.Client,
            ),
        )
        client = Client(config.endpoint, timeout=config.timeout_seconds, auto_reconnect=False)
        client.application_uri = config.application_uri
        client.certificate_validator = self._server_validator()
        await client.set_security(
            SecurityPolicyBasic256Sha256,
            config.client_certificate,
            config.client_private_key,
            server_certificate=str(config.trusted_server_certificate),
            mode=ua.MessageSecurityMode.SignAndEncrypt,
        )
        try:
            await client.connect()
            index = await client.get_namespace_index(NAMESPACE_URI)
            nodes = tuple(
                client.get_node(ua.NodeId(tag["node_id"]["identifier"], index)) for tag in _TAGS
            )
            self._root = client.get_node(ua.NodeId("Simulator", index))
            self.namespace_index, self._nodes, self._client = index, nodes, client
        except BaseException:
            try:
                await client.disconnect()
            except Exception:
                pass
            raise

    async def disconnect(self) -> None:
        self._owning_loop()
        async with self._connection_lock:
            await self._disconnect()

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        self._root, self._nodes, self.namespace_index = None, (), None
        if client is not None:
            await client.disconnect()

    def health(self) -> GatewayHealth:
        """Communication/heartbeat freshness, independent of source timestamps or wall time."""
        try:
            now = self._monotonic()
        except ValueError:
            return GatewayHealth(
                "UNKNOWN",
                self._client is not None,
                0.0,
                tuple((name, 0.0) for name in _HEARTBEATS),
            )
        read_age = max(
            0.0, now - (self._last_read if self._last_read is not None else self._started)
        )
        ages = tuple(
            (name, max(0.0, now - self._heartbeat.get(name, (0, self._started))[1]))
            for name in _HEARTBEATS
        )
        age = max(read_age, *(value for _, value in ages))
        if age >= 5.0:
            state = "ALARM"
        elif age >= 3.0:
            state = "DELAYED"
        elif (
            self._client is None
            or self._last_read is None
            or len(self._heartbeat) != len(_HEARTBEATS)
        ):
            state = "UNKNOWN"
        else:
            state = "GOOD"
        return GatewayHealth(state, self._client is not None, read_age, ages)

    async def read_tags(self) -> GatewayObservation:
        """One complete 48-node read; failed sessions reconnect only on a later read.

        Bad/Uncertain raw values and absent original timestamps remain unchanged.
        Wrong dictionary types/enums fail the capture, rather than coerce a value.
        """
        self._owning_loop()
        try:
            await self.connect()
            values = await self._client.read_attributes(self._nodes, ua.AttributeIds.Value)
            before = self._monotonic()
            acquired_at = _TIME.validate_python(self._utc_clock())
            after = self._monotonic()
            capture_time = CaptureTime(
                clock_epoch=self._clock_epoch,
                monotonic_seconds=after,
                utc_read_span_seconds=after - before,
            )
            if len(values) != len(_TAGS):
                raise ValueError("Read must return all 48 dictionary values")
            samples = []
            for tag, value in zip(_TAGS, values, strict=True):
                if value.StatusCode is None or value.Value is None:
                    raise ValueError("Missing raw OPC status or variant")
                raw = value.Value.Value
                severity = value.StatusCode.value >> 30
                if value.Value.VariantType != getattr(ua.VariantType, tag["data_type"]):
                    if (
                        raw is not None
                        or severity == 0
                        or value.Value.VariantType != ua.VariantType.Null
                    ):
                        raise ValueError(
                            f"OPC data type differs from dictionary for {tag['logical_name']}"
                        )
                samples.append(
                    TagSample(
                        sample_id=str(uuid4()),
                        tag_id=tag["logical_name"],
                        acquired_at=acquired_at,
                        source_timestamp=value.SourceTimestamp,
                        server_timestamp=value.ServerTimestamp,
                        status_code=value.StatusCode.value,
                        reading={"data_type": tag["data_type"], "value": raw},
                    )
                )
            now = self._monotonic()
            # Only a fully validated capture changes receipt/heartbeat freshness.
            self._last_read = now
            for sample in samples:
                if sample.tag_id in _HEARTBEATS and sample.status_code >> 30 == 0:
                    previous = self._heartbeat.get(sample.tag_id)
                    if previous is None or previous[0] != sample.reading.value:
                        self._heartbeat[sample.tag_id] = (sample.reading.value, now)
            health = self.health()
            if capture_time.clock_epoch != self._clock_epoch:
                raise ValueError("Clock continuity changed during capture")
            return GatewayObservation(acquired_at, tuple(samples), health, capture_time)
        except Exception as error:
            try:
                await self.disconnect()
            except Exception:
                pass
            raise GatewayReadError("No valid complete OPC tag capture") from error

    async def _method(self, name: str, request):
        self._owning_loop()
        command_id = getattr(request, "command_id", None)
        if self._client is None:
            raise GatewayMethodUncertain(name, command_id, delivery_possible=False)
        try:
            payload = await self._root.call_method(
                ua.NodeId(name, self.namespace_index),
                request.model_dump_json(),
            )
            if not isinstance(payload, str):
                raise ValueError("OPC method response must be one JSON String")
            response = _METHODS[name].validate_json(payload)
            if name in {"SubmitCommand", "GetCommandResult"}:
                if response.command_id != request.command_id:
                    raise ValueError("Method response belongs to another command ID")
            elif name == "GetRunSummary":
                if response.execution_id != request.execution_id:
                    raise ValueError("Method response belongs to another execution ID")
            else:
                if response.after_seq != request.after_seq:
                    raise ValueError("Event response does not match requested cursor")
                mismatch = (response.source_id, response.stream_id) != (
                    request.source_id,
                    request.stream_id,
                )
                if mismatch and not (
                    isinstance(response, EventReadError) and response.code == "STREAM_MISMATCH"
                ):
                    raise ValueError("Event response belongs to another source/stream")
                if isinstance(response, EventPage) and len(response.events) > request.limit:
                    raise ValueError("Event response exceeds requested page size")
            return response
        except Exception as error:
            raise GatewayMethodUncertain(name, command_id, delivery_possible=True) from error

    async def submit_command(self, request: CommandRequest) -> CommandSubmission:
        return await self._method("SubmitCommand", CommandRequest.model_validate(request))

    async def get_command_result(
        self,
        request: CommandResultRequest,
    ) -> CommandResult | CommandNotFound:
        return await self._method("GetCommandResult", CommandResultRequest.model_validate(request))

    async def read_events(self, request: ReadEventsRequest) -> EventPage | EventReadError:
        return await self._method("ReadEvents", ReadEventsRequest.model_validate(request))

    async def get_run_summary(self, request: RunSummaryRequest) -> RunSummary | RunNotFound:
        return await self._method("GetRunSummary", RunSummaryRequest.model_validate(request))
