"""Shared immutable evidence envelope for combined acceptance scenarios."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]


def utc_now():
    return datetime.now(UTC).isoformat()


def source_manifest():
    """Digest executed backend/contract/test sources, including untracked runners."""
    entries = {}
    for directory in (
        "apps/api",
        "apps/gateway",
        "apps/simulator",
        "contracts",
        "deploy",
        "tests/e2e",
    ):
        for path in sorted((ROOT / directory).rglob("*")):
            if path.is_file() and path.suffix in {".py", ".json", ".yaml", ".yml", ".sql"}:
                entries[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return entries


def load_operable_script():
    """Reuse the existing CLI/browser driver without modifying Claude-owned files."""
    spec = importlib.util.spec_from_file_location(
        "acceptance_operable", ROOT / "scripts/check-operable-system.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AcceptanceEvidence:
    def __init__(self, case_id, artifact_dir):
        self.path = Path(artifact_dir) / f"{case_id}-{uuid4().hex}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = {
            "case_id": case_id,
            "acceptance_id": self.path.stem,
            "target_sha": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "dirty": bool(
                subprocess.check_output(
                    ["git", "status", "--porcelain"], cwd=ROOT, text=True
                ).strip()
            ),
            "started_at_utc": utc_now(),
            "runner_argv": sys.argv,
            "source_sha256": source_manifest(),
            "git_diff_sha256": hashlib.sha256(
                subprocess.check_output(["git", "diff", "--binary", "HEAD"], cwd=ROOT)
            ).hexdigest(),
            "commands": [],
            "executions": [],
            "events": [],
            "final_sequences": {},
            "reports": {},
            "assertions": [],
        }

    def record(self, **fields):
        self.data.update(fields)

    def check(self, name, condition, **actual):
        self.data["assertions"].append({"name": name, "passed": bool(condition), **actual})
        assert condition, f"{self.data['case_id']}: {name}: {actual}"

    def finish(self, outcome, *, failure=None):
        self.data.update(
            outcome=outcome,
            exit_code=0 if outcome == "passed" else 1,
            ended_at_utc=utc_now(),
            failure=failure,
        )
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, default=str) + "\n"
        )


@asynccontextmanager
async def boundary_network_runtime(tmp_path, migrated, *, clock, service):
    import asyncio
    import secrets
    from datetime import timedelta

    import uvicorn
    from test_gateway_pipeline import HTTPServer, free_port

    from apps.api.bootstrap import bootstrap_reference_data
    from apps.api.internal import InternalConfig, ServiceCredential, create_internal_app
    from apps.gateway.buffer import GatewayBuffer
    from apps.gateway.http_client import IntakeClient, IntakeConfig
    from apps.gateway.opcua_client import GatewayOPCUAClient, GatewayOPCUAConfig
    from apps.gateway.worker import GatewayPump
    from apps.simulator.certificates import (
        GATEWAY_URI,
        SERVER_URI,
        provision_development_certificates,
    )
    from apps.simulator.controller import SimulatorController
    from apps.simulator.opcua_server import OPCUAServerConfig, SimulatorOPCUAServer
    from apps.simulator.plc import PLCInputs, RecoveryChecks
    from apps.simulator.process import ProcessConfig
    from apps.simulator.storage import SimulatorStorage

    now = clock.now

    bootstrap_reference_data(migrated)
    certificates = tmp_path / "certs"
    provision_development_certificates(certificates)
    source = SimulatorStorage(
        tmp_path / "source.sqlite3",
        source_id="source",
        stream_id="stream",
        counter_epoch="epoch",
        utc_clock=clock.now,
        monotonic_clock=clock.monotonic,
    )
    runtime = SimulatorController(
        source,
        now=now(),
        process_config=ProcessConfig(
            stage_lengths_mm=(1, 1, 1),
            ambient_temperature_c=150.0,
            pattern=("GOOD", "GOOD", "REJECT"),
        ),
    )
    checks = RecoveryChecks(
        outputs_stopped=True,
        positions_verified=True,
        storage_reconciled=True,
        commands_reconciled=True,
        operator_confirmed=True,
    )
    runtime.recover(now=now(), operator_id="fixture-operator", checks=checks)
    runtime.set_inputs(
        PLCInputs(temperature_c=150.0), now=now(), control_mode="REMOTE", operation_mode="AUTO"
    )
    queue = GatewayBuffer(tmp_path / "gateway.sqlite3", gateway_id="gateway")
    identity = source.identity
    token = secrets.token_urlsafe(32)
    secret = tmp_path / "service-token"
    secret.write_text(token)
    secret.chmod(0o600)
    credential = ServiceCredential.from_secret_file(
        key_id="test-service",
        gateway_id=queue.gateway_id,
        source_id=identity.source_id,
        stream_id=identity.stream_id,
        secret_file=secret,
        expires_at=now() + timedelta(hours=1),
    )
    app = create_internal_app(
        service, [credential], config=InternalConfig(allow_insecure_loopback=True, clock=now)
    )
    http_port, opc_port = free_port(), free_port()
    web = HTTPServer(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=http_port,
            access_log=False,
            log_level="critical",
            proxy_headers=False,
            lifespan="off",
        )
    )
    web_task = asyncio.create_task(web.serve())
    opc = SimulatorOPCUAServer(
        runtime,
        OPCUAServerConfig(
            endpoint=f"opc.tcp://127.0.0.1:{opc_port}/hmi-mes/",
            application_uri=SERVER_URI,
            server_certificate=certificates / "server.der",
            server_private_key=certificates / "server.pem",
            trusted_client_certificate=certificates / "gateway.der",
        ),
    )
    client = GatewayOPCUAClient(
        GatewayOPCUAConfig(
            endpoint=f"opc.tcp://127.0.0.1:{opc_port}/hmi-mes/",
            application_uri=GATEWAY_URI,
            client_certificate=certificates / "gateway.der",
            client_private_key=certificates / "gateway.pem",
            trusted_server_certificate=certificates / "server.der",
            server_application_uri=SERVER_URI,
        ),
        utc_clock=clock.now,
        monotonic_clock=clock.monotonic,
    )
    intake = IntakeClient(
        IntakeConfig(f"http://127.0.0.1:{http_port}", token, allow_loopback_http=True)
    )
    try:
        for _ in range(100):
            if web.started:
                break
            if web_task.done():
                web_task.result()
            await asyncio.sleep(0.01)
        assert web.started
        await opc.start()
        await opc.publish_snapshot(acquired_at=now(), heartbeat=1)
        await client.connect()
        pump = GatewayPump(
            identity.source_id, identity.stream_id, opc=client, buffer=queue, intake=intake
        )
        yield runtime, opc, client, pump, service
    finally:
        await client.disconnect()
        await intake.close()
        await opc.stop()
        web.should_exit = True
        await asyncio.wait_for(web_task, timeout=5)
        queue.close()
        source.close()
