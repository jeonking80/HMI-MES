"""CX22 host operator entrypoints, with a closed local API and no PostgreSQL.

Captured batches remain deferred on disk. This is not API commit acceptance or
public Neon/Vercel commissioning; generated deployment artifacts are never applied.
"""

import hashlib
import json
import os
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from deploy.plant_runtime import CHECKS, IDENTITY, LOGIN

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "deploy/plant_runtime.py"


def test_operator_cli_entrypoints_and_local_supervisor(tmp_path, acceptance_evidence):
    evidence = acceptance_evidence("CX22-plant-cli")
    runtime = tmp_path / "operator-runtime"
    connection = tmp_path / "operator-connection"
    commands, hidden, captured = [], [], []
    child_env = os.environ.copy()
    child_env.pop("HMI_DATABASE_DSN", None)
    child_env.pop("HMI_BACKUP_DATABASE_DSN", None)
    evidence.record(
        commands=commands,
        operator_scope="host CLI lifecycle; closed loopback API; no API commit or public apply",
        operator_cli_sha256={
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (RUNTIME, ROOT / "deploy/generate_gateway_setup.py")
        },
    )

    def private_files(paths):
        return all(path.is_file() and stat.S_IMODE(path.stat().st_mode) == 0o600 for path in paths)

    def no_secret_output():
        if any(secret in output for secret in hidden for output in captured):
            raise RuntimeError("Operator CLI exposed a secret; output withheld")

    def cli(arguments):
        argv = [sys.executable, *map(str, arguments)]
        started = time.monotonic()
        result = subprocess.run(argv, cwd=ROOT, env=child_env, capture_output=True, timeout=45)
        output = result.stdout + result.stderr
        captured.append(output)
        no_secret_output()
        commands.append(
            dict(
                argv=argv,
                exit_code=result.returncode,
                duration_seconds=time.monotonic() - started,
                output_sha256=hashlib.sha256(output).hexdigest(),
            )
        )
        if result.returncode:
            raise RuntimeError("Operator CLI failed; diagnostics withheld")

    def simulator_state():
        with sqlite3.connect((runtime / "simulator.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
            state = json.loads(db.execute("SELECT snapshot_json FROM runtime_state").fetchone()[0])
            scope = db.execute("SELECT source_id,stream_id FROM simulator_identity").fetchone()
            journal = db.execute("SELECT count(*) FROM command_journal").fetchone()[0]
            password_change = db.execute(
                "SELECT must_change_password FROM console_users WHERE login=?", (LOGIN,)
            ).fetchone()[0]
        return state, scope, journal, password_change

    def captured_and_deferred():
        with sqlite3.connect((runtime / "gateway.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
            batches = [
                json.loads(row[0])["body"]
                for row in db.execute("SELECT body FROM outbound_batches")
            ]
            acknowledged = db.execute(
                "SELECT count(*) FROM batch_ledger WHERE state='ACKNOWLEDGED'"
            ).fetchone()[0]
        statuses = (
            (runtime / "supervisor.log").read_text()
            if (runtime / "supervisor.log").exists()
            else ""
        )
        blocked = any(
            json.loads(line.removeprefix("gateway "))
            == {"operation": "delivery", "state": "blocked"}
            for line in statuses.splitlines()
            if line.startswith("gateway {")
        )
        return (
            any(
                len(batch["samples"]) == 48
                and len({sample["tag_id"] for sample in batch["samples"]}) == 48
                and batch["source_id"] == IDENTITY["source_id"]
                and batch["stream_id"] == IDENTITY["stream_id"]
                for batch in batches
            )
            and acknowledged == 0
            and blocked
        )

    reservations = [socket.socket() for _ in range(3)]
    supervisor = None
    children = {}
    serve_entry = None
    output_path = tmp_path / "private-supervisor-output"
    try:
        for reservation in reservations:
            reservation.bind(("127.0.0.1", 0))
        console_port, opc_port, closed_api_port = [sock.getsockname()[1] for sock in reservations]
        expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        cli(
            [
                "-m",
                "deploy.generate_gateway_setup",
                "--source-id",
                IDENTITY["source_id"],
                "--stream-id",
                IDENTITY["stream_id"],
                "--gateway-id",
                IDENTITY["gateway_id"],
                "--line-id",
                IDENTITY["line_id"],
                "--key-id",
                "cx22-local-only",
                "--capture-generation",
                "1",
                "--expires-at",
                expires,
                "--output-directory",
                connection,
            ]
        )
        hidden.append((connection / "token.secret").read_bytes().strip())
        no_secret_output()
        credentials = json.loads((connection / "vercel-gateway-credentials.json").read_text())
        evidence.check(
            "offline generator produces private scoped artifacts without applying them",
            stat.S_IMODE(connection.stat().st_mode) == 0o700
            and private_files(
                [
                    connection / name
                    for name in (
                        "token.secret",
                        "vercel-gateway-credentials.json",
                        "prepare-source.sql",
                    )
                ]
            )
            and credentials[0]["token_sha256"] == hashlib.sha256(hidden[0]).hexdigest()
            and all(
                credentials[0][key] == IDENTITY[key]
                for key in ("source_id", "stream_id", "gateway_id")
            ),
        )
        base = [RUNTIME, "--directory", runtime]
        cli(
            [
                *base,
                "prepare",
                "--initialize",
                "--console-port",
                console_port,
                "--opc-port",
                opc_port,
            ]
        )
        protected = [
            runtime / name
            for name in (
                "runtime.json",
                "simulator.sqlite3",
                "gateway.sqlite3",
                "console-initial-password",
                "console-password",
            )
        ]
        hidden.extend(
            (runtime / name).read_bytes().strip()
            for name in ("console-initial-password", "console-password")
        )
        no_secret_output()
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
        cli([*base, "prepare"])
        state, scope, journal, password_change = simulator_state()
        evidence.check(
            "initialize and existing prepare preserve private identities and stopped PLC",
            stat.S_IMODE(runtime.stat().st_mode) == 0o700
            and private_files(protected)
            and before
            == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
            and scope == (IDENTITY["source_id"], IDENTITY["stream_id"])
            and state["recovery_required"]
            and not state["run_output"]
            and state["execution_id"] is None
            and journal == 0
            and password_change == 1,
        )
        reservations[0].close()
        reservations[1].close()
        # Keep the API port bound but never listen: no public or mock API is used.
        argv = [
            sys.executable,
            *map(
                str,
                [
                    *base,
                    "serve",
                    "--token-file",
                    connection / "token.secret",
                    "--api-url",
                    f"http://127.0.0.1:{closed_api_port}",
                    "--allow-local-test",
                ],
            ),
        ]
        serve_entry = dict(argv=argv, started_at_utc=datetime.now(UTC).isoformat())
        commands.append(serve_entry)
        descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            supervisor = subprocess.Popen(
                argv, cwd=ROOT, env=child_env, stdout=output, stderr=output, start_new_session=True
            )
        deadline = time.monotonic() + 40
        with httpx.Client(trust_env=False, timeout=1) as client:
            while time.monotonic() < deadline:
                if supervisor.poll() is not None:
                    raise RuntimeError("Operator supervisor exited before local readiness")
                try:
                    ready = (
                        client.get(f"http://127.0.0.1:{console_port}/auth/me").status_code == 401
                    )
                    if ready and captured_and_deferred():
                        break
                except (httpx.TransportError, sqlite3.OperationalError):
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Local console, 48-tag capture or deferred delivery timed out")
        children = json.loads((runtime / "processes.json").read_text())
        evidence.check(
            "real supervisor captures 48 tags and retains deferred batches without API commit",
            set(children) == {"simulator", "gateway"} and captured_and_deferred(),
        )
        cli(
            [
                *base,
                "commission",
                "--password-file",
                runtime / "console-initial-password",
                "--new-password-file",
                runtime / "console-password",
                *("--" + name.replace("_", "-") for name in CHECKS),
            ]
        )
        state, scope, journal, password_change = simulator_state()
        evidence.check(
            "explicit commission changes password and selects remote auto without production",
            password_change == 0
            and not state["recovery_required"]
            and state["control_mode"] == "REMOTE"
            and state["operation_mode"] == "AUTO"
            and not state["run_output"]
            and state["execution_id"] is None
            and journal == 0
            and scope == (IDENTITY["source_id"], IDENTITY["stream_id"]),
        )
        supervisor.send_signal(signal.SIGTERM)
        supervisor.wait(timeout=25)
        evidence.check(
            "SIGTERM completes supervisor cleanup and preserves persistent configuration",
            supervisor.returncode == 0
            and not (runtime / "processes.json").exists()
            and all(
                hashlib.sha256((runtime / name).read_bytes()).hexdigest() == before[name]
                for name in ("runtime.json", "console-initial-password", "console-password")
            ),
        )
        for pid in children.values():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            raise RuntimeError("Operator supervisor left a child process alive")
        evidence.check(
            "children exited and queued capture survives shutdown", captured_and_deferred()
        )
    finally:
        if supervisor is not None:
            if supervisor.poll() is None:
                supervisor.send_signal(signal.SIGTERM)
                try:
                    supervisor.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    os.killpg(supervisor.pid, signal.SIGKILL)
                    supervisor.wait(timeout=5)
            # A failing supervisor must not leave its own process-group children behind.
            try:
                os.killpg(supervisor.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if serve_entry is not None:
                serve_entry.update(
                    exit_code=supervisor.returncode, ended_at_utc=datetime.now(UTC).isoformat()
                )
        for reservation in reservations:
            reservation.close()
        if output_path.exists():
            captured.append(output_path.read_bytes())
            no_secret_output()
            evidence.record(supervisor_output_sha256=hashlib.sha256(captured[-1]).hexdigest())
