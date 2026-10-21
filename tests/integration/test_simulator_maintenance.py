"""Opt-in same-loop scheduling, failure supervision and actual CLI shutdown."""

import asyncio
import logging
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from apps.simulator import __main__ as launcher
from apps.simulator.certificates import provision_development_certificates
from apps.simulator.controller import SimulatorController
from apps.simulator.plc import RecoveryChecks
from apps.simulator.storage import (
    RecoveryRequired,
    RetentionBlocked,
    RetentionResult,
    SimulatorStorage,
    StorageCorruption,
)

ROOT = Path(__file__).resolve().parents[2]
CHECKS = RecoveryChecks(
    outputs_stopped=True,
    positions_verified=True,
    storage_reconciled=True,
    commands_reconciled=True,
    operator_confirmed=True,
)


class Clock:
    def __init__(self):
        self.utc = datetime(2026, 10, 21, tzinfo=UTC)
        self.mono = 0.0

    async def sleep(self, seconds):
        self.utc += timedelta(seconds=seconds)
        self.mono += seconds
        await asyncio.sleep(0)


async def test_scheduled_real_sqlite_startup_hold_is_60_seconds_and_does_not_stop_ticks(
    tmp_path,
    monkeypatch,
    caplog,
):
    clock = Clock()
    with SimulatorStorage(
        tmp_path / "held.sqlite3", utc_clock=lambda: clock.utc, monotonic_clock=lambda: clock.mono
    ) as store:
        controller = SimulatorController(store, now=clock.utc)
        controller.recover(now=clock.utc, operator_id="operator", checks=CHECKS)
        before = [tuple(row) for row in store.connection.execute("SELECT * FROM source_events")]
        calls = []
        stop = False
        prune = controller.prune_events
        owner = threading.get_ident()

        def poll(*, limit):
            nonlocal stop
            assert threading.get_ident() == owner
            calls.append((clock.mono, limit))
            if len(calls) == 3:
                stop = True
            return prune(limit=limit)

        monkeypatch.setattr(controller, "prune_events", poll)
        ticks = 0

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0)

        with caplog.at_level(logging.WARNING):
            await asyncio.gather(
                launcher._retention_maintenance(
                    controller,
                    should_exit=lambda: stop,
                    monotonic=lambda: clock.mono,
                    sleep=clock.sleep,
                ),
                ticker(),
            )
        assert calls == [(0, 1000), (60, 1000), (120, 1000)]
        assert ticks >= 2
        assert caplog.text.count("Simulator retention blocked (STARTUP_HOLD)") == 1
        assert [
            tuple(row) for row in store.connection.execute("SELECT * FROM source_events")
        ] == before


async def test_expected_maintenance_blocks_log_only_bounded_codes_and_continue(caplog):
    clock = Clock()
    outcomes = iter(
        [
            RetentionBlocked("private/path: secret-value"),
            RetentionBlocked("UNFINISHED_EXECUTION: private SQL or credential"),
            RecoveryRequired("hidden session token"),
            RetentionResult(0, 0, 1),
        ]
    )
    calls = []

    class Controller:
        def prune_events(self, *, limit):
            calls.append(limit)
            result = next(outcomes)
            if isinstance(result, Exception):
                raise result
            return result

    with caplog.at_level(logging.INFO):
        await launcher._retention_maintenance(
            Controller(),
            should_exit=lambda: len(calls) == 4,
            monotonic=lambda: clock.mono,
            sleep=clock.sleep,
        )
    assert calls == [1000] * 4
    for code in ("BLOCKED", "UNFINISHED_EXECUTION", "RECOVERY_REQUIRED"):
        assert f"blocked ({code})" in caplog.text
    for secret in ("private/path", "secret-value", "private SQL", "hidden session token"):
        assert secret not in caplog.text


async def test_maintenance_cancellation_stops_before_another_sqlite_poll(tmp_path, monkeypatch):
    clock = Clock()
    sleeping = asyncio.Event()
    with SimulatorStorage(
        tmp_path / "cancel.sqlite3", utc_clock=lambda: clock.utc, monotonic_clock=lambda: clock.mono
    ) as store:
        controller = SimulatorController(store, now=clock.utc)
        calls = []
        prune = controller.prune_events

        def poll(*, limit):
            calls.append(limit)
            return prune(limit=limit)

        monkeypatch.setattr(controller, "prune_events", poll)

        async def sleep(seconds):
            sleeping.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(
            launcher._retention_maintenance(
                controller,
                should_exit=lambda: False,
                monotonic=lambda: clock.mono,
                sleep=sleep,
            )
        )
        await asyncio.wait_for(sleeping.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == [1000]


def _port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _arguments(database, certs, http_port, opc_port, *, enabled):
    args = [
        "serve",
        "--database",
        str(database),
        "--host",
        "127.0.0.1",
        "--port",
        str(http_port),
        "--origin",
        f"http://127.0.0.1:{http_port}",
        "--opc-endpoint",
        f"opc.tcp://127.0.0.1:{opc_port}/hmi-mes/",
        "--server-certificate",
        str(certs / "server.der"),
        "--server-private-key",
        str(certs / "server.pem"),
        "--trusted-client-certificate",
        str(certs / "gateway.der"),
    ]
    return [*args, "--enable-retention"] if enabled else args


@pytest.mark.parametrize("enabled", [False, True])
def test_actual_cli_retention_opt_in_and_sigterm_keep_original_history(tmp_path, enabled):
    database = tmp_path / "runtime.sqlite3"
    certs = tmp_path / "certificates"
    provision_development_certificates(certs)
    with SimulatorStorage(database) as store:
        controller = SimulatorController(store, now=datetime.now(UTC))
        controller.recover(now=datetime.now(UTC), operator_id="operator", checks=CHECKS)
        original = {
            row[0]: tuple(row) for row in store.connection.execute("SELECT * FROM source_events")
        }
    http_port, opc_port = _port(), _port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "apps.simulator",
            *_arguments(
                database,
                certs,
                http_port,
                opc_port,
                enabled=enabled,
            ),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        with httpx.Client(
            base_url=f"http://127.0.0.1:{http_port}", trust_env=False, timeout=1
        ) as client:
            while True:
                assert process.poll() is None, "Simulator exited before HTTP readiness"
                try:
                    response = client.get("/auth/me")
                    break
                except httpx.TransportError:
                    if time.monotonic() >= deadline:
                        pytest.fail("Simulator HTTP readiness timed out")
                    time.sleep(0.05)
            assert response.status_code == 401
            with socket.create_connection(("127.0.0.1", opc_port), timeout=1):
                pass
    finally:
        process.terminate()
        try:
            output, errors = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            output, errors = process.communicate(timeout=5)
    assert process.returncode == 0
    assert (b"Simulator retention blocked (RECOVERY_REQUIRED)" in output + errors) is enabled
    with SimulatorStorage(database) as store:
        retained = {
            row[0]: tuple(row) for row in store.connection.execute("SELECT * FROM source_events")
        }
        assert all(retained[key] == value for key, value in original.items())
        assert (
            store.connection.execute("SELECT count(*) FROM archived_event_keys").fetchone()[0] == 0
        )
        assert store.recovery_required


def test_unexpected_retention_corruption_stops_service_before_storage_close(
    tmp_path, monkeypatch, caplog
):
    import uvicorn

    import apps.simulator.opcua_server as opc_module

    events = []
    tasks = []
    database = tmp_path / "corrupt.sqlite3"
    certs = tmp_path / "certificates"
    provision_development_certificates(certs)

    class Storage(SimulatorStorage):
        def prune_events(self, **kwargs):
            tasks.append(asyncio.current_task())
            with self.transaction():
                events.append("retention_corruption")
                raise StorageCorruption("secret SQL details must not be logged")

        def close(self):
            events.append("storage_closed")
            super().close()

    class OPC:
        def __init__(self, controller, config, *, field_channels):
            assert field_channels.storage is controller.storage
            self.controller = controller

        async def start(self):
            events.append("opc_started")

        async def publish_snapshot(self, **kwargs):
            tasks.append(asyncio.current_task())

        async def stop(self):
            assert self.controller.storage.connection.execute("SELECT 1").fetchone()[0] == 1
            events.append("opc_stopped")

    class HTTP:
        def __init__(self, config):
            self.should_exit = False

        async def serve(self):
            tasks.append(asyncio.current_task())
            try:
                while not self.should_exit:
                    await asyncio.sleep(0)
            finally:
                events.append("http_stopped")

    monkeypatch.setattr(launcher, "SimulatorStorage", Storage)
    monkeypatch.setattr(opc_module, "SimulatorOPCUAServer", OPC)
    monkeypatch.setattr(uvicorn, "Server", HTTP)
    monkeypatch.setattr(
        sys, "argv", ["simulator", *_arguments(database, certs, _port(), _port(), enabled=True)]
    )
    with caplog.at_level(logging.ERROR):
        assert launcher.main() == 1
    assert "opc_started" in events
    assert "retention_corruption" in events
    assert all(task.done() for task in tasks)
    assert (
        events.index("http_stopped") < events.index("opc_stopped") < events.index("storage_closed")
    )
    assert "secret SQL details" not in caplog.text
    with SimulatorStorage(database) as store:
        assert store.recovery_required
