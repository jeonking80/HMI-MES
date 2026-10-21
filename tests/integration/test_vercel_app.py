"""Local deployment-contract tests; no external Neon/Vercel connection.

Use the actual Neon SQL initializer in a disposable PostgreSQL17 database, then
inject its non-superuser hmi_app engine into the serverless factory. Local test
PostgreSQL is not a remote TLS certificate verification test.
"""

import hashlib
import importlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import psycopg
import pytest
import sqlalchemy as sa
from psycopg import sql
from sqlalchemy.pool import NullPool

from apps.api import serverless
from apps.api.auth import UserAuth
from apps.api.bootstrap import LINE_ID
from apps.api.database import create_database_engine
from apps.api.ingestion import IngestionService
from apps.api.internal import ServiceCredential
from apps.api.user_http import SESSION_COOKIE
from tests.integration.test_ingestion import AT, batch, event, register
from tests.integration.test_user_http import ORIGIN, login, protection

DSN = "postgresql://hmi_app:fixture-only@neon.example.test/hmi_mes_demo?sslmode=require"
ROOT = Path(__file__).resolve().parents[2]


@dataclass(repr=False)
class Runtime:
    admin: object
    engine: object
    config: object
    token: str


@pytest.fixture(scope="module")
def runtime(postgres_url):
    # Exclusively the generated disposable container; never a deployed database.
    params = dict(
        host=postgres_url.host,
        port=postgres_url.port,
        user=postgres_url.username,
        password=postgres_url.password,
        dbname=postgres_url.database,
    )
    with psycopg.connect(**params, autocommit=True) as admin:
        admin.execute("CREATE DATABASE hmi_mes_demo")
    params["dbname"] = "hmi_mes_demo"
    password = secrets.token_urlsafe(32)
    with psycopg.connect(**params, autocommit=True) as admin:
        admin.execute((ROOT / "deploy/neon-init.sql").read_text(), prepare=False)
        admin.execute(sql.SQL("ALTER ROLE hmi_app LOGIN PASSWORD {}").format(sql.Literal(password)))
    admin_engine = create_database_engine(postgres_url.set(database="hmi_mes_demo"))
    app_engine = create_database_engine(
        postgres_url.set(database="hmi_mes_demo", username="hmi_app", password=password)
    )
    token = secrets.token_urlsafe(32)
    credential = ServiceCredential(
        "fixture-key",
        "gateway-1",
        "simulator-1",
        "stream-1",
        hashlib.sha256(token.encode()).digest(),
        datetime.now(UTC) + timedelta(days=1),
    )
    config = serverless.VercelConfig(ORIGIN, DSN, (credential,))
    try:
        yield Runtime(admin=admin_engine, engine=app_engine, config=config, token=token)
    finally:
        app_engine.dispose()
        admin_engine.dispose()
        # The shared container fixture removes this entire disposable server.


def client_for(runtime, **kwargs):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=serverless.create_vercel_app(
                runtime.config, engine=kwargs.get("engine", runtime.engine)
            )
        ),
        base_url=ORIGIN,
    )


async def test_initializer_runtime_readiness_and_admin_role_rejected(runtime):
    with runtime.engine.connect() as db:
        assert db.execute(sa.text("SELECT current_user")).scalar_one() == "hmi_app"
        assert db.execute(sa.text("SELECT count(*) FROM tag_definitions")).scalar_one() == 48
        assert (
            db.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0014_mixed_alarm_runtime"
        )
        assert not db.execute(
            sa.text("SELECT has_schema_privilege(current_user,'public','CREATE')")
        ).scalar_one()
    async with client_for(runtime) as client:
        assert (await client.get("/api/health")).json() == {"alive": True}
        response = await client.get("/api/ready")
        assert response.status_code == 200 and response.json() == {"ready": True}
        assert response.headers["cache-control"] == "no-store"
    async with client_for(runtime, engine=runtime.admin) as client:
        response = await client.get("/api/ready")
        assert response.status_code == 503 and response.json() == {
            "ready": False,
            "stage": "runtime_role",
            "reason": "RUNTIME_ROLE_REQUIRED",
        }


async def test_https_auth_rotation_secure_cookie_and_passive_scoped_screen(runtime):
    password = secrets.token_urlsafe(24)
    UserAuth(runtime.admin, allowed_origins={ORIGIN}).bootstrap_user(
        "vercel-person", password, roles={"operator"}
    )
    service = IngestionService(runtime.admin)
    service.configure_source(
        source_id="screen-source",
        stream_id="screen-stream",
        gateway_id="screen-gateway",
        line_id=LINE_ID,
        capture_generation=1,
        now=datetime.now(UTC),
    )
    scope = {"line_id": LINE_ID, "source_id": "screen-source", "stream_id": "screen-stream"}
    async with client_for(runtime) as client:
        assert (await client.get("/api/v1/screen", params=scope)).status_code == 401
        first = await login(client, password, login="vercel-person")
        assert first.status_code == 200 and first.json()["must_change_password"]
        cookies = ";".join(first.headers.get_list("set-cookie")).lower()
        assert all(x in cookies for x in ["secure", "httponly", "samesite=strict"])
        assert (await client.get("/api/v1/screen", params=scope)).status_code == 403
        old = client.cookies.get(SESSION_COOKIE)
        changed = await client.post(
            "/auth/password",
            headers=protection(first.json()["csrf_token"]),
            json={"new_password": secrets.token_urlsafe(24)},
        )
        assert changed.status_code == 200 and not changed.json()["must_change_password"]
        assert client.cookies.get(SESSION_COOKIE) != old
        with runtime.engine.connect() as db:
            before = db.execute(
                sa.text("SELECT last_seen_at FROM auth_sessions WHERE token_digest=:token"),
                {"token": hashlib.sha256(client.cookies.get(SESSION_COOKIE).encode()).digest()},
            ).scalar_one()
        result = await client.get("/api/v1/screen", params=scope)
        assert result.status_code == 200
        with runtime.engine.connect() as db:
            after = db.execute(
                sa.text("SELECT last_seen_at FROM auth_sessions WHERE token_digest=:token"),
                {"token": hashlib.sha256(client.cookies.get(SESSION_COOKIE).encode()).digest()},
            ).scalar_one()
        assert before == after
        assert (
            await client.get("/api/v1/screen", params=scope | {"line_id": "other"})
        ).status_code == 404
        assert (
            await client.get("/auth/me", headers={"Cookie": f"{SESSION_COOKIE}={old}"})
        ).status_code == 401
        assert (await client.post("/internal/v1/batches", json={})).status_code == 401


async def test_service_bearer_is_not_user_and_actual_batch_ack_replay(runtime):
    service = IngestionService(runtime.admin)
    service.configure_source(
        source_id="simulator-1",
        stream_id="stream-1",
        gateway_id="gateway-1",
        line_id=LINE_ID,
        capture_generation=1,
        now=AT,
    )
    register(service)
    value = batch(event(quantity=7), batch_id="vercel-actual-batch")
    async with client_for(runtime) as client:
        bearer = {"Authorization": f"Bearer {runtime.token}"}
        assert (await client.get("/auth/me", headers=bearer)).status_code == 401
        denied = await client.post(
            "/internal/v1/batches",
            headers={"Authorization": f"Bearer {secrets.token_urlsafe(32)}"},
            json=value.model_dump(mode="json"),
        )
        assert denied.status_code == 401
        first = await client.post(
            "/internal/v1/batches", headers=bearer, json=value.model_dump(mode="json")
        )
        assert first.status_code == 200, first.text
        repeated = await client.post(
            "/internal/v1/batches", headers=bearer, json=value.model_dump(mode="json")
        )
        assert repeated.status_code == 200 and repeated.json() == first.json()
        assert first.json()["durability"] == "COMMITTED"
    with runtime.engine.connect() as db:
        assert (
            db.execute(
                sa.text("SELECT count(*) FROM ingest_batches WHERE batch_id='vercel-actual-batch'")
            ).scalar_one()
            == 1
        )
        assert (
            db.execute(
                sa.text("SELECT good_quantity FROM execution_totals WHERE execution_id='run-A'")
            ).scalar_one()
            == 7
        )


@pytest.mark.parametrize(
    "query",
    [
        "user=postgres",
        "host=evil.example",
        "options=-crole=postgres",
        "service=admin",
        "dbname=other",
        "sslmode=disable",
        "channel_binding=disable",
        "sslmode=require&sslmode=verify-full",
    ],
)
def test_dsn_override_rejection(query):
    with pytest.raises(ValueError):
        serverless.VercelConfig(
            ORIGIN, "postgresql://hmi_app:fixture-only@neon.example.test/hmi_mes_demo?" + query
        )


@pytest.mark.parametrize(
    "origin", ["http://hmi.example.test", ORIGIN + "/", "https://user@hmi.example.test"]
)
def test_exact_https_origin_required(origin):
    with pytest.raises(ValueError):
        serverless.VercelConfig(origin, DSN)


def test_effective_neon_tls_no_preparation_and_no_retained_pool():
    engine = serverless.neon_engine(serverless.VercelConfig(ORIGIN, DSN))
    assert isinstance(engine.pool, NullPool) and engine.hide_parameters and not engine.echo
    captured = {}

    def no_network(dialect, connection_record, cargs, cparams):
        captured.update(cparams)
        raise RuntimeError("local-connect-intercept")

    sa.event.listen(engine, "do_connect", no_network)
    try:
        with pytest.raises(RuntimeError, match="local-connect-intercept"):
            engine.connect()
        assert captured["user"] == "hmi_app"
        assert captured["sslmode"] == "verify-full" and captured["sslrootcert"] == "system"
        assert captured["prepare_threshold"] is None
    finally:
        engine.dispose()


async def test_lazy_import_and_missing_env_fail_closed_without_db_side_effects(monkeypatch):
    for key in ["HMI_PUBLIC_ORIGIN", "HMI_DATABASE_DSN", "HMI_GATEWAY_CREDENTIALS_JSON"]:
        monkeypatch.delenv(key, raising=False)
    calls = []

    def no_engine(*args, **kwargs):
        calls.append(True)
        raise AssertionError("No engine creation allowed")

    monkeypatch.setattr(serverless, "neon_engine", no_engine)
    import api.index

    app = importlib.reload(api.index).app
    assert not calls
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.get("/api/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "deployment configuration unavailable"} and not calls


@pytest.mark.parametrize(
    "state,reason",
    [
        ("28P01", "AUTHENTICATION_FAILED"),
        ("28000", "AUTHENTICATION_FAILED"),
        ("42501", "PERMISSION_DENIED"),
        ("42P01", "SCHEMA_MISSING"),
        ("3D000", "DATABASE_MISSING"),
        ("53300", "CONNECTION_CAPACITY"),
        ("08006", "CONNECTION_UNAVAILABLE"),
        ("XX999", "CHECK_FAILED"),
    ],
)
async def test_readiness_connection_sqlstate_allowlist_sanitizes_error(state, reason, caplog):
    secret = "fixture-private-host-user-password-and-SELECT-token"

    class DatabaseError(Exception):
        sqlstate = state

    class FailedEngine:
        dialect = SimpleNamespace(name="postgresql")
        hide_parameters = True
        echo = False

        def connect(self):
            raise sa.exc.OperationalError(secret, {"password": secret}, DatabaseError(secret))

    app = serverless.create_vercel_app(serverless.VercelConfig(ORIGIN, DSN), engine=FailedEngine())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.get("/api/ready")
    assert response.status_code == 503
    assert response.json() == {"ready": False, "stage": "connection", "reason": reason}
    assert secret not in response.text + caplog.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "message",
    [
        "certificate verify failed: fixture-private-host-user-password",
        'server certificate for "fixture-private-host-user-password" does not match host name',
    ],
)
async def test_readiness_libpq_certificate_failure_is_safe_fixed_code(message, caplog):
    class FailedEngine:
        dialect = SimpleNamespace(name="postgresql")
        hide_parameters = True
        echo = False

        def connect(self):
            raise sa.exc.OperationalError(message, {}, psycopg.OperationalError(message))

    app = serverless.create_vercel_app(serverless.VercelConfig(ORIGIN, DSN), engine=FailedEngine())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.get("/api/ready")
    assert response.status_code == 503
    assert response.json() == {
        "ready": False,
        "stage": "connection",
        "reason": "TLS_CERTIFICATE_FAILED",
    }
    assert "fixture-private-host-user-password" not in response.text + caplog.text


async def test_readiness_actual_schema_and_reference_failure_stages(runtime):
    # Roll back each deliberate change before returning this shared disposable DB.
    async with client_for(runtime) as client:
        with runtime.admin.begin() as db:
            db.execute(sa.text("UPDATE alembic_version SET version_num='fixture-unsupported'"))
        try:
            response = await client.get("/api/ready")
            assert response.json() == {
                "ready": False,
                "stage": "schema",
                "reason": "REVISION_UNSUPPORTED",
            }
        finally:
            with runtime.admin.begin() as db:
                db.execute(
                    sa.text("UPDATE alembic_version SET version_num='0014_mixed_alarm_runtime'")
                )
        with runtime.admin.begin() as db:
            db.execute(sa.text("REVOKE SELECT ON tag_definitions FROM hmi_app"))
        try:
            response = await client.get("/api/ready")
            assert response.json() == {
                "ready": False,
                "stage": "reference_data",
                "reason": "PERMISSION_DENIED",
            }
        finally:
            with runtime.admin.begin() as db:
                db.execute(sa.text("GRANT SELECT ON tag_definitions TO hmi_app"))
        assert (await client.get("/api/ready")).json() == {"ready": True}


async def test_readiness_connect_time_authentication_without_sqlstate(caplog):
    private = "fixture-private-host-user-password"

    class FailedEngine:
        dialect = SimpleNamespace(name="postgresql")
        hide_parameters = True
        echo = False

        def connect(self):
            raise psycopg.OperationalError("password authentication failed for user " + private)

    app = serverless.create_vercel_app(serverless.VercelConfig(ORIGIN, DSN), engine=FailedEngine())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.get("/api/ready")
    assert response.status_code == 503
    assert response.json() == {
        "ready": False,
        "stage": "connection",
        "reason": "AUTHENTICATION_FAILED",
    }
    assert response.headers["cache-control"] == "no-store"
    assert private not in response.text + caplog.text
