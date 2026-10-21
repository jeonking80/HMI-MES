"""Real OPC+HTTP sockets+PostgreSQL alarm path with injected plant measurements.

The real controller executes LOAD/START, then its persistent temperature_offset_c
fault input biases the physical process temperature. A real 100ms ticker keeps
production and the thermal process running while real OPC snapshots are polled.
Heartbeat publication is independent, so freezing heartbeat values does not stop
the controller/process ticker. This exercises the simulator's persistent measured
temperature fault model and transport/lifecycle integration, not real hardware.
All capture UTC/monotonic clocks and the default 5-second dwell are real clocks.
"""

import asyncio
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import test_gateway_pipeline as gateway_pipeline
import uvicorn
from sqlalchemy import text
from test_gateway_pipeline import HTTPServer, free_port

from apps.api.alarm_consumer import AlarmConsumerConfig, ConfiguredClockPolicy
from apps.api.alarm_lifecycle import AlarmRule, AlarmScope
from apps.api.alarm_qualification import TemperatureRuleConfig
from apps.api.alarm_runtime import (
    RuleOperation,
    RuleProvisioningConfig,
    configure_alarm_rules,
    start_alarm_runtime,
)
from apps.api.auth import UserAuth
from apps.api.bootstrap import LINE_ID
from apps.api.ingestion import IngestionService
from apps.api.internal import InternalConfig, ServiceCredential, create_internal_app
from apps.api.user_http import UserHTTPConfig, create_user_app
from apps.gateway.http_client import IntakeClient, IntakeConfig
from apps.simulator.plc import PLCInputs
from contracts.models import CommandRequest, Parameters
from contracts.transport import BatchBody, GatewayBatch

pipeline = gateway_pipeline.pipeline


def now():
    return datetime.now(UTC)


@asynccontextmanager
async def http_server(app, port):
    server = HTTPServer(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_level="critical",
            proxy_headers=False,
            lifespan="off",
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            if task.done():
                task.result()
            await asyncio.sleep(0.01)
        assert server.started
        yield
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)


async def begin_real_execution(controller, opc, service, pump):
    identity = controller.storage.identity
    service.register_execution(
        execution_id="alarm-run",
        source_id=pump.source_id,
        stream_id=pump.stream_id,
        work_order_id="alarm-order",
        recipe_version="alarm-recipe",
        parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
        counter_epoch=identity.counter_epoch,
        now=now(),
        target_quantity=1000,
    )
    for kind in ("LOAD_JOB", "START"):
        request = CommandRequest(
            command_id="alarm-" + kind,
            target_id="Line",
            execution_id="alarm-run",
            requested_by="alarm-operator",
            requested_at=now(),
            expires_at=now() + timedelta(seconds=5),
            expected_control_version=controller.state.control_version,
            command={
                "kind": kind,
                "args": {
                    "work_order_id": "alarm-order",
                    "recipe_version": "alarm-recipe",
                    "target_quantity": 1000,
                    "parameters": {"temperature_c": 150.0, "speed_m_per_min": 6.0},
                }
                if kind == "LOAD_JOB"
                else {},
            },
        )
        assert (await opc.submit_command(request)).disposition == "ACCEPTED"
        for _ in range(10):
            controller.tick(now=now())
            if controller.get_result(request.command_id).status == "SUCCEEDED":
                break
        assert controller.get_result(request.command_id).status == "SUCCEEDED"
    assert controller.state.state == "RUNNING" and controller.state.inputs.run_feedback


def inject_temperature(controller, value):
    process = controller.storage.load_execution()
    inputs = PLCInputs.model_validate(
        controller.state.inputs.model_dump()
        | {"temperature_offset_c": value - process.temperature_c}
    )
    controller.set_inputs(inputs, now=now())
    assert controller.state.inputs.temperature_c == value
    assert controller.state.state == "RUNNING" and controller.state.inputs.run_feedback


def database_counts(engine):
    with engine.connect() as db:
        return {
            name: db.execute(text("SELECT count(*) FROM alarm_" + name)).scalar_one()
            for name in ("instances", "actions", "outbox", "capture_decisions")
        }


async def test_actual_opc_capture_http_pg_alarm_operator_actions_return_and_recurrence(
    pipeline, tmp_path
):
    controller, server, opc, pump, bootstrap_service = pipeline
    engine = bootstrap_service.engine
    scope = AlarmScope(
        line_id=LINE_ID, source_id=pump.source_id, stream_id=pump.stream_id, asset_id="Sealer"
    )
    generation = pump.buffer.capture_position[0]
    token = pump.intake.config.token

    # Real original schema1.2 capture and authenticated HTTP service discovery,
    # before explicitly opting this deployment into an alarm consumer.
    bootstrap = await pump.capture_once()
    assert bootstrap.sample_count == 48 and await pump.deliver_once()
    async with httpx.AsyncClient(
        base_url=pump.intake.config.base_url, trust_env=False
    ) as discovery:
        response = await discovery.get(
            "/internal/v1/alarm-clock-context", headers={"Authorization": "Bearer " + token}
        )
    assert response.status_code == 200
    discovered = response.json()
    assert discovered["clock_assessment"] == "NOT_VERIFIED"
    clock_epoch = discovered["latest_capture"]["capture_time"]["clock_epoch"]

    user_port = free_port()
    origin = f"http://127.0.0.1:{user_port}"
    auth = UserAuth(engine, allowed_origins={origin})
    initial_password, password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    auth.bootstrap_user("alarm-operator", initial_password, roles={"operator", "equipment_admin"})
    user_app = create_user_app(auth, UserHTTPConfig(origin=origin, allow_insecure_loopback=True))
    rule = TemperatureRuleConfig(
        rule=AlarmRule(rule_id="temperature", version="v1", severity="HIGH"), setpoint_c=150.0
    )
    async with (
        http_server(user_app, user_port),
        httpx.AsyncClient(base_url=origin, trust_env=False) as browser,
    ):
        challenge = await browser.post(
            "/auth/challenge", headers={"Origin": origin, "X-HMI-Request": "1"}
        )
        logged_in = await browser.post(
            "/auth/login",
            headers={"Origin": origin, "X-CSRF-Token": challenge.json()["csrf_token"]},
            json={"login": "alarm-operator", "password": initial_password},
        )
        assert logged_in.status_code == 200
        changed = await browser.post(
            "/auth/password",
            headers={"Origin": origin, "X-CSRF-Token": logged_in.json()["csrf_token"]},
            json={"new_password": password},
        )
        assert changed.status_code == 200
        protection = {"Origin": origin, "X-CSRF-Token": changed.json()["csrf_token"]}
        configure_alarm_rules(
            auth,
            login="alarm-operator",
            password=password,
            origin=origin,
            config=RuleProvisioningConfig(
                scope=scope,
                gateway_id=pump.buffer.gateway_id,
                capture_generation=generation,
                operations=(
                    RuleOperation(
                        operation_id="install-temperature", expected_version=None, config=rule
                    ),
                ),
            ),
        )
        # Exact zero is an explicit guarantee for these local processes reading
        # the SAME kernel UTC clock, not a latency-derived clock estimate. Raw
        # UTC-read bracket uncertainty is still included by the consumer.
        config = AlarmConsumerConfig(
            scope=scope,
            gateway_id=pump.buffer.gateway_id,
            capture_generation=generation,
            rules=(rule,),
            clock_policy=ConfiguredClockPolicy(
                policy_id="local-shared-kernel",
                policy_version="v1",
                reference_id="same-kernel-utc",
                method="CONFIGURED_API_REFERENCE",
                guarantee_kind="SAME_HOST_SHARED_KERNEL_UTC",
                guarantee_basis=(
                    "Test gateway and API use the same host kernel UTC clock with no injected "
                    "clocks; raw read bracket uncertainty remains preserved."
                ),
                gateway_id=pump.buffer.gateway_id,
                source_id=pump.source_id,
                stream_id=pump.stream_id,
                capture_generation=generation,
                clock_epoch=clock_epoch,
                valid_from_api_utc=now() - timedelta(seconds=10),
                valid_until_api_utc=now() + timedelta(minutes=5),
                offset_lower_seconds=0.0,
                offset_upper_seconds=0.0,
            ),
        )
        consumer = start_alarm_runtime(
            engine,
            config,
            args=SimpleNamespace(
                gateway_id=pump.buffer.gateway_id,
                source_id=pump.source_id,
                stream_id=pump.stream_id,
                capture_generation=generation,
            ),
        )
        service = IngestionService(engine, alarm_consumer=consumer)
        secret_file = tmp_path / "alarm-service-token"
        secret_file.write_text(token)
        secret_file.chmod(0o600)
        credential = ServiceCredential.from_secret_file(
            key_id="alarm-intake",
            gateway_id=pump.buffer.gateway_id,
            source_id=pump.source_id,
            stream_id=pump.stream_id,
            secret_file=secret_file,
            expires_at=now() + timedelta(minutes=5),
        )
        runtime_port = free_port()
        app = create_internal_app(
            service, [credential], config=InternalConfig(allow_insecure_loopback=True)
        )
        runtime_intake = IntakeClient(
            IntakeConfig(f"http://127.0.0.1:{runtime_port}", token, allow_loopback_http=True)
        )
        original_intake = pump.intake
        query = scope.model_dump() | {"rule_id": "temperature", "view": "all"}
        heartbeat = 1
        tick_count = 0
        ticker = None

        async def tick_process():
            nonlocal tick_count
            loop = asyncio.get_running_loop()
            next_tick = loop.time()
            while True:
                controller.tick(now=now())
                tick_count += 1
                next_tick += 0.1
                await asyncio.sleep(max(0.0, next_tick - loop.time()))

        async def capture(*, advance=True):
            nonlocal heartbeat
            if ticker is not None and ticker.done():
                ticker.result()
            if advance:
                heartbeat += 1
            await server.publish_snapshot(acquired_at=now(), heartbeat=heartbeat)
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
            assert body.schema_version == "1.2" and body.capture_time.clock_epoch == clock_epoch
            return GatewayBatch(body=body, content_sha256=row["content_sha256"])

        async def listed():
            response = await browser.get("/api/v1/alarms", params=query)
            assert response.status_code == 200
            return response.json()["items"]

        async def until(predicate):
            # Real1Hz samples/default5s dwell. Bounded retries permit transient
            # UNKNOWN (clock/read/HTTP uncertainty) without falsifying elapsed time.
            for _ in range(12):
                await capture()
                rows = await listed()
                if predicate(rows):
                    return rows
                await asyncio.sleep(1.0)
            pytest.fail("Alarm condition did not qualify within twelve real captures")

        async with http_server(app, runtime_port):
            pump.intake = runtime_intake
            try:
                await begin_real_execution(controller, opc, service, pump)
                ticker = asyncio.create_task(tick_process())
                inject_temperature(controller, 165.0)
                assert controller.state.ready is False
                rows = await until(
                    lambda rows: len(rows) == 1 and rows[0]["status"] == "ACTIVE_UNACKNOWLEDGED"
                )
                alarm_id = rows[0]["instance"]["alarm_id"]
                assert tick_count >= 50  # Default five-second dwell with real process ticks.
                assert controller.state.inputs.temperature_offset_c == 15.0
                assert controller.storage.load_execution().good_quantity > 0
                assert database_counts(engine)["outbox"] == 1
                evidence = rows[0]["instance"]["raised_evidence"]
                assert any(
                    item["name"] == "Sealer.TemperaturePV" and item["value"] == 165.0
                    for item in evidence["readings"]
                )
                controller_before = controller.state
                action = dict(
                    action_id="pipeline-ack",
                    scope=scope.model_dump(),
                    rule_id="temperature",
                    alarm_id=alarm_id,
                    actor="alarm-operator",
                    kind="ACKNOWLEDGE",
                )
                ack = await browser.post(
                    f"/api/v1/alarms/{alarm_id}/actions", json=action, headers=protection
                )
                assert ack.status_code == 200
                notes = [
                    action
                    | {
                        "action_id": "pipeline-note",
                        "kind": "NOTE_ADD",
                        "note_id": "note",
                        "text": "injected temperature observed",
                    },
                    action
                    | {
                        "action_id": "pipeline-note-edit",
                        "kind": "NOTE_EDIT",
                        "note_id": "note",
                        "text": "heartbeat recovery checked",
                        "expected_previous_action_id": "pipeline-note",
                    },
                ]
                for note in notes:
                    assert (
                        await browser.post(
                            f"/api/v1/alarms/{alarm_id}/actions", json=note, headers=protection
                        )
                    ).status_code == 200
                recovered = await browser.get(
                    f"/api/v1/alarms/{alarm_id}/action-receipts/pipeline-ack",
                    params=scope.model_dump() | {"rule_id": "temperature"},
                )
                assert recovered.json() == ack.json()
                # Process quantities/event seq legitimately advance during HTTP.
                # ACK/notes must not change command/control/safety/fault inputs.
                for name in (
                    "control_version",
                    "state",
                    "run_output",
                    "execution_id",
                    "latched_fault_code",
                    "control_mode",
                    "operation_mode",
                ):
                    assert getattr(controller.state, name) == getattr(controller_before, name)
                assert controller.state.inputs.temperature_offset_c == (
                    controller_before.inputs.temperature_offset_c
                )
                assert controller.state.inputs.run_feedback is True
                original = await capture()
                assert len(await listed()) == 1

                # Inject healthy analog values, but deliberately freeze all PLC
                # heartbeat values on the real controller-backed OPC server.
                inject_temperature(controller, 150.0)
                ticks_before_freeze = tick_count
                for _ in range(6):
                    await asyncio.sleep(1.0)
                    frozen = await capture(advance=False)
                    assert (await listed())[0]["status"] == "ACTIVE_ACKNOWLEDGED"
                assert frozen.body.communication.state == "ALARM"
                assert tick_count - ticks_before_freeze >= 50
                assert controller.state.inputs.temperature_offset_c == 0.0
                with engine.connect() as db:
                    assert (
                        db.execute(
                            text(
                                "SELECT count(*) FROM alarm_capture_decisions "
                                "WHERE reason='COMMUNICATION_NOT_GOOD'"
                            )
                        ).scalar_one()
                        > 0
                    )
                returned = await until(lambda rows: rows[0]["status"] == "RETURNED_ACKNOWLEDGED")
                assert returned[0]["instance"]["closure_reason"] == "NORMAL_RETURN"
                inject_temperature(controller, 165.0)
                recurred = await until(
                    lambda rows: len(rows) == 2 and rows[0]["status"] == "ACTIVE_UNACKNOWLEDGED"
                )
                assert recurred[0]["instance"]["alarm_id"] != alarm_id
                assert database_counts(engine)["outbox"] == 2
                before = database_counts(engine)
                await runtime_intake.deliver(original)  # Exact original late replay over real HTTP.
                assert database_counts(engine) == before
                assert (await listed())[0]["status"] == "ACTIVE_UNACKNOWLEDGED"
            finally:
                if ticker is not None:
                    ticker.cancel()
                    await asyncio.gather(ticker, return_exceptions=True)
                pump.intake = original_intake
                await runtime_intake.close()
