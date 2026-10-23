"""Real PostgreSQL guest admission, persistence and authorization boundaries."""

import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa

from apps.api.auth import AuthenticationError, AuthorizationError, UserAuth
from apps.api.guest import GuestThrottled, bootstrap_guest, issue_guest
from apps.api.user_http import UserHTTPConfig, create_user_app

ORIGIN = "https://hmi.example.test"


@pytest.fixture
def guest(migrated):
    now = [datetime(2026, 10, 23, tzinfo=UTC)]
    auth = UserAuth(migrated, allowed_origins={ORIGIN}, clock=lambda: now[0], guest_viewer=True)
    bootstrap_guest(auth)
    return auth, now


def app(auth, enabled=True):
    return create_user_app(auth, UserHTTPConfig(origin=ORIGIN, guest_viewer=enabled))


async def test_guest_http_validation_session_and_disabled(guest):
    auth, now = guest
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(auth)), base_url=ORIGIN
    ) as client:
        assert (await client.get("/auth/config")).json() == {"guest_viewer": True}
        assert (await client.post("/auth/guest", json={})).status_code == 403
        headers = {"Origin": ORIGIN}
        for body in ({"role": "operator"}, [], None):
            assert (await client.post("/auth/guest", json=body, headers=headers)).status_code in {
                415,
                422,
            }
        assert (await client.post("/auth/guest", content="{}", headers=headers)).status_code == 415
        assert (
            await client.post(
                "/auth/guest", content="{", headers={**headers, "Content-Type": "application/json"}
            )
        ).status_code == 422
        response = await client.post("/auth/guest", json={}, headers=headers)
        assert response.status_code == 200
        assert response.json()["roles"] == ["viewer"]
        assert response.json()["must_change_password"] is False
        assert "Max-Age=1800" in response.headers["set-cookie"]
        csrf = response.json()["csrf_token"]
        assert (await client.get("/auth/me")).status_code == 200
        assert (
            await client.post(
                "/auth/password",
                json={"new_password": secrets.token_urlsafe(24)},
                headers={**headers, "X-CSRF-Token": csrf},
            )
        ).status_code == 403
        now[0] += timedelta(minutes=29)
        assert (await client.get("/auth/me")).status_code == 200
        now[0] += timedelta(minutes=1)
        assert (await client.get("/auth/me")).status_code == 401
    disabled = UserAuth(auth.engine, allowed_origins={ORIGIN})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(disabled, False)), base_url=ORIGIN
    ) as client:
        assert (await client.get("/auth/config")).json() == {"guest_viewer": False}
        assert (await client.post("/auth/guest", json={}, headers=headers)).status_code == 404


def test_guest_password_role_and_all_write_permissions(guest):
    auth, _ = guest
    session = issue_guest(auth, origin=ORIGIN, peer="192.0.2.1")
    from apps.api.auth import PERMISSIONS

    for permission in PERMISSIONS.keys() - {"read"}:
        with pytest.raises(AuthorizationError):
            with auth.authorized_transaction(
                session.token, permission=permission, csrf_token=session.csrf_token, origin=ORIGIN
            ):
                pytest.fail("guest write authorized")
    with auth.authorized_transaction(session.token, permission="read") as tx:
        assert tx.principal.roles == frozenset({"viewer"})
    challenge = auth.begin_login()
    with pytest.raises(AuthenticationError):
        auth.login(
            "guest-viewer",
            secrets.token_urlsafe(24),
            challenge_token=challenge.token,
            csrf_token=challenge.csrf_token,
            origin=ORIGIN,
        )
    disabled = UserAuth(auth.engine, allowed_origins={ORIGIN})
    with pytest.raises(AuthenticationError):
        disabled.authenticate(session.token)
    with auth.engine.begin() as db:
        assert (
            db.execute(
                sa.text("SELECT count(*) FROM auth_audit WHERE action='GUEST' AND actor='GUEST'")
            ).scalar_one()
            == 1
        )
        db.execute(sa.text("INSERT INTO user_roles VALUES ('guest-viewer','operator')"))
    with pytest.raises(ValueError):
        app(auth)
    with pytest.raises(AuthenticationError):
        auth.authenticate(session.token)


def test_guest_distributed_ip_bounds_and_concurrent_global_cap(guest):
    auth, now = guest
    other = UserAuth(auth.engine, allowed_origins={ORIGIN}, clock=lambda: now[0], guest_viewer=True)
    for i in range(5):
        issue_guest(auth if i % 2 else other, origin=ORIGIN, peer="192.0.2.9")
    with pytest.raises(GuestThrottled):
        issue_guest(other, origin=ORIGIN, peer="192.0.2.9")

    def admit(i):
        try:
            issue_guest(auth if i % 2 else other, origin=ORIGIN, peer=f"198.51.100.{i + 1}")
            return True
        except GuestThrottled:
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        assert sum(pool.map(admit, range(60))) == 45
    with auth.engine.connect() as db:
        assert db.execute(sa.text("SELECT count(*) FROM auth_sessions")).scalar_one() == 50
    now[0] += timedelta(minutes=30)
    assert issue_guest(other, origin=ORIGIN, peer="192.0.2.9").principal.login == "guest-viewer"


def test_guest_requires_explicit_provisioning_and_rejects_repurpose(migrated):
    auth = UserAuth(migrated, allowed_origins={ORIGIN}, guest_viewer=True)
    with pytest.raises(ValueError):
        app(auth)
    bootstrap_guest(auth)
    with pytest.raises(ValueError):
        bootstrap_guest(auth)
    with pytest.raises(ValueError):
        auth.bootstrap_user("guest-viewer", secrets.token_urlsafe(24), roles={"viewer"})


async def test_guest_actual_business_writes_are_forbidden(guest):
    from uuid import uuid4

    from apps.api.downtime_http import register_downtime_routes
    from contracts.models import CommandRequest

    auth, now = guest
    application = app(auth)
    if not any(route.path == "/api/v1/downtime-reasons" for route in application.routes):
        register_downtime_routes(application, auth)
    session = issue_guest(auth, origin=ORIGIN, peer="192.0.2.8")
    scope = {"line_id": "line", "source_id": "source", "stream_id": "stream"}
    command = CommandRequest(
        command_id="guest-command",
        target_id="Line",
        execution_id="execution",
        command={"kind": "START", "args": {}},
        requested_by="guest-viewer",
        requested_at=now[0],
        expires_at=now[0] + timedelta(seconds=5),
        expected_control_version=1,
    ).model_dump(mode="json")
    writes = [
        ("/api/v1/commands", {**scope, "request": command}),
        (
            "/api/v1/work-orders",
            {
                "id": "order",
                "item_id": "item",
                "line_id": "line",
                "recipe_version_id": "recipe",
                "target_quantity": 10,
                "planned_date": "2026-10-23",
            },
        ),
        (
            "/api/v1/alarms/alarm/actions",
            {
                "action_id": "guest-action",
                "scope": {**scope, "asset_id": "Infeed"},
                "rule_id": "pressure",
                "alarm_id": "alarm",
                "actor": "guest-viewer",
                "kind": "ACKNOWLEDGE",
            },
        ),
        (
            "/api/v1/downtime-reasons",
            {
                "request_id": str(uuid4()),
                "execution_id": "execution",
                **scope,
                "segment_from": now[0].isoformat(),
                "reason_code": "EQUIPMENT",
                "note": "test",
            },
        ),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url=ORIGIN
    ) as client:
        client.cookies.set("hmi_session", session.token)
        for path, body in writes:
            response = await client.post(
                path, json=body, headers={"Origin": ORIGIN, "X-CSRF-Token": session.csrf_token}
            )
            assert response.status_code == 403, (path, response.text)
