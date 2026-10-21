"""Vercel ASGI composition using Neon; no background workers or provisioning."""

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime

import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool
from starlette.responses import JSONResponse
from starlette.routing import Mount

from apps.api.auth import UserAuth
from apps.api.database import SUPPORTED_AUTH_REVISIONS
from apps.api.ingestion import IngestionService
from apps.api.internal import InternalConfig, ServiceCredential, create_internal_app
from apps.api.user_http import UserHTTPConfig, create_user_app


@dataclass(frozen=True)
class VercelConfig:
    origin: str
    database_dsn: str = field(repr=False)
    credentials: tuple[ServiceCredential, ...] = field(default=(), repr=False)

    def __post_init__(self):
        UserHTTPConfig(origin=self.origin, allow_insecure_loopback=False)
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
        raw = os.environ.get("HMI_GATEWAY_CREDENTIALS_JSON", "[]")
        if len(raw.encode()) > 65536:
            raise ValueError("Service configuration exceeds its bound")
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) > 128:
            raise ValueError("Invalid service configuration")
        credentials = []
        keys = {"key_id", "gateway_id", "source_id", "stream_id", "token_sha256", "expires_at"}
        for item in values:
            if not isinstance(item, dict) or set(item) != keys:
                raise ValueError("Invalid service credential fields")
            credentials.append(
                ServiceCredential(
                    key_id=item["key_id"],
                    gateway_id=item["gateway_id"],
                    source_id=item["source_id"],
                    stream_id=item["stream_id"],
                    token_digest=bytes.fromhex(item["token_sha256"]),
                    expires_at=datetime.fromisoformat(item["expires_at"]),
                )
            )
        return cls(
            origin=os.environ["HMI_PUBLIC_ORIGIN"],
            database_dsn=os.environ["HMI_DATABASE_DSN"],
            credentials=tuple(credentials),
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
            "sslrootcert": "system",
        },
    )


def create_vercel_app(config: VercelConfig, *, engine=None):
    database = engine if engine is not None else neon_engine(config)
    users = create_user_app(
        UserAuth(database, allowed_origins={config.origin}),
        UserHTTPConfig(origin=config.origin, allow_insecure_loopback=False),
    )
    internal = (
        create_internal_app(
            IngestionService(database),
            credential_bindings=config.credentials,
            config=InternalConfig(allow_insecure_loopback=False),
        )
        if config.credentials
        else None
    )

    async def ready(scope, receive, send):
        def inspect():
            with database.connect() as connection:
                head = (
                    connection.execute(sa.text("SELECT version_num FROM alembic_version"))
                    .scalars()
                    .all()
                )
                role = connection.execute(
                    sa.text(
                        "SELECT current_user='hmi_app' AND NOT "
                        "(rolsuper OR rolcreatedb OR rolcreaterole "
                        "OR rolreplication OR rolbypassrls) "
                        "FROM pg_roles WHERE rolname=current_user"
                    )
                ).scalar_one()
                count = connection.execute(
                    sa.text("SELECT count(*) FROM tag_definitions")
                ).scalar_one()
                return (
                    len(head) == 1 and head[0] in SUPPORTED_AUTH_REVISIONS and role and count == 48
                )

        from starlette.concurrency import run_in_threadpool

        try:
            available = await run_in_threadpool(inspect)
        except Exception:
            available = False
        await JSONResponse({"ready": bool(available)}, status_code=200 if available else 503)(
            scope, receive, send
        )

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
                        {"detail": "deployment configuration unavailable"}, status_code=503
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
