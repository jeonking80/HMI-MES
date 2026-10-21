"""Actual WebSocket sockets + PostgreSQL; no browser/OPC completion claims."""

import asyncio
import json
import secrets
import socket
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
import uvicorn
from fastapi import FastAPI
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from apps.api.auth import UserAuth
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.ingestion import IngestionService
from apps.api.screen_ws import ScreenWSConfig, mount_screen_ws
from contracts.models import UINT64_MAX
from contracts.transport import (
    SCREEN_ADAPTER,
    BatchBody,
    GatewayBatch,
    ScreenSnapshot,
    ScreenUpdate,
    batch_digest,
    validate_screen_update,
)


class Server(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield


def member(auth, origin, login="viewer", roles=frozenset({"viewer"}), changed=True):
    password = secrets.token_urlsafe(24)
    auth.bootstrap_user(login, password, roles=roles)
    challenge = auth.begin_login()
    session = auth.login(
        login,
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=origin,
    )
    if changed:
        session = auth.change_password(
            session.token, secrets.token_urlsafe(24), csrf_token=session.csrf_token, origin=origin
        )
    return session


@pytest.fixture
async def state(migrated):
    clock = [datetime.now(UTC)]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    bootstrap_reference_data(migrated)
    service = IngestionService(migrated)
    service.configure_source(
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        line_id=LINE_ID,
        capture_generation=1,
        now=clock[0],
    )
    auth = UserAuth(migrated, allowed_origins={origin}, clock=lambda: clock[0])
    session = member(auth, origin)
    app = FastAPI()
    mount_screen_ws(
        app,
        auth,
        origin=origin,
        allow_insecure_loopback=True,
        clock=lambda: clock[0],
        config=ScreenWSConfig(
            poll_interval_seconds=0.05, subscribe_timeout_seconds=0.1, send_timeout_seconds=0.1
        ),
    )
    server = Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_level="critical",
            proxy_headers=False,
            lifespan="off",
            ws="websockets-sansio",
            ws_max_size=4096,
            ws_max_queue=1,
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            if task.done():
                task.result()
            await asyncio.sleep(0.01)
        assert server.started
        yield SimpleNamespace(
            auth=auth,
            engine=migrated,
            session=session,
            clock=clock,
            service=service,
            origin=origin,
            url=f"ws://127.0.0.1:{port}/ws/v1/snapshot",
        )
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=3)


def connection(state, *, token=None, origin=None, suffix="", extra_headers=()):
    headers = [("Cookie", "hmi_session=" + (token or state.session.token)), *extra_headers]
    return connect(
        state.url + suffix,
        origin=origin or state.origin,
        additional_headers=headers,
        proxy=None,
        max_size=65536,
        open_timeout=2,
    )


async def subscribe(ws, state, **changes):
    await ws.send(
        json.dumps(
            dict(
                line_id=LINE_ID,
                source_id="source",
                stream_id="stream",
                csrf_token=state.session.csrf_token,
            )
            | changes
        )
    )


async def frame(ws):
    return SCREEN_ADAPTER.validate_json(await asyncio.wait_for(ws.recv(), timeout=2))


async def closed(ws):
    # Frames already authorized and handed to the OS may precede the close.
    # Producer/writer auth checks must eventually close rather than renew idle.
    for _ in range(32):
        try:
            await asyncio.wait_for(ws.recv(), timeout=2)
        except ConnectionClosed as error:
            return error.rcvd.code
    pytest.fail("WebSocket kept delivering after session invalidation")


def ingest(state, *, future=False):
    now = state.clock[0]
    captured = now + timedelta(seconds=10) if future else now
    samples = [
        {
            "sample_id": "sample-" + tag,
            "tag_id": tag,
            "source_timestamp": captured,
            "server_timestamp": captured,
            "acquired_at": captured,
            "status_code": 0,
            "reading": {"data_type": kind, "value": value},
        }
        for tag, kind, value in (
            ("Line.State", "Int32", 1),
            ("Line.GoodTotal", "UInt64", UINT64_MAX),
        )
    ]
    body = BatchBody(
        schema_version="1.1",
        batch_id="batch",
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        capture_generation=1,
        capture_seq=1,
        captured_at=captured,
        samples=samples,
        communication={
            "state": "GOOD",
            "connected": True,
            "read_age_seconds": 0.0,
            "heartbeat_ages_seconds": {
                name: 0.0
                for name in (
                    "Line.Heartbeat",
                    "Infeed.LinkHeartbeat",
                    "Sealer.LinkHeartbeat",
                    "Outfeed.LinkHeartbeat",
                )
            },
        },
    )
    state.service.ingest(
        GatewayBatch(body=body, content_sha256=batch_digest(body)),
        authenticated_gateway_id="gateway",
        authenticated_source_id="source",
        authenticated_stream_id="stream",
        now=now,
    )


async def test_real_socket_snapshot_first_full_replacements_and_reconnect_sync(state):
    async with connection(state) as ws:
        await subscribe(ws, state)
        initial = await frame(ws)
        assert isinstance(initial, ScreenSnapshot) and initial.revision == 0
        assert initial.view.state == "UNKNOWN" and initial.view.samples == ()
        update = await frame(ws)
        assert isinstance(update, ScreenUpdate)
        validate_screen_update(initial, update)
        assert update.view == initial.view  # Complete replacement, including unchanged projection.
        skipped = await frame(ws)
        with pytest.raises(ValueError):
            validate_screen_update(initial, skipped)
    async with connection(state) as ws:
        await subscribe(ws, state)
        reconnect = await frame(ws)
        assert isinstance(reconnect, ScreenSnapshot) and reconnect.sync_id != initial.sync_id
        with pytest.raises(ValueError):
            validate_screen_update(reconnect, update)


async def test_real_socket_raw_uint64_capture_aging_and_conservative_state(state):
    ingest(state)
    async with connection(state) as ws:
        await subscribe(ws, state)
        initial = await frame(ws)
        assert initial.view.state == "READY" and initial.view.control_restricted
        assert (
            next(
                item for item in initial.view.samples if item.tag_id == "Line.GoodTotal"
            ).reading.value
            == UINT64_MAX
        )
        state.clock[0] += timedelta(seconds=5)
        for _ in range(10):
            current = await frame(ws)
            if current.view.state == "UNKNOWN":
                break
        assert current.view.state == "UNKNOWN" and current.view.control_restricted
        value = next(item for item in current.view.samples if item.tag_id == "Line.GoodTotal")
        assert (
            value.reading.value == UINT64_MAX
            and value.acquired_at == initial.view.samples[0].acquired_at
        )
        assert '"18446744073709551615"' in current.model_dump_json()


async def test_future_capture_cannot_become_healthy_on_clock_catchup(state):
    ingest(state, future=True)
    async with connection(state) as ws:
        await subscribe(ws, state)
        assert (await frame(ws)).view.state == "UNKNOWN"
        state.clock[0] += timedelta(seconds=10)
        current = await frame(ws)
        assert current.view.state == "UNKNOWN" and current.view.control_restricted


@pytest.mark.parametrize("failure", ["origin", "cookie", "query", "duplicate-origin", "host"])
async def test_actual_handshake_rejects_wrong_boundary_before_any_screen(state, failure):
    options = {}
    if failure == "origin":
        options["origin"] = state.origin + "/"
    if failure == "cookie":
        options["token"] = secrets.token_urlsafe(32)
    if failure == "query":
        options["suffix"] = "?token=" + secrets.token_urlsafe(32)
    if failure == "duplicate-origin":
        options["extra_headers"] = [("Origin", state.origin)]
    if failure == "host":
        options["extra_headers"] = [("Host", "evil.example.test")]
    with pytest.raises(InvalidStatus) as error:
        async with connection(state, **options):
            pytest.fail("invalid handshake accepted")
    assert error.value.response.status_code in {400, 403}


@pytest.mark.parametrize(
    "changes", [{"csrf_token": "x" * 43}, {"stream_id": "foreign"}, {"extra": "forbidden"}]
)
async def test_first_frame_csrf_scope_and_schema_fail_without_snapshot(state, changes):
    async with connection(state) as ws:
        await subscribe(ws, state, **changes)
        assert await closed(ws) == 1008


async def test_subscription_deadline_and_no_post_subscribe_input(state):
    async with connection(state) as ws:
        assert await closed(ws) == 1008
    async with connection(state) as ws:
        await subscribe(ws, state)
        await frame(ws)
        await ws.send('{"command":"START"}')
        assert await closed(ws) == 1008


@pytest.mark.parametrize("action", ["revoke", "roles", "password", "inactive", "idle", "absolute"])
async def test_live_rechecks_close_real_socket_without_timer_idle_refresh(state, action):
    admin = member(state.auth, state.origin, "admin", {"equipment_admin"})
    if action == "absolute":
        # Begin near the absolute boundary with a recent explicit user activity.
        state.clock[0] += timedelta(hours=7, minutes=59)
        with state.engine.begin() as db:
            db.execute(
                sa.text(
                    "UPDATE auth_sessions SET last_seen_at=:now,idle_expires_at=expires_at "
                    "WHERE login='viewer'"
                ),
                {"now": state.clock[0]},
            )
    async with connection(state) as ws:
        await subscribe(ws, state)
        await frame(ws)
        original_seen = state.clock[0]
        await frame(ws)
        with state.engine.connect() as db:
            assert (
                db.execute(
                    sa.text("SELECT max(last_seen_at) FROM auth_sessions WHERE login='viewer'")
                ).scalar_one()
                == original_seen
            )
        protection = {"csrf_token": state.session.csrf_token, "origin": state.origin}
        if action == "revoke":
            state.auth.revoke(state.session.token, **protection)
        if action == "password":
            state.auth.change_password(state.session.token, secrets.token_urlsafe(24), **protection)
        if action == "roles":
            state.auth.set_user_roles(
                admin.token,
                "viewer",
                {"operator"},
                csrf_token=admin.csrf_token,
                origin=state.origin,
            )
        if action == "inactive":
            state.auth.set_user_active(
                admin.token, "viewer", False, csrf_token=admin.csrf_token, origin=state.origin
            )
        if action == "idle":
            state.clock[0] += timedelta(minutes=30)
        if action == "absolute":
            state.clock[0] += timedelta(minutes=1)
        assert await closed(ws) == 1008


async def test_initial_password_denied_before_accept(state):
    initial = member(state.auth, state.origin, "initial", changed=False)
    with pytest.raises(InvalidStatus) as error:
        async with connection(state, token=initial.token):
            pytest.fail("initial password received business subscription")
    assert error.value.response.status_code == 403


@pytest.mark.parametrize("pressure", ["queue", "send"])
async def test_deterministic_blocked_asgi_writer_bounds_and_releases_db_locks(state, pressure):
    # Socket buffers can absorb small frames and make slow-reader overflow
    # nondeterministic. Block only ASGI send here; auth/projection still use real PG.
    config = ScreenWSConfig(
        queue_size=2 if pressure == "queue" else 32,
        poll_interval_seconds=0.02 if pressure == "queue" else 0.05,
        send_timeout_seconds=0.3 if pressure == "queue" else 0.02,
    )
    app = FastAPI()
    mount_screen_ws(
        app,
        state.auth,
        origin=state.origin,
        allow_insecure_loopback=True,
        clock=lambda: state.clock[0],
        config=config,
    )
    incoming = asyncio.Queue()
    incoming.put_nowait({"type": "websocket.connect"})
    incoming.put_nowait(
        {
            "type": "websocket.receive",
            "text": json.dumps(
                {
                    "line_id": LINE_ID,
                    "source_id": "source",
                    "stream_id": "stream",
                    "csrf_token": state.session.csrf_token,
                }
            ),
        }
    )
    started, cancelled = asyncio.Event(), asyncio.Event()
    delivered, closes = [], []

    async def send(message):
        if message["type"] == "websocket.send":
            frame = SCREEN_ADAPTER.validate_json(message["text"])
            if frame.kind == "SNAPSHOT":
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            else:
                delivered.append(frame)
        elif message["type"] == "websocket.close":
            closes.append(message["code"])

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "scheme": "ws",
        "path": "/ws/v1/snapshot",
        "raw_path": b"/ws/v1/snapshot",
        "query_string": b"",
        "root_path": "",
        "subprotocols": [],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 2),
        "headers": [
            (b"host", state.origin.removeprefix("http://").encode()),
            (b"origin", state.origin.encode()),
            (b"cookie", ("hmi_session=" + state.session.token).encode()),
        ],
    }
    task = asyncio.create_task(app(scope, incoming.get, send))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        # Independent connection obtains both locks while network delivery blocks.
        with state.engine.begin() as db:
            db.execute(sa.text("SELECT login FROM users WHERE login='viewer' FOR UPDATE NOWAIT"))
            db.execute(
                sa.text("SELECT id FROM lines WHERE id=:id FOR UPDATE NOWAIT"), {"id": LINE_ID}
            )
        await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert cancelled.is_set() and closes == [1013]
    assert [frame.kind for frame in delivered] == (
        ["RESYNC_REQUIRED"] if pressure == "queue" else []
    )
    if delivered:
        assert delivered[0].reason == "BACKPRESSURE"


async def test_automatic_reconnect_at_29_minutes_does_not_renew_30_minute_idle(state):
    with state.engine.connect() as db:
        original_seen = db.execute(
            sa.text("SELECT max(last_seen_at) FROM auth_sessions WHERE login='viewer'")
        ).scalar_one()
    async with connection(state) as ws:
        await subscribe(ws, state)
        assert isinstance(await frame(ws), ScreenSnapshot)
    state.clock[0] = original_seen + timedelta(minutes=29)
    async with connection(state) as ws:
        await subscribe(ws, state)
        assert isinstance(await frame(ws), ScreenSnapshot)
        with state.engine.connect() as db:
            assert (
                db.execute(
                    sa.text("SELECT max(last_seen_at) FROM auth_sessions WHERE login='viewer'")
                ).scalar_one()
                == original_seen
            )
        state.clock[0] = original_seen + timedelta(minutes=30)
        assert await closed(ws) == 1008
