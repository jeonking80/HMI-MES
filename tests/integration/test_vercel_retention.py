"""Actual Vercel intake cleanup and user trend reads on disposable runtime PG17."""

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import psycopg
import pytest
import sqlalchemy as sa
from alembic import command
from psycopg import sql

from apps.api.auth import UserAuth
from apps.api.bootstrap import LINE_ID
from apps.api.database import create_database_engine, migration_config
from apps.api.ingestion import IngestionService
from apps.api.internal import ServiceCredential
from apps.api.serverless import VercelConfig, create_vercel_app
from apps.api.trend_history import TrendRetention
from tests.integration.test_ingestion import batch

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://retention.example.test"
DSN = "postgresql://hmi_app:fixture-only@neon.example.test/hmi_mes_demo?sslmode=require"
SCOPE = dict(line_id=LINE_ID, source_id="simulator-1", stream_id="stream-1")


@pytest.fixture
def runtime(postgres_url):
    params = dict(
        host=postgres_url.host,
        port=postgres_url.port,
        user=postgres_url.username,
        password=postgres_url.password,
        dbname=postgres_url.database,
    )
    with psycopg.connect(**params, autocommit=True) as db:
        db.execute("CREATE DATABASE hmi_mes_demo")
    params["dbname"] = "hmi_mes_demo"
    password = secrets.token_urlsafe(32)
    owner = engine = None
    try:
        with psycopg.connect(**params, autocommit=True) as db:
            db.execute((ROOT / "deploy/neon-init.sql").read_text(), prepare=False)
            db.execute(
                sql.SQL("ALTER ROLE hmi_app LOGIN PASSWORD {}").format(sql.Literal(password))
            )
        owner = create_database_engine(postgres_url.set(database="hmi_mes_demo"))
        with owner.connect() as db:
            db.execute(sa.text("SET ROLE hmi_migrator"))
            db.commit()
            command.upgrade(migration_config(db), "0017_guest_viewer")
            db.execute(sa.text("RESET ROLE"))
            db.commit()
        with psycopg.connect(**params, autocommit=True) as db:
            db.execute((ROOT / "deploy/neon-kuls-grants.sql").read_text(), prepare=False)
        engine = create_database_engine(
            postgres_url.set(database="hmi_mes_demo", username="hmi_app", password=password)
        )
        yield SimpleNamespace(owner=owner, engine=engine)
    finally:
        if engine is not None:
            engine.dispose()
        if owner is not None:
            owner.dispose()
        params["dbname"] = postgres_url.database
        with psycopg.connect(**params, autocommit=True) as db:
            db.execute("DROP DATABASE hmi_mes_demo WITH (FORCE)")
            db.execute("DROP ROLE hmi_app")
            db.execute("DROP ROLE hmi_migrator")


def sample_batch(seq, at):
    return batch(
        capture_seq=seq,
        batch_id=f"retention-{seq}",
        captured_at=at.isoformat(),
        samples=[
            dict(
                sample_id=f"retention-sample-{seq}",
                tag_id="Sealer.TemperaturePV",
                source_timestamp=at.isoformat(),
                server_timestamp=None,
                acquired_at=at.isoformat(),
                status_code=0,
                reading=dict(data_type="Double", value=150.0),
            )
        ],
    )


def submit(service, value, now):
    return service.ingest(
        value,
        authenticated_gateway_id="gateway-1",
        authenticated_source_id="simulator-1",
        authenticated_stream_id="stream-1",
        now=now,
    )


@pytest.mark.parametrize("profile", [False, True], ids=["baseline-7d-90d", "deployment-6h-14d"])
async def test_actual_serverless_cleanup_and_user_routes(runtime, monkeypatch, profile):
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    old_minute = now - timedelta(days=20)
    old_raw = now - timedelta(days=1)
    service = IngestionService(runtime.owner)
    service.configure_source(**SCOPE, gateway_id="gateway-1", capture_generation=1, now=old_minute)
    # Build real raw/minute evidence before retention cleanup becomes due.
    submit(service, sample_batch(1, old_minute), old_minute + timedelta(minutes=2))
    submit(service, sample_batch(2, old_raw), old_raw + timedelta(minutes=2))
    token = secrets.token_urlsafe(32)
    credential = ServiceCredential(
        "retention-key",
        "gateway-1",
        "simulator-1",
        "stream-1",
        hashlib.sha256(token.encode()).digest(),
        now + timedelta(days=1),
    )
    monkeypatch.setenv("HMI_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setenv("HMI_DATABASE_DSN", DSN)
    monkeypatch.setenv(
        "HMI_GATEWAY_CREDENTIALS_JSON",
        json.dumps(
            [
                {
                    "key_id": credential.key_id,
                    "gateway_id": credential.gateway_id,
                    "source_id": credential.source_id,
                    "stream_id": credential.stream_id,
                    "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                    "expires_at": credential.expires_at.isoformat(),
                }
            ]
        ),
    )
    monkeypatch.delenv("HMI_GUEST_VIEWER_ENABLED", raising=False)
    for name, value in (("HMI_TREND_RAW_HOURS", "6"), ("HMI_TREND_MINUTE_DAYS", "14")):
        if profile:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
    config = VercelConfig.from_environment()
    assert config.trend_retention == (
        TrendRetention(raw_days=0.25, minute_days=14) if profile else None
    )
    auth = UserAuth(runtime.owner, allowed_origins={ORIGIN})
    password = secrets.token_urlsafe(24)
    auth.bootstrap_user("viewer", password, roles={"viewer"})
    challenge = auth.begin_login()
    session = auth.login(
        "viewer",
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=ORIGIN,
    )
    session = auth.change_password(
        session.token, secrets.token_urlsafe(24), csrf_token=session.csrf_token, origin=ORIGIN
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_vercel_app(config, engine=runtime.engine)),
        base_url=ORIGIN,
    ) as client:
        value = sample_batch(3, now - timedelta(minutes=1))
        assert (
            await client.post("/internal/v1/batches", json=value.model_dump(mode="json"))
        ).status_code == 401
        response = await client.post(
            "/internal/v1/batches",
            json=value.model_dump(mode="json"),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        params = {
            **SCOPE,
            "tag": "Sealer.TemperaturePV",
            "from": old_raw.isoformat(),
            "to": (old_raw + timedelta(minutes=1)).isoformat(),
            "resolution": "second",
        }
        assert (await client.get("/api/v1/trends", params=params)).status_code == 401
        client.cookies.set("hmi_session", session.token)
        response = await client.get("/api/v1/trends", params=params)
        assert response.status_code == 200, response.text
        assert len(response.json()["series"][0]["points"]) == 60
    with runtime.engine.connect() as db:
        raw_count = db.execute(
            sa.text("SELECT count(*) FROM analog_history_raw WHERE batch_id='retention-2'")
        ).scalar_one()
        minute_count = db.execute(
            sa.text("SELECT count(*) FROM analog_history_minutes WHERE minute=:at"),
            {"at": old_minute},
        ).scalar_one()
        assert raw_count == (0 if profile else 1)
        assert minute_count == (0 if profile else 1)
        # Cleanup touches only the new history; original immutable batch evidence remains.
        assert db.execute(sa.text("SELECT count(*) FROM ingest_batches")).scalar_one() == 3
