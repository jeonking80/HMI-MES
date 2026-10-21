"""Actual operator subprocess provisioning, fail-closed startup, protected secrets."""

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from alembic import command

from apps.api.bootstrap import bootstrap_reference_data
from apps.api.database import migration_config
from apps.gateway.__main__ import _token_file

ROOT = Path(__file__).resolve().parents[2]


def invoke(module, *args, env=None):
    return subprocess.run(
        [sys.executable, "-m", module, *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_token_file_requires_protected_regular_canonical_secret(tmp_path):
    secret = tmp_path / "token"
    token = secrets.token_urlsafe(32)
    secret.write_text(token + "\n")
    secret.chmod(0o600)
    assert _token_file(secret) == token
    link = tmp_path / "link"
    link.symlink_to(secret)
    with pytest.raises(ValueError):
        _token_file(link)
    secret.chmod(0o644)
    with pytest.raises(ValueError):
        _token_file(secret)
    secret.chmod(0o600)
    secret.write_text("a" * 10000)
    with pytest.raises(ValueError):
        _token_file(secret)


def test_cli_has_no_implicit_identity_or_database():
    for module in ("apps.api", "apps.gateway"):
        result = invoke(module, "serve")
        assert result.returncode == 2
        assert "required" in result.stderr
    env = os.environ.copy()
    env.pop("HMI_DATABASE_DSN", None)
    result = invoke("apps.api", "status", "--source-id", "source", "--stream-id", "events", env=env)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr


def test_operator_subprocess_registration_and_binding_refusal(engine, tmp_path):
    with engine.connect() as connection:
        command.upgrade(migration_config(connection), "head")
    migrated = engine
    bootstrap_reference_data(migrated)
    env = os.environ.copy()
    env["HMI_DATABASE_DSN"] = migrated.url.render_as_string(hide_password=False)
    scope = ["--source-id", "source", "--stream-id", "events"]
    registration = [
        "configure-source",
        *scope,
        "--gateway-id",
        "gateway",
        "--line-id",
        "packaging-line-1",
        "--capture-generation",
        "1",
    ]
    result = invoke("apps.api", *registration, env=env)
    assert result.returncode == 0, result.stderr
    assert invoke("apps.api", *registration, env=env).returncode == 0
    result = invoke(
        "apps.api",
        "register-execution",
        *scope,
        "--execution-id",
        "execution",
        "--work-order-id",
        "work",
        "--recipe-version",
        "v1",
        "--counter-epoch",
        "epoch",
        "--temperature-c",
        "180",
        "--speed-m-per-min",
        "10",
        "--target-quantity",
        "100",
        env=env,
    )
    assert result.returncode == 0, result.stderr
    result = invoke("apps.api", "status", *scope, env=env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["source"]["control_restricted"] is False
    secret = tmp_path / "token"
    token = secrets.token_urlsafe(32)
    secret.write_text(token)
    secret.chmod(0o600)
    result = invoke(
        "apps.api",
        "serve",
        *scope,
        "--gateway-id",
        "wrong-gateway",
        "--capture-generation",
        "1",
        "--key-id",
        "key",
        "--token-file",
        str(secret),
        "--expires-at",
        "2099-01-01T00:00:00Z",
        env=env,
    )
    assert result.returncode == 1
    assert token not in result.stdout + result.stderr
    assert env["HMI_DATABASE_DSN"] not in result.stdout + result.stderr
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "apps.api",
            "serve",
            *scope,
            "--gateway-id",
            "gateway",
            "--capture-generation",
            "1",
            "--key-id",
            "key",
            "--token-file",
            str(secret),
            "--expires-at",
            "2099-01-01T00:00:00Z",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=1
        ) as client:
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None, "API exited before readiness"
                try:
                    response = client.get("/health/live")
                    break
                except httpx.TransportError:
                    if time.monotonic() >= deadline:
                        pytest.fail("API readiness timed out")
                    time.sleep(0.05)
            assert response.status_code == 200
            assert client.get("/health/ready").status_code == 401
            headers = {"Authorization": f"Bearer {token}"}
            assert client.get("/health/ready", headers=headers).status_code == 200
            # A claimed HTTPS proxy header cannot turn this socket into trusted HTTPS.
            assert (
                client.get(
                    "/health/ready",
                    headers={
                        **headers,
                        "X-Forwarded-Proto": "https",
                        "X-Forwarded-For": "203.0.113.1",
                    },
                ).status_code
                == 200
            )
    finally:
        process.terminate()
        try:
            output, errors = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            output, errors = process.communicate(timeout=5)
        assert token.encode() not in output + errors
    assert process.returncode == 0
