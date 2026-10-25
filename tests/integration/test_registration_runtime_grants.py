"""Canonical bootstrap -> head19 -> explicit grants under the actual hmi_app login."""

import secrets
from pathlib import Path

import httpx
import psycopg
import pytest
from alembic import command
from psycopg import sql
from sqlalchemy import text

from apps.api import serverless
from apps.api.auth import UserAuth
from apps.api.bootstrap import bootstrap_reference_data
from apps.api.database import create_database_engine, migration_config
from apps.api.ingestion import IngestionService
from apps.api.user_http import UserHTTPConfig, create_user_app
from deploy.generate_neon_init import connect
from tests.integration.test_clock_runtime_grants import head17 as head17
from tests.integration.test_clock_runtime_grants import head18 as head18
from tests.integration.test_kuls_runtime_grants import AT, SCOPE, batch, denied
from tests.integration.test_kuls_runtime_grants import installed as installed
from tests.integration.test_registration_ids_http import automatic, receipt_route, route
from tests.integration.test_work_orders_http import ORIGIN, account, protection

ROOT = Path(__file__).resolve().parents[2]
GRANTS = (ROOT / "deploy/neon-registration-grants.sql").read_text()


@pytest.fixture
def head19(head18):
    url, migrator = head18
    with connect(url) as db:
        db.execute((ROOT / "deploy/neon-clock-grants.sql").read_text())
    with migrator.connect() as db:
        command.upgrade(migration_config(db), "0019_registration_ids")
    return url, migrator


async def test_actual_runtime_registers_reads_receipts_and_has_minimal_grants(head19):
    url, migrator = head19
    with connect(url) as db:
        db.execute(GRANTS)
        db.execute(GRANTS)
    bootstrap_reference_data(migrator)
    offline_auth = UserAuth(migrator, allowed_origins={ORIGIN})
    session = account(offline_auth, "production_manager")
    password = secrets.token_urlsafe(32)
    with connect(url) as db:
        db.execute(sql.SQL("ALTER ROLE hmi_app LOGIN PASSWORD {}").format(sql.Literal(password)))
    runtime = create_database_engine(url.set(username="hmi_app", password=password))
    try:
        with runtime.connect() as db:
            assert db.execute(text("SELECT current_user")).scalar_one() == "hmi_app"
        config = serverless.VercelConfig(
            ORIGIN, "postgresql://hmi_app:fixture@neon.example.test/hmi_mes_demo?sslmode=require"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=serverless.create_vercel_app(config, engine=runtime)),
            base_url=ORIGIN,
        ) as ready_client:
            ready = await ready_client.get("/api/ready")
            assert ready.status_code == 200 and ready.json() == {"ready": True}
        service = IngestionService(runtime)
        service.configure_source(**SCOPE, gateway_id="gateway", capture_generation=1, now=AT)
        service.ingest(
            batch(),
            authenticated_gateway_id="gateway",
            authenticated_source_id="source",
            authenticated_stream_id="stream",
            now=AT,
        )
        with runtime.connect() as db:
            # Head19 preserves the head18 evidence path instead of using legacy rules.
            assert db.execute(text("SELECT count(*) FROM capture_clock_evidence")).scalar_one() == 1
        auth = UserAuth(runtime, allowed_origins={ORIGIN})
        app = create_user_app(auth, UserHTTPConfig(origin=ORIGIN))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as client:
            client.cookies.set("hmi_session", session.token)
            recipe = await client.post(
                route("recipe"), json=automatic("recipe"), headers=protection(session)
            )
            assert recipe.status_code == 200, recipe.text
            for request_id in ("order-one", "order-two"):
                body = automatic("work_order", request_id=request_id)
                response = await client.post(
                    route("work_order"), json=body, headers=protection(session)
                )
                assert response.status_code == 200, response.text
                recovered = await client.get(
                    receipt_route("work_order"), params={"request_id": request_id}
                )
                assert recovered.status_code == 200 and recovered.json() == response.json()
                replay = await client.post(
                    route("work_order"), json=body, headers=protection(session)
                )
                assert replay.status_code == 200 and replay.json() == response.json()
        with runtime.begin() as db:
            assert (
                db.execute(text("SELECT count(*) FROM registration_id_counters")).scalar_one() == 2
            )
            assert db.execute(text("SELECT count(*) FROM registration_receipts")).scalar_one() == 3
            db.execute(text("UPDATE registration_id_counters SET last_value=last_value"))
        for statement in (
            "UPDATE registration_receipts SET actor=actor",
            "DELETE FROM registration_receipts",
            "TRUNCATE registration_receipts",
            "DELETE FROM registration_id_counters",
            "TRUNCATE registration_id_counters",
            "CREATE TABLE public.registration_runtime_ddl(id int)",
            "ALTER TABLE registration_receipts ADD COLUMN tamper int",
        ):
            # denied opens and rolls back a fresh transaction for every forbidden statement.
            denied(runtime, statement)
        with runtime.connect() as db:
            assert db.execute(text("SELECT count(*) FROM registration_receipts")).scalar_one() == 3
            for table in ("registration_receipts", "registration_id_counters"):
                assert not db.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_class c, LATERAL aclexplode("
                        "COALESCE(c.relacl,acldefault('r',c.relowner))) a "
                        "WHERE c.oid=to_regclass(:table) AND a.grantee=0)"
                    ),
                    {"table": "public." + table},
                ).scalar_one()
    finally:
        runtime.dispose()


def test_wrong_head_rejects_registration_grants_without_mutation(head18):
    url, _ = head18
    with connect(url) as db:
        before = db.execute(
            "SELECT relname,relacl::text FROM pg_class "
            "WHERE relnamespace='public'::regnamespace ORDER BY relname"
        ).fetchall()
    with connect(url) as db:
        with pytest.raises(psycopg.errors.RaiseException):
            db.execute(GRANTS)
    with connect(url) as db:
        assert (
            db.execute(
                "SELECT relname,relacl::text FROM pg_class "
                "WHERE relnamespace='public'::regnamespace ORDER BY relname"
            ).fetchall()
            == before
        )
