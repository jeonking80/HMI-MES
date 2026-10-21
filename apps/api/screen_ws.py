"""Bounded authenticated v1 screen projections and atomic application envelopes.

UPDATE replaces the complete LineView. Emission time is not capture freshness.
No command, ingest ACK, settlement, cross-connection replay or mutation lives here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit
from uuid import uuid4

import sqlalchemy as sa
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.concurrency import run_in_threadpool

from apps.api.auth import UserAuth
from apps.api.models import Line
from apps.api.screen_frames import APP_SCREEN_MAX_BYTES, ScreenScope, make_screen_frame
from apps.api.snapshot import SnapshotNotFound, read_snapshot
from contracts.models import UINT64_MAX, Identifier
from contracts.transport import (
    LineView,
    ScreenResyncRequired,
    ScreenSnapshot,
    ScreenUpdate,
)


@dataclass(frozen=True)
class ScreenWSConfig:
    queue_size: int = 32
    poll_interval_seconds: float = 1.0
    send_timeout_seconds: float = 2.0
    subscribe_timeout_seconds: float = 3.0
    max_inbound_bytes: int = 4096
    max_outbound_bytes: int = 65536
    max_envelope_bytes: int = APP_SCREEN_MAX_BYTES

    def __post_init__(self):
        for value, maximum in (
            (self.queue_size, 32),
            (self.max_inbound_bytes, 4096),
            (self.max_outbound_bytes, 131072),
            (self.max_envelope_bytes, APP_SCREEN_MAX_BYTES),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("invalid screen transport bound")
        for value in (
            self.poll_interval_seconds,
            self.send_timeout_seconds,
            self.subscribe_timeout_seconds,
        ):
            if type(value) not in (int, float) or not 0.01 <= value <= 10:
                raise ValueError("invalid screen transport timeout")


class _Subscribe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_id: Identifier
    source_id: Identifier
    stream_id: Identifier
    csrf_token: Annotated[SecretStr, Field(strict=True, min_length=43, max_length=43)]


class _Pressure(Exception):
    def __init__(self, can_notify: bool):
        self.can_notify = can_notify


class _RevisionExhausted(Exception):
    pass


class _Outbound:
    """Never drops or coalesces a revision; all stored messages have a size bound."""

    def __init__(self, config: ScreenWSConfig, *, enveloped: bool = False):
        self.config = config
        self.byte_limit = (
            config.max_envelope_bytes if enveloped else min(config.max_outbound_bytes, 65536)
        )
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=config.queue_size)

    def serialize(self, message):
        payload = message.model_dump_json()
        if len(payload.encode("utf-8")) > self.byte_limit:
            raise _Pressure(False)
        return payload

    def put(self, message):
        payload = self.serialize(message)
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            raise _Pressure(True) from None


class _Sequence:
    def __init__(self, line_id: str):
        self.line_id, self.sync_id, self.revision = line_id, str(uuid4()), 0

    def initial(self, view: LineView, emitted_at: datetime):
        return ScreenSnapshot(
            kind="SNAPSHOT", sync_id=self.sync_id, revision=0, emitted_at=emitted_at, view=view
        )

    def update(self, view: LineView, emitted_at: datetime):
        if view.line_id != self.line_id:
            raise ValueError("screen scope changed")
        if self.revision >= UINT64_MAX:
            raise _RevisionExhausted
        previous = self.revision
        self.revision += 1
        return ScreenUpdate(
            kind="UPDATE",
            sync_id=self.sync_id,
            base_revision=previous,
            revision=self.revision,
            emitted_at=emitted_at,
            view=view,
        )


def _valid_origin(origin: str, allow_insecure_loopback: bool):
    try:
        parsed = urlsplit(origin)
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        return (
            bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.path == parsed.query == parsed.fragment == ""
            and not any(c.isspace() for c in origin)
            and "*" not in origin
            and parsed.port != 0
            and (
                parsed.scheme == "https"
                or (allow_insecure_loopback and loopback and parsed.scheme == "http")
            )
            and (not allow_insecure_loopback or loopback)
        )
    except (TypeError, ValueError):
        return False


def mount_screen_ws(
    app: FastAPI,
    auth: UserAuth,
    *,
    origin: str,
    allow_insecure_loopback: bool = False,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    config: ScreenWSConfig | None = None,
    session_cookie: str = "hmi_session",
) -> None:
    """Explicit same-origin mount; parent server must also cap inbound WS frames.

    Any App A read role can subscribe to a registered line/source/stream. No new
    per-user line ACL is invented. Require memory CSRF in the first bounded frame;
    server timers validate live sessions without extending idle expiry.
    """
    if type(allow_insecure_loopback) is not bool or not _valid_origin(
        origin, allow_insecure_loopback
    ):
        raise ValueError("explicit secure origin or loopback development is required")
    if origin not in auth._origins:
        raise ValueError("screen origin must be allowed by authentication")
    options = config or ScreenWSConfig()

    def check(token):
        with auth.authorized_transaction(token, permission="read", refresh_idle=False):
            pass

    def read(token, scope, *, subscribe=False):
        with auth.authorized_transaction(
            token,
            permission="read",
            require_csrf=subscribe,
            refresh_idle=False,
            csrf_token=scope.csrf_token.get_secret_value() if subscribe else None,
            origin=origin if subscribe else None,
        ) as unit:
            # Ingestion also locks line before source. This short shared line lock
            # makes the multi-query projection coherent across one committed capture.
            line = unit.connection.execute(
                sa.select(Line.id).where(Line.id == scope.line_id).with_for_update(read=True)
            ).first()
            if line is None:
                raise ValueError("screen scope is not registered")
            return read_snapshot(
                unit.connection,
                line_id=scope.line_id,
                source_id=scope.source_id,
                stream_id=scope.stream_id,
                now=clock(),
            )

    async def screen(websocket: WebSocket, *, enveloped: bool):
        tasks = []
        sequence = None
        public_scope = None
        token = websocket.cookies.get(session_cookie)
        accepted = False
        close_code = 1008
        notification = None
        try:
            scheme = {"ws": "http", "wss": "https"}.get(websocket.scope.get("scheme"))
            hosts = websocket.headers.getlist("host")
            if (
                websocket.headers.getlist("origin") != [origin]
                or len(hosts) != 1
                or f"{scheme}://{hosts[0]}" != origin
                or websocket.scope.get("query_string")
            ):
                return
            await run_in_threadpool(check, token)
            await websocket.accept()
            accepted = True
            raw = await asyncio.wait_for(
                websocket.receive_text(), options.subscribe_timeout_seconds
            )
            if len(raw.encode("utf-8")) > options.max_inbound_bytes:
                close_code = 1009
                return
            scope = _Subscribe.model_validate_json(raw)
            snapshot = await run_in_threadpool(read, token, scope, subscribe=True)
            public_scope = ScreenScope(
                line_id=scope.line_id, source_id=scope.source_id, stream_id=scope.stream_id
            )
            sequence = _Sequence(scope.line_id)
            outbound = _Outbound(options, enveloped=enveloped)

            def frame(message, projection=None):
                return (
                    make_screen_frame(public_scope, message, projection) if enveloped else message
                )

            outbound.put(
                frame(sequence.initial(snapshot.screen.view, snapshot.screen.emitted_at), snapshot)
            )

            async def produce():
                while True:
                    await asyncio.sleep(options.poll_interval_seconds)
                    current = await run_in_threadpool(read, token, scope)
                    outbound.put(
                        frame(
                            sequence.update(current.screen.view, current.screen.emitted_at), current
                        )
                    )

            async def write():
                while True:
                    payload = await outbound.queue.get()
                    # Discard queued frames if authorization changed while queued.
                    await run_in_threadpool(check, token)
                    try:
                        await asyncio.wait_for(
                            websocket.send_text(payload), options.send_timeout_seconds
                        )
                    except TimeoutError:
                        raise _Pressure(False) from None

            async def receive():
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                # V1 has no client command/resume/patch channel. Reconnect to resync.
                raise ValueError("unexpected screen input")

            tasks = [asyncio.create_task(operation()) for operation in (produce, write, receive)]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            close_code = 1000
        except _Pressure as pressure:
            close_code = 1013
            if pressure.can_notify:
                notification = "BACKPRESSURE"
        except _RevisionExhausted:
            close_code, notification = 1013, "SERVER_RESTART"
        except (PermissionError, ValueError, TimeoutError, SnapshotNotFound):
            close_code = 1008
        except WebSocketDisconnect:
            close_code = 1000
        except asyncio.CancelledError:
            close_code = 1001
            raise
        except Exception:
            # No database, request, validation or credential exception details leave WS.
            close_code = 1011
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if accepted and notification and sequence is not None:
                with suppress(Exception):
                    await run_in_threadpool(check, token)
                    message = ScreenResyncRequired(
                        kind="RESYNC_REQUIRED",
                        sync_id=sequence.sync_id,
                        reason=notification,
                        emitted_at=clock(),
                    )
                    await asyncio.wait_for(
                        websocket.send_text(
                            outbound.serialize(
                                make_screen_frame(public_scope, message) if enveloped else message
                            )
                        ),
                        options.send_timeout_seconds,
                    )
            with suppress(Exception):
                await websocket.close(code=close_code)

    @app.websocket("/ws/v1/snapshot")
    async def flat_screen(websocket: WebSocket):
        await screen(websocket, enveloped=False)

    @app.websocket("/ws/v1/screen")
    async def atomic_screen(websocket: WebSocket):
        await screen(websocket, enveloped=True)
