"""Explicit head18 runtime grants on disposable canonical14 -> 17 -> 18 PG17."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
import pytest
import sqlalchemy as sa
from alembic import command
from psycopg import sql

from apps.api.database import create_database_engine, migration_config
from apps.api.ingestion import IngestionService
from apps.api.internal import InternalConfig, ServiceCredential, create_internal_app
from deploy.generate_neon_init import connect
from tests.integration.test_kuls_runtime_grants import (
    AT,
    SCOPE,
    batch,
    denied,
    role_engine,
)
from tests.integration.test_kuls_runtime_grants import (
    installed as installed,
)

ROOT = Path(__file__).resolve().parents[2]
GRANTS = (ROOT / "deploy/neon-clock-grants.sql").read_text()


@pytest.fixture
def head17(installed):
    migrator = role_engine(installed, "hmi_migrator")
    try:
        with migrator.connect() as db:
            command.upgrade(migration_config(db), "0017_guest_viewer")
        with connect(installed) as db:
            db.execute((ROOT / "deploy/neon-kuls-grants.sql").read_text())
        yield installed, migrator
    finally:
        migrator.dispose()


@pytest.fixture
def head18(head17):
    url, migrator = head17
    with migrator.connect() as db:
        command.upgrade(migration_config(db), "0018_clock_evidence")
    return url, migrator


def apply(url):
    with connect(url) as db:
        db.execute(GRANTS)


def old_capabilities(url):
    with connect(url) as db:
        return db.execute(
            "SELECT relname,relacl::text FROM pg_class WHERE oid IN "
            "('public.analog_history_raw'::regclass,"
            "'public.downtime_reason_history'::regclass,"
            "'public.guest_viewer_accounts'::regclass) ORDER BY relname"
        ).fetchall()


async def test_genuine_runtime_intake_reads_probe_dml_and_evidence_denials(head18):
    url, _ = head18
    before = old_capabilities(url)
    apply(url)
    apply(url)
    assert old_capabilities(url) == before
    password = secrets.token_urlsafe(32)
    with connect(url) as db:
        db.execute(sql.SQL("ALTER ROLE hmi_app LOGIN PASSWORD {}").format(sql.Literal(password)))
    runtime = create_database_engine(url.set(username="hmi_app", password=password))
    try:
        with runtime.connect() as db:
            assert db.execute(sa.text("SELECT current_user")).scalar_one() == "hmi_app"
        service = IngestionService(runtime)
        service.configure_source(**SCOPE, gateway_id="gateway", capture_generation=1, now=AT)
        token = secrets.token_urlsafe(32)
        credential = ServiceCredential(
            "clock-grants",
            "gateway",
            "source",
            "stream",
            hashlib.sha256(token.encode()).digest(),
            datetime.now(UTC) + timedelta(days=1),
        )
        application = create_internal_app(
            service,
            credential_bindings=[credential],
            config=InternalConfig(allow_insecure_loopback=True),
        )
        value = batch()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://127.0.0.1"
        ) as client:
            first = await client.post(
                "/internal/v1/batches",
                json=value.model_dump(mode="json"),
                headers={"Authorization": f"Bearer {token}"},
            )
            assert first.status_code == 200, first.text
            repeat = await client.post(
                "/internal/v1/batches",
                json=value.model_dump(mode="json"),
                headers={"Authorization": f"Bearer {token}"},
            )
            assert repeat.status_code == 200 and repeat.json() == first.json()
        with runtime.begin() as db:
            row = db.execute(sa.text("SELECT method,status FROM capture_clock_evidence")).one()
            assert tuple(row) == ("NONE", "UNKNOWN")
            assert (
                db.execute(sa.text("SELECT count(*) FROM capture_clock_evidence")).scalar_one() == 1
            )
            # Probe lifecycle privileges are exercised without enabling measured clock acceptance.
            db.execute(
                sa.text("""INSERT INTO capture_clock_probes
                (nonce_sha256,credential_key_id,gateway_id,source_id,stream_id,capture_generation,
                 clock_epoch,api_received_at,expires_at,gateway_send_utc,gateway_send_monotonic)
                VALUES (:nonce,'clock-grants','gateway','source','stream',1,'clock',:at,
                        :expires,:at,1)"""),
                {"nonce": "a" * 64, "at": AT, "expires": AT + timedelta(seconds=30)},
            )
            assert (
                db.execute(sa.text("SELECT count(*) FROM capture_clock_probes")).scalar_one() == 1
            )
            db.execute(
                sa.text(
                    "UPDATE capture_clock_probes SET consumed_batch_id='batch', "
                    "consumed_input_sha256=:sha, consumed_at=:at"
                ),
                {"sha": "b" * 64, "at": AT},
            )
            db.execute(sa.text("DELETE FROM capture_clock_probes"))
            assert (
                db.execute(sa.text("SELECT count(*) FROM capture_clock_probes")).scalar_one() == 0
            )
            # New canonical column is covered by the pre-existing raw table grant.
            db.execute(sa.text("UPDATE analog_history_raw SET canonical_utc=canonical_utc"))
        for statement in (
            "UPDATE capture_clock_evidence SET reason_code='changed'",
            "DELETE FROM capture_clock_evidence",
            "TRUNCATE capture_clock_evidence",
            "TRUNCATE capture_clock_probes",
            "UPDATE capture_clock_probes SET expires_at=expires_at",
            "CREATE TABLE public.clock_runtime_ddl(id int)",
            "ALTER TABLE capture_clock_evidence ADD COLUMN tamper int",
        ):
            denied(runtime, statement)
        with runtime.connect() as db:
            assert (
                db.execute(sa.text("SELECT count(*) FROM capture_clock_evidence")).scalar_one() == 1
            )
            assert not db.execute(
                sa.text("""SELECT EXISTS (
                SELECT 1 FROM pg_class c, LATERAL aclexplode(COALESCE(c.relacl,
                    acldefault('r',c.relowner))) a
                WHERE c.oid IN ('capture_clock_evidence'::regclass,'capture_clock_probes'::regclass)
                    AND a.grantee=0)""")
            ).scalar_one()
            assert not db.execute(
                sa.text("""SELECT EXISTS (
                SELECT 1 FROM pg_proc p, LATERAL aclexplode(COALESCE(p.proacl,
                    acldefault('f',p.proowner))) a
                WHERE p.oid='immutable_capture_clock_evidence()'::regprocedure
                    AND a.grantee=0)""")
            ).scalar_one()
    finally:
        runtime.dispose()


def test_wrong_head_and_database_refuse_without_changes(head17, postgres_url):
    url, _ = head17
    before = old_capabilities(url)
    for target in (url, postgres_url):
        with connect(target) as db:
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(GRANTS)
    assert old_capabilities(url) == before
    with connect(url) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0017_guest_viewer",
        )


def test_wrong_owner_and_temp_catalog_spoof_cannot_bypass_preflight(head18):
    url, _ = head18
    for mutation, undo in (
        (
            "ALTER TABLE capture_clock_probes OWNER TO postgres",
            "ALTER TABLE capture_clock_probes OWNER TO hmi_migrator",
        ),
        ("ALTER ROLE hmi_app CREATEDB", "ALTER ROLE hmi_app NOCREATEDB"),
    ):
        with connect(url) as db:
            db.execute(mutation)
        try:
            with connect(url) as db:
                # Deliberate pg_temp shadow objects must not hide the real unsafe role/owner.
                db.execute(
                    "CREATE TEMP TABLE pg_roles AS SELECT * FROM pg_catalog.pg_roles WHERE false"
                )
                db.execute(
                    "CREATE TEMP TABLE pg_class AS SELECT * FROM pg_catalog.pg_class WHERE false"
                )
                db.commit()
                with pytest.raises(psycopg.errors.RaiseException):
                    db.execute(GRANTS)
            with connect(url) as db:
                for table in ("capture_clock_evidence", "capture_clock_probes"):
                    assert not db.execute(
                        "SELECT has_table_privilege('hmi_app',%s,'SELECT')", ("public." + table,)
                    ).fetchone()[0]
        finally:
            with connect(url) as db:
                db.execute(undo)
