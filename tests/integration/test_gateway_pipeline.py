"""Actual OPC sockets -> gateway disk -> authenticated HTTP socket -> PostgreSQL commit."""

import asyncio
import secrets
import socket
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
import uvicorn

from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.ingestion import IngestionService
from apps.api.internal import InternalConfig, ServiceCredential, create_internal_app
from apps.gateway.buffer import GatewayBuffer
from apps.gateway.http_client import DeliveryUncertain, IntakeClient, IntakeConfig
from apps.gateway.opcua_client import GatewayOPCUAClient, GatewayOPCUAConfig
from apps.gateway.worker import GatewayPump
from apps.simulator.certificates import GATEWAY_URI, SERVER_URI, provision_development_certificates
from apps.simulator.controller import SimulatorController
from apps.simulator.opcua_server import OPCUAServerConfig, SimulatorOPCUAServer
from apps.simulator.plc import PLCInputs, RecoveryChecks
from apps.simulator.process import ProcessConfig
from apps.simulator.storage import SimulatorStorage
from contracts.models import CommandRequest, Parameters, RunSummaryRequest


def free_port():
    with socket.socket() as value:
        value.bind(("127.0.0.1", 0))
        return value.getsockname()[1]


class HTTPServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield


@pytest.fixture
async def pipeline(tmp_path, migrated):
    def now():
        return datetime.now(UTC)

    bootstrap_reference_data(migrated)
    service = IngestionService(migrated)
    certificates = tmp_path / "certs"
    provision_development_certificates(certificates)
    source = SimulatorStorage(tmp_path / "source.sqlite3")
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
    queue = GatewayBuffer(tmp_path / "gateway.sqlite3", gateway_id="gateway-1")
    identity = source.identity
    service.configure_source(
        source_id=identity.source_id,
        stream_id=identity.stream_id,
        gateway_id=queue.gateway_id,
        line_id=LINE_ID,
        capture_generation=queue.capture_position[0],
        now=now(),
    )
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
        service, [credential], config=InternalConfig(allow_insecure_loopback=True)
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
        )
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


def drive_request(runtime, request):
    """Bound simulation time by a real command result and the configured good target."""
    for _ in range(100):
        runtime.tick(now=datetime.now(UTC))
        result = runtime.get_result(request.command_id)
        assert result.status in {"ACCEPTED", "SUCCEEDED"}, result
        if request.command.kind == "START":
            process = runtime.storage.load_execution()
            assert process.input_quantity == (
                process.good_quantity
                + process.reject_quantity
                + process.scrap_quantity
                + process.wip_quantity
            )
            assert process.good_quantity + process.wip_quantity <= 4
            if process.good_quantity != 4 or process.wip_quantity != 0:
                continue
            assert runtime.state.run_state == "RUNNING"
        if result.status == "SUCCEEDED":
            return
    pytest.fail(f"{request.command.kind} did not reach its required boundary within 100 ticks")


def assert_final_target(summary):
    assert summary.final and summary.good_quantity == 4 and summary.wip_quantity == 0
    assert summary.input_quantity == (
        summary.good_quantity + summary.reject_quantity + summary.scrap_quantity
    )
    # Pinned inspection pattern: GOOD,GOOD,REJECT,GOOD,GOOD. The reject is replaced.
    assert (summary.input_quantity, summary.reject_quantity, summary.scrap_quantity) == (5, 1, 0)


async def make_finished_run(runtime, server, client):
    for index, kind in enumerate(["LOAD_JOB", "START", "END_RUN"], 1):
        args = (
            {
                "work_order_id": "order-A",
                "recipe_version": "recipe-A",
                "target_quantity": 4,
                "auto_end_at_target": False,
                "parameters": {"temperature_c": 150.0, "speed_m_per_min": 6.0},
            }
            if kind == "LOAD_JOB"
            else {}
        )
        request = CommandRequest(
            command_id=f"command-{kind}",
            target_id="Line",
            execution_id="run-A",
            requested_by="test-operator",
            requested_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(seconds=5),
            expected_control_version=runtime.state.control_version,
            command={"kind": kind, "args": args},
        )
        submission = await client.submit_command(request)
        assert submission.disposition == "ACCEPTED"
        drive_request(runtime, request)
        assert runtime.get_result(request.command_id).status == "SUCCEEDED"
        await server.publish_snapshot(acquired_at=datetime.now(UTC), heartbeat=index + 1)
    summary = await client.get_run_summary(RunSummaryRequest(execution_id="run-A"))
    assert_final_target(summary)
    return summary


def register(service, runtime):
    identity = runtime.storage.identity
    service.register_execution(
        execution_id="run-A",
        source_id=identity.source_id,
        stream_id=identity.stream_id,
        work_order_id="order-A",
        recipe_version="recipe-A",
        parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
        counter_epoch=identity.counter_epoch,
        now=datetime.now(UTC),
    )


async def test_full_network_path_retry_after_commit_does_not_double_count(pipeline):
    runtime, server, client, pump, service = pipeline
    summary = await make_finished_run(runtime, server, client)
    register(service, runtime)
    capture = await pump.capture_once()
    assert capture.sample_count == 48 and capture.event_count >= 4
    original = pump.intake.deliver

    async def lose_response_after_commit(batch):
        await original(batch)
        raise DeliveryUncertain("Injected response loss after actual PostgreSQL commit")

    pump.intake.deliver = lose_response_after_commit
    with pytest.raises(DeliveryUncertain):
        await pump.deliver_once()
    assert pump.buffer.status(now=datetime.now(UTC)).pending_batches == 1
    before = service.get_totals("run-A")
    pump.intake.deliver = original
    assert await pump.deliver_once()
    assert service.get_totals("run-A") == before
    assert before["good_quantity"] == summary.good_quantity
    assert before["reject_quantity"] == summary.reject_quantity
    assert before["scrap_quantity"] == summary.scrap_quantity
    progress = pump.buffer.progress(pump.source_id, pump.stream_id)
    assert progress.api_progress.verified_through_seq == summary.last_event_seq
    assert pump.buffer.status(now=datetime.now(UTC)).pending_batches == 0


async def test_api_outage_keeps_collecting_then_quarantine_replays_original_job(pipeline):
    runtime, server, client, pump, service = pipeline
    summary = await make_finished_run(runtime, server, client)
    await pump.capture_once()
    original = pump.intake.deliver

    async def offline(batch):
        raise DeliveryUncertain("Injected API outage")

    pump.intake.deliver = offline
    result = await pump.cycle()
    assert result.delivery_error == "DeliveryUncertain"
    assert result.capture.sample_count == 48
    assert pump.buffer.status(now=datetime.now(UTC)).pending_batches == 2
    pump.intake.deliver = original
    while await pump.deliver_once():
        pass
    assert service.get_totals("run-A") is None
    register(service, runtime)
    progress = service.replay_quarantined(
        source_id=pump.source_id, stream_id=pump.stream_id, now=datetime.now(UTC)
    )
    assert progress.verified_through_seq == summary.last_event_seq
    assert service.get_totals("run-A")["good_quantity"] == summary.good_quantity
    await pump.capture_once()
    assert await pump.deliver_once()
    assert pump.buffer.progress(pump.source_id, pump.stream_id).api_progress == progress


async def test_slow_api_wait_does_not_block_independent_opc_capture_loop(pipeline):
    from dataclasses import replace

    runtime, server, client, pump, service = pipeline
    await make_finished_run(runtime, server, client)
    register(service, runtime)
    await pump.capture_once()
    client.config = replace(client.config, poll_interval_seconds=0.05)
    entered = asyncio.Event()
    waiting = asyncio.Event()

    async def slow_response(batch):
        entered.set()
        await waiting.wait()
        raise DeliveryUncertain("Injected slow connection")

    pump.intake.deliver = slow_response
    stop = asyncio.Event()
    observed = []
    running = asyncio.create_task(pump.run(stop, on_status=observed.append))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        deadline = asyncio.get_running_loop().time() + 3
        while pump.buffer.status(now=datetime.now(UTC)).pending_batches < 4:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)
        assert sum(item["state"] == "collected" for item in observed) >= 3
    finally:
        stop.set()
        await asyncio.wait_for(running, timeout=2)
    assert pump.buffer.status(now=datetime.now(UTC)).inflight_batches == 0
    assert pump.buffer.status(now=datetime.now(UTC)).pending_batches >= 4


def journal_network_command(service, runtime, command_id, kind):
    from apps.api.auth import Principal
    from apps.api.commands import enqueue_command

    now = datetime.now(UTC)
    args = (
        {
            "work_order_id": "order-A",
            "recipe_version": "recipe-A",
            "target_quantity": 4,
            "auto_end_at_target": False,
            "parameters": {"temperature_c": 150.0, "speed_m_per_min": 6.0},
        }
        if kind == "LOAD_JOB"
        else {}
    )
    request = CommandRequest(
        command_id=command_id,
        target_id="Line",
        execution_id="run-A",
        requested_by="network-operator",
        requested_at=now,
        expires_at=now + timedelta(seconds=5),
        expected_control_version=runtime.state.control_version,
        command={"kind": kind, "args": args},
    )
    identity = runtime.storage.identity
    with service.engine.begin() as connection:
        enqueue_command(
            connection,
            request,
            principal=Principal("network-operator", frozenset({"operator"}), False),
            line_id=LINE_ID,
            source_id=identity.source_id,
            stream_id=identity.stream_id,
            now=now,
        )
    return request


def journal_result(service, command_id):
    from sqlalchemy import text

    with service.engine.connect() as connection:
        return connection.execute(
            text("SELECT result FROM command_requests WHERE command_id=:id"),
            {"id": command_id},
        ).scalar_one()


async def test_network_command_reservation_opc_execution_and_original_id_reconciliation(pipeline):
    from sqlalchemy import text

    from apps.gateway.commands import CommandTransport, CommandWorker
    from apps.gateway.opcua_client import GatewayMethodUncertain
    from apps.gateway.settlement import RunSummaryTransport, RunSummaryWorker

    runtime, server, client, pump, service = pipeline
    register(service, runtime)
    transport = CommandTransport(pump.intake.config)
    worker = CommandWorker(client, transport)
    summaries = RunSummaryTransport(pump.intake.config)
    collector = RunSummaryWorker(
        client, summaries, source_id=pump.source_id, stream_id=pump.stream_id
    )
    original_report = summaries.report
    reported_finals = []
    committed_receipts = []
    original = client.submit_command
    calls = []

    async def lose_actual_opc_submission_response(request):
        calls.append(request.command_id)
        await original(request)
        raise GatewayMethodUncertain("SubmitCommand", request.command_id, delivery_possible=True)

    client.submit_command = lose_actual_opc_submission_response
    try:
        for index, kind in enumerate(("LOAD_JOB", "START", "END_RUN"), 1):
            request = journal_network_command(service, runtime, "network-" + kind, kind)
            first = await worker.run_once()
            assert first.command_id == request.command_id
            drive_request(runtime, request)
            await server.publish_snapshot(acquired_at=datetime.now(UTC), heartbeat=index + 1)
            await worker.run_once()
            assert journal_result(service, request.command_id)["status"] == "SUCCEEDED"
            assert calls.count(request.command_id) == 1
        summary = await client.get_run_summary(RunSummaryRequest(execution_id="run-A"))
        assert_final_target(summary)

        async def lose_final_receipt_after_commit(value):
            # This calls the real authenticated HTTP socket and commits actual
            # PLC final evidence before simulating only the response loss.
            reported_finals.append(value)
            receipt = await original_report(value)
            committed_receipts.append(receipt)
            raise DeliveryUncertain("Injected final-summary receipt loss after PostgreSQL commit")

        def durable_settlement():
            with service.engine.connect() as connection:
                rows = (
                    connection.execute(
                        text("SELECT execution_id,status,final_summary FROM run_reconciliations")
                    )
                    .mappings()
                    .all()
                )
                assert len(rows) == 1
                assert rows[0]["execution_id"] == "run-A"
                return dict(rows[0])

        summaries.report = lose_final_receipt_after_commit
        first = await collector.run_once()
        assert first.queried == 1 and first.reported == 0 and first.uncertain == 1
        assert committed_receipts[0].execution_id == "run-A"
        assert committed_receipts[0].status == "PENDING"  # Original events have not arrived.
        pending = durable_settlement()
        assert pending["status"] == "PENDING"
        assert pending["final_summary"]["source_id"] == pump.source_id
        assert pending["final_summary"]["stream_id"] == pump.stream_id
        assert pending["final_summary"]["last_event_seq"] == str(summary.last_event_seq)

        async def record_original_final_retry(value):
            reported_finals.append(value)
            return await original_report(value)

        summaries.report = record_original_final_retry
        replay = await collector.run_once()
        assert replay.reported == 1 and replay.uncertain == 0
        assert durable_settlement()["status"] == "PENDING"
        assert len(reported_finals) == 2
        original_final = reported_finals[0].model_dump(mode="json", exclude={"as_of"})
        assert reported_finals[1].model_dump(mode="json", exclude={"as_of"}) == original_final
        assert original_final == summary.model_dump(mode="json", exclude={"as_of"})

        await pump.capture_once()
        assert await pump.deliver_once()
        totals = service.get_totals("run-A")
        assert totals["good_quantity"] == summary.good_quantity
        assert totals["reject_quantity"] == summary.reject_quantity
        assert totals["scrap_quantity"] == summary.scrap_quantity
        with service.engine.connect() as connection:
            event_count = connection.execute(
                text("SELECT count(*) FROM production_events")
            ).scalar_one()
        settled = await collector.run_once()
        assert settled.reported == 1 and settled.uncertain == 0
        assert durable_settlement()["status"] == "SETTLED"
        assert reported_finals[-1].model_dump(mode="json", exclude={"as_of"}) == original_final
        assert (await summaries.pending()).execution_ids == ()
        assert (await collector.run_once()).state == "idle"
        assert service.get_totals("run-A") == totals
        with service.engine.connect() as connection:
            assert (
                connection.execute(text("SELECT count(*) FROM production_events")).scalar_one()
                == event_count
            )
            assert (
                connection.execute(text("SELECT count(*) FROM run_summary_conflicts")).scalar_one()
                == 0
            )
        # Summary polling is read-only: none of the original control IDs was submitted twice.
        assert calls == ["network-LOAD_JOB", "network-START", "network-END_RUN"]
    finally:
        summaries.report = original_report
        client.submit_command = original
        await summaries.close()
        await transport.close()


async def test_lost_network_claim_never_submits_after_retry_or_expiry(pipeline):
    from apps.gateway.commands import CommandTransport, CommandWorker

    runtime, server, client, pump, service = pipeline
    register(service, runtime)
    request = journal_network_command(service, runtime, "lost-claim", "LOAD_JOB")
    transport = CommandTransport(pump.intake.config)
    worker = CommandWorker(client, transport)
    original_claim = transport.claim
    original_submit = client.submit_command
    submitted = []

    async def lose_committed_claim():
        await original_claim()
        raise DeliveryUncertain("Injected loss after actual HTTP reservation commit")

    async def record_submit(request):
        submitted.append(request.command_id)
        return await original_submit(request)

    transport.claim = lose_committed_claim
    client.submit_command = record_submit
    try:
        assert (await worker.run_once()).state == "uncertain"
        transport.claim = original_claim
        assert (await worker.run_once()).state == "reported"
        assert submitted == []
        result = journal_result(service, request.command_id)
        assert result["status"] == "OUTCOME_UNKNOWN"
        assert result["reason_code"] == "PLC_COMMAND_NOT_FOUND"
        assert runtime.state.execution_id is None
    finally:
        client.submit_command = original_submit
        await transport.close()


async def test_browser_next_load_waits_for_network_settlement_then_submits_once(pipeline):
    """Browser ASGI authorization -> PostgreSQL -> real gateway HTTP/OPC sockets."""
    import hashlib
    import json

    import httpx
    from sqlalchemy import text

    from apps.api.auth import UserAuth
    from apps.api.user_http import UserHTTPConfig, create_user_app
    from apps.gateway.commands import CommandTransport, CommandWorker
    from apps.gateway.settlement import RunSummaryTransport, RunSummaryWorker

    runtime, server, client, pump, service = pipeline
    register(service, runtime)
    commands = CommandTransport(pump.intake.config)
    worker = CommandWorker(client, commands)
    summaries = RunSummaryTransport(pump.intake.config)
    collector = RunSummaryWorker(
        client, summaries, source_id=pump.source_id, stream_id=pump.stream_id
    )
    original_submit = client.submit_command
    submitted = []

    async def track_submit(request):
        submitted.append(request.command_id)
        return await original_submit(request)

    async def fresh_capture(heartbeat):
        await server.publish_snapshot(acquired_at=datetime.now(UTC), heartbeat=heartbeat)
        assert (await pump.capture_once()).sample_count == 48
        assert client.health().state == "GOOD"
        assert await pump.deliver_once()

    client.submit_command = track_submit
    try:
        # This trusted fixture bypasses the browser gate only for the first job.
        # The second LOAD below must pass the real authenticated user adapter.
        for index, kind in enumerate(("LOAD_JOB", "START", "END_RUN"), 1):
            request = journal_network_command(service, runtime, "first-" + kind, kind)
            assert (await worker.run_once()).command_id == request.command_id
            drive_request(runtime, request)
            await server.publish_snapshot(acquired_at=datetime.now(UTC), heartbeat=index + 1)
            await worker.run_once()
            assert journal_result(service, request.command_id)["status"] == "SUCCEEDED"
        old_summary = await client.get_run_summary(RunSummaryRequest(execution_id="run-A"))
        assert_final_target(old_summary)
        old_final = old_summary.model_dump(mode="json", exclude={"as_of"})
        identity = runtime.storage.identity
        service.register_execution(
            execution_id="run-B",
            source_id=identity.source_id,
            stream_id=identity.stream_id,
            work_order_id="order-B",
            recipe_version="recipe-B",
            target_quantity=4,
            parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
            counter_epoch=identity.counter_epoch,
            now=datetime.now(UTC),
        )

        origin = "http://127.0.0.1:8002"
        auth = UserAuth(service.engine, allowed_origins={origin})
        password = secrets.token_urlsafe(32)
        auth.bootstrap_user("browser-operator", password, roles={"operator"})
        app = create_user_app(auth, UserHTTPConfig(origin=origin, allow_insecure_loopback=True))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=origin
        ) as browser:
            challenge = await browser.post(
                "/auth/challenge", headers={"Origin": origin, "X-HMI-Request": "1"}
            )
            assert challenge.status_code == 200
            login = await browser.post(
                "/auth/login",
                headers={"Origin": origin, "X-CSRF-Token": challenge.json()["csrf_token"]},
                json={"login": "browser-operator", "password": password},
            )
            assert login.status_code == 200
            changed = await browser.post(
                "/auth/password",
                headers={"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]},
                json={"new_password": secrets.token_urlsafe(32)},
            )
            assert changed.status_code == 200
            protection = {"Origin": origin, "X-CSRF-Token": changed.json()["csrf_token"]}
            me = await browser.get("/auth/me")
            assert me.json() == {
                "login": "browser-operator",
                "roles": ["operator"],
                "must_change_password": False,
            }

            def next_load():
                now = datetime.now(UTC)
                return CommandRequest(
                    command_id="browser-load-B",
                    target_id="Line",
                    execution_id="run-B",
                    requested_by="browser-operator",
                    requested_at=now,
                    expires_at=now + timedelta(seconds=5),
                    expected_control_version=runtime.state.control_version,
                    command={
                        "kind": "LOAD_JOB",
                        "args": {
                            "work_order_id": "order-B",
                            "recipe_version": "recipe-B",
                            "target_quantity": 4,
                            "auto_end_at_target": False,
                            "parameters": {"temperature_c": 150.0, "speed_m_per_min": 6.0},
                        },
                    },
                )

            def body(request):
                return {
                    "line_id": LINE_ID,
                    "source_id": pump.source_id,
                    "stream_id": pump.stream_id,
                    "request": request.model_dump(mode="json"),
                }

            # All events, cursor, quality and 48 fresh samples are already valid.
            # Missing final settlement alone must keep this browser LOAD blocked.
            await fresh_capture(5)
            assert service.get_progress(pump.source_id, pump.stream_id).verified_through_seq == (
                old_summary.last_event_seq
            )
            blocked = await browser.post(
                "/api/v1/commands", headers=protection, json=body(next_load())
            )
            assert blocked.status_code == 409
            with service.engine.connect() as connection:
                assert (
                    connection.execute(
                        text(
                            "SELECT count(*) FROM command_requests "
                            "WHERE command_id='browser-load-B'"
                        )
                    ).scalar_one()
                    == 0
                )
                assert (
                    connection.execute(
                        text(
                            "SELECT count(*) FROM command_request_audit "
                            "WHERE command_id='browser-load-B'"
                        )
                    ).scalar_one()
                    == 0
                )
            assert submitted == ["first-LOAD_JOB", "first-START", "first-END_RUN"]

            settled = await collector.run_once()
            assert settled.reported == 1 and settled.skipped == 1 and settled.uncertain == 0
            with service.engine.connect() as connection:
                previous = (
                    connection.execute(
                        text(
                            "SELECT status,final_summary FROM run_reconciliations "
                            "WHERE execution_id='run-A'"
                        )
                    )
                    .mappings()
                    .one()
                )
                assert previous["status"] == "SETTLED"
                durable_final = dict(previous["final_summary"])

            await fresh_capture(6)
            request = next_load()
            accepted = await browser.post(
                "/api/v1/commands", headers=protection, json=body(request)
            )
            assert accepted.status_code == 200 and accepted.json()["status"] == "REQUESTED"
            # A repeated browser response does not create another dispatch reservation.
            assert (
                await browser.post("/api/v1/commands", headers=protection, json=body(request))
            ).status_code == 200
            assert (await worker.run_once()).command_id == request.command_id
            runtime.tick(now=datetime.now(UTC))
            await server.publish_snapshot(acquired_at=datetime.now(UTC), heartbeat=7)
            assert (await worker.run_once()).command_id == request.command_id
            assert (await worker.run_once()).state == "idle"
            assert journal_result(service, request.command_id)["status"] == "SUCCEEDED"
            assert submitted.count(request.command_id) == 1
            assert runtime.state.execution_id == "run-B"

            status = await browser.get(
                "/api/v1/commands/" + request.command_id,
                params={
                    "line_id": LINE_ID,
                    "source_id": pump.source_id,
                    "stream_id": pump.stream_id,
                },
            )
            assert status.status_code == 200
            assert status.json()["result"]["status"] == "SUCCEEDED"
            with service.engine.connect() as connection:
                audit = connection.execute(
                    text(
                        "SELECT actor,action FROM command_request_audit "
                        "WHERE command_id=:id ORDER BY id"
                    ),
                    {"id": request.command_id},
                ).all()
                assert audit[0] == ("browser-operator", "REQUESTED")
                assert sum(action == "REQUESTED" for _, action in audit) == 1
                assert sum(action == "DISPATCH_RESERVED" for _, action in audit) == 1
                assert {actor for actor, action in audit if action != "REQUESTED"} == {"gateway-1"}
                assert (
                    connection.execute(
                        text(
                            "SELECT final_summary FROM run_reconciliations "
                            "WHERE execution_id='run-A'"
                        )
                    ).scalar_one()
                    == durable_final
                )

            old_after = await client.get_run_summary(RunSummaryRequest(execution_id="run-A"))
            assert old_after.model_dump(mode="json", exclude={"as_of"}) == old_final
            attestation = runtime.storage.connection.execute(
                "SELECT details_json FROM runtime_audit WHERE cause='REMOTE_SETTLEMENT_ATTESTATION'"
            ).fetchall()
            assert len(attestation) == 1
            evidence = json.loads(attestation[0][0])
            assert evidence["command_id"] == request.command_id
            assert evidence["authenticated_gateway_principal"] == server.config.gateway_principal
            assert evidence["asserted_requested_by"] == "browser-operator"
            assert evidence["previous_execution_id"] == "run-A"
            assert evidence["previous_source_id"] == pump.source_id
            assert evidence["previous_stream_id"] == pump.stream_id
            assert evidence["previous_counter_epoch"] == identity.counter_epoch
            canonical = json.dumps(
                old_final, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            assert (
                evidence["canonical_final_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
            )
    finally:
        client.submit_command = original_submit
        await summaries.close()
        await commands.close()


async def test_managed_two_job_binding_replaces_only_the_exact_settled_reservation(pipeline):
    """User publication/binding -> real gateway HTTP/OPC -> exact prior settlement."""
    import httpx
    from sqlalchemy import text

    from apps.api.auth import UserAuth
    from apps.api.user_http import UserHTTPConfig, create_user_app
    from apps.gateway.commands import CommandTransport, CommandWorker
    from apps.gateway.settlement import RunSummaryTransport, RunSummaryWorker

    runtime, server, client, pump, service = pipeline
    origin = "https://managed.example.test"
    auth = UserAuth(service.engine, allowed_origins={origin})
    password = secrets.token_urlsafe(32)
    auth.bootstrap_user("planner", password, roles={"production_manager", "operator"})
    challenge = auth.begin_login()
    session = auth.login(
        "planner",
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=origin,
    )
    session = auth.change_password(
        session.token, secrets.token_urlsafe(32), csrf_token=session.csrf_token, origin=origin
    )
    protection = {"Origin": origin, "X-CSRF-Token": session.csrf_token}
    app = create_user_app(auth, UserHTTPConfig(origin=origin))
    commands = CommandTransport(pump.intake.config)
    worker = CommandWorker(client, commands)
    summaries = RunSummaryTransport(pump.intake.config)
    collector = RunSummaryWorker(
        client, summaries, source_id=pump.source_id, stream_id=pump.stream_id
    )
    original_submit = client.submit_command
    submitted = []
    heartbeat = 1

    async def track_submit(request):
        submitted.append(request.command_id)
        return await original_submit(request)

    async def capture():
        nonlocal heartbeat
        heartbeat += 1
        await server.publish_snapshot(acquired_at=datetime.now(UTC), heartbeat=heartbeat)
        assert (await pump.capture_once()).sample_count == 48
        assert client.health().state == "GOOD"
        assert await pump.deliver_once()

    def bind_intent(command):
        return {
            "command_id": command,
            "line_id": LINE_ID,
            "source_id": pump.source_id,
            "stream_id": pump.stream_id,
            "expected_work_order_version": "2",
            "expected_control_version": str(runtime.state.control_version),
            "auto_end_at_target": False,
        }

    async def dispatch(request):
        assert (await worker.run_once()).command_id == request.command_id
        drive_request(runtime, request)
        await capture()
        assert (await worker.run_once()).command_id == request.command_id
        assert journal_result(service, request.command_id)["status"] == "SUCCEEDED"

    client.submit_command = track_submit
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=origin,
            cookies={"hmi_session": session.token},
        ) as browser:
            recipe = await browser.post(
                "/api/v1/recipe-versions",
                headers=protection,
                json={
                    "id": "managed-v1",
                    "recipe_id": "managed",
                    "version": "v1",
                    "item_id": "item",
                    "parameters": {"temperature_c": 150.0, "speed_m_per_min": 6.0},
                },
            )
            assert recipe.status_code == 200
            for order in ("managed-A", "managed-B"):
                created = await browser.post(
                    "/api/v1/work-orders",
                    headers=protection,
                    json={
                        "id": order,
                        "item_id": "item",
                        "line_id": LINE_ID,
                        "recipe_version_id": "managed-v1",
                        "target_quantity": 4,
                        "planned_date": "2026-10-24",
                    },
                )
                assert created.status_code == 200 and created.json()["status"] == "DRAFT"
                released = await browser.post(
                    f"/api/v1/work-orders/{order}/release",
                    headers=protection,
                    json={
                        "release_id": "release-" + order,
                        "expected_row_version": "1",
                    },
                )
                assert released.status_code == 200
            await capture()
            first = await browser.post(
                "/api/v1/work-orders/managed-A/bind",
                headers=protection,
                json=bind_intent("managed-load-A"),
            )
            assert first.status_code == 200, first.text
            run_a = first.json()["execution_id"]
            await dispatch(CommandRequest.model_validate(first.json()["load_request"]))
            for kind in ("START", "END_RUN"):
                now = datetime.now(UTC)
                request = CommandRequest(
                    command_id="managed-" + kind,
                    target_id="Line",
                    execution_id=run_a,
                    requested_by="planner",
                    requested_at=now,
                    expires_at=now + timedelta(seconds=5),
                    expected_control_version=runtime.state.control_version,
                    command={"kind": kind},
                )
                accepted = await browser.post(
                    "/api/v1/commands",
                    headers=protection,
                    json={
                        "line_id": LINE_ID,
                        "source_id": pump.source_id,
                        "stream_id": pump.stream_id,
                        "request": request.model_dump(mode="json"),
                    },
                )
                assert accepted.status_code == 200, accepted.text
                await dispatch(request)
            final = await client.get_run_summary(RunSummaryRequest(execution_id=run_a))
            assert_final_target(final)
            original_final = final.model_dump(mode="json", exclude={"as_of"})
            second_intent = bind_intent("managed-load-B")
            blocked = await browser.post(
                "/api/v1/work-orders/managed-B/bind", headers=protection, json=second_intent
            )
            assert blocked.status_code == 409
            with service.engine.connect() as db:
                assert (
                    db.execute(
                        text("SELECT execution_id FROM line_execution_reservations")
                    ).scalar_one()
                    == run_a
                )
                assert (
                    db.execute(text("SELECT count(*) FROM production_executions")).scalar_one() == 1
                )
            assert (await collector.run_once()).reported == 1
            await capture()
            second = await browser.post(
                "/api/v1/work-orders/managed-B/bind", headers=protection, json=second_intent
            )
            assert second.status_code == 200, second.text
            replay = await browser.post(
                "/api/v1/work-orders/managed-B/bind", headers=protection, json=second_intent
            )
            assert replay.json() == second.json()
            run_b = second.json()["execution_id"]
            assert run_b != run_a
            await dispatch(CommandRequest.model_validate(second.json()["load_request"]))
            assert submitted == [
                "managed-load-A",
                "managed-START",
                "managed-END_RUN",
                "managed-load-B",
            ]
            assert runtime.state.execution_id == run_b
            after = await client.get_run_summary(RunSummaryRequest(execution_id=run_a))
            assert after.model_dump(mode="json", exclude={"as_of"}) == original_final
            with service.engine.connect() as db:
                assert (
                    db.execute(
                        text("SELECT execution_id FROM line_execution_reservations")
                    ).scalar_one()
                    == run_b
                )
                assert (
                    db.execute(
                        text("SELECT status FROM run_reconciliations WHERE execution_id=:id"),
                        {"id": run_a},
                    ).scalar_one()
                    == "SETTLED"
                )
                audit = db.execute(
                    text(
                        "SELECT actor,action FROM command_request_audit "
                        "WHERE command_id='managed-load-B' ORDER BY id"
                    )
                ).all()
                assert audit[0] == ("planner", "REQUESTED")
                assert {actor for actor, action in audit if action != "REQUESTED"} == {"gateway-1"}
    finally:
        client.submit_command = original_submit
        await summaries.close()
        await commands.close()
