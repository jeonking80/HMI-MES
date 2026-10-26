"""Response maintenance stays bounded and belongs to the ASGI invocation."""

import asyncio
import threading

import pytest
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse

from apps.api import ingest_retention as retention


class Engine:
    pass


def test_response_body_is_sent_before_maintenance_and_invocation_waits(monkeypatch):
    engine = Engine()
    started, release = threading.Event(), threading.Event()
    sent = []

    def prune(selected_engine, hours):
        assert selected_engine is engine and hours == 3
        assert sent[-1]["type"] == "http.response.body"
        assert not sent[-1].get("more_body", False)
        started.set()
        assert release.wait(5)

    async def send(message):
        sent.append(message)

    async def exercise():
        response = JSONResponse(
            {"durability": "COMMITTED"},
            background=BackgroundTask(retention.schedule_ingest_maintenance, engine, 3),
        )
        invocation = asyncio.create_task(response({"type": "http"}, None, send))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            assert not invocation.done()
        finally:
            release.set()
            await invocation

    monkeypatch.setattr(retention, "maybe_prune_ingest", prune)
    asyncio.run(exercise())


def test_slow_maintenance_never_queues_duplicate_database_work(monkeypatch):
    engine = Engine()
    started, release = threading.Event(), threading.Event()
    calls = []
    tick = [100.0]

    def prune(selected_engine, hours):
        calls.append((selected_engine, hours))
        started.set()
        assert release.wait(5)

    monkeypatch.setattr(retention, "maybe_prune_ingest", prune)
    monkeypatch.setattr(retention.time, "monotonic", lambda: tick[0])
    worker = threading.Thread(target=retention.schedule_ingest_maintenance, args=(engine, 3))
    worker.start()
    try:
        assert started.wait(5)
        # Elapsing the interval cannot admit a second in-flight DB transaction.
        tick[0] += 120
        for _ in range(100):
            retention.schedule_ingest_maintenance(engine, 3)
        assert calls == [(engine, 3)]
    finally:
        release.set()
        worker.join(5)
    retention.schedule_ingest_maintenance(engine, 3)
    assert calls == [(engine, 3)]
    tick[0] += 60
    retention.schedule_ingest_maintenance(engine, 3)
    assert calls == [(engine, 3), (engine, 3)]


def test_unexpected_failure_releases_inflight_reservation(monkeypatch):
    engine = Engine()
    tick = [100.0]
    calls = []

    def fail(*args):
        calls.append(args)
        raise RuntimeError("injected unexpected failure")

    monkeypatch.setattr(retention, "maybe_prune_ingest", fail)
    monkeypatch.setattr(retention.time, "monotonic", lambda: tick[0])
    with pytest.raises(RuntimeError, match="injected unexpected failure"):
        retention.schedule_ingest_maintenance(engine)
    assert retention._MAINTENANCE_STATE[engine][0] is False
    retention.schedule_ingest_maintenance(engine)
    assert len(calls) == 1
    tick[0] += 60
    with pytest.raises(RuntimeError, match="injected unexpected failure"):
        retention.schedule_ingest_maintenance(engine)
    assert len(calls) == 2
