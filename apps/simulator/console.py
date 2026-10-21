"""Simulator-only HTTP console, on the canonical SQLite owner's event-loop thread."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    TypeAdapter,
    ValidationError,
)

from apps.simulator.auth import (
    AuthenticationError,
    AuthorizationError,
    Principal,
    SessionCredentials,
    SimulatorAuth,
)
from apps.simulator.controller import SimulatorController
from apps.simulator.field_channels import (
    FieldChannels,
    FieldEnvironmentUnavailable,
    FieldFaultInput,
    Group,
)
from apps.simulator.field_publication import get_publication_runtime
from apps.simulator.plc import Mode, OperationMode, PLCInputs, RecoveryChecks
from apps.simulator.storage import RecoveryRequired, StorageCorruption
from apps.simulator.tags import project_tags
from contracts.models import CommandRequest, Identifier, PositiveUInt64

SESSION_COOKIE = "simulator_session"
PREAUTH_COOKIE = "simulator_preauth"
_UINT64_TAGS = {
    "Line.GoodTotal",
    "Line.RejectTotal",
    "Line.ScrapTotal",
    "Line.Heartbeat",
    "Line.ControlVersion",
    "Line.EventSeq",
    "Infeed.LinkHeartbeat",
    "Sealer.LinkHeartbeat",
    "Outfeed.LinkHeartbeat",
    "Outfeed.GoodCounter",
    "Outfeed.RejectCounter",
}


@dataclass(frozen=True)
class ConsoleConfig:
    origin: str
    allow_insecure_loopback: bool = False
    login_limit: int = 5
    challenge_limit: int = 30
    window_seconds: int = 60
    max_throttle_keys: int = 2048
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        parts = urlsplit(self.origin)
        if (
            not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.path
            or parts.query
            or parts.fragment
            or "*" in self.origin
            or any(character.isspace() for character in self.origin)
        ):
            raise ValueError("console origin must be an exact HTTP(S) origin")
        if parts.scheme != "https" and not (
            self.allow_insecure_loopback
            and parts.scheme == "http"
            and parts.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("HTTPS is required except explicit loopback development")
        if self.allow_insecure_loopback and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("insecure development must use a loopback origin")
        if parts.port == 0:
            raise ValueError("invalid origin port")
        for value in (
            self.login_limit,
            self.challenge_limit,
            self.window_seconds,
            self.max_throttle_keys,
        ):
            if type(value) is not int or value <= 0:
                raise ValueError("throttle bounds must be positive integers")

    @property
    def secure_cookies(self) -> bool:
        return urlsplit(self.origin).scheme == "https"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Login(_Body):
    login: Annotated[str, Field(strict=True, min_length=1, max_length=64)]
    password: SecretStr


class _Password(_Body):
    new_password: SecretStr


class _Inputs(_Body):
    inputs: PLCInputs
    control_mode: Mode | None = None
    operation_mode: OperationMode | None = None


class _Scrap(_Body):
    item_ids: Annotated[tuple[PositiveUInt64, ...], Field(min_length=1, max_length=3)]
    reason: Annotated[str, Field(strict=True, min_length=1, max_length=256)]
    positions_verified: StrictBool


class _Settlement(_Body):
    operator_confirmed: StrictBool


class _Throttled(Exception):
    pass


class _Limiter:
    """Bounded process-local attempt windows; never trusts forwarded IP headers."""

    def __init__(self, config: ConsoleConfig) -> None:
        self.config = config
        self.windows: OrderedDict[str, deque[float]] = OrderedDict()

    def take(self, keys: tuple[str, ...], limit: int) -> None:
        now = time.monotonic()
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


def _model[Model: BaseModel](raw: bytes, model: type[Model]) -> Model:
    try:
        return model.model_validate_json(raw)
    except ValidationError:
        # Framework defaults include rejected input, which could be a password.
        raise HTTPException(422, "invalid request") from None


def create_app(
    controller: SimulatorController,
    auth: SimulatorAuth,
    config: ConsoleConfig,
    *,
    field_channels: FieldChannels | None = None,
) -> FastAPI:
    """Compose explicitly; the caller initializes credentials and owns lifecycle/ticks."""
    storage = controller.storage
    if auth._db is not storage.connection or config.origin not in auth._origins:
        raise ValueError("console and authentication must share storage and allowed origin")
    if field_channels is not None and field_channels.storage is not storage:
        raise ValueError("Field channels must share the simulator storage owner")
    owner_thread = threading.get_ident()
    limiter = _Limiter(config)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    with storage.transaction():
        storage.connection.execute(
            "CREATE TABLE IF NOT EXISTS console_schema_version (singleton INTEGER PRIMARY KEY "
            "CHECK(singleton=1),version INTEGER NOT NULL)"
        )
        rows = storage.connection.execute("SELECT * FROM console_schema_version").fetchall()
        if not rows:
            storage.connection.execute("INSERT INTO console_schema_version VALUES (1,1)")
        elif [tuple(row) for row in rows] != [(1, 1)]:
            raise ValueError("unsupported console schema")
        storage.connection.execute(
            "CREATE TABLE IF NOT EXISTS console_request_audit (audit_id INTEGER PRIMARY KEY,"
            "actor TEXT NOT NULL,action TEXT NOT NULL,details_json TEXT NOT NULL,"
            "occurred_at TEXT NOT NULL)"
        )

    def field_statuses():
        if field_channels is None:
            return None
        # Read one adopted immutable view. GET never prepares/adopts a publication.
        statuses = field_channels.describe()
        runtime = get_publication_runtime(field_channels)
        by_group = {item.group_id: item for item in runtime} if runtime is not None else {}
        return [
            status.model_dump(mode="json")
            | {
                "publication_runtime": (
                    by_group[status.configured.group_id].model_dump(mode="json")
                    if status.configured.group_id in by_group
                    else None
                )
            }
            for status in statuses
        ]

    def now() -> datetime:
        value = config.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("console clock must supply an aware timestamp")
        return value.astimezone(UTC)

    def audit(actor: str, action: str, details: dict | None = None) -> None:
        storage.connection.execute(
            "INSERT INTO console_request_audit(actor,action,details_json,occurred_at) "
            "VALUES (?,?,?,?)",
            (actor, action, json.dumps(details or {}, sort_keys=True), now().isoformat()),
        )

    def origin(request: Request) -> None:
        if request.headers.get("origin") != config.origin:
            raise AuthorizationError("origin is not allowed")

    def operator(request: Request) -> Principal:
        origin(request)
        return auth.authorize_mutation(
            request.cookies.get(SESSION_COOKIE),
            csrf_token=request.headers.get("x-csrf-token"),
            origin=config.origin,
        )

    def user(request: Request) -> Principal:
        return auth.authenticate(request.cookies.get(SESSION_COOKIE))

    def cookie(response: JSONResponse, name: str, value: str, seconds: int) -> None:
        response.set_cookie(
            name,
            value,
            max_age=seconds,
            httponly=True,
            secure=config.secure_cookies,
            samesite="strict",
            path="/",
        )

    def credentials(session: SessionCredentials) -> JSONResponse:
        response = JSONResponse(
            {
                "login": session.principal.login,
                "role": session.principal.role,
                "must_change_password": session.principal.must_change_password,
                "csrf_token": session.csrf_token,
                "expires_at": session.expires_at.isoformat(),
            }
        )
        cookie(response, SESSION_COOKIE, session.token, 8 * 60 * 60)
        response.delete_cookie(
            PREAUTH_COOKIE,
            path="/",
            secure=config.secure_cookies,
            httponly=True,
            samesite="strict",
        )
        return response

    async def rejected(request: Request, status: int) -> JSONResponse:
        # The handler runs after the request's transaction has rolled back.
        # Use only static route names/status; never incoming credentials or bodies.
        route = request.scope.get("route")
        with storage.transaction():
            audit(
                "anonymous",
                "REQUEST_REJECTED",
                {"route": getattr(route, "name", "unknown"), "status": status},
            )
        response = JSONResponse({"detail": "request rejected"}, status_code=status)
        if status == 429:
            response.headers["Retry-After"] = str(config.window_seconds)
        return response

    @app.exception_handler(AuthenticationError)
    async def authentication_error(request: Request, error: AuthenticationError):
        return await rejected(request, 401)

    @app.exception_handler(AuthorizationError)
    async def authorization_error(request: Request, error: AuthorizationError):
        return await rejected(request, 403)

    @app.exception_handler(_Throttled)
    async def rate_error(request: Request, error: _Throttled):
        return await rejected(request, 429)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return JSONResponse({"detail": "invalid request"}, status_code=422)

    @app.exception_handler(RecoveryRequired)
    async def recovery_error(request: Request, error: RecoveryRequired):
        return JSONResponse({"detail": "explicit recovery required"}, status_code=409)

    @app.exception_handler(FieldEnvironmentUnavailable)
    async def field_environment_error(request: Request, error: FieldEnvironmentUnavailable):
        return JSONResponse({"detail": "field environment unavailable"}, status_code=503)

    @app.exception_handler(StorageCorruption)
    async def corrupt_error(request: Request, error: StorageCorruption):
        return JSONResponse({"detail": "simulator storage unavailable"}, status_code=503)

    @app.exception_handler(ValueError)
    async def value_error(request: Request, error: ValueError):
        return JSONResponse(
            {"detail": "request conflicts with simulator conditions"}, status_code=409
        )

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        if threading.get_ident() != owner_thread:
            return JSONResponse({"detail": "simulator owner thread required"}, status_code=503)
        # This check also prevents a different Host/origin alias from receiving cookies.
        if str(request.base_url).rstrip("/") != config.origin:
            return JSONResponse({"detail": "console host is not allowed"}, status_code=400)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 16_384:
                return JSONResponse({"detail": "request too large"}, status_code=413)
            raw.extend(chunk)
        request._body = bytes(raw)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.get("/", response_class=FileResponse)
    async def index():
        return FileResponse(Path(__file__).with_name("static") / "index.html")

    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).with_name("static"), check_dir=False),
        name="static",
    )

    @app.post("/auth/challenge")
    async def challenge(request: Request):
        origin(request)
        if request.headers.get("x-simulator-request") != "1":
            raise AuthorizationError("login challenge requires explicit console request header")
        ip = request.client.host if request.client else "unknown"
        limiter.take(("challenge-ip:" + ip,), config.challenge_limit)
        with storage.transaction():
            result = auth.begin_login()
        response = JSONResponse({"csrf_token": result.csrf_token})
        cookie(response, PREAUTH_COOKIE, result.token, 5 * 60)
        return response

    @app.post("/auth/login")
    async def login(request: Request):
        origin(request)
        ip = request.client.host if request.client else "unknown"
        limiter.take(("login-ip:" + ip,), config.login_limit)
        body = _model(await request.body(), _Login)
        account_key = hashlib.sha256(body.login.casefold().encode()).hexdigest()
        limiter.take(("login-account:" + account_key,), config.login_limit)
        with storage.transaction():
            session = auth.login(
                body.login,
                body.password.get_secret_value(),
                challenge_token=request.cookies.get(PREAUTH_COOKIE),
                csrf_token=request.headers.get("x-csrf-token"),
                origin=config.origin,
            )
        return credentials(session)

    @app.get("/auth/me")
    async def me(request: Request):
        with storage.transaction():
            principal = user(request)
        return {
            "login": principal.login,
            "role": principal.role,
            "must_change_password": principal.must_change_password,
        }

    @app.post("/auth/password")
    async def password(request: Request):
        raw = await request.body()
        origin(request)
        with storage.transaction():
            body = _model(raw, _Password)
            session = auth.change_password(
                request.cookies.get(SESSION_COOKIE),
                body.new_password.get_secret_value(),
                csrf_token=request.headers.get("x-csrf-token"),
                origin=config.origin,
            )
        return credentials(session)

    @app.post("/auth/logout")
    async def logout(request: Request):
        origin(request)
        with storage.transaction():
            auth.revoke(
                request.cookies.get(SESSION_COOKIE),
                csrf_token=request.headers.get("x-csrf-token"),
                origin=config.origin,
            )
        response = JSONResponse({"logged_out": True})
        response.delete_cookie(
            SESSION_COOKIE, path="/", secure=config.secure_cookies, httponly=True, samesite="strict"
        )
        return response

    @app.get("/snapshot")
    async def snapshot(request: Request):
        with storage.transaction():
            user(request)
            state = controller.state
            process = storage.load_execution()
            tags = project_tags(
                state,
                process,
                counter_epoch=storage.identity.counter_epoch,
                heartbeat=process.tick if process else 0,
            )
            tags = {
                key: str(value) if key in _UINT64_TAGS else value for key, value in tags.items()
            }
            result = {
                "environment": "SIMULATOR TEST ENVIRONMENT",
                "state": state.model_dump(mode="json"),
                "tags": tags,
                "observed_at": now().isoformat(),
            }
            result["field_channels"] = field_statuses()
            result["process"] = (
                None
                if process is None
                else {
                    "execution_id": process.execution_id,
                    "temperature_c": process.temperature_c,
                    "slots": [
                        None
                        if item is None
                        else {
                            "item_id": str(item.item_id),
                            "execution_id": item.execution_id,
                            "progress_mm": str(item.progress_mm),
                        }
                        for item in process.slots
                    ],
                    **{
                        name: str(getattr(process, name))
                        for name in (
                            "input_quantity",
                            "good_quantity",
                            "reject_quantity",
                            "scrap_quantity",
                            "tick",
                        )
                    },
                    "wip_quantity": process.wip_quantity,
                }
            )
            result["pending_commands"] = [
                {
                    "command_id": record.request.command_id,
                    "kind": record.request.command.kind,
                    "result": controller.get_result(record.request.command_id).model_dump(
                        mode="json"
                    ),
                }
                for record in controller.commands.pending_records()
            ]
        return result

    @app.post("/commands")
    async def submit(request: Request):
        raw = await request.body()
        with storage.transaction():
            principal = operator(request)
            command = _model(raw, CommandRequest)
            if command.requested_by != principal.login:
                raise AuthorizationError("requested_by must match the authenticated operator")
            result = controller.submit(command, now=now(), authority="LOCAL")
            audit(
                principal.login,
                "COMMAND_SUBMITTED",
                {
                    "command_id": command.command_id,
                    "kind": command.command.kind,
                    "created": result.created,
                },
            )
            response = {
                "submission": result.submission.model_dump(mode="json"),
                "result": result.result.model_dump(mode="json") if result.result else None,
                "created": result.created,
            }
        return response

    @app.get("/commands/{command_id}")
    async def result(request: Request, command_id: str):
        with storage.transaction():
            user(request)
            outcome = controller.get_result(command_id)
        return outcome.model_dump(mode="json")

    def query_identity(request: Request, key: str) -> str:
        if set(request.query_params) != {key} or len(request.query_params.getlist(key)) != 1:
            raise HTTPException(422, "exact identity query required")
        try:
            return TypeAdapter(Identifier).validate_python(request.query_params[key])
        except ValidationError:
            raise HTTPException(422, "invalid identity") from None

    @app.get("/command-record")
    async def result_by_identity(request: Request):
        return await result(request, query_identity(request, "command_id"))

    @app.get("/field-faults")
    async def read_field_faults(request: Request):
        with storage.transaction():
            user(request)
            if field_channels is None:
                raise HTTPException(503, "field fault input is not configured")
            return field_statuses()

    @app.post("/field-faults/{group}")
    async def set_field_fault(request: Request, group: Group):
        raw = await request.body()
        with storage.transaction():
            principal = operator(request)
            if field_channels is None:
                raise HTTPException(503, "field fault input is not configured")
            intent = _model(raw, FieldFaultInput)
            state = field_channels.set_fault(group, intent, actor=principal.login, at=now())
        # This confirms committed configuration, not the next OPC publication.
        return state.model_dump(mode="json")

    @app.post("/inputs")
    async def inputs(request: Request):
        raw = await request.body()
        with storage.transaction():
            principal = operator(request)
            body = _model(raw, _Inputs)
            state = controller.set_inputs(
                body.inputs,
                now=now(),
                control_mode=body.control_mode,
                operation_mode=body.operation_mode,
            )
            audit(principal.login, "INPUTS_CHANGED", body.model_dump(mode="json"))
        return state.model_dump(mode="json")

    @app.get("/runs/{execution_id}")
    async def summary(request: Request, execution_id: str):
        with storage.transaction():
            user(request)
            outcome = controller.get_summary(execution_id)
        return outcome.model_dump(mode="json")

    @app.get("/run-summary")
    async def summary_by_identity(request: Request):
        return await summary(request, query_identity(request, "execution_id"))

    @app.post("/recover")
    async def recover(request: Request):
        raw = await request.body()
        with storage.transaction():
            principal = operator(request)
            checks = _model(raw, RecoveryChecks)
            state = controller.recover(now=now(), operator_id=principal.login, checks=checks)
            audit(principal.login, "RECOVERY_CONFIRMED", checks.model_dump(mode="json"))
        return state.model_dump(mode="json")

    @app.post("/runs/{execution_id}/settle")
    async def settle(request: Request, execution_id: str):
        raw = await request.body()
        with storage.transaction():
            principal = operator(request)
            confirmation = _model(raw, _Settlement)
            if not confirmation.operator_confirmed:
                raise ValueError("explicit local settlement confirmation required")
            state = controller.acknowledge_settlement(
                execution_id,
                now=now(),
                operator_id=principal.login,
            )
            audit(principal.login, "LOCAL_SETTLEMENT_CONFIRMED", {"execution_id": execution_id})
        return state.model_dump(mode="json")

    @app.post("/reconcile/{command_id}")
    async def reconcile(request: Request, command_id: str):
        raw = await request.body()
        with storage.transaction():
            principal = operator(request)
            checks = _model(raw, RecoveryChecks)
            outcome = controller.reconcile_interrupted(command_id, now=now(), checks=checks)
            audit(
                principal.login,
                "INTERRUPTED_COMMAND_RECONCILED",
                {"command_id": command_id, **checks.model_dump()},
            )
        return outcome.model_dump(mode="json")

    @app.post("/run-settlement")
    async def settle_by_identity(request: Request):
        return await settle(request, query_identity(request, "execution_id"))

    @app.post("/command-reconciliation")
    async def reconcile_by_identity(request: Request):
        return await reconcile(request, query_identity(request, "command_id"))

    @app.post("/scrap")
    async def scrap(request: Request):
        raw = await request.body()
        with storage.transaction():
            principal = operator(request)
            body = _model(raw, _Scrap)
            state = controller.scrap_wip(
                body.item_ids,
                reason=body.reason,
                now=now(),
                positions_verified=body.positions_verified,
            )
            audit(principal.login, "WIP_SCRAPPED", body.model_dump(mode="json"))
        return state.model_dump(mode="json")

    return app
