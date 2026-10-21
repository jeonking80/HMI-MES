"""Real browser-session launcher process with explicit PostgreSQL account setup."""

import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from apps.api.__main__ import _user_http_config, parser
from apps.api.auth import UserAuth

ROOT = Path(__file__).resolve().parents[2]


def environment(engine):
    env = os.environ.copy()
    env["HMI_DATABASE_DSN"] = engine.url.render_as_string(hide_password=False)
    return env


def test_user_server_process_login_and_graceful_shutdown(migrated):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    password = secrets.token_urlsafe(32)
    UserAuth(migrated, allowed_origins={origin}).bootstrap_user(
        "operator.a", password, roles={"operator"}
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "apps.api", "serve-user", "--origin", origin, "--port", str(port)],
        cwd=ROOT,
        env=environment(migrated),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    csrf, cookie = None, None
    try:
        with httpx.Client(base_url=origin, trust_env=False, timeout=2) as client:
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None, "User API exited before readiness"
                try:
                    response = client.get("/auth/me")
                    break
                except httpx.TransportError:
                    if time.monotonic() >= deadline:
                        pytest.fail("User API readiness timed out")
                    time.sleep(0.05)
            assert response.status_code == 401
            assert client.get("/internal/v1/batches").status_code == 404
            challenge = client.post(
                "/auth/challenge", headers={"Origin": origin, "X-HMI-Request": "1"}
            )
            assert challenge.status_code == 200
            logged_in = client.post(
                "/auth/login",
                headers={"Origin": origin, "X-CSRF-Token": challenge.json()["csrf_token"]},
                json={"login": "operator.a", "password": password},
            )
            assert logged_in.status_code == 200
            csrf = logged_in.json()["csrf_token"]
            cookie = client.cookies.get("hmi_session")
            me = client.get("/auth/me")
            assert me.status_code == 200
            assert me.json() == {
                "login": "operator.a",
                "roles": ["operator"],
                "must_change_password": True,
            }
            assert client.get("/auth/me", headers={"Host": "untrusted.test"}).status_code == 400
    finally:
        process.terminate()
        try:
            output, errors = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            output, errors = process.communicate(timeout=5)
        for secret in (password, csrf, cookie):
            if secret is not None:
                assert secret.encode() not in output + errors
    assert process.returncode == 0
    with migrated.connect() as db:
        assert db.execute(text("SELECT count(*) FROM users")).scalar_one() == 1


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:8003",
        "http://localhost:8002",
        "https://127.0.0.1:8002",
        "http://127.0.0.1:8002/",
        "http://remote.test:8002",
    ],
)
def test_user_launcher_rejects_origin_listener_mismatch(origin):
    with pytest.raises(ValueError):
        _user_http_config(parser().parse_args(["serve-user", "--origin", origin]))
    env = os.environ.copy()
    env.pop("HMI_DATABASE_DSN", None)
    result = subprocess.run(
        [sys.executable, "-m", "apps.api", "serve-user", "--origin", origin],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1
    assert "Traceback" not in result.stderr


def test_user_server_does_not_migrate_or_provision(engine):
    result = subprocess.run(
        [sys.executable, "-m", "apps.api", "serve-user", "--origin", "http://127.0.0.1:8002"],
        cwd=ROOT,
        env=environment(engine),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1
    with engine.connect() as db:
        assert db.execute(text("SELECT to_regclass('users')")).scalar_one() is None
        assert db.execute(text("SELECT to_regclass('alembic_version')")).scalar_one() is None
