"""Actual CLI/OPC/HTTP sockets; HTTP journal is a test boundary, not PostgreSQL proof."""

import asyncio
import secrets
import socket
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from apps.gateway.__main__ import parser
from apps.gateway.buffer import GatewayBuffer
from apps.simulator.certificates import GATEWAY_URI, SERVER_URI, provision_development_certificates
from apps.simulator.controller import SimulatorController
from apps.simulator.opcua_server import OPCUAServerConfig, SimulatorOPCUAServer
from apps.simulator.plc import PLCInputs, RecoveryChecks
from apps.simulator.storage import SimulatorStorage
from contracts.models import CommandNotFound, CommandRequest, RunSummary

ROOT = Path(__file__).resolve().parents[2]


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class HTTPServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield


def cli_arguments(tmp_path, source, token, opc_port, http_port):
    certs = tmp_path / "certs"
    return [
        "serve",
        "--gateway-id",
        "gateway-cli",
        "--source-id",
        source.identity.source_id,
        "--stream-id",
        source.identity.stream_id,
        "--queue-file",
        str(tmp_path / "queue.sqlite3"),
        "--opc-endpoint",
        f"opc.tcp://127.0.0.1:{opc_port}/hmi-mes/",
        "--application-uri",
        GATEWAY_URI,
        "--server-application-uri",
        SERVER_URI,
        "--client-certificate",
        str(certs / "gateway.der"),
        "--client-private-key",
        str(certs / "gateway.pem"),
        "--trusted-server-certificate",
        str(certs / "server.der"),
        "--api-url",
        f"http://127.0.0.1:{http_port}",
        "--allow-loopback-http",
        "--token-file",
        str(token),
        "--poll-interval",
        "0.1",
    ]


def finished_run(controller):
    controller.set_inputs(
        PLCInputs(temperature_c=150.0),
        now=datetime.now(UTC),
        control_mode="REMOTE",
        operation_mode="AUTO",
    )
    for kind in ("LOAD_JOB", "END_RUN"):
        args = (
            {
                "work_order_id": "fixture-work",
                "recipe_version": "fixture-recipe",
                "target_quantity": 1,
                "parameters": {"temperature_c": 150.0, "speed_m_per_min": 6.0},
            }
            if kind == "LOAD_JOB"
            else {}
        )
        now = datetime.now(UTC)
        request = CommandRequest(
            command_id=f"fixture-{kind}",
            execution_id="original-run",
            target_id="Line",
            requested_by="test-fixture",
            requested_at=now,
            expires_at=now + timedelta(seconds=10),
            expected_control_version=controller.state.control_version,
            command={"kind": kind, "args": args},
        )
        assert controller.submit(request, now=now).submission.disposition == "ACCEPTED"
        controller.tick(now=now)
        assert controller.get_result(request.command_id).status == "SUCCEEDED"
    summary = controller.get_summary("original-run")
    assert summary.final and summary.wip_quantity == 0
    return summary


@pytest.mark.parametrize(
    "enabled,settlement_enabled,wrong_source,pending_stall",
    [
        (False, False, False, False),
        (True, False, False, False),
        (False, True, False, False),
        (True, True, False, False),
        (True, True, True, False),
        (True, True, False, True),
    ],
)
async def test_gateway_process_optional_loops_and_cleanup(
    tmp_path, enabled, settlement_enabled, wrong_source, pending_stall
):
    provision_development_certificates(tmp_path / "certs")
    secret = secrets.token_urlsafe(32)
    token = tmp_path / "token"
    token.write_text(secret)
    token.chmod(0o600)
    source = SimulatorStorage(tmp_path / "simulator.sqlite3")
    controller = SimulatorController(source, now=datetime.now(UTC))
    controller.recover(
        now=datetime.now(UTC),
        operator_id="test-fixture",
        checks=RecoveryChecks(
            outputs_stopped=True,
            positions_verified=True,
            storage_reconciled=True,
            commands_reconciled=True,
            operator_confirmed=True,
        ),
    )
    final_summary = finished_run(controller) if settlement_enabled else None
    initial_control_version = controller.state.control_version
    opc_port, http_port = free_port(), free_port()
    opc = SimulatorOPCUAServer(
        controller,
        OPCUAServerConfig(
            endpoint=f"opc.tcp://127.0.0.1:{opc_port}/hmi-mes/",
            application_uri=SERVER_URI,
            server_certificate=tmp_path / "certs/server.der",
            server_private_key=tmp_path / "certs/server.pem",
            trusted_client_certificate=tmp_path / "certs/gateway.der",
        ),
    )
    counts = {
        "batches": 0,
        "claims": 0,
        "results": 0,
        "submissions": 0,
        "pending": 0,
        "summaries": 0,
    }
    app = FastAPI()
    release_pending = asyncio.Event()

    @app.post("/internal/v1/batches")
    async def batch(request: Request):
        assert request.headers["authorization"] == f"Bearer {secret}"
        counts["batches"] += 1
        return JSONResponse({"detail": "test intake unavailable"}, status_code=503)

    @app.post("/internal/v1/commands/claim")
    async def claim(request: Request):
        assert request.headers["authorization"] == f"Bearer {secret}"
        assert await request.json() == {}
        counts["claims"] += 1
        return {"work": {"action": "QUERY", "request": {"command_id": "original-command"}}}

    @app.post("/internal/v1/commands/result")
    async def report(request: Request):
        result = CommandNotFound.model_validate(await request.json())
        assert result.command_id == "original-command"
        counts["results"] += 1
        return {"committed": True, "command_id": result.command_id}

    @app.post("/internal/v1/commands/submission")
    async def submission():
        counts["submissions"] += 1
        return JSONResponse({}, status_code=500)

    @app.post("/internal/v1/runs/pending")
    async def pending(request: Request):
        assert request.headers["authorization"] == f"Bearer {secret}"
        assert await request.json() == {"after_execution_id": None, "limit": 100}
        counts["pending"] += 1
        if pending_stall:
            await release_pending.wait()
        if counts["pending"] == 1:
            return JSONResponse({"detail": "test pending unavailable"}, status_code=503)
        return {"execution_ids": ["a-missing", "original-run"], "next_cursor": None}

    @app.post("/internal/v1/runs/summary")
    async def summary(request: Request):
        assert request.headers["authorization"] == f"Bearer {secret}"
        value = RunSummary.model_validate(await request.json())
        assert value == final_summary
        counts["summaries"] += 1
        if counts["summaries"] == 1:
            return JSONResponse({"detail": "test report unavailable"}, status_code=503)
        return {
            "committed": True,
            "execution_id": value.execution_id,
            "status": "PENDING",
            "reason_code": "EVENTS_PENDING",
        }

    web = HTTPServer(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=http_port,
            log_level="critical",
            access_log=False,
            proxy_headers=False,
            lifespan="off",
        )
    )
    web_task, publisher, process = None, None, None
    output, errors = b"", b""
    try:
        await opc.start()
        web_task = asyncio.create_task(web.serve())
        for _ in range(100):
            if web.started:
                break
            await asyncio.sleep(0.01)
        assert web.started

        async def publish():
            heartbeat = 1
            while True:
                await opc.publish_snapshot(acquired_at=datetime.now(UTC), heartbeat=heartbeat)
                heartbeat += 1
                await asyncio.sleep(0.1)

        publisher = asyncio.create_task(publish())
        args = cli_arguments(tmp_path, source, token, opc_port, http_port)
        assert parser().parse_args(args).enable_commands is False
        assert parser().parse_args(args).enable_settlement is False
        if enabled:
            args.append("--enable-commands")
        if settlement_enabled:
            args.append("--enable-settlement")
        if wrong_source:
            args[args.index("--source-id") + 1] = "wrong-source"
        process = subprocess.Popen(
            [sys.executable, "-m", "apps.gateway", *args],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = asyncio.get_running_loop().time() + 15
        while True:
            if wrong_source and process.poll() is not None:
                break
            assert process.poll() is None, "Gateway exited before the socket check completed"
            if (
                counts["batches"] >= 2
                and (not enabled or counts["results"] >= 2)
                and (
                    not settlement_enabled
                    or (counts["pending"] >= 1 if pending_stall else counts["summaries"] >= 2)
                )
            ):
                break
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail("Gateway command/collection socket check timed out")
            await asyncio.sleep(0.05)
        if not wrong_source:
            assert counts["submissions"] == 0
            if not enabled:
                assert counts["claims"] == counts["results"] == 0
            if not settlement_enabled:
                assert counts["pending"] == counts["summaries"] == 0
            assert controller.state.control_version == initial_control_version
    finally:
        shutdown_started = asyncio.get_running_loop().time()
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                output, errors = await asyncio.to_thread(process.communicate, timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                output, errors = await asyncio.to_thread(process.communicate, timeout=5)
        shutdown_seconds = asyncio.get_running_loop().time() - shutdown_started
        release_pending.set()
        if publisher is not None:
            publisher.cancel()
            await asyncio.gather(publisher, return_exceptions=True)
        web.should_exit = True
        if web_task is not None:
            await web_task
        await opc.stop()
        source.close()
    assert process.returncode == (1 if wrong_source else 0)
    assert secret.encode() not in output + errors
    if pending_stall:
        assert shutdown_seconds < 3, "Shutdown waited for a stalled settlement HTTP response"
        assert counts["summaries"] == 0
    if enabled and not wrong_source:
        assert b'"operation": "command"' in output
        assert b'"state": "reported"' in output
    if settlement_enabled and not wrong_source and not pending_stall:
        assert b'"operation": "settlement", "state": "uncertain"' in output
        assert b'"operation": "settlement", "state": "polled"' in output
    with GatewayBuffer(tmp_path / "queue.sqlite3", gateway_id="gateway-cli") as reopened:
        if not wrong_source:
            assert reopened.status(now=datetime.now(UTC)).pending_batches >= 2
