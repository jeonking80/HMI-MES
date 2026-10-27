"""Real PostgreSQL runtime on/off request, equipment-admin boundary and agent report."""

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

from apps.api.auth import UserAuth
from apps.api.bootstrap import bootstrap_reference_data
from apps.api.runtime_control import RUN_LIMIT, agent_exchange, view
from apps.api.user_http import UserHTTPConfig, create_user_app
from tests.integration.test_work_orders import AT
from tests.integration.test_work_orders_http import ORIGIN, account, protection

SQL = Path(__file__).resolve().parents[2] / "deploy/runtime-control.sql"


def install(engine):
    with engine.begin() as db:
        for role in ("hmi_migrator", "hmi_app"):
            if not db.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:r"), {"r": role}).first():
                db.execute(text(f"CREATE ROLE {role} NOLOGIN"))
        db.exec_driver_sql(SQL.read_text())


@pytest.fixture
def state(migrated):
    bootstrap_reference_data(migrated)
    clock = SimpleNamespace(now=AT)
    auth = UserAuth(migrated, allowed_origins={ORIGIN}, clock=lambda: clock.now)
    yield SimpleNamespace(
        engine=migrated,
        clock=clock,
        admin=account(auth, "equipment_admin"),
        manager=account(auth, "production_manager"),
        app=create_user_app(auth, UserHTTPConfig(origin=ORIGIN)),
    )
    with migrated.begin() as db:
        db.execute(text("DROP SCHEMA IF EXISTS runtime_ops CASCADE"))


async def call(state, session, method, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=state.app), base_url=ORIGIN
    ) as client:
        client.cookies.set("hmi_session", session.token)
        return await client.request(method, "/api/v1/runtime-control", **kwargs)


async def test_missing_table_reports_not_installed(state):
    assert (await call(state, state.admin, "GET")).status_code == 404


async def test_admin_only_bounded_start_stop_and_agent_report(state):
    install(state.engine)
    first = await call(state, state.manager, "GET")
    assert first.status_code == 200, first.text
    assert first.json()["desired"] == "OFF" and first.json()["reported_state"] == "UNKNOWN"

    start = {"json": {"action": "start"}}
    denied = await call(state, state.manager, "POST", **start, headers=protection(state.manager))
    assert denied.status_code == 403
    unprotected = await call(state, state.admin, "POST", **start)
    assert unprotected.status_code == 403
    bad = await call(
        state, state.admin, "POST", json={"action": "reboot"}, headers=protection(state.admin)
    )
    assert bad.status_code == 422

    started = await call(state, state.admin, "POST", **start, headers=protection(state.admin))
    assert started.status_code == 200, started.text
    assert started.json()["desired"] == "ON"
    assert started.json()["run_until"] == (AT + RUN_LIMIT).isoformat()
    assert started.json()["requested_by"] == "equipment_admin"

    with state.engine.begin() as db:
        assert agent_exchange(db, state="RUNNING", detail=None, now=AT)["desired"] == "ON"
        assert view(db, AT + timedelta(seconds=5))["reported_state"] == "RUNNING"
        # A silent agent is not reported as running.
        assert view(db, AT + timedelta(minutes=5))["reported_state"] == "UNKNOWN"
        # The request expires by itself; the agent is then told to stop.
        later = AT + RUN_LIMIT + timedelta(seconds=1)
        assert agent_exchange(db, state="RUNNING", detail=None, now=later)["desired"] == "OFF"
        with pytest.raises(ValueError):
            agent_exchange(db, state="FLYING", detail=None, now=later)

    stopped = await call(
        state, state.admin, "POST", json={"action": "stop"}, headers=protection(state.admin)
    )
    assert stopped.status_code == 200 and stopped.json()["desired"] == "OFF"
    assert stopped.json()["run_until"] is None
