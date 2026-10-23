"""Disposable canonical14 -> migrator17 -> explicit grants -> runtime capabilities.

No deployment DSN is accepted. SET ROLE on fresh test-engine connections makes
all business SQL execute with hmi_app's effective privileges, without changing
production-style NOLOGIN role attributes or granting extra memberships.
"""

import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import DBAPIError

from apps.api.auth import UserAuth
from apps.api.bootstrap import LINE_ID
from apps.api.database import create_database_engine, migration_config
from apps.api.guest import bootstrap_guest, issue_guest
from apps.api.ingestion import IngestionService
from apps.api.user_http import UserHTTPConfig, create_user_app
from contracts.transport import BatchBody, GatewayBatch, batch_digest
from deploy.generate_neon_init import connect

ROOT = Path(__file__).resolve().parents[2]
GRANTS = (ROOT / "deploy/neon-kuls-grants.sql").read_text()
ORIGIN = "https://kuls.example.test"
AT = datetime(2026, 10, 23, 12, tzinfo=UTC)
SCOPE = dict(line_id=LINE_ID, source_id="source", stream_id="stream")
TABLES = (
    "analog_history_raw",
    "analog_history_minutes",
    "analog_history_clock_state",
    "downtime_reason_history",
    "guest_viewer_accounts",
    "guest_rate_windows",
)


@pytest.fixture
def installed(postgres_url):
    # postgres_url is created only by the pinned disposable Docker fixture.
    with connect(postgres_url, autocommit=True) as db:
        db.execute("CREATE DATABASE hmi_mes_demo")
    url = postgres_url.set(database="hmi_mes_demo")
    try:
        with connect(url) as db:
            db.execute((ROOT / "deploy/neon-init.sql").read_text())
        yield url
    finally:
        with connect(postgres_url, autocommit=True) as db:
            db.execute("DROP DATABASE hmi_mes_demo WITH (FORCE)")
            db.execute("DROP ROLE hmi_app")
            db.execute("DROP ROLE hmi_migrator")


def role_engine(url, role):
    engine = create_database_engine(url)

    @sa.event.listens_for(engine, "connect")
    def set_role(dbapi, record):
        dbapi.autocommit = True
        try:
            with dbapi.cursor() as cursor:
                # Role is a fixed test constant, never request data.
                cursor.execute("SET ROLE " + role)
        finally:
            dbapi.autocommit = False

    return engine


@pytest.fixture
def upgraded(installed):
    migrator = role_engine(installed, "hmi_migrator")
    try:
        with migrator.connect() as db:
            assert db.execute(sa.text("SELECT current_user")).scalar_one() == "hmi_migrator"
            db.commit()
            command.upgrade(migration_config(db), "0017_guest_viewer")
        yield installed, migrator
    finally:
        migrator.dispose()


def apply(url):
    with connect(url) as db:
        db.execute(GRANTS)


def denied(engine, statement):
    with pytest.raises(DBAPIError) as error, engine.begin() as db:
        db.execute(sa.text(statement))
    assert error.value.orig.sqlstate == "42501"


def account(auth):
    password = secrets.token_urlsafe(24)
    auth.bootstrap_user("operator", password, roles={"operator"})
    challenge = auth.begin_login()
    session = auth.login(
        "operator",
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=ORIGIN,
    )
    return auth.change_password(
        session.token, secrets.token_urlsafe(24), csrf_token=session.csrf_token, origin=ORIGIN
    )


def batch():
    body = BatchBody(
        schema_version="1.2",
        batch_id="batch",
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        capture_generation=1,
        capture_seq=1,
        captured_at=AT,
        capture_time=dict(clock_epoch="clock", monotonic_seconds=1.0, utc_read_span_seconds=0.0),
        communication=dict(
            state="GOOD",
            connected=True,
            read_age_seconds=0.0,
            heartbeat_ages_seconds={
                key: 0.0
                for key in (
                    "Line.Heartbeat",
                    "Infeed.LinkHeartbeat",
                    "Sealer.LinkHeartbeat",
                    "Outfeed.LinkHeartbeat",
                )
            },
        ),
        samples=[
            dict(
                sample_id="sample",
                tag_id="Sealer.TemperaturePV",
                source_timestamp=AT,
                server_timestamp=None,
                acquired_at=AT,
                status_code=0,
                reading=dict(data_type="Double", value=150.0),
            )
        ],
    )
    return GatewayBatch(body=body, content_sha256=batch_digest(body))


async def test_runtime_capture_guest_trend_and_annotation_permissions(upgraded, monkeypatch):
    url, migrator = upgraded
    runtime = role_engine(url, "hmi_app")
    try:
        denied(runtime, "SELECT * FROM public.guest_viewer_accounts")
        apply(url)
        apply(url)  # Safe repeat preserves the same capability surface.
        offline = UserAuth(migrator, allowed_origins={ORIGIN}, clock=lambda: AT)
        bootstrap_guest(offline)
        operator = account(offline)
        auth = UserAuth(runtime, allowed_origins={ORIGIN}, clock=lambda: AT, guest_viewer=True)
        service = IngestionService(runtime)
        service.configure_source(**SCOPE, gateway_id="gateway", capture_generation=1, now=AT)
        service.ingest(
            batch(),
            authenticated_gateway_id="gateway",
            authenticated_source_id="source",
            authenticated_stream_id="stream",
            now=AT + timedelta(minutes=2),
        )
        with runtime.connect() as db:
            assert db.execute(sa.text("SELECT current_user")).scalar_one() == "hmi_app"
            assert db.execute(sa.text("SELECT count(*) FROM analog_history_raw")).scalar_one() == 1
            assert (
                db.execute(sa.text("SELECT count(*) FROM analog_history_clock_state")).scalar_one()
                == 1
            )
        session = issue_guest(auth, origin=ORIGIN, peer="192.0.2.5")
        application = create_user_app(auth, UserHTTPConfig(origin=ORIGIN, guest_viewer=True))
        # Fixed STOPPED projection isolates annotation privileges from timeline calculations,
        # which are tested separately against real equipment event evidence.
        monkeypatch.setattr(
            "apps.api.reports.read_execution_timeline",
            lambda *args, **kwargs: {
                "segments": [
                    {
                        "from": AT.isoformat(),
                        "to": (AT + timedelta(seconds=30)).isoformat(),
                        "class": "STOPPED",
                    }
                ]
            },
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url=ORIGIN
        ) as client:
            client.cookies.set("hmi_session", session.token)
            response = await client.get(
                "/api/v1/trends",
                params={
                    **SCOPE,
                    "tag": "Sealer.TemperaturePV",
                    "from": AT.isoformat(),
                    "to": (AT + timedelta(minutes=1)).isoformat(),
                    "resolution": "minute",
                },
            )
            assert response.status_code == 200, response.text
            definition = dict(
                request_id=str(uuid4()),
                execution_id="run",
                **SCOPE,
                segment_from=AT.isoformat(),
                reason_code="EQUIPMENT",
                note="test",
            )
            assert (
                await client.post(
                    "/api/v1/downtime-reasons",
                    json=definition,
                    headers={"Origin": ORIGIN, "X-CSRF-Token": session.csrf_token},
                )
            ).status_code == 403
            client.cookies.set("hmi_session", operator.token)
            response = await client.post(
                "/api/v1/downtime-reasons",
                json=definition,
                headers={"Origin": ORIGIN, "X-CSRF-Token": operator.csrf_token},
            )
            assert response.status_code == 200, response.text
        for table in TABLES:
            denied(runtime, f"TRUNCATE public.{table}")
        for table in ("downtime_reason_history", "guest_viewer_accounts"):
            denied(runtime, f"DELETE FROM public.{table}")
        denied(runtime, "UPDATE public.downtime_reason_history SET note='changed'")
        denied(runtime, "UPDATE public.guest_viewer_accounts SET provisioned_at=now()")
        denied(runtime, "INSERT INTO public.guest_viewer_accounts VALUES ('guest-viewer',now())")
        denied(runtime, "DELETE FROM public.analog_history_clock_state")
        denied(runtime, "SELECT setval('public.downtime_reason_history_id_seq',100)")
        denied(runtime, "CREATE TABLE public.runtime_ddl_denied(id int)")
        with runtime.connect() as db:
            assert (
                db.execute(sa.text("SELECT count(*) FROM downtime_reason_history")).scalar_one()
                == 1
            )
            for table in TABLES:
                assert not db.execute(
                    sa.text(
                        "SELECT EXISTS (SELECT 1 FROM pg_class c, "
                        "LATERAL aclexplode(COALESCE(c.relacl, "
                        "acldefault('r',c.relowner))) a "
                        "WHERE c.oid=to_regclass(:table) AND a.grantee=0)"
                    ),
                    {"table": "public." + table},
                ).scalar_one()
    finally:
        runtime.dispose()


def test_bad_database_and_head_reject_without_grants(installed, postgres_url):
    with connect(postgres_url) as db:
        with pytest.raises(psycopg.errors.RaiseException):
            db.execute(GRANTS)
    with connect(installed) as db:
        with pytest.raises(psycopg.errors.RaiseException):
            db.execute(GRANTS)
    with connect(installed) as db:
        assert db.execute("SELECT version_num FROM public.alembic_version").fetchone() == (
            "0014_mixed_alarm_runtime",
        )
        assert db.execute("SELECT to_regclass('public.guest_rate_windows')").fetchone() == (None,)
        assert not db.execute(
            "SELECT has_schema_privilege('hmi_app','public','CREATE')"
        ).fetchone()[0]


def test_wrong_owner_and_privileged_runtime_reject_transactionally(upgraded):
    url, _ = upgraded
    for mutation, undo in (
        (
            "ALTER TABLE public.guest_rate_windows OWNER TO postgres",
            "ALTER TABLE public.guest_rate_windows OWNER TO hmi_migrator",
        ),
        ("ALTER ROLE hmi_app CREATEDB", "ALTER ROLE hmi_app NOCREATEDB"),
    ):
        with connect(url) as db:
            db.execute(mutation)
        try:
            with pytest.raises(psycopg.errors.RaiseException):
                apply(url)
            with connect(url) as db:
                for table in TABLES:
                    assert not db.execute(
                        "SELECT has_table_privilege('hmi_app',%s,'SELECT')", ("public." + table,)
                    ).fetchone()[0]
        finally:
            with connect(url) as db:
                db.execute(undo)
