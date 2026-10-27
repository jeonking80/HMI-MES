"""Explicit browser-session HTTP adapter; no service bearer or business endpoints."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from apps.api.alarm_http import mount_alarm_routes
from apps.api.analytics_availability import AnalyticsAvailabilityCache, analytics_available
from apps.api.assistant import AssistantConfig, register_assistant_routes
from apps.api.auth import (
    ABSOLUTE_TIMEOUT,
    LOGIN_TIMEOUT,
    AuthenticationError,
    AuthorizationError,
    Principal,
    SessionCredentials,
    UserAuth,
)
from apps.api.control_http import mount_control_routes
from apps.api.execution_http import mount_execution_routes
from apps.api.guest import GUEST_TIMEOUT, GuestThrottled, issue_guest, validate_startup
from apps.api.ingestion import source_streams
from apps.api.items_http import mount_item_routes
from apps.api.line_queue_http import mount_line_queue_routes
from apps.api.models import Line
from apps.api.runtime_control_http import mount_runtime_control_routes
from apps.api.screen_frames import APP_SCREEN_MAX_BYTES, ScreenScope, make_screen_frame
from apps.api.screen_ws import mount_screen_ws
from apps.api.snapshot import SnapshotNotFound, read_snapshot
from apps.api.work_order_cancel_http import mount_work_order_cancel_routes
from apps.api.work_order_revise_http import mount_work_order_revise_routes
from apps.api.work_orders_http import mount_work_order_routes

SESSION_COOKIE = "hmi_session"
PREAUTH_COOKIE = "hmi_preauth"


@dataclass(frozen=True)
class UserHTTPConfig:
    origin: str
    allow_insecure_loopback: bool = False
    login_limit: int = 5
    challenge_limit: int = 30
    window_seconds: int = 60
    max_throttle_keys: int = 2048
    max_body_bytes: int = 16_384
    monotonic: Callable[[], float] = time.monotonic
    guest_viewer: bool = False
    # None disables the read-only assistant; its key never leaves server memory.
    assistant: AssistantConfig | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if type(self.guest_viewer) is not bool:
            raise ValueError("guest_viewer must be a boolean")
        try:
            parts = urlsplit(self.origin)
            valid = (
                bool(parts.hostname)
                and parts.username is None
                and parts.password is None
                and parts.path == parts.query == parts.fragment == ""
                and "*" not in self.origin
                and not any(character.isspace() for character in self.origin)
                and parts.port != 0
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("an exact HTTP(S) origin is required")
        loopback = parts.hostname in {"localhost", "127.0.0.1", "::1"}
        if parts.scheme != "https" and not (
            self.allow_insecure_loopback and parts.scheme == "http" and loopback
        ):
            raise ValueError("HTTPS is required except explicit loopback development")
        if self.allow_insecure_loopback and not loopback:
            raise ValueError("insecure development must use a loopback origin")
        for value in (
            self.login_limit,
            self.challenge_limit,
            self.window_seconds,
            self.max_throttle_keys,
            self.max_body_bytes,
        ):
            if type(value) is not int or value <= 0:
                raise ValueError("request and throttle bounds must be positive integers")

    @property
    def secure_cookies(self) -> bool:
        return urlsplit(self.origin).scheme == "https"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Login(_Body):
    login: Annotated[str, Field(strict=True, min_length=1, max_length=64)]
    password: Annotated[SecretStr, Field(strict=True)]


class _Password(_Body):
    new_password: Annotated[SecretStr, Field(strict=True)]


class _Throttled(Exception):
    pass


class _Limiter:
    """Bounded process-local windows; peer IP only, no forwarded headers."""

    def __init__(self, config: UserHTTPConfig):
        self.config = config
        self.windows: OrderedDict[str, deque[float]] = OrderedDict()
        self.lock = threading.Lock()

    def take(self, keys: tuple[str, ...], limit: int) -> None:
        with self.lock:
            now = self.config.monotonic()
            for key in tuple(self.windows):
                window = self.windows[key]
                while window and window[0] <= now - self.config.window_seconds:
                    window.popleft()
                if not window:
                    del self.windows[key]
            new_keys = set(keys) - self.windows.keys()
            if len(self.windows) + len(new_keys) > self.config.max_throttle_keys:
                raise _Throttled
            if any(len(self.windows.get(key, ())) >= limit for key in keys):
                raise _Throttled
            for key in keys:
                self.windows.setdefault(key, deque()).append(now)


def _model[Model: BaseModel](request: Request, raw: bytes, model: type[Model]) -> Model:
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise HTTPException(415, "JSON is required")
    try:
        return model.model_validate_json(raw)
    except ValidationError:
        # Pydantic's default error details can echo passwords and other inputs.
        raise HTTPException(422, "invalid request") from None


def _principal(principal: Principal) -> dict:
    return {
        "login": principal.login,
        "roles": sorted(principal.roles),
        "must_change_password": principal.must_change_password,
    }


def create_user_app(auth: UserAuth, config: UserHTTPConfig) -> FastAPI:
    """Compose explicitly; caller owns schema migration, bootstrap and lifecycle.

    This app is independent of the gateway service bearer intake. Future business
    routes must use UserAuth.authorized_transaction with the same business DB
    connection; authenticate/me alone never grants a business permission.
    """
    if config.origin not in auth._origins:
        raise ValueError("HTTP origin must be allowed by the authentication service")
    if config.guest_viewer != auth.guest_viewer:
        raise ValueError("guest HTTP and authentication configuration must agree")
    if config.guest_viewer:
        validate_startup(auth)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.analytics_routes_registered = False
    analytics_cache = AnalyticsAvailabilityCache(config.monotonic)
    limiter = _Limiter(config)

    def origin(request: Request) -> None:
        if request.headers.getlist("origin") != [config.origin]:
            raise AuthorizationError("origin is not allowed")

    def peer(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def cookie(response: JSONResponse, name: str, token: str, seconds: int) -> None:
        response.set_cookie(
            name,
            token,
            max_age=seconds,
            httponly=True,
            secure=config.secure_cookies,
            samesite="strict",
            path="/",
        )

    def clear_cookie(response: JSONResponse, name: str) -> None:
        response.delete_cookie(
            name, path="/", secure=config.secure_cookies, httponly=True, samesite="strict"
        )

    def credentials(session: SessionCredentials) -> JSONResponse:
        response = JSONResponse(
            {
                **_principal(session.principal),
                "csrf_token": session.csrf_token,
                "expires_at": session.expires_at.isoformat(),
            }
        )
        timeout = GUEST_TIMEOUT if session.principal.login == "guest-viewer" else ABSOLUTE_TIMEOUT
        cookie(response, SESSION_COOKIE, session.token, int(timeout.total_seconds()))
        clear_cookie(response, PREAUTH_COOKIE)
        return response

    def rejected(status: int) -> JSONResponse:
        response = JSONResponse({"detail": "request rejected"}, status_code=status)
        if status == 429:
            response.headers["Retry-After"] = str(config.window_seconds)
        return response

    @app.exception_handler(AuthenticationError)
    async def authentication_error(request: Request, error: AuthenticationError):
        return rejected(401)

    @app.exception_handler(AuthorizationError)
    async def authorization_error(request: Request, error: AuthorizationError):
        return rejected(403)

    @app.exception_handler(GuestThrottled)
    async def guest_throttle_error(request: Request, error: GuestThrottled):
        return rejected(429)

    @app.exception_handler(_Throttled)
    async def throttle_error(request: Request, error: _Throttled):
        return rejected(429)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return JSONResponse({"detail": "invalid request"}, status_code=422)

    @app.exception_handler(ValueError)
    async def value_error(request: Request, error: ValueError):
        return JSONResponse({"detail": "invalid authentication change"}, status_code=422)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, error: SQLAlchemyError):
        return JSONResponse({"detail": "authentication unavailable"}, status_code=503)

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        # Validate the actual request scheme/Host, never forwarded header values.
        hosts = request.headers.getlist("host")
        if len(hosts) != 1 or f"{request.url.scheme}://{hosts[0]}" != config.origin:
            response = JSONResponse({"detail": "host is not allowed"}, status_code=400)
        else:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > config.max_body_bytes:
                    response = JSONResponse({"detail": "request too large"}, status_code=413)
                    break
                raw.extend(chunk)
            else:
                request._body = bytes(raw)
                response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        return response

    @app.get("/api/v1/sources")
    async def sources(request: Request):
        def read():
            with auth.authorized_transaction(
                request.cookies.get(SESSION_COOKIE), permission="read"
            ) as authorized:
                rows = authorized.connection.execute(
                    select(
                        source_streams.c.source_id,
                        source_streams.c.stream_id,
                        source_streams.c.line_id,
                        Line.name.label("line_name"),
                    )
                    .join(Line, source_streams.c.line_id == Line.id)
                    .order_by(Line.id, source_streams.c.source_id, source_streams.c.stream_id)
                ).mappings()
                return {"sources": [dict(row) for row in rows]}

        return await run_in_threadpool(read)

    @app.get("/api/v1/snapshot")
    async def snapshot(request: Request, line_id: str, source_id: str, stream_id: str):
        token = request.cookies.get(SESSION_COOKIE)

        def read():
            with auth.authorized_transaction(
                token, permission="read", refresh_idle=False
            ) as authorized:
                authorized.connection.execute(
                    select(Line.id).where(Line.id == line_id).with_for_update(read=True)
                )
                return read_snapshot(
                    authorized.connection,
                    line_id=line_id,
                    source_id=source_id,
                    stream_id=stream_id,
                    now=datetime.now(UTC),
                    prefer_complete_capture=True,
                ).model_dump(mode="json")

        try:
            return await run_in_threadpool(read)
        except SnapshotNotFound:
            raise HTTPException(404, "source is not configured for this line") from None

    @app.get("/api/v1/screen")
    async def screen(request: Request):
        required = {"line_id", "source_id", "stream_id"}
        if set(request.query_params) != required or any(
            len(request.query_params.getlist(name)) != 1 for name in required
        ):
            raise HTTPException(422, "exact screen scope is required")
        try:
            scope = ScreenScope.model_validate(dict(request.query_params))
        except ValidationError:
            raise HTTPException(422, "invalid screen scope") from None

        def read():
            with auth.authorized_transaction(
                request.cookies.get(SESSION_COOKIE), permission="read", refresh_idle=False
            ) as authorized:
                # Match WS/intake lock order; all screen metadata is one projection.
                line = authorized.connection.execute(
                    select(Line.id).where(Line.id == scope.line_id).with_for_update(read=True)
                ).first()
                if line is None:
                    raise SnapshotNotFound("Screen line is not registered")
                projection = read_snapshot(
                    authorized.connection,
                    line_id=scope.line_id,
                    source_id=scope.source_id,
                    stream_id=scope.stream_id,
                    now=datetime.now(UTC),
                    prefer_complete_capture=True,
                )
                payload = make_screen_frame(scope, projection.screen, projection).model_dump_json()
                if len(payload.encode("utf-8")) > APP_SCREEN_MAX_BYTES:
                    raise HTTPException(503, "screen projection unavailable")
                return payload

        try:
            payload = await run_in_threadpool(read)
        except SnapshotNotFound:
            raise HTTPException(404, "source is not configured for this line") from None
        return Response(
            payload, media_type="application/json", headers={"Cache-Control": "no-store"}
        )

    @app.get("/auth/config")
    async def auth_config():
        return {
            "guest_viewer": config.guest_viewer,
            "assistant": config.assistant is not None,
            "analytics": await run_in_threadpool(
                analytics_available, app, auth, cache=analytics_cache
            ),
        }

    @app.post("/auth/guest")
    async def guest(request: Request):
        if not config.guest_viewer:
            raise HTTPException(404, "guest access is disabled")
        origin(request)
        _model(request, await request.body(), _Body)
        session = await run_in_threadpool(
            issue_guest, auth, origin=config.origin, peer=peer(request)
        )
        return credentials(session)

    @app.post("/auth/challenge")
    async def challenge(request: Request):
        origin(request)
        if request.headers.getlist("x-hmi-request") != ["1"]:
            raise AuthorizationError("explicit HMI request header required")
        limiter.take(("challenge-ip:" + peer(request),), config.challenge_limit)
        result = await run_in_threadpool(auth.begin_login)
        response = JSONResponse({"csrf_token": result.csrf_token})
        cookie(response, PREAUTH_COOKIE, result.token, int(LOGIN_TIMEOUT.total_seconds()))
        return response

    @app.post("/auth/login")
    async def login(request: Request):
        origin(request)
        limiter.take(("login-ip:" + peer(request),), config.login_limit)
        body = _model(request, await request.body(), _Login)
        account_key = hashlib.sha256(body.login.casefold().encode()).hexdigest()
        limiter.take(("login-account:" + account_key,), config.login_limit)
        session = await run_in_threadpool(
            auth.login,
            body.login,
            body.password.get_secret_value(),
            challenge_token=request.cookies.get(PREAUTH_COOKIE),
            csrf_token=request.headers.get("x-csrf-token"),
            origin=config.origin,
        )
        return credentials(session)

    @app.get("/auth/me")
    async def me(request: Request):
        principal = await run_in_threadpool(auth.authenticate, request.cookies.get(SESSION_COOKIE))
        return _principal(principal)

    @app.post("/auth/password")
    async def password(request: Request):
        origin(request)
        body = _model(request, await request.body(), _Password)
        session = await run_in_threadpool(
            auth.change_password,
            request.cookies.get(SESSION_COOKIE),
            body.new_password.get_secret_value(),
            csrf_token=request.headers.get("x-csrf-token"),
            origin=config.origin,
        )
        return credentials(session)

    @app.post("/auth/logout")
    async def logout(request: Request):
        origin(request)
        await run_in_threadpool(
            auth.revoke,
            request.cookies.get(SESSION_COOKIE),
            csrf_token=request.headers.get("x-csrf-token"),
            origin=config.origin,
        )
        response = JSONResponse({"logged_out": True})
        clear_cookie(response, SESSION_COOKIE)
        clear_cookie(response, PREAUTH_COOKIE)
        return response

    mount_control_routes(app, auth, session_cookie=SESSION_COOKIE)
    mount_work_order_routes(app, auth, session_cookie=SESSION_COOKIE)
    mount_item_routes(app, auth, session_cookie=SESSION_COOKIE)
    mount_alarm_routes(app, auth, session_cookie=SESSION_COOKIE)
    mount_execution_routes(app, auth, session_cookie=SESSION_COOKIE)
    mount_work_order_cancel_routes(app, auth, session_cookie=SESSION_COOKIE)
    mount_work_order_revise_routes(app, auth, session_cookie=SESSION_COOKIE)
    mount_line_queue_routes(app, auth, session_cookie=SESSION_COOKIE)
    mount_runtime_control_routes(app, auth, session_cookie=SESSION_COOKIE)
    from apps.api.dashboard_http import register_dashboard_routes
    from apps.api.downtime_http import register_downtime_routes
    from apps.api.reports_http import register_report_routes
    from apps.api.spc_http import register_spc_routes
    from apps.api.trend_http import register_trend_routes

    register_trend_routes(app, auth)
    register_report_routes(app, auth)
    register_downtime_routes(app, auth)
    from apps.api.downtime import annotate_report_segments

    app.state.report_segment_annotator = annotate_report_segments
    register_spc_routes(app, auth)
    register_dashboard_routes(app, auth)
    register_assistant_routes(
        app, auth, config.assistant, origin=config.origin, session_cookie=SESSION_COOKIE
    )
    app.state.analytics_routes_registered = True
    mount_screen_ws(
        app,
        auth,
        origin=config.origin,
        allow_insecure_loopback=config.allow_insecure_loopback,
        session_cookie=SESSION_COOKIE,
    )
    return app
