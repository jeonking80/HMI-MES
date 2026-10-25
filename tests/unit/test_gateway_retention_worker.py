"""Bounded ledger maintenance runs independently of network waits and ACKs."""

import asyncio
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from apps.gateway import worker
from apps.gateway.buffer import BufferCorruption, GatewayBuffer
from apps.gateway.worker import GatewayPump
from tests.integration.test_gateway_buffer import AT, make_ack, make_batch


def make_pump(buffer):
    return GatewayPump(
        "source-A",
        "stream-A",
        opc=SimpleNamespace(config=SimpleNamespace(poll_interval_seconds=0.01)),
        buffer=buffer,
        intake=SimpleNamespace(),
    )


async def test_periodic_prune_bounded_utc_owner_thread_and_independent(monkeypatch):
    interval = 0.01
    assert worker.LEDGER_PRUNE_INTERVAL_SECONDS == 3600
    monkeypatch.setattr(worker, "LEDGER_PRUNE_INTERVAL_SECONDS", interval)
    assert worker.LEDGER_PRUNE_LIMIT == 1000
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    owner = threading.get_ident()
    before = datetime.now(UTC)
    started = loop.time()
    calls, cancelled = [], []

    def prune(*, now, limit):
        calls.append((loop.time(), now, limit, threading.get_ident()))
        return {"pruned_rows": 0}

    pump = make_pump(SimpleNamespace(prune_acknowledged=prune))

    async def blocked():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    pump.capture_once = blocked
    pump.deliver_once = blocked

    def status(value):
        assert value == {"operation": "maintenance", "state": "pruned", "error": None}
        if len(calls) == 2:
            stop.set()

    await asyncio.wait_for(pump.run(stop, on_status=status), timeout=1)
    assert len(calls) == 2
    assert calls[0][0] - started < interval
    assert calls[1][0] - calls[0][0] >= interval
    assert all(before <= now <= datetime.now(UTC) for _, now, _, _ in calls)
    assert all(
        now.tzinfo is UTC and limit == 1000 and thread == owner for _, now, limit, thread in calls
    )
    assert len(cancelled) == 2


async def test_full_maintenance_chunk_continues_without_another_hour_wait(monkeypatch):
    monkeypatch.setattr(worker, "LEDGER_PRUNE_INTERVAL_SECONDS", 0.2)
    stop = asyncio.Event()
    calls = []

    def prune(*, now, limit):
        calls.append((asyncio.get_running_loop().time(), limit))
        if len(calls) == 3:
            stop.set()
        return {"pruned_rows": limit if len(calls) < 3 else 7}

    pump = make_pump(SimpleNamespace(prune_acknowledged=prune))

    async def blocked():
        await asyncio.Event().wait()

    pump.capture_once = blocked
    pump.deliver_once = blocked
    await asyncio.wait_for(pump.run(stop), timeout=1)
    assert len(calls) == 3
    assert all(limit == 1000 for _, limit in calls)
    assert calls[-1][0] - calls[0][0] < 0.2


async def test_prune_corruption_stops_and_cancels_workers(monkeypatch):
    monkeypatch.setattr(worker, "LEDGER_PRUNE_INTERVAL_SECONDS", 0.001)
    cancelled = []

    def prune(**kwargs):
        raise BufferCorruption("checkpoint mismatch")

    pump = make_pump(SimpleNamespace(prune_acknowledged=prune))

    async def blocked():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    pump.capture_once = blocked
    pump.deliver_once = blocked
    with pytest.raises(BufferCorruption, match="checkpoint mismatch"):
        await asyncio.wait_for(pump.run(asyncio.Event()), timeout=1)
    assert len(cancelled) == 2


async def test_ack_does_not_trigger_prune(tmp_path, monkeypatch):
    with GatewayBuffer(tmp_path / "queue", gateway_id="gateway-A") as buffer:
        batch = make_batch()
        buffer.enqueue(batch, now=AT)

        def unexpected(**kwargs):
            pytest.fail("ACK must not run ledger maintenance")

        monkeypatch.setattr(buffer, "prune_acknowledged", unexpected)
        pump = make_pump(buffer)

        async def deliver(value):
            return make_ack(value)

        pump.intake.deliver = deliver
        assert await pump.deliver_once()
