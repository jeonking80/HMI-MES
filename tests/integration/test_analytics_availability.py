"""Real PostgreSQL schema, registration and read-only pre-login feature checks."""

import secrets

import httpx
import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import OperationalError

from apps.api.analytics_availability import REQUIRED_ROUTES
from apps.api.auth import UserAuth
from apps.api.database import migration_config
from apps.api.user_http import UserHTTPConfig, create_user_app

ORIGIN = "https://analytics.example.test"


def application(engine):
    return create_user_app(
        UserAuth(engine, allowed_origins={ORIGIN}), UserHTTPConfig(origin=ORIGIN)
    )


async def config(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.get("/auth/config")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert "set-cookie" not in response.headers
        return response.json()


@pytest.mark.parametrize(
    "revision,expected",
    [
        ("0014_mixed_alarm_runtime", False),
        ("0015_analog_history", False),
        ("0016_downtime_reasons", True),
        ("0017_guest_viewer", True),
        ("0018_clock_evidence", True),
    ],
)
async def test_prelogin_supported_revision_and_existing_guest_flag(engine, revision, expected):
    with engine.connect() as db:
        command.upgrade(migration_config(db), revision)
    app = application(engine)
    assert await config(app) == {"guest_viewer": False, "assistant": False, "analytics": expected}
    with engine.connect() as db:
        assert db.execute(sa.text("SELECT count(*) FROM users")).scalar_one() == 0
        assert db.execute(sa.text("SELECT count(*) FROM auth_sessions")).scalar_one() == 0
        assert db.execute(sa.text("SELECT count(*) FROM auth_audit")).scalar_one() == 0


@pytest.mark.parametrize("missing", sorted(REQUIRED_ROUTES))
async def test_each_required_route_and_method_must_be_registered(migrated, missing):
    app = application(migrated)
    assert (await config(app))["analytics"] is True
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (
            getattr(route, "path", None) == missing[0]
            and missing[1] in (getattr(route, "methods", None) or ())
        )
    ]
    assert await config(app) == {"guest_viewer": False, "assistant": False, "analytics": False}


async def test_incomplete_registration_marker_and_database_errors_fail_closed(
    migrated, monkeypatch
):
    app = application(migrated)
    app.state.analytics_routes_registered = False
    assert (await config(app))["analytics"] is False
    app.state.analytics_routes_registered = True
    secret = secrets.token_urlsafe(24)

    def unavailable(*args, **kwargs):
        raise OperationalError("secret database configuration " + secret, {}, Exception(secret))

    monkeypatch.setattr(migrated, "begin", unavailable)
    assert await config(app) == {"guest_viewer": False, "assistant": False, "analytics": False}


async def test_missing_schema_object_is_unavailable(migrated):
    app = application(migrated)
    with migrated.begin() as db:
        db.execute(sa.text("DROP TABLE analog_history_clock_state"))
    assert (await config(app))["analytics"] is False


async def test_read_only_runtime_can_advertise_available_and_missing_grant_cannot(migrated):
    app = application(migrated)
    with migrated.begin() as db:
        db.execute(
            sa.text(
                "CREATE ROLE analytics_reader NOLOGIN NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            )
        )
        db.execute(sa.text("GRANT USAGE ON SCHEMA public TO analytics_reader"))
        db.execute(sa.text("GRANT SELECT ON ALL TABLES IN SCHEMA public TO analytics_reader"))
    # Use a fresh pool whose effective DB user has SELECT privileges only.
    reader = sa.create_engine(
        migrated.url,
        hide_parameters=True,
        echo=False,
        connect_args={"options": "-c role=analytics_reader"},
    )
    try:
        readonly_app = application(reader)
        assert (await config(readonly_app))["analytics"] is True
        with migrated.begin() as db:
            db.execute(sa.text("REVOKE SELECT ON capture_clock_evidence FROM analytics_reader"))
        assert (await config(readonly_app))["analytics"] is False
        with migrated.begin() as db:
            db.execute(sa.text("GRANT SELECT ON capture_clock_evidence TO analytics_reader"))
        assert (await config(readonly_app))["analytics"] is True
        with reader.connect() as db:
            assert db.execute(sa.text("SELECT current_user")).scalar_one() == "analytics_reader"
            assert not db.execute(
                sa.text(
                    "SELECT has_table_privilege(current_user,'downtime_reason_history','INSERT')"
                )
            ).scalar_one()
        with migrated.begin() as db:
            db.execute(sa.text("REVOKE SELECT ON downtime_reason_history FROM analytics_reader"))
        assert (await config(readonly_app))["analytics"] is False
        assert (await config(app))["analytics"] is True
    finally:
        reader.dispose()
        with migrated.begin() as db:
            db.execute(sa.text("DROP OWNED BY analytics_reader"))
            db.execute(sa.text("DROP ROLE analytics_reader"))
