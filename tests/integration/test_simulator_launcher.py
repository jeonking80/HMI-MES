"""Real independent app process startup and explicit credential provisioning."""

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from apps.simulator.__main__ import _password_file
from apps.simulator.certificates import provision_development_certificates

ROOT = Path(__file__).resolve().parents[2]


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_password_file_rejects_public_and_symlink_inputs(tmp_path):
    secret = tmp_path / "password"
    secret.write_text("not-a-real-credential\n")
    secret.chmod(0o644)
    with pytest.raises(ValueError):
        _password_file(secret)
    secret.chmod(0o600)
    assert _password_file(secret) == "not-a-real-credential"
    link = tmp_path / "link"
    link.symlink_to(secret)
    with pytest.raises(OSError):
        _password_file(link)


def test_development_identity_creation_refuses_existing_directory(tmp_path):
    directory = tmp_path / "certs"
    provision_development_certificates(directory)
    assert len(list(directory.iterdir())) == 4
    assert all((p.stat().st_mode & 0o777) == 0o600 for p in directory.iterdir())
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    with pytest.raises(FileExistsError):
        provision_development_certificates(directory)
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}


def test_separate_server_process_authentication_and_clean_shutdown(tmp_path):
    database = tmp_path / "runtime.sqlite3"
    certs = tmp_path / "certs"
    provision_development_certificates(certs)
    http_port, opc_port = port(), port()
    origin = f"http://127.0.0.1:{http_port}"
    secret = tmp_path / "initial-password"
    secret.write_text("test-only-initial-secret\n")
    secret.chmod(0o600)
    base = [sys.executable, "-m", "apps.simulator"]
    initialized = subprocess.run(
        base
        + [
            "bootstrap-user",
            "--database",
            str(database),
            "--origin",
            origin,
            "--login",
            "operator",
            "--password-file",
            str(secret),
            "--role",
            "simulator_operator",
        ],
        cwd=ROOT,
        capture_output=True,
        timeout=15,
    )
    assert initialized.returncode == 0
    assert secret.read_bytes().strip() not in initialized.stdout + initialized.stderr
    secret.unlink()
    process = subprocess.Popen(
        base
        + [
            "serve",
            "--database",
            str(database),
            "--origin",
            origin,
            "--port",
            str(http_port),
            "--opc-endpoint",
            f"opc.tcp://127.0.0.1:{opc_port}/hmi-mes/",
            "--server-certificate",
            str(certs / "server.der"),
            "--server-private-key",
            str(certs / "server.pem"),
            "--trusted-client-certificate",
            str(certs / "gateway.der"),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        with httpx.Client(base_url=origin, trust_env=False, timeout=2) as client:
            deadline = time.monotonic() + 15
            while True:
                assert process.poll() is None, "Simulator process exited before HTTP readiness"
                try:
                    response = client.get("/auth/me")
                    break
                except httpx.TransportError:
                    if time.monotonic() >= deadline:
                        pytest.fail("Simulator HTTP readiness timed out")
                    time.sleep(0.1)
            assert response.status_code == 401
            challenge = client.post(
                "/auth/challenge", headers={"Origin": origin, "X-Simulator-Request": "1"}
            )
            assert challenge.status_code == 200
            login = client.post(
                "/auth/login",
                headers={"Origin": origin, "X-CSRF-Token": challenge.json()["csrf_token"]},
                json={"login": "operator", "password": "test-only-initial-secret"},
            )
            assert login.status_code == 200
            snapshot = client.get("/snapshot")
            assert snapshot.status_code == 200
            assert snapshot.json()["state"]["recovery_required"] is True
            assert snapshot.json()["state"]["run_output"] is False
            with socket.create_connection(("127.0.0.1", opc_port), timeout=2):
                pass
    finally:
        process.terminate()
        try:
            output, errors = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            output, errors = process.communicate(timeout=5)
        assert b"test-only-initial-secret" not in output + errors
    assert process.returncode == 0
    # Canonical owner lock was released; restart still requires explicit recovery.
    from apps.simulator.storage import SimulatorStorage

    with SimulatorStorage(database) as reopened:
        assert reopened.recovery_required
