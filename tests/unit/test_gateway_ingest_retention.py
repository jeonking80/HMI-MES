"""Authenticated permanent expiry stops delivery while preserving the original disk batch."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from apps.gateway.buffer import GatewayBuffer
from apps.gateway.http_client import (
    DeliveryRetentionExpired,
    DeliveryUncertain,
    IntakeClient,
    IntakeConfig,
)
from apps.gateway.worker import GatewayPump
from tests.integration.test_gateway_buffer import AT, make_batch


async def test_permanent_expiry_keeps_original_buffer_and_propagates_diagnostic(tmp_path):
    original = make_batch()

    async def handler(request):
        assert request.content == original.model_dump_json().encode()
        return httpx.Response(410, json={"reason_code": "RETENTION_EXPIRED"})

    client = IntakeClient(
        IntakeConfig("https://intake.example", "x" * 40),
        transport=httpx.MockTransport(handler),
    )
    try:
        with GatewayBuffer(tmp_path / "queue", gateway_id="gateway-A") as buffer:
            buffer.enqueue(original, now=AT)
            pump = GatewayPump(
                "source-A",
                "stream-A",
                buffer=buffer,
                intake=client,
                opc=SimpleNamespace(config=SimpleNamespace(poll_interval_seconds=0.01)),
            )
            with pytest.raises(DeliveryRetentionExpired) as error:
                await pump.deliver_once()
            assert error.value.reason_code == "RETENTION_EXPIRED"
            assert buffer.lease_next().batch == original
            buffer.retry(original.body.batch_id)

            # Cycle must fail permanently instead of swallowing the diagnostic in
            # its ordinary uncertain-delivery retry path.
            async def no_capture():
                return None

            pump.capture_once = no_capture
            with pytest.raises(DeliveryRetentionExpired):
                await asyncio.wait_for(pump.cycle(), timeout=1)
            assert buffer.lease_next().batch == original
    finally:
        await client.close()


@pytest.mark.parametrize("payload", [b"not-json", b"[]", b'{"reason_code":"OTHER"}', b"x" * 8193])
async def test_untrusted_or_oversized_410_cannot_become_permanent_expiry(payload):
    async def handler(request):
        return httpx.Response(410, content=payload)

    client = IntakeClient(
        IntakeConfig("https://intake.example", "x" * 40),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DeliveryUncertain):
            await client.deliver(make_batch())
    finally:
        await client.close()
