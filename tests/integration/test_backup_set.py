"""Backup validation fails before any database restore side effect."""

import hashlib
import json

import pytest

from apps.gateway.buffer import GatewayBuffer
from deploy.backup_set import MEMBERS, SETTING_KEYS, _stopped, local_url, validate_backup_set


def sample(tmp_path):
    from dataclasses import asdict

    from apps.simulator.plc import PLCConfig
    from apps.simulator.process import ProcessConfig

    settings = {"backup_set_id": "one", "settings": dict.fromkeys(SETTING_KEYS, "test")}
    settings["settings"]["controller"] = {
        "process_config": asdict(ProcessConfig()),
        "plc_config": PLCConfig().model_dump(mode="json"),
        "seed": 1,
    }
    for name in MEMBERS:
        (tmp_path / name).write_bytes(b"data")
    (tmp_path / "settings.json").write_text(json.dumps(settings))
    manifest = {
        "format": 1,
        "backup_set_id": "one",
        "checksums": {
            name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() for name in MEMBERS
        },
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def test_corruption_mixed_sets_extra_files_and_symlinks_are_rejected(tmp_path):
    manifest = sample(tmp_path)
    assert validate_backup_set(tmp_path) == manifest
    (tmp_path / "gateway.sqlite3").write_bytes(b"changed")
    with pytest.raises(ValueError, match="Checksum"):
        validate_backup_set(tmp_path)
    sample(tmp_path)
    manifest["backup_set_id"] = "different"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Mixed"):
        validate_backup_set(tmp_path)
    sample(tmp_path)
    (tmp_path / "extra").write_bytes(b"x")
    with pytest.raises(ValueError, match="Unexpected"):
        validate_backup_set(tmp_path)
    (tmp_path / "extra").unlink()
    (tmp_path / "gateway.sqlite3").unlink()
    (tmp_path / "gateway.sqlite3").symlink_to(tmp_path / "simulator.sqlite3")
    with pytest.raises(ValueError, match="Symlink"):
        validate_backup_set(tmp_path)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://user@example.com/db",
        "sqlite:///db",
        "postgresql://user@localhost/db?host=example.com",
    ],
)
def test_remote_and_implicit_connection_overrides_rejected(dsn):
    with pytest.raises(ValueError):
        local_url(dsn)


def test_active_owner_prevents_backup_barrier(tmp_path):
    with GatewayBuffer(tmp_path / "gateway.sqlite3", gateway_id="test") as queue:
        with pytest.raises(ValueError, match="Stop"):
            with _stopped((queue.path,)):
                pytest.fail("writer was not stopped")


def docker_pg_tools(url):
    """Select only the generated test container by its explicit published loopback port."""
    import os
    import subprocess

    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_TLS",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        }
    }
    prefix = ["docker", "--host=unix:///var/run/docker.sock"]
    ids = subprocess.run(
        [*prefix, "ps", "--filter", f"publish={url.port}", "--format", "{{.ID}}"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.split()
    if len(ids) != 1:
        raise RuntimeError("Expected exactly one disposable test PostgreSQL container")
    container = ids[0]

    def run(tool, args, selected_url, payload=None):
        child = dict(env, PGPASSWORD=selected_url.password)
        command = [
            *prefix,
            "exec",
            "-i",
            "-e",
            "PGPASSWORD",
            container,
            tool,
            "-h",
            "127.0.0.1",
            "-U",
            selected_url.username,
            *args,
        ]
        if tool == "pg_dump":
            command.append(selected_url.database)
        result = subprocess.run(command, input=payload, capture_output=True, env=child, timeout=180)
        if result.returncode:
            raise RuntimeError(f"{tool} failed (exit {result.returncode}); diagnostics withheld")
        return result.stdout

    return run


@pytest.mark.parametrize("selector", ["PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"])
def test_inherited_libpq_redirect_selectors_rejected(monkeypatch, selector):
    monkeypatch.setenv(selector, "untrusted-override")
    with pytest.raises(ValueError, match="Inherited libpq"):
        local_url("postgresql://user@127.0.0.1/local")


async def test_recovery_gate_blocks_direct_and_indirect_commands_but_allows_reads_and_intake():
    import httpx
    from starlette.responses import JSONResponse

    from deploy.backup_set import recovery_control_gate

    observed = []

    async def endpoint(scope, receive, send):
        observed.append((scope["method"], scope["path"]))
        await JSONResponse({"reached": True})(scope, receive, send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=recovery_control_gate(endpoint)),
        base_url="http://localhost",
    ) as client:
        for path in (
            "/api/v1/commands",
            "/internal/v1/commands/claim",
            "/api/v1/work-orders/order-A/bind",
            "/api/v1/work-order-actions/bind?identity=order-A",
        ):
            assert (await client.post(path)).status_code == 423
        assert observed == []
        for path in ("/api/v1/commands/command-A", "/api/v1/work-orders/order-A"):
            assert (await client.get(path)).status_code == 200
        assert (await client.post("/internal/v1/batches")).status_code == 200
        assert len(observed) == 3
