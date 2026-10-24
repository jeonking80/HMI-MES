"""Real Chromium boundary over the existing encrypted OPC/HTTP/PG fixture.

Plant input injection uses the controller's trusted test seam. App A operator
commands and alarm acknowledgements use the actual shipped UI (except rejected
START requests: its safety-disabled UI is checked separately from browser HTTP).
No production deployment or Claude-owned source is changed.
"""

from __future__ import annotations

import asyncio
import secrets
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
from acceptance_runtime import ROOT
from playwright.async_api import async_playwright, expect
from sqlalchemy import text
from test_alarm_pipeline import begin_real_execution, http_server, now
from test_gateway_pipeline import free_port

from apps.api.alarm_consumer import ConfiguredClockPolicy
from apps.api.alarm_coordinator import MixedAlarmChannel, MixedAlarmConsumerConfig
from apps.api.alarm_lifecycle import AlarmRule
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
from apps.api.reports import read_execution_reports
from apps.api.user_http import UserHTTPConfig, create_user_app
from apps.api.web import mount_web
from apps.gateway.commands import CommandTransport, CommandWorker
from apps.gateway.http_client import IntakeClient, IntakeConfig
from apps.gateway.opcua_client import GatewayReadError
from apps.simulator.field_channels import FieldChannels, FieldFaultInput
from apps.simulator.plc import PLCInputs
from contracts.models import CommandRequest


class BrowserBoundary:
    def __init__(self, pipeline, directory, evidence):
        self.controller, self.server, self.opc, self.pump, service = pipeline
        self.engine = service.engine
        self.directory = Path(directory)
        self.evidence = evidence
        self.binding = dict(
            line_id=LINE_ID, source_id=self.pump.source_id, stream_id=self.pump.stream_id
        )
        self.heartbeat = 1
        self.field_connected = True
        self.opc_connected = True
        self.worker_enabled = True
        self.capture_lock = asyncio.Lock()
        self.fields = FieldChannels(self.controller.storage)
        self.server.field_channels = self.fields
        self.commands = []
        self.captures = []
        self.origin = f"http://127.0.0.1:{free_port()}"
        self.login_name = "acceptance-operator"
        self.password = secrets.token_urlsafe(24)
        self.initial = secrets.token_urlsafe(24)

    @asynccontextmanager
    async def running(self):
        assert Path("/usr/bin/chromium").is_file(), "Actual /usr/bin/chromium required"
        assert (ROOT / "apps/web/dist/index.html").is_file(), "Build web distribution first"
        async with AsyncExitStack() as stack:
            auth = UserAuth(self.engine, allowed_origins={self.origin})
            auth.bootstrap_user(
                self.login_name, self.initial, roles={"operator", "equipment_admin"}
            )
            app = create_user_app(
                auth, UserHTTPConfig(origin=self.origin, allow_insecure_loopback=True)
            )
            mount_web(app, ROOT / "apps/web/dist")
            await stack.enter_async_context(http_server(app, int(self.origin.rsplit(":", 1)[1])))
            playwright = await stack.enter_async_context(async_playwright())
            browser = await playwright.chromium.launch(
                executable_path="/usr/bin/chromium",
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            stack.push_async_callback(browser.close)
            self.context = await browser.new_context()
            self.page = await self.context.new_page()
            self.page.set_default_timeout(20_000)
            await self.page.goto(self.origin)
            await self.page.locator("#login").fill(self.login_name)
            await self.page.locator("#login-password").fill(self.initial)
            await self.page.locator("#login-submit").click()
            await self.page.locator("#new-password").fill(self.password)
            await self.page.locator("#confirm-password").fill(self.password)
            async with self.page.expect_response(
                lambda response: response.url == self.origin + "/auth/password"
            ) as changed:
                await self.page.locator("#password-submit").click()
            self.csrf = (await (await changed.value).json())["csrf_token"]
            await self.page.locator("#current-login").wait_for()
            await self.configure_alarms(auth, stack)
            self.old_intake = self.pump.intake
            self.pump.intake = self.intake
            stack.callback(setattr, self.pump, "intake", self.old_intake)
            await begin_real_execution(self.controller, self.opc, self.service, self.pump)
            await self.capture()
            task = asyncio.create_task(self.loop())

            async def stop():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

            stack.push_async_callback(stop)
            self.task = task
            await expect(self.page.locator("#line-state")).to_have_text("운전 중")
            try:
                yield self
            finally:
                self.collect()

    async def configure_alarms(self, auth, stack):
        await self.pump.capture_once()
        assert await self.pump.deliver_once()
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(
                self.pump.intake.config.base_url + "/internal/v1/alarm-clock-context",
                headers={"Authorization": "Bearer " + self.pump.intake.config.token},
            )
        epoch = response.json()["latest_capture"]["capture_time"]["clock_epoch"]
        generation = self.pump.buffer.capture_position[0]
        channels = (
            MixedAlarmChannel(
                asset_id="Line",
                config=PLCFaultRuleConfig(
                    rule=AlarmRule(rule_id="browser-plc-fault", version="v1", severity="HIGH")
                ),
            ),
            MixedAlarmChannel(
                asset_id="Sealer",
                config=TemperatureRuleConfig(
                    rule=AlarmRule(rule_id="browser-temperature", version="v1", severity="HIGH"),
                    setpoint_c=150.0,
                ),
            ),
        )
        configure_alarm_rules(
            auth,
            login=self.login_name,
            password=self.password,
            origin=self.origin,
            config=MixedRuleProvisioningConfig(
                kind="MIXED_ALARM_RULES",
                **self.binding,
                gateway_id=self.pump.buffer.gateway_id,
                capture_generation=generation,
                operations=tuple(
                    MixedRuleOperation(
                        asset_id=c.asset_id,
                        operation_id="install-" + c.asset_id,
                        expected_version=None,
                        config=c.config,
                    )
                    for c in channels
                ),
            ),
        )
        config = MixedAlarmConsumerConfig(
            kind="MIXED_ALARM_RUNTIME",
            **self.binding,
            gateway_id=self.pump.buffer.gateway_id,
            capture_generation=generation,
            channels=channels,
            clock_policy=ConfiguredClockPolicy(
                policy_id="browser-local-kernel",
                policy_version="v1",
                reference_id="same-kernel-utc",
                method="CONFIGURED_API_REFERENCE",
                guarantee_kind="SAME_HOST_SHARED_KERNEL_UTC",
                guarantee_basis=(
                    "All local fixture components share the same host kernel UTC clock."
                ),
                gateway_id=self.pump.buffer.gateway_id,
                source_id=self.pump.source_id,
                stream_id=self.pump.stream_id,
                capture_generation=generation,
                clock_epoch=epoch,
                valid_from_api_utc=now() - timedelta(seconds=10),
                valid_until_api_utc=now() + timedelta(minutes=10),
                offset_lower_seconds=0.0,
                offset_upper_seconds=0.0,
            ),
        )
        coordinator = start_alarm_runtime(
            self.engine,
            config,
            args=SimpleNamespace(
                gateway_id=self.pump.buffer.gateway_id,
                source_id=self.pump.source_id,
                stream_id=self.pump.stream_id,
                capture_generation=generation,
            ),
        )
        self.service = IngestionService(self.engine, alarm_consumer=coordinator)
        token = self.pump.intake.config.token
        secret = self.directory / "browser-intake-token"
        secret.write_text(token)
        secret.chmod(0o600)
        credential = ServiceCredential.from_secret_file(
            key_id="browser-intake",
            gateway_id=self.pump.buffer.gateway_id,
            source_id=self.pump.source_id,
            stream_id=self.pump.stream_id,
            secret_file=secret,
            expires_at=now() + timedelta(minutes=10),
        )
        port = free_port()
        internal = create_internal_app(
            self.service, [credential], config=InternalConfig(allow_insecure_loopback=True)
        )
        await stack.enter_async_context(http_server(internal, port))
        intake_config = IntakeConfig(f"http://127.0.0.1:{port}", token, allow_loopback_http=True)
        self.intake = IntakeClient(intake_config)
        self.transport = CommandTransport(intake_config)
        stack.push_async_callback(self.intake.close)
        stack.push_async_callback(self.transport.close)
        self.worker = CommandWorker(self.opc, self.transport)

    async def capture(self):
        async with self.capture_lock:
            await self._capture()

    async def _capture(self):
        if self.opc_connected:
            if self.field_connected:
                self.heartbeat += 1
            await self.server.publish_snapshot(acquired_at=now(), heartbeat=self.heartbeat)
        try:
            capture = await self.pump.capture_once()
        except GatewayReadError:
            if self.opc_connected:
                raise
            return
        assert await self.pump.deliver_once()
        self.captures.append(capture.batch_id)

    async def stop_opc(self):
        # Finish any in-flight read before changing the simulated transport.
        async with self.capture_lock:
            self.opc_connected = False
            await self.opc.disconnect()
            await self.server.stop()

    async def restart_opc(self):
        # A read begun while down must finish before the recovered flag changes.
        async with self.capture_lock:
            await self.server.start()
            self.opc_connected = True

    async def loop(self):
        tick = 0
        while True:
            self.controller.tick(now=now())
            if tick % 3 == 0:
                await self.capture()
                if self.worker_enabled:
                    await self.worker.run_once()
            tick += 1
            await asyncio.sleep(0.1)

    async def wait(self, predicate, timeout=20):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.task.done():
                self.task.result()
            result = await predicate()
            if result:
                return result
            await asyncio.sleep(0.2)
        raise AssertionError("Browser boundary condition not reached within real-time deadline")

    def inputs(self, **fields):
        mode = fields.pop("control_mode", None)
        self.controller.set_inputs(
            PLCInputs.model_validate(self.controller.state.inputs.model_dump() | fields),
            now=now(),
            control_mode=mode,
        )

    def field_mode(self, mode):
        state = next(s for s in self.fields.states() if s.group_id == "Sealer")
        self.fields.set_fault(
            "Sealer",
            FieldFaultInput(mode=mode, expected_revision=state.revision),
            actor="acceptance-plant",
            at=now(),
        )

    async def get(self, path, params=None):
        response = await self.context.request.get(self.origin + path, params=params)
        assert response.status == 200, f"Browser GET {path}: {response.status}"
        return await response.json()

    async def command(self, kind, *, browser_request=False):
        if browser_request:
            issued = now()
            request = CommandRequest(
                command_id=str(uuid4()),
                target_id="Line",
                execution_id=self.controller.state.execution_id,
                requested_by=self.login_name,
                requested_at=issued,
                expires_at=issued + timedelta(seconds=5),
                expected_control_version=self.controller.state.control_version,
                command={"kind": kind, "args": {}},
            )
            response = await self.context.request.post(
                self.origin + "/api/v1/commands",
                headers={"Origin": self.origin, "X-CSRF-Token": self.csrf},
                data=self.binding | {"request": request.model_dump(mode="json")},
            )
            assert response.status == 200, f"Browser command POST: {response.status}"
            command_id = request.command_id
            await self.page.locator("#command-lookup-id").fill(command_id)
            await self.page.locator("#command-lookup-submit").click()
        else:
            await expect(self.page.locator("#command-" + kind)).to_be_enabled()
            await self.page.locator("#command-" + kind).click()
            await self.page.locator("#original-command-id").wait_for()
            command_id = await self.page.locator("#original-command-id").inner_text()

        async def terminal():
            row = await self.get(
                "/api/v1/command-record", self.binding | {"command_id": command_id}
            )
            return (
                row
                if row["result"]["status"] in {"SUCCEEDED", "REJECTED", "FAILED", "EXPIRED"}
                else None
            )

        row = await self.wait(terminal)
        await expect(self.page.locator("#command-status")).to_have_attribute(
            "data-status", row["result"]["status"]
        )
        self.commands.append(
            {
                "kind": kind,
                "boundary": "browser_request" if browser_request else "UI",
                "command_id": command_id,
                "record": row,
            }
        )
        await self.page.locator("#command-acknowledge").click()
        return row

    async def alarm(self, asset, status, alarm_id=None):
        await self.page.get_by_label("알람 대상", exact=True).select_option(asset)
        await self.page.locator(".alarm-filters select").select_option("all")

        async def find():
            await self.page.get_by_role("button", name="기록 새로 조회", exact=True).click()
            rows = await self.get(
                "/api/v1/alarms", self.binding | {"asset_id": asset, "view": "all"}
            )
            found = [
                r
                for r in rows["items"]
                if r["status"] == status
                and (alarm_id is None or r["instance"]["alarm_id"] == alarm_id)
            ]
            if found:
                row = found[0]
                locator = self.page.locator('[data-alarm-id="' + row["instance"]["alarm_id"] + '"]')
                if (
                    await locator.count()
                    and await locator.get_attribute("data-alarm-status") == status
                ):
                    await locator.locator("button").click()
                    return row

        return await self.wait(find, 25)

    async def ack(self):
        await expect(self.page.locator("#alarm-acknowledge")).to_be_enabled()
        await self.page.locator("#alarm-acknowledge").click()
        await expect(self.page.locator("#alarm-action-phase")).to_have_attribute(
            "data-phase", "CONFIRMED"
        )
        action_id = await self.page.locator("#alarm-original-id").inner_text()
        await self.page.get_by_role("button", name="기록 확인 · 다음 조치 준비", exact=True).click()
        return action_id

    def collect(self):
        with self.engine.connect() as db:
            batches = db.execute(
                text("SELECT body FROM ingest_batches ORDER BY stored_at")
            ).scalars()
            bodies = list(batches)
            instant = now()
            report_values = read_execution_reports(
                db,
                LINE_ID,
                instant - timedelta(days=1),
                instant + timedelta(seconds=1),
                now=instant,
            )
            progress = (
                db.execute(
                    text(
                        "SELECT verified_through_seq,latest_ingested_seq FROM stream_progress "
                        "WHERE source_id=:source_id AND stream_id=:stream_id"
                    ),
                    self.binding,
                )
                .mappings()
                .one()
            )
            progress = {key: int(value) for key, value in progress.items()}
        events = {event["event_id"]: event for body in bodies for event in body.get("events", [])}
        self.evidence.record(
            commands=self.commands,
            executions=[
                {
                    "execution_id": self.controller.state.execution_id,
                    "source_id": self.pump.source_id,
                    "stream_id": self.pump.stream_id,
                    "work_order_id": "alarm-order",
                    "recipe_version": "alarm-recipe",
                    "fixture_physics": "100ms ticker; short 1mm stages; ambient150C",
                }
            ],
            events=list(events.values()),
            final_sequences={
                "source_last_seq": int(
                    self.controller.storage.connection.execute(
                        "SELECT last_seq FROM simulator_identity WHERE singleton=1"
                    ).fetchone()[0]
                ),
                "gateway_capture": self.pump.buffer.capture_position,
                **progress,
            },
            reports=self.evidence.data.get("reports", {})
            | {
                "execution_reports": report_values,
                "scope": self.binding,
                "capture_ids": self.captures,
                "simulator_state": self.controller.state.model_dump(mode="json"),
            },
        )

        self.evidence.check(
            "original event prefix matches verified cursor",
            max((int(event["seq"]) for event in events.values()), default=0)
            == progress["verified_through_seq"],
            progress=progress,
        )
        report = next(
            row
            for row in report_values["items"]
            if row["execution_id"] == self.controller.state.execution_id
        )
        totals = self.service.get_totals(self.controller.state.execution_id)
        self.evidence.check(
            "actual execution report matches ingested three quantities",
            report["totals"]
            == {name: str(totals[name + "_quantity"]) for name in ("good", "reject", "scrap")},
            report_totals=report["totals"],
            ingested_totals=totals,
        )
