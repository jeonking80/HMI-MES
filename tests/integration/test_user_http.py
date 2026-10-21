"""Actual PostgreSQL and ASGI checks for the browser-only authentication boundary."""

import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa

from apps.api.auth import UserAuth
from apps.api.user_http import (
    PREAUTH_COOKIE,
    SESSION_COOKIE,
    UserHTTPConfig,
    create_user_app,
)

ORIGIN = "https://hmi.example.test"


@pytest.fixture
def state(migrated):
    clock = [datetime(2026, 10, 21, 12, tzinfo=UTC)]
    monotonic = [100.0]
    auth = UserAuth(migrated, allowed_origins={ORIGIN}, clock=lambda: clock[0])
    password = secrets.token_urlsafe(24)
    auth.bootstrap_user("person", password, roles={"operator"})
    config = UserHTTPConfig(origin=ORIGIN, monotonic=lambda: monotonic[0])
    return SimpleNamespace(
        auth=auth, password=password, config=config, clock=clock, monotonic=monotonic
    )


@pytest.fixture
def app(state):
    return create_user_app(state.auth, state.config)


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        yield client


async def challenge(client):
    response = await client.post(
        "/auth/challenge", headers={"Origin": ORIGIN, "X-HMI-Request": "1"}
    )
    assert response.status_code == 200
    return response.json()["csrf_token"]


async def login(client, password, login="person"):
    csrf = await challenge(client)
    return await client.post(
        "/auth/login",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        json={"login": login, "password": password},
    )


def protection(csrf):
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf}


def scalar(state, sql):
    with state.auth.engine.connect() as db:
        return db.execute(sa.text(sql)).scalar_one()


async def test_login_password_rotation_me_logout_and_no_secret_json(client, state, caplog):
    assert (await client.get("/auth/me")).status_code == 401
    csrf = await challenge(client)
    preauth = client.cookies.get(PREAUTH_COOKIE)
    response = await client.post(
        "/auth/login",
        headers=protection(csrf),
        json={"login": "PERSON", "password": state.password},
    )
    assert response.status_code == 200
    initial_token = client.cookies.get(SESSION_COOKIE)
    data = response.json()
    assert data["login"] == "person" and data["roles"] == ["operator"]
    assert data["must_change_password"] is True
    assert set(data) == {"login", "roles", "must_change_password", "csrf_token", "expires_at"}
    assert client.cookies.get(PREAUTH_COOKIE) is None
    for secret in (state.password, preauth, initial_token):
        assert secret not in response.text + caplog.text
    me = await client.get("/auth/me")
    assert me.status_code == 200 and me.json()["must_change_password"]
    csrf = data["csrf_token"]
    new_password = secrets.token_urlsafe(24)
    rotated = await client.post(
        "/auth/password", headers=protection(csrf), json={"new_password": new_password}
    )
    assert rotated.status_code == 200 and not rotated.json()["must_change_password"]
    assert client.cookies.get(SESSION_COOKIE) != initial_token
    for secret in (initial_token, client.cookies.get(SESSION_COOKIE), new_password):
        assert secret not in rotated.text + caplog.text
    old = await client.get("/auth/me", headers={"Cookie": f"{SESSION_COOKIE}={initial_token}"})
    assert old.status_code == 401
    csrf = rotated.json()["csrf_token"]
    current_token = client.cookies.get(SESSION_COOKIE)
    assert (await client.post("/auth/logout", headers=protection(csrf))).status_code == 200
    assert client.cookies.get(SESSION_COOKIE) is None
    assert (await client.get("/auth/me")).status_code == 401
    replay = await client.get("/auth/me", headers={"Cookie": f"{SESSION_COOKIE}={current_token}"})
    assert replay.status_code == 401
    assert (await login(client, state.password)).status_code == 401
    assert (await login(client, new_password)).status_code == 200


async def test_cookies_are_secure_httponly_strict_host_only(client, state):
    response = await client.post(
        "/auth/challenge", headers={"Origin": ORIGIN, "X-HMI-Request": "1"}
    )
    value = response.headers["set-cookie"].lower()
    assert all(
        part in value for part in ("httponly", "secure", "samesite=strict", "path=/", "max-age=300")
    )
    assert "domain=" not in value
    response = await login(client, state.password)
    cookie = next(
        value.lower()
        for value in response.headers.get_list("set-cookie")
        if value.startswith(SESSION_COOKIE + "=")
    )
    assert all(
        part in cookie for part in ("httponly", "secure", "samesite=strict", "max-age=28800")
    )
    assert "domain=" not in cookie
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


async def test_challenge_requires_exact_origin_and_explicit_header(client, state):
    for headers in (
        {},
        {"Origin": ORIGIN},
        {"Origin": ORIGIN, "X-HMI-Request": "0"},
        {"Origin": ORIGIN + "/", "X-HMI-Request": "1"},
        {"Origin": "https://hmi.example.test.evil", "X-HMI-Request": "1"},
    ):
        response = await client.post("/auth/challenge", headers=headers)
        assert response.status_code == 403
    assert scalar(state, "SELECT count(*) FROM auth_login_challenges") == 0


async def test_login_and_changes_reject_csrf_and_origin(client, state):
    csrf = await challenge(client)
    for headers in (
        {},
        {"Origin": ORIGIN},
        protection(secrets.token_urlsafe(32)),
        {"Origin": "null", "X-CSRF-Token": csrf},
    ):
        response = await client.post(
            "/auth/login", headers=headers, json={"login": "person", "password": state.password}
        )
        assert response.status_code == 403
    # New app has separate throttle state, while the actual DB challenge remains valid.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_user_app(state.auth, state.config)),
        base_url=ORIGIN,
    ) as fresh:
        logged_in = await login(fresh, state.password)
        csrf = logged_in.json()["csrf_token"]
        for route, body in (
            ("/auth/password", {"new_password": secrets.token_urlsafe(24)}),
            ("/auth/logout", {}),
        ):
            for headers in (
                {},
                {"Origin": ORIGIN},
                protection(secrets.token_urlsafe(32)),
                {"Origin": ORIGIN + "/", "X-CSRF-Token": csrf},
            ):
                response = await fresh.post(route, headers=headers, json=body)
                assert response.status_code == 403
        assert (await fresh.get("/auth/me")).status_code == 200


async def test_challenge_expiration_single_use_and_session_idle_expiry(client, state):
    csrf = await challenge(client)
    state.clock[0] += timedelta(minutes=5)
    assert (
        await client.post(
            "/auth/login",
            headers=protection(csrf),
            json={"login": "person", "password": state.password},
        )
    ).status_code == 401
    logged_in = await login(client, state.password)
    assert logged_in.status_code == 200
    # Successful login consumed and deleted the pre-authentication cookie.
    assert (
        await client.post(
            "/auth/login",
            headers=protection(csrf),
            json={"login": "person", "password": state.password},
        )
    ).status_code == 401
    state.clock[0] += timedelta(minutes=30)
    assert (await client.get("/auth/me")).status_code == 401


async def test_absolute_expiry_even_with_http_idle_refresh(client, state):
    assert (await login(client, state.password)).status_code == 200
    start = state.clock[0]
    for step in range(1, 17):
        state.clock[0] = start + timedelta(minutes=step * 29)
        assert (await client.get("/auth/me")).status_code == 200
    state.clock[0] = start + timedelta(hours=8)
    assert (await client.get("/auth/me")).status_code == 401


async def test_role_revocation_is_observed_by_next_http_request(client, state):
    password = secrets.token_urlsafe(24)
    state.auth.bootstrap_user("manager", password, roles={"equipment_admin"})
    challenge = state.auth.begin_login()
    admin = state.auth.login(
        "manager",
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=ORIGIN,
    )
    admin = state.auth.change_password(
        admin.token, secrets.token_urlsafe(24), csrf_token=admin.csrf_token, origin=ORIGIN
    )
    assert (await login(client, state.password)).status_code == 200
    state.auth.set_user_roles(
        admin.token, "person", {"viewer"}, csrf_token=admin.csrf_token, origin=ORIGIN
    )
    assert (await client.get("/auth/me")).status_code == 401
    assert (await login(client, state.password)).json()["roles"] == ["viewer"]


async def test_host_scheme_and_duplicate_host_checks(client):
    for headers in (
        {"Host": "evil.example.test", "X-Forwarded-Host": "hmi.example.test"},
        {"Host": "hmi.example.test:443"},
    ):
        response = await client.get("/auth/me", headers=headers)
        assert response.status_code == 400 and response.headers["cache-control"] == "no-store"
    response = await client.get(
        "/auth/me", headers=[("Host", "hmi.example.test"), ("Host", "evil.example.test")]
    )
    assert response.status_code == 400
    response = await client.get(
        "http://hmi.example.test/auth/me", headers={"X-Forwarded-Proto": "https"}
    )
    assert response.status_code == 400


async def test_no_service_bearer_or_simulator_session_authentication(client):
    response = await client.get(
        "/auth/me",
        headers={
            "Authorization": "Bearer " + secrets.token_urlsafe(32),
            "Cookie": "simulator_session=" + secrets.token_urlsafe(32),
        },
    )
    assert response.status_code == 401
    for route in ("/internal/ingest", "/commands", "/snapshot", "/docs", "/openapi.json"):
        assert (await client.get(route)).status_code == 404


async def test_invalid_payloads_never_echo_credentials(client, state, caplog):
    secret = secrets.token_urlsafe(24)
    csrf = await challenge(client)
    headers = protection(csrf)
    for payload in (
        {"login": "person", "password": {"secret": secret}},
        {"login": "person", "password": secret, "extra": secret},
    ):
        response = await client.post("/auth/login", headers=headers, json=payload)
        assert response.status_code == 422 and secret not in response.text + caplog.text
    response = await client.post(
        "/auth/login",
        headers=headers,
        content='{"login":"person","password":"' + secret,
    )
    assert response.status_code == 415 and secret not in response.text
    response = await client.post(
        "/auth/login",
        headers={**headers, "Content-Type": "application/json"},
        content='{"password":"' + secret,
    )
    assert response.status_code == 422 and secret not in response.text + caplog.text
    logged_in = await login(client, state.password)
    assert logged_in.status_code == 200
    response = await client.post(
        "/auth/password",
        headers=protection(logged_in.json()["csrf_token"]),
        json={"new_password": "short"},
    )
    assert response.status_code == 422


async def test_streamed_body_bound_does_not_trust_content_length(client, state):
    async def body():
        yield b"a" * state.config.max_body_bytes
        yield b"b"

    response = await client.post("/auth/login", content=body(), headers={"Origin": ORIGIN})
    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"
    assert scalar(state, "SELECT count(*) FROM auth_sessions") == 0


async def test_challenge_and_login_throttling_reset_after_window(state):
    config = replace(state.config, login_limit=2, challenge_limit=2)
    app = create_user_app(state.auth, config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        await challenge(client)
        await challenge(client)
        response = await client.post(
            "/auth/challenge",
            headers={"Origin": ORIGIN, "X-HMI-Request": "1", "X-Forwarded-For": "192.0.2.5"},
        )
        assert response.status_code == 429 and response.headers["retry-after"] == "60"
        state.monotonic[0] += config.window_seconds
        csrf = await challenge(client)
        for _ in range(2):
            response = await client.post(
                "/auth/login",
                headers=protection(csrf),
                json={"login": "person", "password": "wrong"},
            )
            assert response.status_code == 401
        response = await client.post(
            "/auth/login",
            headers={**protection(csrf), "X-Forwarded-For": "192.0.2.7"},
            json={"login": "person", "password": state.password},
        )
        assert response.status_code == 429
        state.monotonic[0] += config.window_seconds
        assert (await login(client, state.password)).status_code == 200


async def test_account_throttle_combines_case_variants_across_peer_ips(state):
    app = create_user_app(state.auth, replace(state.config, login_limit=2))
    for index, name in enumerate(("person", "PERSON", "Person")):
        transport = httpx.ASGITransport(app=app, client=(f"192.0.2.{index + 1}", 1234))
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
            response = await login(client, "incorrect-password", name)
            assert response.status_code == (401 if index < 2 else 429)


async def test_limiter_key_capacity_fails_closed_and_recovers(state):
    config = replace(state.config, max_throttle_keys=1)
    app = create_user_app(state.auth, config)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.1", 1234)), base_url=ORIGIN
    ) as first:
        await challenge(first)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.2", 1234)), base_url=ORIGIN
    ) as second:
        headers = {"Origin": ORIGIN, "X-HMI-Request": "1"}
        assert (await second.post("/auth/challenge", headers=headers)).status_code == 429
        state.monotonic[0] += config.window_seconds
        assert (await second.post("/auth/challenge", headers=headers)).status_code == 200


@pytest.mark.parametrize(
    "origin,development",
    [
        ("http://localhost:8000", False),
        ("http://hmi.example.test", True),
        ("https://hmi.example.test/", False),
        ("https://*.example.test", False),
        ("https://user:password@hmi.example.test", False),
        ("https://hmi.example.test:0", False),
    ],
)
def test_explicit_origin_configuration_fails_closed(origin, development):
    with pytest.raises(ValueError):
        UserHTTPConfig(origin=origin, allow_insecure_loopback=development)


async def test_explicit_loopback_development_only_disables_secure_cookie(state):
    origin = "http://127.0.0.1:8000"
    auth = UserAuth(state.auth.engine, allowed_origins={origin})
    app = create_user_app(auth, UserHTTPConfig(origin=origin, allow_insecure_loopback=True))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
        response = await client.post(
            "/auth/challenge", headers={"Origin": origin, "X-HMI-Request": "1"}
        )
        assert response.status_code == 200
        value = response.headers["set-cookie"].lower()
        assert "secure" not in value and "httponly" in value and "samesite=strict" in value


async def test_database_errors_have_static_safe_response(client, state, monkeypatch, caplog):
    secret = secrets.token_urlsafe(32)

    def unavailable(token):
        raise sa.exc.OperationalError(
            "SELECT authentication", {"secret": secret}, RuntimeError(secret), hide_parameters=True
        )

    monkeypatch.setattr(state.auth, "authenticate", unavailable)
    response = await client.get("/auth/me")
    assert response.status_code == 503
    assert response.json() == {"detail": "authentication unavailable"}
    assert secret not in response.text + caplog.text
    assert response.headers["cache-control"] == "no-store"


async def test_snapshot_requires_changed_live_session_and_bound_source(client, state):
    from apps.api.bootstrap import bootstrap_reference_data
    from apps.api.ingestion import IngestionService

    bootstrap_reference_data(state.auth.engine)
    service = IngestionService(state.auth.engine)
    service.configure_source(
        source_id="source-a",
        stream_id="stream-a",
        gateway_id="gateway-a",
        line_id="packaging-line-1",
        capture_generation=1,
        now=state.clock[0],
    )
    query = {"line_id": "packaging-line-1", "source_id": "source-a", "stream_id": "stream-a"}
    assert (await client.get("/api/v1/snapshot", params=query)).status_code == 401
    session = (await login(client, state.password)).json()
    assert (await client.get("/api/v1/snapshot", params=query)).status_code == 403
    changed = await client.post(
        "/auth/password",
        headers=protection(session["csrf_token"]),
        json={"new_password": secrets.token_urlsafe(24)},
    )
    assert changed.status_code == 200
    response = await client.get("/api/v1/snapshot", params=query)
    assert response.status_code == 200
    sources = await client.get("/api/v1/sources")
    assert sources.status_code == 200
    assert sources.json()["sources"][0]["source_id"] == "source-a"
    data = response.json()
    assert data["health"]["state"] == "NEVER_OBSERVED"
    assert len(data["missing_tag_ids"]) == 48
    other_line = {**query, "line_id": "unrelated-line"}
    assert (await client.get("/api/v1/snapshot", params=other_line)).status_code == 404
    state.auth.revoke(
        client.cookies.get(SESSION_COOKIE),
        csrf_token=changed.json()["csrf_token"],
        origin=ORIGIN,
    )
    assert (await client.get("/api/v1/snapshot", params=query)).status_code == 401


async def test_explicit_built_web_assets_and_no_directory_escape(app, client, tmp_path):
    from apps.api.web import mount_web

    build = tmp_path / "web"
    (build / "assets").mkdir(parents=True)
    (build / "index.html").write_text('<html><script src="/assets/app.js"></script></html>')
    (build / "assets" / "app.js").write_text('document.title="HMI";')
    (tmp_path / "private.txt").write_text("must-not-be-served")
    (build / "assets" / "outside.txt").symlink_to(tmp_path / "private.txt")
    mount_web(app, build)
    response = await client.get("/")
    assert response.status_code == 200 and "text/html" in response.headers["content-type"]
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert (await client.get("/assets/app.js")).status_code == 200
    assert (await client.get("/assets/outside.txt")).status_code == 404
    assert (await client.get("/assets/%2e%2e/private.txt")).status_code == 404
    assert (await client.get("/auth/me")).status_code == 401
