"""CX22 operator CLI entrypoints, with PG17 tools in the exact disposable PG container.

This verifies the real Python CLI through a temporary PATH adapter, not host-native
PostgreSQL clients. The adapter starts no container and never passes secrets in argv.
"""

import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from test_gateway_pipeline import make_finished_run, register

from apps.api.bootstrap import LINE_ID
from apps.api.ingestion import IngestionService
from apps.api.recovery import MARKER_PREFIX, recovery_required
from apps.gateway.buffer import GatewayBuffer
from apps.simulator.controller import SimulatorController
from apps.simulator.storage import SimulatorStorage
from deploy.backup_set import controller_settings, inventory, load_controller_settings

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts/backup-set.py"
DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]
IMAGE = (
    "postgres:17-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826"
)
DOCKER_SELECTORS = (
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_TLS",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
)


def pg_adapter(tmp_path, url, restored_database):
    """Pin tool execution to the fixture's exact published loopback port and PG17 image."""
    env = os.environ.copy()
    for name in DOCKER_SELECTORS:
        env.pop(name, None)

    def docker(*arguments):
        result = subprocess.run([*DOCKER, *arguments], env=env, capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError("Disposable PG identification failed; diagnostics withheld")
        return result.stdout.decode()

    if url.host != "127.0.0.1" or url.database != "hmi_mes_test" or url.username != "postgres":
        raise ValueError("The CLI adapter requires the existing disposable fixture URL")
    ids = docker("ps", "--filter", f"publish={url.port}", "--format", "{{.ID}}").split()
    if len(ids) != 1:
        raise RuntimeError("Exactly one disposable PostgreSQL container must match the port")
    container = ids[0]
    ports = json.loads(docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", container))
    if ports.get("5432/tcp") != [{"HostIp": "127.0.0.1", "HostPort": str(url.port)}]:
        raise RuntimeError("The selected PostgreSQL container has a different published endpoint")
    if docker("inspect", "--format", "{{.Config.Image}}", container).strip() != IMAGE:
        raise RuntimeError("The selected PostgreSQL container is not the pinned fixture image")
    directory = tmp_path / "pg-cli-adapter"
    directory.mkdir(mode=0o700)
    audit = directory / "calls.jsonl"
    source = f"""#!{sys.executable}
import json,os,subprocess,sys
from pathlib import Path
tool=Path(sys.argv[0]).name
if (tool not in {{'pg_dump','pg_restore'}}
    or os.environ.get('PGHOST')!='127.0.0.1'
    or os.environ.get('PGPORT')!={str(url.port)!r}
    or os.environ.get('PGUSER')!='postgres'
    or os.environ.get('PGDATABASE') not in {[url.database, restored_database]!r}):
    raise SystemExit('Refused a different PostgreSQL tool or endpoint')
env=os.environ.copy()
for key in {DOCKER_SELECTORS!r}:
    env.pop(key,None)
env.update(PGHOST='127.0.0.1',PGPORT='5432')
command={DOCKER!r}+['exec','-i','-e','PGPASSWORD','-e','PGUSER','-e','PGDATABASE',
    '-e','PGHOST','-e','PGPORT',{container!r},tool,*sys.argv[1:]]
payload=sys.stdin.buffer.read() if tool=='pg_restore' else None
result=subprocess.run(command,input=payload,capture_output=True,env=env,timeout=180)
with Path({str(audit)!r}).open('a') as log:
    log.write(json.dumps(dict(tool=tool,database=env['PGDATABASE'],
        exit_code=result.returncode,container={container!r}))+'\\n')
if result.returncode:
    raise SystemExit('Disposable PG17 tool failed; diagnostics withheld')
sys.stdout.buffer.write(result.stdout)
"""
    for name in ("pg_dump", "pg_restore"):
        path = directory / name
        path.write_text(source)
        path.chmod(0o700)
    return directory, audit, hashlib.sha256(source.encode()).hexdigest()


async def test_backup_restore_resume_operator_cli(pipeline, tmp_path, acceptance_evidence):
    e = acceptance_evidence("CX22-backup-cli")
    runtime, server, client, pump, service = pipeline
    engine = service.engine
    original_url = engine.url
    database_name = "hmi_restore_" + secrets.token_hex(8)
    adapter, audit, adapter_sha = pg_adapter(tmp_path, original_url, database_name)
    commands = []
    e.record(
        commands=commands,
        backup_cli_sha256=hashlib.sha256(CLI.read_bytes()).hexdigest(),
        pg_tools={"mode": "exact-fixture-docker-exec", "adapter_sha256": adapter_sha},
    )

    def cli(command, url, *arguments):
        child_env = os.environ.copy()
        child_env["PATH"] = str(adapter) + os.pathsep + child_env.get("PATH", "")
        dsn = url.render_as_string(hide_password=False)
        child_env["HMI_BACKUP_DATABASE_DSN"] = dsn
        argv = [sys.executable, str(CLI), command, *map(str, arguments)]
        started = time.monotonic()
        result = subprocess.run(argv, cwd=ROOT, env=child_env, capture_output=True, timeout=240)
        combined = result.stdout + result.stderr
        commands.append(
            {
                "argv": argv,
                "exit_code": result.returncode,
                "duration_seconds": time.monotonic() - started,
                "output_sha256": hashlib.sha256(combined).hexdigest(),
            }
        )
        if dsn.encode() in combined or url.password.encode() in combined:
            raise RuntimeError("Backup operator CLI exposed a credential; output withheld")
        if result.returncode:
            raise RuntimeError(f"Backup operator CLI {command} failed; diagnostics withheld")
        return json.loads(result.stdout)

    summary = await make_finished_run(runtime, server, client)
    register(service, runtime)
    await pump.capture_once()
    while await pump.deliver_once():
        pass
    totals = service.get_totals("run-A")
    e.check(
        "source production and drained gateway",
        totals["good_quantity"] == summary.good_quantity
        and totals["reject_quantity"] == summary.reject_quantity
        and totals["scrap_quantity"] == summary.scrap_quantity
        and pump.buffer.status(now=datetime.now(UTC)).pending_batches == 0,
    )
    identity = runtime.storage.identity
    with engine.connect() as db:
        revision = db.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    settings = dict(
        app_version="v3",
        contract_version="1",
        schema_version=revision,
        line_id=LINE_ID,
        source_id=identity.source_id,
        stream_id=identity.stream_id,
        counter_epoch=identity.counter_epoch,
        gateway_id=pump.buffer.gateway_id,
        controller=controller_settings(runtime),
    )
    # The CLI's settings contract is JSON, including arrays for process tuples.
    settings = json.loads(json.dumps(settings))
    source_path, queue_path = runtime.storage.path, pump.buffer.path
    await client.disconnect()
    await server.stop()
    pump.buffer.close()
    runtime.storage.close()
    original_inventory = inventory(engine, source_path, queue_path)
    settings_file = tmp_path / "nonsecret-settings.json"
    settings_file.write_text(json.dumps(settings))
    settings_file.chmod(0o600)
    backup = tmp_path / "cli-backup"
    restored = tmp_path / "cli-restored"
    set_id = "cli-" + secrets.token_hex(8)
    restored_engine = None
    try:
        backed_up = cli(
            "backup",
            original_url,
            "--simulator",
            source_path,
            "--gateway",
            queue_path,
            "--settings",
            settings_file,
            "--backup-set-id",
            set_id,
            "--destination",
            backup,
        )
        manifest = json.loads((backup / "manifest.json").read_text())
        e.check(
            "backup CLI preserves full source inventory and configuration",
            backed_up["backup_set_id"] == set_id
            and manifest["inventory"] == original_inventory
            and json.loads((backup / "settings.json").read_text())["settings"] == settings,
        )
        receipt = cli(
            "restore",
            original_url,
            "--directory",
            backup,
            "--database-name",
            database_name,
            "--destination",
            restored,
        )
        restored_url = original_url.set(database=database_name)
        restored_engine = sa.create_engine(restored_url, hide_parameters=True)
        with restored_engine.connect() as db:
            marker = db.execute(
                sa.text(
                    "SELECT shobj_description(oid,'pg_database') FROM pg_database "
                    "WHERE datname=current_database()"
                )
            ).scalar_one()
            blocked = recovery_required(db)
        e.check(
            "restore CLI verifies inventory and installs recovery marker",
            receipt["inventory_verified"]
            and not receipt["automatic_start"]
            and not receipt["control_enabled"]
            and blocked
            and marker == MARKER_PREFIX + set_id,
        )
        restored_source = SimulatorStorage(restored / "simulator.sqlite3")
        try:
            restored_runtime = SimulatorController(
                restored_source, now=datetime.now(UTC), **load_controller_settings(settings)
            )
            e.check(
                "restored PLC starts with recovery required and outputs off",
                restored_runtime.state.state == "RECOVERY_REQUIRED"
                and restored_source.recovery_required
                and not restored_runtime.state.run_output,
            )
        finally:
            restored_source.close()
        restored_queue = GatewayBuffer(
            restored / "gateway.sqlite3", gateway_id=settings["gateway_id"]
        )
        restored_queue.close()
        resumed = cli(
            "resume",
            restored_url,
            "--directory",
            backup,
            "--restored-directory",
            restored,
            "--operator-id",
            "cli-verified-operator",
            "--acknowledge-verified-recovery",
        )
        with restored_engine.connect() as db:
            still_blocked = recovery_required(db)
            verified_marker = db.execute(
                sa.text(
                    "SELECT shobj_description(oid,'pg_database') FROM pg_database "
                    "WHERE datname=current_database()"
                )
            ).scalar_one()
        with sqlite3.connect(restored / "simulator.sqlite3") as db:
            state = json.loads(db.execute("SELECT snapshot_json FROM runtime_state").fetchone()[0])
        actual = inventory(
            restored_engine, restored / "simulator.sqlite3", restored / "gateway.sqlite3"
        )
        e.check(
            "resume CLI releases only database restriction and preserves production",
            resumed["control_restriction_released"]
            and resumed["plc_recovery_required"]
            and not resumed["automatic_start"]
            and not still_blocked
            and verified_marker == f"HMI_MES_RECOVERY_VERIFIED:{set_id}:cli-verified-operator"
            and state["state"] == "RECOVERY_REQUIRED"
            and not state["run_output"]
            and actual["postgres"] == original_inventory["postgres"]
            and IngestionService(restored_engine).get_totals("run-A") == totals
            and inventory(engine, source_path, queue_path) == original_inventory,
        )
        calls = [json.loads(line) for line in audit.read_text().splitlines()]
        e.check(
            "real CLI used PG17 tools in the exact disposable container",
            [call["tool"] for call in calls] == ["pg_dump", "pg_restore"]
            and all(call["exit_code"] == 0 for call in calls)
            and [call["database"] for call in calls] == [original_url.database, database_name],
        )
        e.record(
            executions=["run-A"],
            final_sequences={identity.stream_id: str(summary.last_event_seq)},
            reports={"backup_set_id": set_id, "totals": totals, "tool_calls": calls},
        )
    finally:
        if restored_engine:
            restored_engine.dispose()
        # Only this test's independently generated database is eligible for cleanup.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as db:
            exists = db.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": database_name}
            ).scalar_one_or_none()
            if exists:
                db.execute(sa.text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
