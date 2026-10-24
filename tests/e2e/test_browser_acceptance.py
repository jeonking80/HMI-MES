"""CX-17 T02/T03/T04/T05/T06: real Chromium, API/PG and encrypted OPC.

Run with --junitxml and HMI_ACCEPTANCE_ARTIFACTS to retain every case envelope.
The pipeline's short physical stage lengths are explicit fixture acceleration;
all freshness and alarm dwell measurements below use real elapsed clocks.
"""

import asyncio
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from browser_boundary import BrowserBoundary
from playwright.async_api import expect
from test_alarm_pipeline import now

from apps.gateway.opcua_client import GatewayMethodUncertain
from contracts.models import CommandRequest


@pytest.mark.parametrize("condition", ["guard_open", "LOCAL", "not_preheated"])
async def test_t02_browser_start_refusal(pipeline, tmp_path, acceptance_evidence, condition):
    e = acceptance_evidence("T02-" + condition)
    async with BrowserBoundary(pipeline, tmp_path, e).running() as h:
        await h.command("PAUSE")
        await expect(h.page.locator("#line-state")).to_have_text("일시 정지")
        if condition == "guard_open":
            h.inputs(guard_closed=False)
        elif condition == "LOCAL":
            h.inputs(control_mode="LOCAL")
        else:
            h.inputs(temperature_offset_c=-40.0)
        await h.capture()
        await expect(h.page.locator("#command-START")).to_be_disabled()
        e.check("browser_START_disabled", await h.page.locator("#command-START").is_disabled())
        if condition == "LOCAL":
            issued = now()
            request = CommandRequest(
                command_id=str(uuid4()),
                target_id="Line",
                execution_id=h.controller.state.execution_id,
                requested_by=h.login_name,
                requested_at=issued,
                expires_at=issued + timedelta(seconds=5),
                expected_control_version=h.controller.state.control_version,
                command={"kind": "START", "args": {}},
            )
            response = await h.context.request.post(
                h.origin + "/api/v1/commands",
                headers={"Origin": h.origin, "X-CSRF-Token": h.csrf},
                data=h.binding | {"request": request.model_dump(mode="json")},
            )
            e.check(
                "LOCAL_browser_HTTP_prevents_remote_control",
                response.status == 409,
                http_status=response.status,
            )
            submission = await h.opc.submit_command(request)
            plc = h.controller.get_result(request.command_id)
            h.commands.append(
                {
                    "boundary": "authenticated_OPC_after_HTTP_refusal",
                    "request": request.model_dump(mode="json"),
                    "submission": submission.model_dump(mode="json"),
                    "result": plc.model_dump(mode="json"),
                }
            )
            e.check(
                "PLC_LOCAL_refusal",
                plc.status == "REJECTED" and plc.reason_code == "LOCAL_MODE",
                result=plc.model_dump(mode="json"),
            )
        else:
            row = await h.command("START", browser_request=True)
            e.check(
                "PLC_guard_or_preheat_refusal",
                row["result"]["status"] == "REJECTED"
                and row["result"]["reason_code"] == "NOT_READY",
                result=row["result"],
            )
        e.check(
            "actual_PLC_no_running",
            not h.controller.state.run_output and h.controller.state.state != "RUNNING",
        )
        e.check(
            "browser_no_running_display",
            await h.page.locator("#line-state").inner_text() != "운전 중",
        )


async def test_t03_browser_unknown_and_original_id_recovery(
    pipeline, tmp_path, acceptance_evidence
):
    e = acceptance_evidence("T03-browser")
    async with BrowserBoundary(pipeline, tmp_path, e).running() as h:
        await h.command("PAUSE")
        submit, query = h.opc.submit_command, h.opc.get_command_result
        submitted = []
        hold = True

        async def lose_submit(request):
            submitted.append(request.command_id)
            await submit(request)
            raise GatewayMethodUncertain(
                "SubmitCommand", request.command_id, delivery_possible=True
            )

        async def lose_query(request):
            result = await query(request)
            if hold:
                raise GatewayMethodUncertain(
                    "GetCommandResult", request.command_id, delivery_possible=True
                )
            return result

        h.opc.submit_command, h.opc.get_command_result = lose_submit, lose_query
        try:
            await expect(h.page.locator("#command-START")).to_be_enabled()
            await h.page.locator("#command-START").click()
            await expect(h.page.locator("#command-status")).to_have_attribute(
                "data-status", "OUTCOME_UNKNOWN"
            )
            command_id = await h.page.locator("#original-command-id").inner_text()
            unknown = await h.get("/api/v1/command-record", h.binding | {"command_id": command_id})
            e.check(
                "browser_displays_actual_uncertainty",
                unknown["result"]["status"] == "OUTCOME_UNKNOWN",
                command_id=command_id,
                journal=unknown,
            )
            hold = False
            await expect(h.page.locator("#command-status")).to_have_attribute(
                "data-status", "SUCCEEDED"
            )
            final = await h.get("/api/v1/command-record", h.binding | {"command_id": command_id})
            e.check("same_original_ID_only_once", submitted == [command_id], submitted=submitted)
            e.check(
                "original_execution_recovered",
                final["result"]["status"] == "SUCCEEDED" and h.controller.state.state == "RUNNING",
                journal=final,
            )
            h.commands.append(
                {
                    "boundary": "UI_with_real_OPC_response_loss",
                    "command_id": command_id,
                    "unknown": unknown,
                    "record": final,
                }
            )
        finally:
            h.opc.submit_command, h.opc.get_command_result = submit, query


@pytest.mark.parametrize("order", ["ack_first", "return_first"])
async def test_t04_browser_alarm_both_orders(pipeline, tmp_path, acceptance_evidence, order):
    e = acceptance_evidence("T04-" + order)
    async with BrowserBoundary(pipeline, tmp_path, e).running() as h:
        h.inputs(temperature_offset_c=25.0)
        active = await h.alarm("Sealer", "ACTIVE_UNACKNOWLEDGED")
        alarm_id = active["instance"]["alarm_id"]
        seen = ["ACTIVE_UNACKNOWLEDGED"]
        if order == "ack_first":
            action_id = await h.ack()
            await h.alarm("Sealer", "ACTIVE_ACKNOWLEDGED", alarm_id)
            seen.append("ACTIVE_ACKNOWLEDGED")
            h.inputs(temperature_offset_c=0.0)
        else:
            h.inputs(temperature_offset_c=0.0)
            await h.alarm("Sealer", "RETURNED_UNACKNOWLEDGED", alarm_id)
            seen.append("RETURNED_UNACKNOWLEDGED")
            action_id = await h.ack()
        returned = await h.alarm("Sealer", "RETURNED_ACKNOWLEDGED", alarm_id)
        seen.append("RETURNED_ACKNOWLEDGED")
        instance = returned["instance"]
        e.check(
            "raised_return_ack_times_persisted",
            bool(instance["raised_evidence"]["observed_at"])
            and bool(instance["condition_closed_at"])
            and bool(instance["acknowledged_at"]),
            instance=instance,
        )
        raised_at = datetime.fromisoformat(instance["raised_evidence"]["observed_at"])
        ack_at = datetime.fromisoformat(instance["acknowledged_at"])
        closed_at = datetime.fromisoformat(instance["condition_closed_at"])
        e.check(
            "persisted times preserve the exercised order",
            raised_at <= ack_at <= closed_at
            if order == "ack_first"
            else raised_at <= closed_at <= ack_at,
            raised_at=raised_at,
            ack_at=ack_at,
            closed_at=closed_at,
        )
        e.check("authenticated_actor_persisted", instance["acknowledged_by"] == h.login_name)
        await expect(h.page.locator(".alarm-facts")).to_contain_text(h.login_name)
        history = await h.get(
            "/api/v1/alarm-actions",
            h.binding
            | {"asset_id": "Sealer", "rule_id": instance["rule"]["rule_id"], "alarm_id": alarm_id},
        )
        ack = [a for a in history["items"] if a["action_id"] == action_id]
        e.check(
            "browser_ACK_history_exact_id_actor_time",
            len(ack) == 1 and ack[0]["actor"] == h.login_name and bool(ack[0]["at"]),
            history=history,
        )
        await expect(h.page.locator(".alarm-history")).to_contain_text(action_id)
        e.record(
            reports={
                "alarm_order": order,
                "states_seen_through_browser": seen,
                "alarm": returned,
                "history": history,
            }
        )


async def test_t05_browser_fault_ack_reset_separate_start(pipeline, tmp_path, acceptance_evidence):
    e = acceptance_evidence("T05")
    async with BrowserBoundary(pipeline, tmp_path, e).running() as h:
        h.inputs(fault_code=123)
        raised = await h.alarm("Line", "ACTIVE_UNACKNOWLEDGED")
        alarm_id = raised["instance"]["alarm_id"]
        await h.ack()
        await h.alarm("Line", "ACTIVE_ACKNOWLEDGED", alarm_id)
        blocked = await h.command("RESET_FAULT")
        e.check(
            "active_cause_reset_rejected",
            blocked["result"]["status"] == "REJECTED"
            and blocked["result"]["reason_code"] == "RESET_BLOCKED",
            result=blocked,
        )
        e.check(
            "fault_latched_after_ack_reset",
            h.controller.state.latched_fault_code == 123 and not h.controller.state.run_output,
        )
        h.inputs(fault_code=0)
        await h.capture()
        await asyncio.sleep(0.5)
        e.check("clear_cause_does_not_reset", h.controller.state.latched_fault_code == 123)
        reset = await h.command("RESET_FAULT")
        e.check("explicit_reset_succeeds", reset["result"]["status"] == "SUCCEEDED")
        returned = await h.alarm("Line", "RETURNED_ACKNOWLEDGED", alarm_id)
        e.check(
            "reset_does_not_restart",
            h.controller.state.latched_fault_code == 0
            and not h.controller.state.run_output
            and h.controller.state.state in {"READY", "STOPPED"},
        )
        started = await h.command("START")
        e.check(
            "separate_START_required",
            started["result"]["status"] == "SUCCEEDED" and h.controller.state.run_output,
        )
        e.record(reports={"raised_alarm": raised, "returned_alarm": returned})


@pytest.mark.parametrize("boundary", ["field", "OPC"])
async def test_t06_browser_real_disconnect(pipeline, tmp_path, acceptance_evidence, boundary):
    e = acceptance_evidence("T06-" + boundary)
    async with BrowserBoundary(pipeline, tmp_path, e).running() as h:
        h.inputs(temperature_offset_c=25.0)
        alarm = await h.alarm("Sealer", "ACTIVE_UNACKNOWLEDGED")
        alarm_id = alarm["instance"]["alarm_id"]
        await expect(h.page.locator("#heartbeat-state")).to_have_attribute("data-state", "GOOD")
        await expect(h.page.locator("#command-PAUSE")).to_be_enabled()
        h.worker_enabled = False
        if boundary == "field":
            h.field_mode("LINK_DOWN")
        else:
            await h.stop_opc()
        start = asyncio.get_running_loop().time()
        timeline = []
        for target in (2.0, 3.6, 5.8):
            await asyncio.sleep(max(0, start + target - asyncio.get_running_loop().time()))
            timeline.append(
                {
                    "elapsed": asyncio.get_running_loop().time() - start,
                    "heartbeat": await h.page.locator("#heartbeat-state").get_attribute(
                        "data-state"
                    ),
                    "line": await h.page.locator("#line-state").inner_text(),
                    "start_disabled": await h.page.locator("#command-START").is_disabled(),
                    "pause_disabled": await h.page.locator("#command-PAUSE").is_disabled(),
                }
            )
        e.check(
            "real_3_second_delay_boundary",
            timeline[0]["heartbeat"] == "GOOD" and timeline[1]["heartbeat"] == "DELAYED",
            timeline=timeline,
        )
        e.check(
            "real_5_second_alarm_boundary", timeline[2]["heartbeat"] == "ALARM", timeline=timeline
        )
        e.check(
            "UNKNOWN_no_running_control_restricted",
            timeline[2]["line"] == "불명"
            and timeline[2]["start_disabled"]
            and timeline[2]["pause_disabled"],
            timeline=timeline,
        )
        values = h.page.locator('[data-tag-id="Sealer.TemperaturePV"]')
        e.check(
            "no_zero_substitution",
            await values.locator("strong").inner_text() == "—",
            tag_text=await values.inner_text(),
        )
        retained = await h.get("/api/v1/alarms", h.binding | {"asset_id": "Sealer", "view": "all"})
        e.check(
            "disconnect_does_not_return_active_alarm",
            any(
                r["instance"]["alarm_id"] == alarm_id and r["status"] == "ACTIVE_UNACKNOWLEDGED"
                for r in retained["items"]
            ),
            alarms=retained,
        )
        if boundary == "field":
            h.field_mode("NORMAL")
        else:
            await h.restart_opc()

        async def recovered_health():
            return await h.page.locator("#heartbeat-state").get_attribute("data-state") == "GOOD"

        await h.wait(recovered_health, timeout=20)
        recovered = await h.alarm("Sealer", "ACTIVE_UNACKNOWLEDGED", alarm_id)
        e.check(
            "transport_recovers_without_auto_alarm_return",
            recovered["instance"]["condition_closed_at"] is None,
        )
        e.record(
            reports={
                "disconnect_boundary": boundary,
                "real_seconds": timeline,
                "retained_alarm": retained,
                "recovered_alarm": recovered,
            }
        )
