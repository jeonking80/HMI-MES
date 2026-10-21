"""Real controller + OPC TLS + HTTP + PG global Line fault path.

App B authenticates cause
changes; App A authenticates ACK and both RESET requests. RESET uses the real
CommandWorker (current gateway command pump name), never direct PLC submission.
Only initial execution setup reuses the existing real OPC LOAD/START helper;
initial MES UI job release/binding/START is not exercised by this scenario.
The physical process ticks every 100ms independently of OPC publication.
This is simulator/transport integration, not equipment attribution/full T05/MES stop.
"""

import asyncio
import secrets
from contextlib import suppress
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text
from test_alarm_pipeline import begin_real_execution, http_server, now
from test_gateway_pipeline import free_port
from test_gateway_pipeline import pipeline as pipeline

from apps.api.alarm_consumer import ConfiguredClockPolicy
from apps.api.alarm_coordinator import MixedAlarmChannel, MixedAlarmConsumerConfig
from apps.api.alarm_lifecycle import AlarmRule, AlarmScope
from apps.api.alarm_qualification import TemperatureRuleConfig
from apps.api.alarm_rules import PLCFaultRuleConfig
from apps.api.alarm_runtime import (
    MixedRuleOperation,
    MixedRuleProvisioningConfig,
    configure_alarm_rules,
    start_alarm_runtime,
)
from apps.api.auth import UserAuth
from apps.api.bootstrap import LINE_ID
from apps.api.ingestion import IngestionService
from apps.api.internal import InternalConfig, ServiceCredential, create_internal_app
from apps.api.user_http import UserHTTPConfig, create_user_app
from apps.gateway.commands import CommandTransport, CommandWorker
from apps.gateway.http_client import IntakeClient, IntakeConfig
from apps.simulator.auth import SimulatorAuth
from apps.simulator.console import ConsoleConfig
from apps.simulator.console import create_app as create_console_app
from contracts.models import CommandRequest, ReadEventsRequest
from contracts.transport import BatchBody, GatewayBatch


async def login(
    browser, origin, login_name, initial_password, *, simulator=False, new_password=None
):
    challenge = await browser.post(
        "/auth/challenge",
        headers={"Origin": origin, "X-Simulator-Request" if simulator else "X-HMI-Request": "1"},
    )
    assert challenge.status_code == 200, challenge.text
    logged = await browser.post(
        "/auth/login",
        json={"login": login_name, "password": initial_password},
        headers={"Origin": origin, "X-CSRF-Token": challenge.json()["csrf_token"]},
    )
    assert logged.status_code == 200, logged.text
    changed = await browser.post(
        "/auth/password",
        json={"new_password": new_password or secrets.token_urlsafe(24)},
        headers={"Origin": origin, "X-CSRF-Token": logged.json()["csrf_token"]},
    )
    assert changed.status_code == 200, changed.text
    return {"Origin": origin, "X-CSRF-Token": changed.json()["csrf_token"]}


def counts(engine):
    # Read-only assertions; never insert/update fault events or qualified conditions.
    with engine.connect() as db:
        return {
            name: db.execute(text("SELECT count(*) FROM alarm_" + name)).scalar_one()
            for name in ("instances", "actions", "outbox", "capture_decisions")
        }


async def test_actual_global_fault_ack_app_a_reset_command_worker_and_source_proof(
    pipeline, tmp_path
):
    controller, opc_server, opc, pump, bootstrap_service = pipeline
    engine = bootstrap_service.engine
    binding = dict(line_id=LINE_ID, source_id=pump.source_id, stream_id=pump.stream_id)
    line_scope = AlarmScope(**binding, asset_id="Line")
    fault_rule = PLCFaultRuleConfig(
        rule=AlarmRule(rule_id="global-plc-fault", version="v1", severity="HIGH")
    )
    analog_rule = TemperatureRuleConfig(
        rule=AlarmRule(rule_id="healthy-temperature", version="v1", severity="HIGH"),
        setpoint_c=150.0,
    )
    bootstrap = await pump.capture_once()
    assert bootstrap.sample_count == 48 and await pump.deliver_once()
    token = pump.intake.config.token
    async with httpx.AsyncClient(
        base_url=pump.intake.config.base_url, trust_env=False
    ) as discovery:
        discovered = await discovery.get(
            "/internal/v1/alarm-clock-context", headers={"Authorization": "Bearer " + token}
        )
    assert discovered.status_code == 200
    epoch = discovered.json()["latest_capture"]["capture_time"]["clock_epoch"]
    generation = pump.buffer.capture_position[0]
    user_port, simulator_port, intake_port = free_port(), free_port(), free_port()
    user_origin, simulator_origin = (
        f"http://127.0.0.1:{user_port}",
        f"http://127.0.0.1:{simulator_port}",
    )
    auth = UserAuth(engine, allowed_origins={user_origin})
    initial = secrets.token_urlsafe(24)
    auth.bootstrap_user("fault-operator", initial, roles={"operator", "equipment_admin"})
    user_app = create_user_app(
        auth, UserHTTPConfig(origin=user_origin, allow_insecure_loopback=True)
    )
    simulator_auth = SimulatorAuth(
        controller.storage.connection, allowed_origins={simulator_origin}, clock=now
    )
    simulator_password = secrets.token_urlsafe(24)
    with controller.storage.transaction():
        simulator_auth.initialize_schema()
        simulator_auth.bootstrap_user(
            "plant-operator", simulator_password, role="simulator_operator"
        )
    simulator_app = create_console_app(
        controller,
        simulator_auth,
        ConsoleConfig(origin=simulator_origin, allow_insecure_loopback=True),
    )
    config = MixedAlarmConsumerConfig(
        kind="MIXED_ALARM_RUNTIME",
        **binding,
        gateway_id=pump.buffer.gateway_id,
        capture_generation=generation,
        channels=(
            MixedAlarmChannel(asset_id="Line", config=fault_rule),
            MixedAlarmChannel(asset_id="Sealer", config=analog_rule),
        ),
        clock_policy=ConfiguredClockPolicy(
            policy_id="local-shared-kernel",
            policy_version="v1",
            reference_id="same-kernel-utc",
            method="CONFIGURED_API_REFERENCE",
            guarantee_kind="SAME_HOST_SHARED_KERNEL_UTC",
            guarantee_basis=(
                "These API/gateway processes read the same host kernel UTC clock; "
                "original UTC read bracket uncertainty is retained."
            ),
            gateway_id=pump.buffer.gateway_id,
            source_id=pump.source_id,
            stream_id=pump.stream_id,
            capture_generation=generation,
            clock_epoch=epoch,
            valid_from_api_utc=now() - timedelta(seconds=10),
            valid_until_api_utc=now() + timedelta(minutes=5),
            offset_lower_seconds=0.0,
            offset_upper_seconds=0.0,
        ),
    )
    async with (
        http_server(user_app, user_port),
        http_server(simulator_app, simulator_port),
        httpx.AsyncClient(base_url=user_origin, trust_env=False) as browser,
        httpx.AsyncClient(base_url=simulator_origin, trust_env=False) as plant_browser,
    ):
        password = secrets.token_urlsafe(24)
        protection = await login(
            browser, user_origin, "fault-operator", initial, new_password=password
        )
        plant_protection = await login(
            plant_browser, simulator_origin, "plant-operator", simulator_password, simulator=True
        )
        # Actual runtime provisioning authenticates the equipment administrator,
        # preserves one atomic rule file, and revokes its own temporary session.
        configure_alarm_rules(
            auth,
            login="fault-operator",
            password=password,
            origin=user_origin,
            config=MixedRuleProvisioningConfig(
                kind="MIXED_ALARM_RULES",
                **binding,
                gateway_id=pump.buffer.gateway_id,
                capture_generation=generation,
                operations=(
                    MixedRuleOperation(
                        asset_id="Line",
                        operation_id="install-global-fault",
                        expected_version=None,
                        config=fault_rule,
                    ),
                    MixedRuleOperation(
                        asset_id="Sealer",
                        operation_id="install-healthy-temperature",
                        expected_version=None,
                        config=analog_rule,
                    ),
                ),
            ),
        )
        coordinator = start_alarm_runtime(
            engine,
            config,
            args=SimpleNamespace(
                gateway_id=pump.buffer.gateway_id,
                source_id=pump.source_id,
                stream_id=pump.stream_id,
                capture_generation=generation,
            ),
        )
        service = IngestionService(engine, alarm_consumer=coordinator)
        secret_file = tmp_path / "fault-intake-token"
        secret_file.write_text(token)
        secret_file.chmod(0o600)
        credential = ServiceCredential.from_secret_file(
            key_id="fault-intake",
            gateway_id=pump.buffer.gateway_id,
            source_id=pump.source_id,
            stream_id=pump.stream_id,
            secret_file=secret_file,
            expires_at=now() + timedelta(minutes=5),
        )
        internal_app = create_internal_app(
            service, [credential], config=InternalConfig(allow_insecure_loopback=True)
        )
        intake_config = IntakeConfig(
            f"http://127.0.0.1:{intake_port}", token, allow_loopback_http=True
        )
        intake, command_transport = IntakeClient(intake_config), CommandTransport(intake_config)
        command_worker = CommandWorker(opc, command_transport)
        old_intake, old_submit = pump.intake, opc.submit_command
        submitted = []
        heartbeat = 1
        ticks = 0
        ticker = None

        async def tracked_submit(request):
            submitted.append(request.command_id)
            return await old_submit(request)

        async def tick_process():
            nonlocal ticks
            deadline = asyncio.get_running_loop().time()
            while True:
                controller.tick(now=now())
                ticks += 1
                deadline += 0.1
                await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))

        async def capture():
            nonlocal heartbeat
            if ticker is not None and ticker.done():
                ticker.result()
            heartbeat += 1
            await opc_server.publish_snapshot(acquired_at=now(), heartbeat=heartbeat)
            result = await pump.capture_once()
            assert result.sample_count == 48 and await pump.deliver_once()
            with engine.connect() as db:
                row = (
                    db.execute(
                        text("SELECT body,content_sha256 FROM ingest_batches WHERE batch_id=:id"),
                        {"id": result.batch_id},
                    )
                    .mappings()
                    .one()
                )
            body = BatchBody.model_validate(row["body"])
            assert body.schema_version == "1.2" and body.capture_time.clock_epoch == epoch
            return GatewayBatch(body=body, content_sha256=row["content_sha256"])

        async def alarm_rows():
            response = await browser.get(
                "/api/v1/alarms",
                params=line_scope.model_dump()
                | {"rule_id": fault_rule.rule.rule_id, "view": "all"},
            )
            assert response.status_code == 200, response.text
            return response.json()["items"]

        async def await_alarm(status):
            for _ in range(12):
                original = await capture()
                rows = await alarm_rows()
                if len(rows) == 1 and rows[0]["status"] == status:
                    return rows[0], original
                await asyncio.sleep(0.2)
            pytest.fail("Line fault alarm did not reach expected state in bounded real captures")

        async def cause(code):
            # Preserve all current fields, including physical feedback. No readonly PV write.
            response = await plant_browser.post(
                "/inputs",
                headers=plant_protection,
                json={
                    "inputs": controller.state.inputs.model_dump(mode="json")
                    | {"fault_code": code},
                    "control_mode": "REMOTE",
                    "operation_mode": "AUTO",
                },
            )
            assert response.status_code == 200, response.text

        async def lookup(command_id):
            response = await browser.get(
                "/api/v1/command-record", params=binding | {"command_id": command_id}
            )
            assert response.status_code == 200, response.text
            return response.json()

        async def reset_via_app_a(command_id):
            await capture()  # complete fresh Good snapshot + exact current control version
            issued = now()
            request = CommandRequest(
                command_id=command_id,
                target_id="Line",
                execution_id="alarm-run",
                requested_by="fault-operator",
                requested_at=issued,
                expires_at=issued + timedelta(seconds=5),
                expected_control_version=controller.state.control_version,
                command={"kind": "RESET_FAULT", "args": {}},
            )
            response = await browser.post(
                "/api/v1/commands",
                headers=protection,
                json=binding | {"request": request.model_dump(mode="json")},
            )
            assert response.status_code == 200, response.text
            # No command retries or manual controller drive; only original-ID QUERY work.
            for _ in range(20):
                await capture()
                await command_worker.run_once()
                journal = await lookup(command_id)
                if journal["result"]["status"] in {"SUCCEEDED", "REJECTED", "FAILED", "EXPIRED"}:
                    return request, journal
                await asyncio.sleep(0.1)
            pytest.fail("Original RESET command did not reach terminal journal result")

        async with http_server(internal_app, intake_port):
            pump.intake = intake
            try:
                await begin_real_execution(controller, opc, service, pump)
                opc.submit_command = (
                    tracked_submit  # Count RESET delivery, after initial OPC setup.
                )
                ticker = asyncio.create_task(tick_process())
                await capture()
                await asyncio.sleep(0.3)
                assert controller.state.state == "RUNNING" and controller.state.inputs.run_feedback
                assert controller.storage.load_execution().good_quantity > 0
                await cause(123)
                row, original_fault_batch = await await_alarm("ACTIVE_UNACKNOWLEDGED")
                alarm_id = row["instance"]["alarm_id"]
                assert (
                    controller.state.execution_id == "alarm-run"
                    and controller.state.latched_fault_code == 123
                )
                assert counts(engine)["instances"] == counts(engine)["outbox"] == 1
                fault_version = controller.state.control_version
                ack = await browser.post(
                    "/api/v1/alarm-actions",
                    headers=protection,
                    json=dict(
                        action_id="fault-pipeline-ack",
                        scope=line_scope.model_dump(),
                        rule_id=fault_rule.rule.rule_id,
                        alarm_id=alarm_id,
                        actor="fault-operator",
                        kind="ACKNOWLEDGE",
                    ),
                )
                assert ack.status_code == 200, ack.text
                recovered = await browser.get(
                    "/api/v1/alarm-action-receipts",
                    params=line_scope.model_dump()
                    | {
                        "rule_id": fault_rule.rule.rule_id,
                        "alarm_id": alarm_id,
                        "action_id": "fault-pipeline-ack",
                    },
                )
                assert recovered.json() == ack.json()
                assert (
                    controller.state.latched_fault_code == 123
                    and controller.state.control_version == fault_version
                )
                rejected_request, rejected = await reset_via_app_a("fault-reset-cause-present")
                assert (
                    rejected["result"]["status"] == "REJECTED"
                    and rejected["result"]["reason_code"] == "RESET_BLOCKED"
                )
                assert submitted.count(rejected_request.command_id) == 1
                assert (await lookup(rejected_request.command_id)) == rejected
                assert (await alarm_rows())[0]["status"] == "ACTIVE_ACKNOWLEDGED"
                await cause(0)
                for _ in range(3):
                    await asyncio.sleep(0.1)
                    await capture()
                    assert controller.state.latched_fault_code == 123
                    assert (await alarm_rows())[0]["status"] == "ACTIVE_ACKNOWLEDGED"
                assert (await lookup(rejected_request.command_id)) == rejected
                successful_request, succeeded = await reset_via_app_a(
                    "fault-reset-explicit-after-clear"
                )
                assert succeeded["result"]["status"] == "SUCCEEDED"
                assert submitted == [rejected_request.command_id, successful_request.command_id]
                returned, _ = await await_alarm("RETURNED_ACKNOWLEDGED")
                assert returned["instance"]["alarm_id"] == alarm_id
                assert returned["instance"]["closure_reason"] == "NORMAL_RETURN"
                assert (
                    controller.state.execution_id == "alarm-run"
                    and controller.state.latched_fault_code == 0
                )
                for _ in range(3):
                    await asyncio.sleep(0.1)
                    await capture()
                    assert controller.state.state in {"STOPPED", "READY"}
                    assert (
                        not controller.state.run_output and not controller.state.inputs.run_feedback
                    )
                    assert controller.state.inputs.speed_m_per_min == 0
                # Exact original producer records, not fabricated conditions/fault rows.
                source_page = controller.read_events(
                    ReadEventsRequest(
                        source_id=pump.source_id, stream_id=pump.stream_id, after_seq=0, limit=1000
                    )
                )
                assert not source_page.has_more
                source_events = source_page.events
                by_id = {event.event_id: event for event in source_events}
                references = set(returned["instance"]["return_evidence"]["references"])
                original_pair = [
                    event
                    for event in source_events
                    if event.event_id in references
                    and event.kind in {"STATE_CHANGED", "FAULT_CHANGED"}
                ]
                clear = next(
                    event
                    for event in original_pair
                    if event.kind == "FAULT_CHANGED" and not event.payload.active
                )
                state_event = next(
                    event
                    for event in original_pair
                    if event.kind == "STATE_CHANGED" and event.seq + 1 == clear.seq
                )
                assert (
                    state_event.payload.previous_state == "FAULT"
                    and state_event.payload.state == "STOPPED"
                )
                assert state_event.execution_id == clear.execution_id == "alarm-run"
                assert state_event.occurred_at == clear.occurred_at
                assert state_event.payload.control_version == int(
                    succeeded["result"]["applied_control_version"]
                )
                assert succeeded["result"]["command_id"] == successful_request.command_id
                assert succeeded["result"]["evidence"]["execution_id"] == "alarm-run"
                assert clear.payload.fault_code == 123
                assert state_event.event_id in by_id and clear.event_id in by_id
                active_faults = [
                    event
                    for event in source_events
                    if event.kind == "FAULT_CHANGED"
                    and event.payload.active
                    and event.payload.fault_code == 123
                ]
                assert len(active_faults) == 1
                assert active_faults[0].event_id in row["instance"]["raised_evidence"]["references"]
                assert active_faults[0].execution_id == "alarm-run"
                assert (
                    active_faults[0].source_id
                    == state_event.source_id
                    == clear.source_id
                    == pump.source_id
                )
                assert (
                    active_faults[0].stream_id
                    == state_event.stream_id
                    == clear.stream_id
                    == pump.stream_id
                )
                with engine.connect() as db:
                    for event in (state_event, clear):
                        stored = db.execute(
                            text(
                                "SELECT body FROM event_inbox WHERE event_id=:id "
                                "AND applied_at IS NOT NULL"
                            ),
                            {"id": event.event_id},
                        ).scalar_one()
                        assert stored == event.model_dump(mode="json")
                before = counts(engine)
                await intake.deliver(original_fault_batch)
                assert counts(engine) == before
                assert (await alarm_rows())[0]["status"] == "RETURNED_ACKNOWLEDGED"
                assert ticks >= 5
            finally:
                opc.submit_command = old_submit
                if ticker is not None:
                    ticker.cancel()
                    with suppress(asyncio.CancelledError):
                        await ticker
                pump.intake = old_intake
                await command_transport.close()
                await intake.close()
