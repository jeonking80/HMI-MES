"""Bounded commissioning tool checks; never starts production or external clients."""

import hashlib
import io
import json
import os
import secrets
import signal
import socket
import sqlite3
import stat

import httpx
import pytest

import deploy.plant_runtime as runtime


@pytest.fixture
def prepared_ports():
    # Hold both reservations together so the selected ports cannot be equal.
    with socket.socket() as console, socket.socket() as opc:
        console.bind(("127.0.0.1", 0))
        opc.bind(("127.0.0.1", 0))
        return console.getsockname()[1], opc.getsockname()[1]


@pytest.fixture
def prepared(tmp_path, prepared_ports):
    directory = tmp_path / "runtime"
    runtime.prepare(
        directory,
        initialize=True,
        console_port=prepared_ports[0],
        opc_port=prepared_ports[1],
    )
    return directory


def digest(path):
    return hashlib.sha256(path.read_bytes()).digest()


def test_prepare_exact_scope_recovery_and_resume_never_resets(prepared, prepared_ports):
    config = runtime.validate(prepared)
    assert config == {
        **runtime.IDENTITY,
        "console_port": prepared_ports[0],
        "opc_port": prepared_ports[1],
        "console_login": runtime.LOGIN,
    }
    before = {
        name: digest(prepared / name)
        for name in (
            "simulator.sqlite3",
            "gateway.sqlite3",
            "console-initial-password",
            "console-password",
        )
    }
    with sqlite3.connect(prepared / "simulator.sqlite3") as db:
        state = json.loads(db.execute("SELECT snapshot_json FROM runtime_state").fetchone()[0])
        assert state["recovery_required"] and not state["run_output"]
        assert state["execution_id"] is None and state["control_mode"] == "LOCAL"
        assert db.execute("SELECT count(*) FROM command_journal").fetchone() == (0,)
    runtime.prepare(prepared)
    with pytest.raises(ValueError, match="never initialized"):
        runtime.prepare(prepared, initialize=True)
    assert all(digest(prepared / name) == value for name, value in before.items())
    assert stat.S_IMODE(prepared.stat().st_mode) == 0o700
    for name in before:
        assert stat.S_IMODE((prepared / name).stat().st_mode) == 0o600
    with pytest.raises(BlockingIOError):
        with runtime.runtime_lock(prepared), runtime.runtime_lock(prepared):
            pass


def test_target_token_and_commands_have_no_start_or_recover(prepared):
    token = prepared / "service-token"
    runtime.private_write(token, secrets.token_urlsafe(32))
    commands = runtime.commands(prepared, runtime.validate(prepared), token)
    assert commands["simulator"][2:4] == ["apps.simulator", "serve"]
    gateway = commands["gateway"]
    assert gateway[gateway.index("--api-url") + 1] == runtime.API
    assert "--enable-commands" in gateway and "--enable-settlement" in gateway
    assert "--allow-loopback-http" not in gateway
    assert not any(
        value in {"START", "LOAD_JOB", "commission", "recover"}
        for values in commands.values()
        for value in values
    )
    assert runtime.api_target("http://127.0.0.1:8080", True) == "http://127.0.0.1:8080"
    for target in (
        "https://foreign.example",
        runtime.API + "/",
        "http://localhost:8/path",
        "http://user@localhost:8",
    ):
        with pytest.raises(ValueError):
            runtime.api_target(target, True)
    with pytest.raises(ValueError):
        runtime.api_target("http://127.0.0.1:8080", False)
    token.chmod(0o644)
    with pytest.raises(ValueError):
        runtime.commands(prepared, runtime.validate(prepared), token)


def test_scope_tampering_and_symlink_are_rejected(prepared, tmp_path):
    manifest = prepared / "runtime.json"
    value = json.loads(manifest.read_text())
    value["source_id"] = "old-scope"
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="identity"):
        runtime.prepare(prepared)
    link = tmp_path / "alias"
    link.symlink_to(prepared, target_is_directory=True)
    with pytest.raises(ValueError):
        runtime.validate(link)


def test_redaction_emits_only_bounded_gateway_status():
    assert runtime.sanitized_line("secret token DSN traceback") is None
    assert (
        runtime.sanitized_line('{"operation":"command","state":"reported","token":"private"}')
        == '{"operation": "command", "state": "reported"}'
    )
    assert runtime.sanitized_line('{"operation":"command","state":"https://secret"}') is None
    assert runtime.sanitized_line('{"operation":"unknown","state":"GOOD"}') is None


@pytest.mark.parametrize("reason", ["child_failure", "sigterm"])
def test_child_failure_cleans_only_owned_children_and_preserves_databases(
    prepared, monkeypatch, reason
):
    token = prepared / "service-token"
    runtime.private_write(token, secrets.token_urlsafe(32))
    before = digest(prepared / "simulator.sqlite3")
    children = []

    class Child:
        def __init__(self, *_args, **_kwargs):
            self.pid = 900000 + len(children)
            self.stdout = io.StringIO("suppressed secret\n")
            self.returncode = 1 if not children and reason == "child_failure" else None
            self.terminated = False
            children.append(self)
            if reason == "sigterm" and len(children) == 2:
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout):
            return self.returncode

    monkeypatch.setattr(runtime.subprocess, "Popen", Child)
    if reason == "child_failure":
        with pytest.raises(RuntimeError, match="child exited"):
            runtime.serve(prepared, token_file=token)
    else:
        runtime.serve(prepared, token_file=token)
        assert children[0].terminated
    assert len(children) == 2 and children[1].terminated
    assert not (prepared / "processes.json").exists()
    assert digest(prepared / "simulator.sqlite3") == before
    assert "secret" not in (prepared / "supervisor.log").read_text()


def test_live_prior_pid_prevents_start_and_is_not_killed(prepared, monkeypatch):
    runtime.private_write(
        prepared / "processes.json", json.dumps({"simulator": os.getpid(), "gateway": os.getpid()})
    )

    def forbidden(*_args, **_kwargs):
        pytest.fail("Must not start children over a prior live process")

    monkeypatch.setattr(runtime.subprocess, "Popen", forbidden)
    with pytest.raises(ValueError, match="still be alive"):
        runtime.serve(prepared, token_file=prepared / "not-required")


def test_explicit_commission_authenticates_rotates_and_uses_only_safe_routes(prepared, monkeypatch):
    paths = []
    real_client = httpx.Client

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/auth/challenge":
            assert request.headers["x-simulator-request"] == "1"
            return httpx.Response(200, json={"csrf_token": "challenge"})
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"csrf_token": "initial", "must_change_password": True})
        if request.url.path == "/auth/password":
            return httpx.Response(200, json={"csrf_token": "changed"})
        assert request.headers.get("x-csrf-token", "changed") == "changed"
        if request.url.path == "/snapshot":
            return httpx.Response(
                200,
                json={
                    "state": {
                        "run_output": False,
                        "wip_quantity": 0,
                        "execution_id": None,
                        "recovery_required": True,
                        "inputs": {"guard_closed": True},
                    }
                },
            )
        if request.url.path == "/recover":
            assert json.loads(request.content) == {name: True for name in runtime.CHECKS}
        if request.url.path == "/inputs":
            assert json.loads(request.content) == {
                "inputs": {"guard_closed": True},
                "control_mode": "REMOTE",
                "operation_mode": "AUTO",
            }
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        runtime.httpx,
        "Client",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handler)),
    )
    checks = {name: True for name in runtime.CHECKS}
    with pytest.raises(ValueError):
        runtime.commission(
            prepared,
            password_file=prepared / "console-initial-password",
            new_password_file=None,
            checks={},
        )
    assert paths == []
    runtime.commission(
        prepared,
        password_file=prepared / "console-initial-password",
        new_password_file=prepared / "console-password",
        checks=checks,
    )
    assert paths == [
        "/auth/challenge",
        "/auth/login",
        "/auth/password",
        "/snapshot",
        "/recover",
        "/inputs",
        "/auth/logout",
    ]
