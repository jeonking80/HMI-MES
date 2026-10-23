"""T09/T12: real complete PostgreSQL/SQLite set, restarted OPC/HTTP and revoked access."""

import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from test_backup_set import docker_pg_tools
from test_control_http import ORIGIN, user
from test_gateway_pipeline import HTTPServer, free_port, make_finished_run, register

from apps.api.alarm_lifecycle import AlarmEvidence, AlarmRule, AlarmScope, QualifiedCondition
from apps.api.alarm_qualification import PressureRuleConfig
from apps.api.alarm_store import apply_condition, install_rule
from apps.api.auth import Principal, UserAuth
from apps.api.bootstrap import LINE_ID
from apps.api.commands import CommandScopeError, enqueue_command
from apps.api.ingestion import IngestionService
from apps.api.internal import InternalConfig, ServiceCredential, create_internal_app
from apps.api.user_http import UserHTTPConfig, create_user_app
from apps.gateway.buffer import GatewayBuffer
from apps.gateway.http_client import DeliveryUncertain, IntakeClient, IntakeConfig
from apps.gateway.opcua_client import GatewayOPCUAClient
from apps.gateway.worker import GatewayPump
from apps.simulator.controller import SimulatorController
from apps.simulator.opcua_server import SimulatorOPCUAServer
from apps.simulator.storage import SimulatorStorage
from contracts.models import CommandRequest
from deploy.backup_set import (
    controller_settings,
    create_backup_set,
    load_controller_settings,
    restore_backup_set,
    resume_restored_set,
)


@asynccontextmanager
async def http_runtime(app, port=None):
    import uvicorn

    port = port or free_port()
    web = HTTPServer(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, access_log=False, log_level="critical", lifespan="off"
        )
    )
    task = asyncio.create_task(web.serve())
    try:
        for _ in range(100):
            if web.started:
                break
            if task.done():
                task.result()
            await asyncio.sleep(0.01)
        assert web.started
        yield f"http://127.0.0.1:{port}"
    finally:
        web.should_exit = True
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.parametrize("case_id", ["T09", "T12"])
async def test_full_set_restart_restore_and_real_viewer_denial(
    pipeline, tmp_path, acceptance_evidence, case_id
):
    e = acceptance_evidence(case_id)
    runtime, server, client, pump, service = pipeline
    engine = service.engine
    summary = await make_finished_run(runtime, server, client)
    register(service, runtime)
    await pump.capture_once()
    deliver = pump.intake.deliver

    async def lose_ack(batch):
        await deliver(batch)
        raise DeliveryUncertain("Injected after actual PostgreSQL commit")

    pump.intake.deliver = lose_ack
    with pytest.raises(DeliveryUncertain):
        await pump.deliver_once()
    e.check(
        "committed_batch_remains_unacked",
        pump.buffer.status(now=datetime.now(UTC)).pending_batches == 1,
    )
    auth = UserAuth(engine, allowed_origins={ORIGIN})
    session = user(auth, login="viewer", role="viewer")
    user(auth, login="administrator", role="equipment_admin")
    identity = runtime.storage.identity
    scope = AlarmScope(
        line_id=LINE_ID,
        source_id=identity.source_id,
        stream_id=identity.stream_id,
        asset_id="Sealer",
    )
    now = datetime.now(UTC)
    request = CommandRequest(
        command_id="restore-pending-command",
        target_id="Line",
        execution_id="run-A",
        requested_by="administrator",
        requested_at=now,
        expires_at=now + timedelta(seconds=5),
        expected_control_version=runtime.state.control_version,
        command={"kind": "START", "args": {}},
    )
    with engine.begin() as db:
        install_rule(
            db,
            scope,
            PressureRuleConfig(rule=AlarmRule(rule_id="pressure", version="v1", severity="HIGH")),
            input_id="restore-install",
            principal=Principal("administrator", frozenset({"equipment_admin"}), False),
            at=now,
        )
        apply_condition(
            db,
            "pressure",
            QualifiedCondition(
                event_id="restore-active",
                scope=scope,
                rule_version="v1",
                position=1,
                condition="ACTIVE",
                new_alarm_id="restore-open-alarm",
                evidence=AlarmEvidence(observed_at=now, references=("restore-active",)),
            ),
        )
        enqueue_command(
            db,
            request,
            principal=Principal("administrator", frozenset({"operator"}), False),
            line_id=LINE_ID,
            source_id=identity.source_id,
            stream_id=identity.stream_id,
            now=now,
        )
    original_url = engine.url
    run_pg = docker_pg_tools(original_url)
    source_path, queue_path = runtime.storage.path, pump.buffer.path
    old_token = pump.intake.config.token
    await client.disconnect()
    await server.stop()
    pump.buffer.close()
    runtime.storage.close()
    settings = dict(
        app_version="v3",
        contract_version="1",
        schema_version="0018_clock_evidence",
        line_id=LINE_ID,
        source_id=identity.source_id,
        stream_id=identity.stream_id,
        counter_epoch=identity.counter_epoch,
        gateway_id="gateway-1",
        controller=controller_settings(runtime),
    )
    start = time.monotonic()
    raw_pg = run_pg

    def barrier_checked_pg(tool, args, url, payload=None):
        if tool == "pg_dump":
            from sqlalchemy.exc import DBAPIError

            with pytest.raises(DBAPIError):
                with engine.begin() as db:
                    db.execute(sa.text("SET LOCAL lock_timeout='100ms'"))
                    db.execute(
                        sa.text(
                            "UPDATE stream_progress SET verified_through_seq=verified_through_seq"
                        )
                    )
            e.check("noncooperating_db_writer_blocked_during_dump", True)
        return raw_pg(tool, args, url, payload)

    with pytest.raises(ValueError, match="gateway identity"):
        create_backup_set(
            engine=engine,
            simulator=source_path,
            gateway=queue_path,
            settings={**settings, "gateway_id": "wrong-gateway"},
            destination=tmp_path / "wrong-set",
            backup_set_id="wrong-set",
            run_pg=run_pg,
        )
    e.check("wrong_identity_settings_rejected_before_dump", not (tmp_path / "wrong-set").exists())
    manifest = create_backup_set(
        engine=engine,
        simulator=source_path,
        gateway=queue_path,
        settings=settings,
        destination=tmp_path / "backup",
        backup_set_id=f"acceptance-{case_id}",
        run_pg=barrier_checked_pg,
    )
    target = tmp_path / "restored"
    database_name = "hmi_restore_" + secrets.token_hex(8)
    restored_result = restore_backup_set(
        directory=tmp_path / "backup",
        admin_engine=engine,
        database_name=database_name,
        destination=target,
        run_pg=run_pg,
    )
    restored_engine = sa.create_engine(
        original_url.set(database=database_name), hide_parameters=True
    )
    restored_source = SimulatorStorage(target / "simulator.sqlite3")
    restored_runtime = SimulatorController(
        restored_source,
        now=datetime.now(UTC),
        **load_controller_settings(json.loads((target / "settings.json").read_text())["settings"]),
    )
    restored_queue = GatewayBuffer(target / "gateway.sqlite3", gateway_id="gateway-1")
    restored_server = SimulatorOPCUAServer(
        restored_runtime,
        replace(server.config, endpoint=f"opc.tcp://127.0.0.1:{free_port()}/hmi-mes/"),
    )
    restored_client = GatewayOPCUAClient(
        replace(client.config, endpoint=restored_server.config.endpoint)
    )
    new_secret = target / "service-token"
    credential = ServiceCredential.from_secret_file(
        key_id="restored-key",
        gateway_id="gateway-1",
        source_id=identity.source_id,
        stream_id=identity.stream_id,
        secret_file=new_secret,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    restored_service = IngestionService(restored_engine)
    user_port = free_port()
    user_origin = f"http://127.0.0.1:{user_port}"
    restored_auth = UserAuth(restored_engine, allowed_origins={ORIGIN, user_origin})
    try:
        e.check(
            "whole_manifest_inventory_verified",
            restored_result["inventory_verified"],
            database_name=database_name,
            elapsed_seconds=time.monotonic() - start,
        )
        e.check(
            "restored_runtime_recovery_required",
            restored_runtime.state.state == "RECOVERY_REQUIRED",
        )
        e.check(
            "no_automatic_start",
            not restored_runtime.state.run_output and restored_source.recovery_required,
        )
        with restored_engine.connect() as db:
            e.check(
                "pending_command_and_open_alarm_preserved",
                db.execute(
                    sa.text(
                        "SELECT count(*) FROM command_requests "
                        "WHERE command_id='restore-pending-command'"
                    )
                ).scalar()
                == 1
                and db.execute(
                    sa.text(
                        "SELECT count(*) FROM alarm_instances "
                        "WHERE alarm_id='restore-open-alarm' AND returned_at IS NULL"
                    )
                ).scalar()
                == 1,
            )
        user_app = create_user_app(
            restored_auth, UserHTTPConfig(origin=user_origin, allow_insecure_loopback=True)
        )
        async with (
            http_runtime(user_app, user_port) as base,
            httpx.AsyncClient(base_url=base) as browser,
        ):
            browser.cookies.set("hmi_session", session.token)
            expired = await browser.get(
                "/api/v1/commands/restore-pending-command",
                params={
                    "line_id": LINE_ID,
                    "source_id": identity.source_id,
                    "stream_id": identity.stream_id,
                },
            )
            e.check(
                "old_session_revoked_on_real_http",
                expired.status_code == 401,
                status=expired.status_code,
            )
            fresh = user(restored_auth, login="restored-viewer", role="viewer")
            browser.cookies.set("hmi_session", fresh.token)
            denied = await browser.post(
                "/api/v1/commands",
                headers={"Origin": user_origin, "X-CSRF-Token": fresh.csrf_token},
                json={
                    "request": request.model_dump(mode="json"),
                    "line_id": LINE_ID,
                    "source_id": identity.source_id,
                    "stream_id": identity.stream_id,
                },
            )
            e.check(
                "restored_viewer_control_denied",
                denied.status_code == 403,
                status=denied.status_code,
            )
        with pytest.raises(CommandScopeError, match="RECOVERY_REQUIRED"):
            with restored_engine.begin() as db:
                enqueue_command(
                    db,
                    request,
                    principal=Principal("administrator", frozenset({"operator"}), False),
                    line_id=LINE_ID,
                    source_id=identity.source_id,
                    stream_id=identity.stream_id,
                    now=datetime.now(UTC),
                )
        e.check("unwrapped_authorized_enqueue_blocked_by_durable_recovery", True)
        await restored_server.start()
        await restored_server.publish_snapshot(acquired_at=datetime.now(UTC), heartbeat=1)
        await restored_client.connect()
        app = create_internal_app(
            restored_service, [credential], config=InternalConfig(allow_insecure_loopback=True)
        )
        async with http_runtime(app) as base:
            async with httpx.AsyncClient(base_url=base) as raw:
                blocked = await raw.post(
                    "/internal/v1/commands/claim",
                    headers={"Authorization": f"Bearer {new_secret.read_text()}"},
                    json={},
                )
                e.check(
                    "unwrapped_api_pending_start_blocked_by_durable_recovery",
                    blocked.status_code == 403,
                    status=blocked.status_code,
                )
                stale = await raw.post(
                    "/internal/v1/batches",
                    headers={"Authorization": f"Bearer {old_token}"},
                    json={},
                )
                e.check(
                    "old_service_key_rejected", stale.status_code == 401, status=stale.status_code
                )
            intake = IntakeClient(
                IntakeConfig(base, new_secret.read_text(), allow_loopback_http=True)
            )
            try:
                restarted = GatewayPump(
                    identity.source_id,
                    identity.stream_id,
                    opc=restored_client,
                    buffer=restored_queue,
                    intake=intake,
                )
                before = restored_service.get_totals("run-A")
                e.check("unacked_commit_replayed_after_restart", await restarted.deliver_once())
                e.check(
                    "replay_preserves_three_totals",
                    before == restored_service.get_totals("run-A"),
                    totals=before,
                )
                e.check(
                    "manifest_matches_original_counts",
                    before["good_quantity"] == summary.good_quantity
                    and before["reject_quantity"] == summary.reject_quantity
                    and before["scrap_quantity"] == summary.scrap_quantity,
                )
            finally:
                await intake.close()
        await restored_client.disconnect()
        await restored_server.stop()
        restored_queue.close()
        restored_source.close()
        with pytest.raises(ValueError, match="acknowledgement"):
            resume_restored_set(
                engine=restored_engine,
                directory=tmp_path / "backup",
                restored_directory=target,
                operator_id="acceptance-operator",
                acknowledged=False,
            )
        with restored_engine.begin() as db:
            db.execute(
                sa.text(
                    "UPDATE execution_totals SET good_quantity=good_quantity+1 "
                    "WHERE execution_id='run-A'"
                )
            )
        with pytest.raises(ValueError, match="database evidence changed"):
            resume_restored_set(
                engine=restored_engine,
                directory=tmp_path / "backup",
                restored_directory=target,
                operator_id="acceptance-operator",
                acknowledged=True,
            )
        with restored_engine.begin() as db:
            db.execute(
                sa.text(
                    "UPDATE execution_totals SET good_quantity=good_quantity-1 "
                    "WHERE execution_id='run-A'"
                )
            )
        resumed = resume_restored_set(
            engine=restored_engine,
            directory=tmp_path / "backup",
            restored_directory=target,
            operator_id="acceptance-operator",
            acknowledged=True,
        )
        e.check(
            "explicit_verified_resume_keeps_plc_recovery_required",
            resumed["control_restriction_released"]
            and resumed["plc_recovery_required"]
            and not resumed["automatic_start"],
        )
        e.record(
            commands=[
                "restore-pending-command",
                "command-LOAD_JOB",
                "command-START",
                "command-END_RUN",
            ],
            executions=["run-A"],
            events=[
                json.loads(row)[0] for row in manifest["inventory"]["simulator"]["source_events"]
            ],
            final_sequences={identity.stream_id: str(summary.last_event_seq)},
            reports={
                "backup_set_id": manifest["backup_set_id"],
                "inventory": manifest["inventory"],
            },
        )
    finally:
        await restored_client.disconnect()
        await restored_server.stop()
        restored_queue.close()
        restored_source.close()
        restored_engine.dispose()
        # Only the independently generated restored test database is removed.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as db:
            db.execute(sa.text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
