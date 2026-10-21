"""Atomic application stream over real sockets and disposable PostgreSQL."""

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
import test_screen_ws as legacy

import apps.api.screen_ws as screen_ws
from apps.api.screen_frames import APP_SCREEN_ADAPTER, ScreenDataFrame
from contracts.models import UINT64_MAX
from contracts.transport import BatchBody, GatewayBatch, validate_screen_update

# Reuse the real PostgreSQL/uvicorn fixture, while giving this module its own DB.
legacy_state = legacy.state


@pytest.fixture(params=["snapshot", "screen"])
async def state(legacy_state, request):
    legacy_state.url = legacy_state.url.removesuffix("snapshot") + request.param
    return legacy_state


def envelope_connection(state):
    state.url = state.url.rsplit("/", 1)[0] + "/screen"
    return legacy.connection(state)


async def receive(ws):
    return APP_SCREEN_ADAPTER.validate_json(await asyncio.wait_for(ws.recv(), 2))


@pytest.mark.parametrize("action", ["revoke", "roles", "password", "inactive", "idle", "absolute"])
async def test_both_routes_recheck_auth_without_idle_refresh(state, action):
    # This existing behavioral test only parses the initial flat messages. Its
    # frame reader is adapted locally, leaving the wire route itself unchanged.
    original = legacy.frame

    async def inner(ws):
        raw = await asyncio.wait_for(ws.recv(), 2)
        if state.url.endswith("screen"):
            return APP_SCREEN_ADAPTER.validate_json(raw).screen
        return legacy.SCREEN_ADAPTER.validate_json(raw)

    legacy.frame = inner
    try:
        await legacy.test_live_rechecks_close_real_socket_without_timer_idle_refresh(state, action)
    finally:
        legacy.frame = original


async def test_both_routes_auto_reconnect_does_not_extend_idle(state):
    original = legacy.frame

    async def inner(ws):
        raw = await asyncio.wait_for(ws.recv(), 2)
        if state.url.endswith("screen"):
            return APP_SCREEN_ADAPTER.validate_json(raw).screen
        return legacy.SCREEN_ADAPTER.validate_json(raw)

    legacy.frame = inner
    try:
        await legacy.test_automatic_reconnect_at_29_minutes_does_not_renew_30_minute_idle(state)
    finally:
        legacy.frame = original


@pytest.mark.parametrize("failure", ["origin", "cookie", "query", "duplicate-origin", "host"])
async def test_both_routes_exact_handshake_boundary(state, failure):
    await legacy.test_actual_handshake_rejects_wrong_boundary_before_any_screen(state, failure)


@pytest.mark.parametrize("changes", [{"csrf_token": "x" * 43}, {"stream_id": "foreign"}])
async def test_both_routes_first_frame_csrf_and_scope(state, changes):
    await legacy.test_first_frame_csrf_scope_and_schema_fail_without_snapshot(state, changes)


async def test_envelope_atomic_metadata_aging_order_reconnect_and_legacy_shape(legacy_state):
    state = legacy_state
    legacy.ingest(state)
    async with envelope_connection(state) as ws:
        await legacy.subscribe(ws, state)
        first = await receive(ws)
        assert isinstance(first, ScreenDataFrame)
        assert first.scope.model_dump() == {
            "line_id": legacy.LINE_ID,
            "source_id": "source",
            "stream_id": "stream",
        }
        assert first.screen.kind == "SNAPSHOT" and first.screen.revision == 0
        assert first.health.heartbeat == "GOOD" and first.health.state == "DEGRADED"
        assert {e.tag_id for e in first.evidence} == {s.tag_id for s in first.screen.view.samples}
        assert {e.batch_id for e in first.evidence} == {"batch"}
        assert all(e.fresh and e.capture_seq == 1 for e in first.evidence)
        assert not set(first.missing_tag_ids) & {e.tag_id for e in first.evidence}
        assert (
            next(s.reading.value for s in first.screen.view.samples if s.tag_id == "Line.GoodTotal")
            == UINT64_MAX
        )
        next_frame = await receive(ws)
        validate_screen_update(first.screen, next_frame.screen)
        state.clock[0] += legacy.timedelta(seconds=5)
        for _ in range(10):
            aged = await receive(ws)
            if aged.health.state == "STALE":
                break
        assert aged.health.state == "STALE" and aged.health.heartbeat == "ALARM"
        assert all(not e.fresh for e in aged.evidence)
        assert aged.screen.view.state == "UNKNOWN"
        assert aged.evidence[0].captured_at == first.evidence[0].captured_at
    async with envelope_connection(state) as ws:
        await legacy.subscribe(ws, state)
        reconnect = await receive(ws)
        assert reconnect.screen.kind == "SNAPSHOT"
        assert reconnect.screen.sync_id != first.screen.sync_id
        with pytest.raises(ValueError):
            validate_screen_update(reconnect.screen, next_frame.screen)
    state.url = state.url.removesuffix("screen") + "snapshot"
    async with legacy.connection(state) as ws:
        await legacy.subscribe(ws, state)
        wire = json.loads(await asyncio.wait_for(ws.recv(), 2))
        assert set(wire) == {"schema_version", "kind", "sync_id", "revision", "emitted_at", "view"}


async def test_coherent_projection_blocks_concurrent_ingest_until_shared_line_lock_releases(
    legacy_state, monkeypatch
):
    state = legacy_state
    legacy.ingest(state)
    with state.engine.connect() as db:
        old = db.execute(legacy.sa.text("SELECT body FROM ingest_batches WHERE batch_id='batch'"))
        body = dict(old.scalar_one())
    body.update(batch_id="next-batch", capture_seq="2")
    for sample in body["samples"]:
        sample["sample_id"] += "-next"
        if sample["tag_id"] == "Line.GoodTotal":
            sample["reading"]["value"] = "7"
    body = BatchBody.model_validate(body)
    batch = GatewayBatch(body=body, content_sha256=legacy.batch_digest(body))
    attempted = Event()

    def ingest_next():
        attempted.set()
        return state.service.ingest(
            batch,
            authenticated_gateway_id="gateway",
            authenticated_source_id="source",
            authenticated_stream_id="stream",
            now=state.clock[0],
        )

    original = screen_ws.read_snapshot
    future = None
    with ThreadPoolExecutor(max_workers=1) as pool:

        def interleaved_read(*args, **kwargs):
            nonlocal future
            result = original(*args, **kwargs)
            if future is None:
                future = pool.submit(ingest_next)
                assert attempted.wait(1)
                # Observe PostgreSQL's actual blocked lock request; scheduling a
                # worker alone would not prove that FOR SHARE guards the read.
                deadline = time.monotonic() + 1
                with state.engine.connect() as probe:
                    while True:
                        waiting = probe.execute(
                            legacy.sa.text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE wait_event_type='Lock' AND query LIKE '%lines%' "
                                "AND pid <> pg_backend_pid()"
                            )
                        ).scalar_one()
                        if waiting:
                            break
                        assert time.monotonic() < deadline, "ingest did not block on line lock"
                        probe.commit()
                        time.sleep(0.01)
                assert not future.done()
            return result

        monkeypatch.setattr(screen_ws, "read_snapshot", interleaved_read)
        async with envelope_connection(state) as ws:
            await legacy.subscribe(ws, state)
            first = await receive(ws)
            assert {e.batch_id for e in first.evidence} == {"batch"}
            assert (
                next(
                    s.reading.value
                    for s in first.screen.view.samples
                    if s.tag_id == "Line.GoodTotal"
                )
                == UINT64_MAX
            )
            await asyncio.to_thread(future.result, 2)
            for _ in range(10):
                current = await receive(ws)
                if {e.batch_id for e in current.evidence} == {"next-batch"}:
                    break
            assert {e.capture_seq for e in current.evidence} == {2}
            assert (
                next(
                    s.reading.value
                    for s in current.screen.view.samples
                    if s.tag_id == "Line.GoodTotal"
                )
                == 7
            )
            assert current.health.communication_captured_at == current.evidence[0].captured_at


@pytest.mark.parametrize("pressure", ["queue", "send"])
async def test_envelope_pressure_reset_uses_same_shared_writer_and_excludes_health(
    legacy_state, monkeypatch, pressure
):
    # Run the established deterministic blocked-ASGI test on the envelope route.
    original_mount = legacy.mount_screen_ws
    original_adapter = legacy.SCREEN_ADAPTER

    class InnerAdapter:
        @staticmethod
        def validate_json(raw):
            parsed = APP_SCREEN_ADAPTER.validate_json(raw)
            if parsed.screen.kind == "RESYNC_REQUIRED":
                assert set(json.loads(raw)) == {"schema_version", "scope", "screen"}
            return parsed.screen

    def mount(app, *args, **kwargs):
        original_mount(app, *args, **kwargs)
        # Test's synthetic ASGI path is legacy; route it through envelope handler.
        atomic = next(r for r in app.routes if getattr(r, "path", None) == "/ws/v1/screen")
        flat = next(r for r in app.routes if getattr(r, "path", None) == "/ws/v1/snapshot")
        flat.app = atomic.app

    monkeypatch.setattr(legacy, "mount_screen_ws", mount)
    monkeypatch.setattr(legacy, "SCREEN_ADAPTER", InnerAdapter)
    try:
        await legacy.test_deterministic_blocked_asgi_writer_bounds_and_releases_db_locks(
            legacy_state, pressure
        )
    finally:
        legacy.SCREEN_ADAPTER = original_adapter
