import json
from pathlib import Path

import httpx
import pytest

from apps.gateway.http_client import DeliveryRejected, DeliveryUncertain, IntakeClient, IntakeConfig
from contracts.transport import GatewayBatch


def batch():
    return GatewayBatch.model_validate_json(
        Path("contracts/examples/transport-batch.json").read_text()
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://u:p@example.com",
        "https://example.com/a",
        "https://example.com/?token=a",
    ],
)
def test_insecure_or_credential_urls_rejected(url):
    with pytest.raises(ValueError):
        IntakeConfig(url, "x" * 40, allow_loopback_http=True)


async def test_delivery_does_not_follow_redirect_or_retry_uncertain_command():
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://untrusted.invalid/"})

    client = IntakeClient(
        IntakeConfig("https://intake.example", "x" * 40), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(DeliveryUncertain):
            await client.deliver(batch())
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.parametrize("status", [401, 403, 409, 413])
async def test_explicit_rejection_preserves_original_request(status):
    value = batch()

    async def handler(request):
        assert GatewayBatch.model_validate_json(request.content) == value
        return httpx.Response(status, json={"secret": "must-not-leak"})

    client = IntakeClient(
        IntakeConfig("https://intake.example", "x" * 40), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(DeliveryRejected) as error:
            await client.deliver(value)
        assert "must-not-leak" not in str(error.value)
    finally:
        await client.close()


async def test_receipt_must_bind_original_batch_identity_and_digest():
    ack = json.loads(Path("contracts/examples/transport-ack.json").read_text())
    ack["content_sha256"] = "0" * 64

    async def handler(request):
        return httpx.Response(200, json=ack)

    client = IntakeClient(
        IntakeConfig("https://intake.example", "x" * 40), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(DeliveryUncertain):
            await client.deliver(batch())
    finally:
        await client.close()
