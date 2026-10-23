"""Vercel ASGI composition using Neon; no background workers or provisioning."""

import json
import os
import re
import ssl
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import certifi
import psycopg
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool
from starlette.responses import JSONResponse
from starlette.routing import Mount

from apps.api.assistant import DEFAULT_MODEL, AssistantConfig
from apps.api.auth import UserAuth
from apps.api.database import SUPPORTED_MIXED_ALARM_REVISIONS
from apps.api.ingestion import IngestionService
from apps.api.internal import (
    InternalConfig,
    ServiceCredential,
    SourceBindingPolicy,
    create_internal_app,
)
from apps.api.trend_history import TrendRetention
from apps.api.user_http import UserHTTPConfig, create_user_app


def _credential(item):
    keys = {"key_id", "gateway_id", "source_id", "stream_id", "token_sha256", "expires_at"}
    if not isinstance(item, dict) or set(item) != keys:
        raise ValueError("Invalid service credential fields")
    if not isinstance(item["token_sha256"], str) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", item["token_sha256"]
    ):
        raise ValueError("Invalid service credential digest")
    return ServiceCredential(
        key_id=item["key_id"],
        gateway_id=item["gateway_id"],
        source_id=item["source_id"],
        stream_id=item["stream_id"],
        token_digest=bytes.fromhex(item["token_sha256"]),
        expires_at=datetime.fromisoformat(item["expires_at"]),
    )


def _packaged_binding(origin):
    # Fixed application package path, never a request/env-specified file or URL.
    with Path(__file__).with_name("cloud_plant.json").open("rb") as handle:
        raw = handle.read(8193)
    if len(raw) > 8192:
        raise ValueError("Deployment policy exceeds its bound")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate deployment policy field")
            result[key] = value
        return result

    values = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(values, dict) or set(values) != {
        "schema_version",
        "enabled",
        "origin",
        "binding",
    }:
        raise ValueError("Invalid deployment policy fields")
    if type(values["schema_version"]) is not int or values["schema_version"] != 1:
        raise ValueError("Unknown deployment policy version")
    if values["enabled"] is not True or values["origin"] != origin:
        raise ValueError("Deployment policy is not enabled for this origin")
    binding = values["binding"]
    if not isinstance(binding, dict) or set(binding) != {
        "credential",
        "line_id",
        "capture_generation",
    }:
        raise ValueError("Invalid deployment binding fields")
    return SourceBindingPolicy(
        credential=_credential(binding["credential"]),
        line_id=binding["line_id"],
        capture_generation=binding["capture_generation"],
    )


@dataclass(frozen=True)
class VercelConfig:
    origin: str
    database_dsn: str = field(repr=False)
    credentials: tuple[ServiceCredential, ...] = field(default=(), repr=False)
    source_binding_policy: SourceBindingPolicy | None = field(default=None, repr=False)
    guest_viewer: bool = False
    # None keeps the baseline (7-day raw, 90-day minute). A public free-tier profile
    # may shorten it; see docs/architecture/TREND_HISTORY.md.
    trend_retention: TrendRetention | None = None
    # Read-only assistant; absent key keeps the feature off.
    assistant: AssistantConfig | None = field(default=None, repr=False)

    def __post_init__(self):
        UserHTTPConfig(
            origin=self.origin, allow_insecure_loopback=False, guest_viewer=self.guest_viewer
        )
        if self.source_binding_policy is not None and (
            not isinstance(self.source_binding_policy, SourceBindingPolicy)
            or self.source_binding_policy.credential not in self.credentials
        ):
            raise ValueError("Deployment binding must match its active credential")
        url = make_url(self.database_dsn)
        if (
            url.drivername not in {"postgresql", "postgresql+psycopg"}
            or url.username != "hmi_app"
            or not url.host
            or not url.database
            or not url.password
        ):
            raise ValueError("A dedicated Neon hmi_app connection is required")
        # libpq query keys can override URL username/host, or SET ROLE via options.
        # Preserve only the two settings included in ordinary Neon connection URLs.
        if set(url.query) - {"sslmode", "channel_binding"} or any(
            not isinstance(value, str) for value in url.query.values()
        ):
            raise ValueError("Unsupported Neon connection parameters")
        if url.query.get("sslmode", "verify-full") not in {"require", "verify-ca", "verify-full"}:
            raise ValueError("Encrypted Neon transport is required")
        if url.query.get("channel_binding", "prefer") not in {"prefer", "require"}:
            raise ValueError("Unsupported channel binding policy")

    @classmethod
    def from_environment(cls):
        guest_setting = os.environ.get("HMI_GUEST_VIEWER_ENABLED", "false")
        if guest_setting not in {"true", "false"}:
            raise ValueError("HMI_GUEST_VIEWER_ENABLED must be true or false")
        origin = os.environ["HMI_PUBLIC_ORIGIN"]
        database_dsn = os.environ["HMI_DATABASE_DSN"]
        trend_retention = _trend_retention()
        assistant = None
        if os.environ.get("HMI_ASSISTANT_API_KEY"):
            assistant = AssistantConfig(
                api_key=os.environ["HMI_ASSISTANT_API_KEY"],
                model=os.environ.get("HMI_ASSISTANT_MODEL", DEFAULT_MODEL),
            )
        policy = None
        if "HMI_GATEWAY_CREDENTIALS_JSON" not in os.environ:
            policy = _packaged_binding(origin)
            credentials = (policy.credential,)
        else:
            raw = os.environ["HMI_GATEWAY_CREDENTIALS_JSON"]
            if len(raw.encode()) > 65536:
                raise ValueError("Service configuration exceeds its bound")
            values = json.loads(raw)
            if not isinstance(values, list) or len(values) > 128:
                raise ValueError("Invalid service configuration")
            credentials = tuple(_credential(item) for item in values)
        return cls(
            origin=origin,
            database_dsn=database_dsn,
            guest_viewer=guest_setting == "true",
            credentials=credentials,
            source_binding_policy=policy,
            trend_retention=trend_retention,
            assistant=assistant,
        )


_WHOLE_NUMBER = re.compile(r"[1-9][0-9]{0,3}")


def _trend_retention():
    """Optional public-profile retention; absent means the baseline, invalid fails startup."""
    raw = os.environ.get("HMI_TREND_RAW_HOURS")
    minute = os.environ.get("HMI_TREND_MINUTE_DAYS")
    if raw is None and minute is None:
        return None
    values = {}
    for name, value, bound in (
        ("HMI_TREND_RAW_HOURS", raw, 168),
        ("HMI_TREND_MINUTE_DAYS", minute, 90),
    ):
        if value is None:
            continue
        if not _WHOLE_NUMBER.fullmatch(value) or int(value) > bound:
            raise ValueError(f"{name} must be a whole number from 1 to {bound}")
        values[name] = int(value)
    defaults = TrendRetention()
    return TrendRetention(
        raw_days=values["HMI_TREND_RAW_HOURS"] / 24
        if "HMI_TREND_RAW_HOURS" in values
        else defaults.raw_days,
        minute_days=values.get("HMI_TREND_MINUTE_DAYS", defaults.minute_days),
    )


def neon_engine(config: VercelConfig):
    """Transaction pooling compatible; verified TLS and no retained application pool."""
    return sa.create_engine(
        make_url(config.database_dsn).set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
        echo=False,
        connect_args={
            "connect_timeout": 10,
            "prepare_threshold": None,
            "sslmode": "verify-full",
            "sslrootcert": certifi.where(),
        },
    )


_READINESS_SQLSTATES = {
    "28P01": "AUTHENTICATION_FAILED",
    "28000": "AUTHENTICATION_FAILED",
    "42501": "PERMISSION_DENIED",
    "42P01": "SCHEMA_MISSING",
    "3D000": "DATABASE_MISSING",
    "53300": "CONNECTION_CAPACITY",
    "08000": "CONNECTION_UNAVAILABLE",
    "08001": "CONNECTION_UNAVAILABLE",
    "08003": "CONNECTION_UNAVAILABLE",
    "08004": "CONNECTION_UNAVAILABLE",
    "08006": "CONNECTION_UNAVAILABLE",
    "08007": "CONNECTION_UNAVAILABLE",
    "08P01": "CONNECTION_UNAVAILABLE",
}


def _readiness_failure(stage, error):
    """Only fixed codes leave this boundary; never serialize/log database errors."""
    original = getattr(error, "orig", error)
    reason = _READINESS_SQLSTATES.get(getattr(original, "sqlstate", None))
    if reason is None:
        certificate_error = isinstance(original, ssl.SSLCertVerificationError)
        if isinstance(original, psycopg.OperationalError):
            # libpq exposes certificate failures as text, not an SQLSTATE. Inspect
            # a few fixed markers internally; no substring is returned or logged.
            message = str(original).lower()
            if "password authentication failed" in message:
                return {"ready": False, "stage": stage, "reason": "AUTHENTICATION_FAILED"}
            certificate_error = (
                certificate_error
                or any(
                    marker in message
                    for marker in (
                        "certificate verify failed",
                        "certificate verification failed",
                        "root certificate file",
                    )
                )
                or ("server certificate" in message and "does not match host name" in message)
            )
        reason = "TLS_CERTIFICATE_FAILED" if certificate_error else "CHECK_FAILED"
    return {"ready": False, "stage": stage, "reason": reason}


def create_vercel_app(config: VercelConfig, *, engine=None):
    database = engine if engine is not None else neon_engine(config)
    users = create_user_app(
        UserAuth(database, allowed_origins={config.origin}, guest_viewer=config.guest_viewer),
        UserHTTPConfig(
            origin=config.origin,
            allow_insecure_loopback=False,
            guest_viewer=config.guest_viewer,
            assistant=config.assistant,
        ),
    )
    internal = (
        create_internal_app(
            IngestionService(database, trend_retention=config.trend_retention),
            credential_bindings=config.credentials,
            config=InternalConfig(allow_insecure_loopback=False),
            source_binding_policy=config.source_binding_policy,
        )
        if config.credentials
        else None
    )

    async def ready(scope, receive, send):
        def inspect():
            stage = "connection"
            try:
                with database.connect() as connection:
                    stage = "schema"
                    head = (
                        connection.execute(sa.text("SELECT version_num FROM alembic_version"))
                        .scalars()
                        .all()
                    )
                    if len(head) != 1 or head[0] not in SUPPORTED_MIXED_ALARM_REVISIONS:
                        return {"ready": False, "stage": stage, "reason": "REVISION_UNSUPPORTED"}
                    stage = "runtime_role"
                    role = connection.execute(
                        sa.text(
                            "SELECT current_user='hmi_app' AND NOT "
                            "(rolsuper OR rolcreatedb OR rolcreaterole "
                            "OR rolreplication OR rolbypassrls) "
                            "FROM pg_roles WHERE rolname=current_user"
                        )
                    ).scalar_one()
                    if not role:
                        return {"ready": False, "stage": stage, "reason": "RUNTIME_ROLE_REQUIRED"}
                    stage = "reference_data"
                    count = connection.execute(
                        sa.text("SELECT count(*) FROM tag_definitions")
                    ).scalar_one()
                    if count != 48:
                        return {"ready": False, "stage": stage, "reason": "REFERENCE_INCOMPLETE"}
                return {"ready": True}
            except Exception as error:
                return _readiness_failure(stage, error)

        from starlette.concurrency import run_in_threadpool

        try:
            result = await run_in_threadpool(inspect)
        except Exception as error:
            result = _readiness_failure("connection", error)
        await JSONResponse(
            result,
            status_code=200 if result["ready"] else 503,
            headers={"Cache-Control": "no-store"},
        )(scope, receive, send)

    async def dispatch(scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            return
        path = scope["path"]
        if path in {"/api/health", "/health/live"}:
            await JSONResponse({"alive": True})(scope, receive, send)
        elif path == "/api/ready":
            await ready(scope, receive, send)
        elif path.startswith("/internal/") or path == "/health/ready":
            if internal is None:
                await JSONResponse({"detail": "service intake is not configured"}, status_code=503)(
                    scope, receive, send
                )
            else:
                await internal(scope, receive, send)
        else:
            await users(scope, receive, send)

    return dispatch


class _LazyApplication:
    """Builds configuration once per warm worker, without network I/O at import."""

    def __init__(self):
        self.application = None
        self.lock = threading.Lock()

    async def __call__(self, scope, receive, send):
        if self.application is None:
            try:
                with self.lock:
                    if self.application is None:
                        self.application = create_vercel_app(VercelConfig.from_environment())
            except Exception:
                if scope["type"] == "http":
                    await JSONResponse(
                        {"detail": "deployment configuration unavailable"},
                        status_code=503,
                        headers={"Cache-Control": "no-store"},
                    )(scope, receive, send)
                elif scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                return
        await self.application(scope, receive, send)


def vercel_application():
    # Export a real FastAPI instance for Vercel's framework/runtime detection.
    return FastAPI(
        routes=[Mount("/", app=_LazyApplication())],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
