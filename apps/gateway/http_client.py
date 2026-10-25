"""Authenticated immutable batch delivery; ambiguous delivery never releases disk data."""

import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from contracts.transport import DurableAck, GatewayBatch, validate_ack


class DeliveryUncertain(RuntimeError):
    """No bound durable ACK; retain the original batch and identity for retry."""


class DeliveryRejected(RuntimeError):
    """API explicitly rejected this batch; retain it for diagnosis/reconciliation."""


class DeliveryRetentionExpired(DeliveryRejected):
    """Permanent raw expiry: stop delivery and require explicit recovery."""

    reason_code = "RETENTION_EXPIRED"


@dataclass(frozen=True)
class IntakeConfig:
    base_url: str
    token: str = field(repr=False)
    allow_loopback_http: bool = False
    timeout_seconds: float = 10.0
    max_response_bytes: int = 4 * 1024 * 1024

    def __post_init__(self):
        parsed = urlsplit(self.base_url)
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or not parsed.hostname
        ):
            raise ValueError("Intake base URL must be a credential-free origin")
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and local and self.allow_loopback_http
        ):
            raise ValueError("HTTPS required except explicit loopback development HTTP")
        if (
            not isinstance(self.token, str)
            or len(self.token) < 32
            or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in self.token)
        ):
            raise ValueError("An explicit high entropy service token is required")
        if (
            not 0 < self.timeout_seconds <= 120
            or not 1024 <= self.max_response_bytes <= 16 * 1024 * 1024
        ):
            raise ValueError("Invalid delivery bounds")


class IntakeClient:
    def __init__(self, config: IntakeConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            headers={"Authorization": f"Bearer {config.token}"},
            timeout=config.timeout_seconds,
            follow_redirects=False,
            # Direct loopback is local IPC; remote HTTPS keeps environment proxy and CA trust.
            trust_env=urlsplit(config.base_url).hostname not in {"127.0.0.1", "localhost", "::1"},
            transport=transport,
        )

    async def close(self):
        await self._client.aclose()

    async def deliver(self, batch: GatewayBatch) -> DurableAck:
        # Roundtrip rejects forged pydantic model_copy instances before network I/O.
        validated = GatewayBatch.model_validate_json(batch.model_dump_json())
        try:
            async with self._client.stream(
                "POST",
                "/internal/v1/batches",
                content=validated.model_dump_json(),
                headers={"Content-Type": "application/json"},
            ) as response:
                if response.status_code == 410:
                    rejection = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(rejection) + len(chunk) > min(8192, self.config.max_response_bytes):
                            raise DeliveryUncertain("Intake rejection exceeds configured bound")
                        rejection.extend(chunk)
                    try:
                        reason = json.loads(bytes(rejection))
                    except (ValueError, UnicodeError, RecursionError):
                        raise DeliveryUncertain("Invalid intake rejection") from None
                    if (
                        isinstance(reason, dict)
                        and reason.get("reason_code") == "RETENTION_EXPIRED"
                    ):
                        raise DeliveryRetentionExpired("Original capture retention expired")
                    raise DeliveryUncertain("Unrecognized intake rejection")
                if response.status_code in {400, 401, 403, 409, 413, 422}:
                    raise DeliveryRejected(f"Intake rejected batch (HTTP {response.status_code})")
                if response.status_code != 200:
                    raise DeliveryUncertain("Intake did not return a committed receipt")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > self.config.max_response_bytes:
                        raise DeliveryUncertain("Intake response exceeds configured bound")
                    body.extend(chunk)
            ack = DurableAck.model_validate_json(bytes(body))
            validate_ack(validated, ack)
            return ack
        except (DeliveryRejected, DeliveryUncertain):
            raise
        except (httpx.HTTPError, ValueError):
            # Never include request headers, URL credentials or API error body in diagnostics.
            raise DeliveryUncertain("No valid durable receipt; retain original batch") from None

    async def clock_probe(self, probe):
        """A small bounded request using the same authenticated, TLS-verified intake."""
        import asyncio

        from contracts.clock_evidence import ClockProbeRequest, ClockProbeResponse

        validated = ClockProbeRequest.model_validate_json(probe.model_dump_json())
        try:
            async with asyncio.timeout(min(2.0, self.config.timeout_seconds)):
                async with self._client.stream(
                    "POST",
                    "/internal/v1/clock-probes",
                    content=validated.model_dump_json(),
                    headers={"Content-Type": "application/json"},
                    timeout=min(2.0, self.config.timeout_seconds),
                ) as response:
                    if response.status_code in {400, 401, 403, 409, 413, 422}:
                        raise DeliveryRejected("Clock probe was rejected")
                    if response.status_code != 200:
                        raise DeliveryUncertain("Clock probe response unavailable")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > min(8192, self.config.max_response_bytes):
                            raise DeliveryUncertain("Clock probe response exceeds bound")
                        body.extend(chunk)
            return ClockProbeResponse.model_validate_json(bytes(body))
        except (DeliveryRejected, DeliveryUncertain):
            raise
        except (httpx.HTTPError, ValueError, TimeoutError):
            raise DeliveryUncertain("No valid clock probe response") from None
