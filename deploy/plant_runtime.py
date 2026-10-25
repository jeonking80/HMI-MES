"""Explicit persistent commissioning simulator/gateway lifecycle; no production commands."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIRECTORY = Path(
    os.environ.get("HMI_PLANT_RUNTIME_DIR", str(ROOT / ".local/commissioning/runtime"))
)
API = "https://hmi-mes.vercel.app"
IDENTITY = {
    "source_id": "demo-packaging-source-v1",
    "stream_id": "events-main",
    "gateway_id": "demo-packaging-gateway-v1",
    "line_id": "packaging-line-1",
}
LOGIN = "commissioning.operator"
CHECKS = (
    "outputs_stopped",
    "positions_verified",
    "storage_reconciled",
    "commands_reconciled",
    "operator_confirmed",
)


def private_write(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(content)


def protected(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("Owner-only regular file required")
        value = handle.read(4097)
        if len(value) > 4096:
            raise ValueError("Protected file exceeds bound")
        return value.removesuffix("\n")


def configuration(directory):
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Prepared real directory required")
    value = json.loads(protected(directory / "runtime.json"))
    if (
        set(value) != {*IDENTITY, "console_port", "opc_port", "console_login"}
        or any(value.get(key) != expected for key, expected in IDENTITY.items())
        or value["console_login"] != LOGIN
    ):
        raise ValueError("Runtime identity differs; never replace existing scope")
    for name in ("console_port", "opc_port"):
        if type(value[name]) is not int or not 1024 <= value[name] <= 65535:
            raise ValueError("Invalid runtime port")
    if value["console_port"] == value["opc_port"]:
        raise ValueError("Ports must differ")
    return value


def validate(directory):
    value = configuration(directory)
    for file in ("simulator.sqlite3", "gateway.sqlite3"):
        path = directory / file
        if path.is_symlink() or not path.is_file():
            raise ValueError("Existing runtime database required")
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
            if file == "simulator.sqlite3":
                row = db.execute("SELECT source_id,stream_id FROM simulator_identity").fetchone()
                if row != (IDENTITY["source_id"], IDENTITY["stream_id"]):
                    raise ValueError("Simulator scope mismatch")
                if db.execute(
                    "SELECT role FROM console_users WHERE login=?", (LOGIN,)
                ).fetchone() != ("simulator_operator",):
                    raise ValueError("Original console account required")
            elif db.execute("SELECT gateway_id FROM gateway_identity").fetchone() != (
                IDENTITY["gateway_id"],
            ):
                raise ValueError("Gateway identity mismatch")
    for name in ("server.der", "server.pem", "gateway.der", "gateway.pem"):
        path = directory / "certs" / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Existing certificate files required")
    return value


def prepare(directory, *, initialize=False, console_port=8011, opc_port=4850):
    if not initialize:
        return validate(directory)
    if directory.exists() or directory.is_symlink():
        raise ValueError("Existing runtime is never initialized or reset")
    if console_port == opc_port or any(
        type(p) is not int or not 1024 <= p <= 65535 for p in (console_port, opc_port)
    ):
        raise ValueError("Distinct valid ports required")
    from apps.gateway.buffer import GatewayBuffer
    from apps.simulator.auth import SimulatorAuth
    from apps.simulator.certificates import provision_development_certificates
    from apps.simulator.controller import SimulatorController
    from apps.simulator.storage import SimulatorStorage

    directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=".runtime-prepare-", dir=directory.parent))
    try:
        provision_development_certificates(staging / "certs")
        initial = secrets.token_urlsafe(32)
        private_write(staging / "console-initial-password", initial)
        private_write(staging / "console-password", secrets.token_urlsafe(32))
        with SimulatorStorage(
            staging / "simulator.sqlite3",
            source_id=IDENTITY["source_id"],
            stream_id=IDENTITY["stream_id"],
        ) as storage:
            SimulatorController(storage, now=datetime.now(UTC))
            auth = SimulatorAuth(
                storage.connection, allowed_origins={f"http://127.0.0.1:{console_port}"}
            )
            with storage.transaction():
                auth.initialize_schema()
                auth.bootstrap_user(LOGIN, initial, role="simulator_operator")
        with GatewayBuffer(staging / "gateway.sqlite3", gateway_id=IDENTITY["gateway_id"]):
            pass
        for name in ("simulator.sqlite3", "gateway.sqlite3"):
            (staging / name).chmod(0o600)
        value = {
            **IDENTITY,
            "console_port": console_port,
            "opc_port": opc_port,
            "console_login": LOGIN,
        }
        private_write(staging / "runtime.json", json.dumps(value))
        os.rename(staging, directory)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return validate(directory)


def api_target(value, allow_local_test):
    if value == API:
        return value
    parsed = urlsplit(value)
    if (
        allow_local_test
        and parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and not (
            parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path
        )
        and parsed.port
    ):
        return value
    raise ValueError("Only the deployment or explicitly allowed loopback test API is supported")


def commands(
    directory, config, token_file, api_url=API, allow_local_test=False, measured_clock=False
):
    from apps.gateway.__main__ import _token_file

    target = api_target(api_url, allow_local_test)
    _token_file(token_file)  # Verify permissions/encoding before creating any child.
    certs = directory / "certs"
    opc = f"opc.tcp://127.0.0.1:{config['opc_port']}/hmi-mes/"
    origin = f"http://127.0.0.1:{config['console_port']}"
    simulator = [
        sys.executable,
        "-m",
        "apps.simulator",
        "serve",
        "--database",
        str(directory / "simulator.sqlite3"),
        "--port",
        str(config["console_port"]),
        "--origin",
        origin,
        "--opc-endpoint",
        opc,
        "--server-certificate",
        str(certs / "server.der"),
        "--server-private-key",
        str(certs / "server.pem"),
        "--trusted-client-certificate",
        str(certs / "gateway.der"),
    ]
    gateway = [
        sys.executable,
        "-m",
        "apps.gateway",
        "serve",
        "--gateway-id",
        IDENTITY["gateway_id"],
        "--source-id",
        IDENTITY["source_id"],
        "--stream-id",
        IDENTITY["stream_id"],
        "--queue-file",
        str(directory / "gateway.sqlite3"),
        "--opc-endpoint",
        opc,
        "--application-uri",
        "urn:hmi-mes:gateway:client",
        "--server-application-uri",
        "urn:hmi-mes:simulator:server",
        "--client-certificate",
        str(certs / "gateway.der"),
        "--client-private-key",
        str(certs / "gateway.pem"),
        "--trusted-server-certificate",
        str(certs / "server.der"),
        "--api-url",
        target,
        "--token-file",
        str(token_file),
        "--enable-commands",
        "--enable-settlement",
    ]
    if target != API:
        gateway.append("--allow-loopback-http")
    if measured_clock:
        gateway.append("--clock-measured-round-trip")
    return {"simulator": simulator, "gateway": gateway}


@contextmanager
def runtime_lock(directory):
    descriptor = os.open(
        directory / "supervisor.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def sanitized_line(line):
    try:
        value = json.loads(line)
    except (ValueError, TypeError):
        return None
    if (
        isinstance(value, dict)
        and isinstance(value.get("operation"), str)
        and value["operation"] in {"capture", "delivery", "command", "settlement", "gateway"}
        and isinstance(value.get("state"), str)
        and value["state"]
        in {
            "blocked",
            "idle",
            "uncertain",
            "deferred",
            "reported",
            "polled",
            "collected",
            "committed",
            "stopped",
        }
    ):
        output = {"operation": value["operation"], "state": value["state"]}
        error = value.get("error")
        if isinstance(error, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", error):
            output["error"] = error
        error_code = value.get("error_code")
        if (
            error == "BufferFull"
            and isinstance(error_code, str)
            and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", error_code)
        ):
            output["error_code"] = error_code
        return json.dumps(output)
    return None


def serve(directory, *, token_file, api_url=API, allow_local_test=False, measured_clock=False):
    with runtime_lock(directory):
        config = validate(directory)
        previous_pids = directory / "processes.json"
        if previous_pids.exists():
            recorded = json.loads(protected(previous_pids))
            if not isinstance(recorded, dict) or set(recorded) != {"simulator", "gateway"}:
                raise ValueError("Invalid prior process record; inspect manually")
            for pid in recorded.values():
                if type(pid) is not int or pid <= 0:
                    raise ValueError("Invalid prior PID")
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                raise ValueError("Prior process may still be alive; never stop it implicitly")
            previous_pids.unlink()
        launches = commands(
            directory, config, token_file, api_url, allow_local_test, measured_clock
        )
        # Fail before starting either child if the selected local ports are occupied.
        for port in (config["console_port"], config["opc_port"]):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
        stopped = threading.Event()
        children = {}
        threads = []
        previous = {
            sig: signal.signal(sig, lambda *_: stopped.set())
            for sig in (signal.SIGTERM, signal.SIGINT)
        }
        descriptor = os.open(
            directory / "supervisor.log",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "a", buffering=1) as log:

            def consume(name, stream):
                while line := stream.readline(4096):
                    safe = sanitized_line(line)
                    if safe:
                        log.write(name + " " + safe + "\n")
                stream.close()

            try:
                for name, argv in launches.items():
                    child = subprocess.Popen(
                        argv, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
                    )
                    children[name] = child
                    log.write(f"{name} started pid={child.pid}\n")
                    thread = threading.Thread(
                        target=consume, args=(name, child.stdout), daemon=True
                    )
                    thread.start()
                    threads.append(thread)
                private_write(
                    directory / "processes.json",
                    json.dumps({name: c.pid for name, c in children.items()}),
                )
                while not stopped.wait(0.25):
                    for name, child in children.items():
                        if child.poll() is not None:
                            log.write(f"{name} exited code={child.returncode}\n")
                            raise RuntimeError("Runtime child exited; scope remains preserved")
            finally:
                for child in reversed(list(children.values())):
                    if child.poll() is None:
                        child.terminate()
                for child in children.values():
                    try:
                        child.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
                for thread in threads:
                    thread.join(timeout=1)
                (directory / "processes.json").unlink(missing_ok=True)
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
                log.write("supervisor stopped; persistent scope retained\n")


def commission(directory, *, password_file, new_password_file, checks):
    config = validate(directory)
    if set(checks) != set(CHECKS) or not all(checks.values()):
        raise ValueError("All explicit recovery attestations are required")
    origin = f"http://127.0.0.1:{config['console_port']}"
    with httpx.Client(
        base_url=origin, timeout=10, trust_env=False, follow_redirects=False
    ) as client:

        def post(path, body, csrf, extra=None):
            response = client.post(
                path, json=body, headers={"Origin": origin, "X-CSRF-Token": csrf, **(extra or {})}
            )
            response.raise_for_status()
            return response.json()

        challenge = post("/auth/challenge", {}, "", {"X-Simulator-Request": "1"})
        session = post(
            "/auth/login",
            {"login": LOGIN, "password": protected(password_file)},
            challenge["csrf_token"],
        )
        csrf = session["csrf_token"]
        try:
            if session["must_change_password"]:
                if new_password_file is None:
                    raise ValueError("Explicit new password file required for first change")
                session = post(
                    "/auth/password", {"new_password": protected(new_password_file)}, csrf
                )
                csrf = session["csrf_token"]
            snapshot = client.get("/snapshot")
            snapshot.raise_for_status()
            state = snapshot.json()["state"]
            if (
                state["run_output"]
                or state["wip_quantity"]
                # A loaded but never started job has no motion or WIP; it survives
                # recovery and can still be started or ended explicitly.
                or (
                    state["execution_id"] is not None
                    and state["run_state"] not in {"ENDED", "LOADED"}
                )
            ):
                raise ValueError("Commissioning requires no active execution, WIP or motion")
            if state["recovery_required"]:
                post("/recover", checks, csrf)
            post(
                "/inputs",
                {"inputs": state["inputs"], "control_mode": "REMOTE", "operation_mode": "AUTO"},
                csrf,
            )
        finally:
            post("/auth/logout", {}, csrf)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIRECTORY)
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--initialize", action="store_true")
    prep.add_argument("--console-port", type=int, default=8011)
    prep.add_argument("--opc-port", type=int, default=4850)
    run = sub.add_parser("serve")
    run.add_argument("--token-file", type=Path, required=True)
    run.add_argument("--api-url", default=API)
    run.add_argument("--allow-local-test", action="store_true")
    run.add_argument("--clock-measured-round-trip", action="store_true")
    setup = sub.add_parser("commission")
    setup.add_argument("--password-file", type=Path, required=True)
    setup.add_argument("--new-password-file", type=Path)
    for check in CHECKS:
        setup.add_argument("--" + check.replace("_", "-"), action="store_true", required=True)
    args = parser.parse_args()
    try:
        directory = args.directory.absolute()
        if args.action == "prepare":
            prepare(
                directory,
                initialize=args.initialize,
                console_port=args.console_port,
                opc_port=args.opc_port,
            )
        elif args.action == "serve":
            serve(
                directory,
                token_file=args.token_file,
                api_url=args.api_url,
                allow_local_test=args.allow_local_test,
                measured_clock=args.clock_measured_round_trip,
            )
        else:
            commission(
                directory,
                password_file=args.password_file,
                new_password_file=args.new_password_file,
                checks={name: getattr(args, name) for name in CHECKS},
            )
    except Exception:
        print(
            "Runtime operation failed; inspect private configuration and existing state.",
            file=sys.stderr,
        )
        return 1
    print("Runtime operation completed; no production command submitted.")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
