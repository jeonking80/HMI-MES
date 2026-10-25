"""Service-only batch HTTP boundary; browser users cannot provision or ingest here."""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import os
import re
import secrets
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from apps.api.alarm_lifecycle import AlarmConflict
from apps.api.alarm_store import AlarmStoreUnavailable
from apps.api.capture_clock import CLOCK_EVIDENCE_REVISIONS
from apps.api.commands import (
    CommandConflict,
    CommandScopeError,
    apply_result,
    apply_submission,
    claim_next,
)
from apps.api.database import (
    SCHEMA_LOCK_ID,
    SUPPORTED_COMMAND_REVISIONS,
    SUPPORTED_INGESTION_REVISIONS,
)
from apps.api.ingestion import (
    IngestConflict,
    IngestionService,
    IngestionUnavailable,
    SourceBindingError,
)
from apps.api.measured_clock import (
    MeasuredClockConflict,
    ProbeCapacityError,
    ProbeScopeError,
    issue_probe,
)
from apps.api.settlement_http import mount_settlement_routes
from contracts.clock_evidence import ClockProbeRequest
from contracts.models import CommandNotFound, CommandResult, CommandSubmission, Identifier, UInt64
from contracts.transport import GatewayBatch

_IDENTIFIER = TypeAdapter(Identifier)
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43,86}\Z")


def _token(value: str) -> bytes:
    """Validate representation/size; operators must generate the random entropy."""
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError("service token must encode 32..64 random bytes as unpadded base64url")
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("invalid service token encoding") from None
    if (
        not 32 <= len(decoded) <= 64
        or base64.urlsafe_b64encode(decoded).rstrip(b"=").decode() != value
    ):
        raise ValueError("invalid service token encoding")
    return hashlib.sha256(value.encode("ascii")).digest()


def _read_token_digest(path: Path) -> bytes:
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or not 43 <= info.st_size <= 88
        ):
            raise ValueError("service secret must be a protected owner-only regular file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw = handle.read(89)
        value = raw.removesuffix(b"\n").removesuffix(b"\r").decode("ascii")
        return _token(value)
    except (OSError, UnicodeError, ValueError):
        raise ValueError("required service secret file is unavailable or invalid") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


@dataclass(frozen=True)
class ServiceCredential:
    key_id: str
    gateway_id: str
    source_id: str
    stream_id: str
    token_digest: bytes = field(repr=False)
    expires_at: datetime
    revoked: bool = False

    def __post_init__(self) -> None:
        for value in (self.key_id, self.gateway_id, self.source_id, self.stream_id):
            try:
                _IDENTIFIER.validate_python(value)
            except ValidationError:
                raise ValueError("invalid service credential identity") from None
        if not isinstance(self.token_digest, bytes) or len(self.token_digest) != 32:
            raise ValueError("service token digest must be SHA-256 bytes")
        if (
            not isinstance(self.expires_at, datetime)
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
        ):
            raise ValueError("service credential expiry must include timezone")
        if type(self.revoked) is not bool:
            raise ValueError("service credential revoked flag must be boolean")

    @classmethod
    def from_secret_file(
        cls,
        *,
        key_id: str,
        gateway_id: str,
        source_id: str,
        stream_id: str,
        secret_file: Path,
        expires_at: datetime,
        revoked: bool = False,
    ) -> ServiceCredential:
        """Explicit operator-provided secret; no default path, token or principal."""
        return cls(
            key_id,
            gateway_id,
            source_id,
            stream_id,
            _read_token_digest(secret_file),
            expires_at,
            revoked,
        )


@dataclass(frozen=True)
class SourceBindingPolicy:
    """Trusted deployment capability; never constructed from an HTTP request."""

    credential: ServiceCredential
    line_id: str
    capture_generation: int

    def __post_init__(self):
        if not isinstance(self.credential, ServiceCredential):
            raise ValueError("Explicit service credential required")
        _IDENTIFIER.validate_python(self.line_id)
        # Normalize canonical UInt64 strings from trusted JSON, preserving exact range.
        object.__setattr__(
            self, "capture_generation", TypeAdapter(UInt64).validate_python(self.capture_generation)
        )

    def permits(self, selected: ServiceCredential) -> bool:
        expected = self.credential
        return (
            selected.key_id,
            selected.gateway_id,
            selected.source_id,
            selected.stream_id,
            selected.expires_at,
            selected.revoked,
        ) == (
            expected.key_id,
            expected.gateway_id,
            expected.source_id,
            expected.stream_id,
            expected.expires_at,
            expected.revoked,
        ) and secrets.compare_digest(selected.token_digest, expected.token_digest)


@dataclass(frozen=True)
class InternalConfig:
    allow_insecure_loopback: bool = False
    max_body_bytes: int = 4 * 1024 * 1024
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        if type(self.max_body_bytes) is not int or not 1 <= self.max_body_bytes <= 4 * 1024 * 1024:
            raise ValueError("internal request bound must be within 1..4194304 bytes")
        if type(self.allow_insecure_loopback) is not bool:
            raise ValueError("loopback development override must be boolean")


def _loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return bool(host and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        return False


def create_internal_app(
    ingestion_service: IngestionService,
    credential_bindings: Sequence[ServiceCredential],
    *,
    config: InternalConfig | None = None,
    source_binding_policy: SourceBindingPolicy | None = None,
) -> FastAPI:
    """Use pre-migrated PostgreSQL and explicitly provisioned service bindings."""
    config = config or InternalConfig()
    bindings = tuple(credential_bindings)
    if not 1 <= len(bindings) <= 128:
        raise ValueError("1..128 explicit service credentials are required")
    if len({binding.key_id for binding in bindings}) != len(bindings) or len(
        {binding.token_digest for binding in bindings}
    ) != len(bindings):
        raise ValueError("service credential keys and token digests must be unique")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def now() -> datetime:
        value = config.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("internal clock must supply an aware timestamp")
        return value.astimezone(UTC)

    def authenticate(request: Request) -> ServiceCredential:
        headers = request.headers.getlist("authorization")
        if len(headers) != 1:
            raise HTTPException(401, "service authentication required")
        scheme, separator, token = headers[0].partition(" ")
        if scheme.casefold() != "bearer" or separator != " ":
            raise HTTPException(401, "service authentication required")
        try:
            digest = _token(token)
        except ValueError:
            raise HTTPException(401, "service authentication required") from None
        selected = None
        # Never use the supplied gateway/source fields to select an identity.
        # Check every fixed-size digest instead of returning on the first match.
        for binding in bindings:
            matches = secrets.compare_digest(binding.token_digest, digest)
            if matches and not binding.revoked and now() < binding.expires_at:
                selected = binding
        if selected is None:
            raise HTTPException(401, "service authentication required")
        return selected

    @app.middleware("http")
    async def transport(request: Request, call_next):
        request.state.clock_received_at = now()
        if (
            request.url.path != "/health/live"
            and request.url.scheme != "https"
            and not (
                config.allow_insecure_loopback
                and request.url.scheme == "http"
                and _loopback(request.url.hostname)
                and _loopback(request.client.host if request.client else None)
            )
        ):
            return JSONResponse({"detail": "secure service transport required"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, error: RequestValidationError):
        return JSONResponse({"detail": "invalid batch"}, status_code=422)

    @app.exception_handler(IngestConflict)
    async def conflict(request: Request, error: IngestConflict):
        return JSONResponse({"detail": "batch conflicts with stored content"}, status_code=409)

    @app.exception_handler(SourceBindingError)
    async def scope_error(request: Request, error: SourceBindingError):
        return JSONResponse({"detail": "service source scope is not allowed"}, status_code=403)

    @app.exception_handler(AlarmConflict)
    @app.exception_handler(AlarmStoreUnavailable)
    @app.exception_handler(IngestionUnavailable)
    async def unavailable(request: Request, error: IngestionUnavailable):
        return JSONResponse({"detail": "ingestion unavailable"}, status_code=503)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, error: SQLAlchemyError):
        return JSONResponse({"detail": "ingestion unavailable"}, status_code=503)

    @app.exception_handler(MeasuredClockConflict)
    async def measured_conflict(request: Request, error):
        return JSONResponse(
            {"detail": "clock evidence conflicts with stored proof"}, status_code=409
        )

    @app.exception_handler(ProbeScopeError)
    async def probe_scope(request: Request, error):
        return JSONResponse({"detail": "clock probe scope is not allowed"}, status_code=403)

    @app.exception_handler(ProbeCapacityError)
    async def probe_capacity(request: Request, error):
        return JSONResponse({"detail": "clock probe capacity unavailable"}, status_code=429)

    @app.exception_handler(CommandConflict)
    async def command_conflict(request: Request, error: CommandConflict):
        return JSONResponse({"detail": "command conflicts with journal"}, status_code=409)

    @app.exception_handler(CommandScopeError)
    async def command_scope(request: Request, error: CommandScopeError):
        return JSONResponse({"detail": "command source scope is not allowed"}, status_code=403)

    async def command_body(request: Request, adapter):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > min(config.max_body_bytes, 65536):
                raise HTTPException(413, "command exceeds request bound")
            raw.extend(chunk)
        try:
            return adapter.validate_json(bytes(raw))
        except ValidationError:
            raise HTTPException(422, "invalid command payload") from None

    def command_transaction(binding, operation, payload=None):
        with ingestion_service.engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": SCHEMA_LOCK_ID}
            )
            revisions = connection.execute(text("SELECT version_num FROM alembic_version"))
            heads = revisions.scalars().all()
            if len(heads) != 1 or heads[0] not in SUPPORTED_COMMAND_REVISIONS:
                raise IngestionUnavailable("command dispatch schema unavailable")
            scope = dict(
                authenticated_gateway_id=binding.gateway_id,
                authenticated_source_id=binding.source_id,
                authenticated_stream_id=binding.stream_id,
                now=now(),
            )
            result = (
                operation(connection, **scope)
                if payload is None
                else operation(connection, payload, **scope)
            )
        # No dispatch ticket or receipt leaves this function before COMMIT.
        return result

    if source_binding_policy is not None:
        if not isinstance(source_binding_policy, SourceBindingPolicy):
            raise ValueError("Trusted source binding policy required")

        @app.post("/internal/v1/source-binding")
        async def configure_source_binding(request: Request):
            selected = authenticate(request)
            if not source_binding_policy.permits(selected):
                raise HTTPException(403, "source binding capability is not allowed")
            if request.query_params:
                raise HTTPException(422, "source binding request must have no query")
            async for chunk in request.stream():
                if chunk:
                    raise HTTPException(422, "source binding request must have no body")

            # Canonical method owns one schema/gateway/line/source transaction.
            # No registration occurs on startup or GET; never reset flags/cursors.
            def register():
                with ingestion_service._transaction() as connection:
                    database = connection.execute(text("SELECT current_database()")).scalar_one()
                    heads = (
                        connection.execute(text("SELECT version_num FROM alembic_version"))
                        .scalars()
                        .all()
                    )
                    if database != "hmi_mes_demo" or heads != ["0014_mixed_alarm_runtime"]:
                        raise IngestionUnavailable("Deployment binding database is unavailable")
                    ingestion_service.configure_source_on_connection(
                        connection,
                        source_id=selected.source_id,
                        stream_id=selected.stream_id,
                        gateway_id=selected.gateway_id,
                        line_id=source_binding_policy.line_id,
                        capture_generation=source_binding_policy.capture_generation,
                        now=now(),
                    )
                    progress = connection.execute(
                        text(
                            "SELECT 1 FROM stream_progress "
                            "WHERE source_id=:source AND stream_id=:stream"
                        ),
                        {"source": selected.source_id, "stream": selected.stream_id},
                    ).first()
                    if progress is None:
                        raise IngestConflict("Source progress must already be complete")

            await run_in_threadpool(register)
            return {"binding_registered": True}

    @app.post("/internal/v1/clock-probes")
    async def clock_probe(request: Request):
        selected = authenticate(request)
        if not ingestion_service.measured_clock_enabled:
            raise HTTPException(404, "clock probes are disabled")
        if request.query_params:
            raise HTTPException(422, "clock probe query is not allowed")
        payload = await command_body(request, TypeAdapter(ClockProbeRequest))
        if (payload.gateway_id, payload.source_id, payload.stream_id) != (
            selected.gateway_id,
            selected.source_id,
            selected.stream_id,
        ):
            raise ProbeScopeError("credential scope mismatch")

        def issue():
            with ingestion_service._transaction() as connection:
                heads = connection.execute(text("SELECT version_num FROM alembic_version"))
                revisions = heads.scalars().all()
                if len(revisions) != 1 or revisions[0] not in CLOCK_EVIDENCE_REVISIONS:
                    raise IngestionUnavailable("clock probe schema unavailable")
                ingestion_service._advisory(connection, "gateway", payload.gateway_id)
                source = ingestion_service._lock_stream(
                    connection, payload.source_id, payload.stream_id
                )
                if (
                    source["gateway_id"] != payload.gateway_id
                    or int(source["capture_generation"]) != payload.capture_generation
                    or source["conflicted"]
                    or source["reconciliation_required"]
                ):
                    raise ProbeScopeError("persisted source scope mismatch")
                response = issue_probe(
                    connection,
                    payload,
                    credential_key_id=selected.key_id,
                    api_received_at=request.state.clock_received_at,
                    now=now(),
                )
            return response

        result = await run_in_threadpool(issue)
        return result.model_dump(mode="json")

    @app.post("/internal/v1/commands/claim")
    async def command_claim(request: Request):
        binding = authenticate(request)
        payload = await command_body(request, TypeAdapter(dict))
        if payload:
            raise HTTPException(422, "claim body must be empty")
        work = await run_in_threadpool(command_transaction, binding, claim_next)
        return {"work": work.model_dump(mode="json") if work is not None else None}

    @app.post("/internal/v1/commands/submission")
    async def command_submission(request: Request):
        binding = authenticate(request)
        payload = await command_body(request, TypeAdapter(CommandSubmission))
        await run_in_threadpool(command_transaction, binding, apply_submission, payload)
        return {"committed": True, "command_id": payload.command_id}

    @app.post("/internal/v1/commands/result")
    async def command_result(request: Request):
        binding = authenticate(request)
        payload = await command_body(request, TypeAdapter(CommandResult | CommandNotFound))
        await run_in_threadpool(command_transaction, binding, apply_result, payload)
        return {"committed": True, "command_id": payload.command_id}

    @app.get("/health/live")
    async def live():
        return {"status": "alive"}

    @app.get("/internal/v1/alarm-clock-context")
    async def alarm_clock_context(request: Request):
        binding = authenticate(request)
        if request.query_params:
            raise HTTPException(422, "clock context uses the authenticated source scope")

        def read():
            with ingestion_service._transaction() as connection:
                source = (
                    connection.execute(
                        text(
                            "SELECT line_id,capture_generation FROM source_streams "
                            "WHERE source_id=:s AND stream_id=:t AND gateway_id=:g"
                        ),
                        {"s": binding.source_id, "t": binding.stream_id, "g": binding.gateway_id},
                    )
                    .mappings()
                    .first()
                )
                if source is None:
                    raise SourceBindingError("Configured source binding is unavailable")
                latest = (
                    connection.execute(
                        text(
                            "SELECT batch_id,capture_seq,content_sha256,stored_at,body "
                            "FROM ingest_batches WHERE source_id=:s AND stream_id=:t "
                            "AND gateway_id=:g AND capture_generation=:generation "
                            "ORDER BY capture_seq DESC LIMIT 1"
                        ),
                        {
                            "s": binding.source_id,
                            "t": binding.stream_id,
                            "g": binding.gateway_id,
                            "generation": source["capture_generation"],
                        },
                    )
                    .mappings()
                    .first()
                )
                capture = None
                if latest is not None:
                    # Expose original metadata only. An observed epoch is not proof
                    # of synchronization, freshness, or a shared host clock.
                    capture = {
                        "batch_id": latest["batch_id"],
                        "capture_seq": str(latest["capture_seq"]),
                        "content_sha256": latest["content_sha256"],
                        "stored_at": latest["stored_at"],
                        "captured_at": latest["body"]["captured_at"],
                        "capture_time": latest["body"].get("capture_time"),
                    }
                return {
                    "scope": {
                        "line_id": source["line_id"],
                        "source_id": binding.source_id,
                        "stream_id": binding.stream_id,
                        "gateway_id": binding.gateway_id,
                        "capture_generation": str(source["capture_generation"]),
                    },
                    "latest_capture": capture,
                    "clock_assessment": "NOT_VERIFIED",
                }

        return await run_in_threadpool(read)

    @app.get("/health/ready")
    async def ready(request: Request):
        authenticate(request)
        engine = ingestion_service.engine
        if engine.dialect.name != "postgresql":
            raise IngestionUnavailable("PostgreSQL is required")
        with engine.connect() as connection:
            connection.execute(text("SELECT 1")).scalar_one()
            rows = (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
            )
            if len(rows) != 1 or rows[0] not in SUPPORTED_INGESTION_REVISIONS:
                raise IngestionUnavailable("unsupported ingestion schema")
        return {"status": "ready"}

    @app.post("/internal/v1/batches")
    async def ingest(request: Request):
        binding = authenticate(request)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > config.max_body_bytes:
                raise HTTPException(413, "batch exceeds request bound")
            raw.extend(chunk)
        try:
            batch = GatewayBatch.model_validate_json(bytes(raw))
        except ValidationError:
            raise HTTPException(422, "invalid batch") from None
        body = batch.body
        if (body.gateway_id, body.source_id, body.stream_id) != (
            binding.gateway_id,
            binding.source_id,
            binding.stream_id,
        ):
            raise SourceBindingError("configured credential scope does not match batch")
        # The service owns its complete PostgreSQL transaction and returns only
        # after commit. No response is constructed from provisional receipts.
        ack = ingestion_service.ingest(
            batch,
            authenticated_gateway_id=binding.gateway_id,
            authenticated_source_id=binding.source_id,
            authenticated_stream_id=binding.stream_id,
            authenticated_key_id=binding.key_id,
            now=request.state.clock_received_at,
        )
        return JSONResponse(ack.model_dump(mode="json"))

    mount_settlement_routes(app, ingestion_service.engine, authenticate, now)
    return app
