#!/usr/bin/env python3
"""Exercise the real simulator CLI and console in Chromium, separately from pytest.

Run with ``.venv/bin/python scripts/check-simulator-browser.py``. A local Chromium
and the development Playwright dependency are required; missing tools fail clearly.
All credentials, certificates and SQLite state are temporary and never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def exercise(directory: Path, executable: str, screenshot: Path) -> None:
    import httpx
    from playwright.async_api import async_playwright

    from apps.simulator.certificates import provision_development_certificates

    database = directory / "simulator.sqlite3"
    certs = directory / "certificates"
    provision_development_certificates(certs)
    initial, changed = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    password_file = directory / "initial-password"
    password_file.write_text(initial + "\n")
    password_file.chmod(0o600)
    http_port, opc_port = free_port(), free_port()
    while http_port == opc_port:
        opc_port = free_port()
    origin = f"http://127.0.0.1:{http_port}"
    base = [sys.executable, "-m", "apps.simulator"]
    provisioned = subprocess.run(
        base
        + [
            "bootstrap-user",
            "--database",
            str(database),
            "--origin",
            origin,
            "--login",
            "browser-operator",
            "--password-file",
            str(password_file),
            "--role",
            "simulator_operator",
        ],
        cwd=ROOT,
        capture_output=True,
        timeout=20,
    )
    require(provisioned.returncode == 0, "Explicit test-user provisioning failed")
    require(
        all(
            secret.encode() not in provisioned.stdout + provisioned.stderr
            for secret in (initial, changed)
        ),
        "Provisioning exposed a test credential",
    )
    password_file.unlink()
    log_path = directory / "server.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            base
            + [
                "serve",
                "--database",
                str(database),
                "--port",
                str(http_port),
                "--origin",
                origin,
                "--opc-endpoint",
                f"opc.tcp://127.0.0.1:{opc_port}/browser/",
                "--server-certificate",
                str(certs / "server.der"),
                "--server-private-key",
                str(certs / "server.pem"),
                "--trusted-client-certificate",
                str(certs / "gateway.der"),
            ],
            cwd=ROOT,
            stdout=log,
            stderr=log,
        )
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
                deadline = time.monotonic() + 20
                while True:
                    require(process.poll() is None, "Simulator exited before HTTP readiness")
                    try:
                        response = await client.get(origin + "/auth/me")
                        require(response.status_code == 401, "Anonymous session was not denied")
                        break
                    except httpx.TransportError:
                        if time.monotonic() >= deadline:
                            raise AssertionError("Simulator HTTP readiness timed out") from None
                        await asyncio.sleep(0.1)

            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    executable_path=executable,
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                try:
                    context = await browser.new_context(
                        viewport={"width": 1440, "height": 1080}, locale="ko-KR"
                    )
                    page = await context.new_page()
                    page.set_default_timeout(15_000)
                    errors: list[str] = []
                    page.on("pageerror", lambda error: errors.append(type(error).__name__))
                    page.on("dialog", lambda dialog: dialog.accept())
                    print("Browser: initial login and password change", flush=True)
                    await page.goto(origin, wait_until="domcontentloaded")
                    print("Browser: login page loaded", flush=True)
                    await page.locator("#login-form [name=login]").fill("browser-operator")
                    await page.locator("#login-form [name=password]").fill(initial)
                    async with page.expect_response(
                        lambda r: r.url == origin + "/auth/login"
                    ) as response:
                        await page.locator("#login-form button").click()
                    require((await response.value).status == 200, "Browser login failed")
                    print("Browser: login accepted", flush=True)
                    await page.locator("#password-panel").wait_for(state="visible")
                    await page.locator("#password-form [name=password]").fill(changed)
                    await page.locator("#password-form [name=confirm]").fill(changed)
                    async with page.expect_response(
                        lambda r: r.url == origin + "/auth/password"
                    ) as response:
                        await page.locator("#password-form button").click()
                    require((await response.value).status == 200, "Initial-password change failed")
                    print("Browser: password changed", flush=True)
                    await page.locator("#password-panel").wait_for(state="hidden")

                    async def snapshot():
                        response = await context.request.get(origin + "/snapshot")
                        require(response.status == 200, "Authenticated snapshot failed")
                        return await response.json()

                    async def wait_state(predicate, description, timeout=20):
                        deadline = time.monotonic() + timeout
                        while time.monotonic() < deadline:
                            value = await snapshot()
                            if predicate(value):
                                return value
                            await asyncio.sleep(0.1)
                        raise AssertionError(description)

                    before = await snapshot()
                    print("Browser: explicit recovery and LOCAL/AUTO", flush=True)
                    require(before["state"]["recovery_required"], "Startup bypassed recovery")
                    require(not before["state"]["run_output"], "Startup enabled motion")
                    for name in (
                        "outputs_stopped",
                        "positions_verified",
                        "storage_reconciled",
                        "commands_reconciled",
                        "operator_confirmed",
                    ):
                        await page.locator(f"#recovery-form [name={name}]").check()
                    async with page.expect_response(
                        lambda r: r.url == origin + "/recover"
                    ) as response:
                        await page.locator("#recovery-form button[type=submit]").click()
                    require((await response.value).status == 200, "Explicit UI recovery failed")
                    await wait_state(
                        lambda s: (
                            not s["state"]["recovery_required"] and not s["state"]["run_output"]
                        ),
                        "Recovery did not leave outputs stopped",
                    )
                    await page.locator("#mode-form [name=operation_mode]").select_option("AUTO")
                    async with page.expect_response(
                        lambda r: r.url == origin + "/inputs"
                    ) as response:
                        await page.locator("#mode-form button").click()
                    require((await response.value).status == 200, "LOCAL/AUTO mode change failed")

                    async def command(kind, execution="browser/A"):
                        print("Browser: " + kind + " " + execution, flush=True)
                        form = page.locator("#command-form")
                        await form.locator("[name=kind]").select_option(kind)
                        await form.locator("[name=execution_id]").fill(execution)
                        if kind == "LOAD_JOB":
                            for name, value in {
                                "work_order_id": "order-" + execution,
                                "target_quantity": "4",
                                "recipe_version": "recipe-1",
                                "temperature_c": "50",
                                "speed_m_per_min": "6",
                            }.items():
                                await form.locator(f"[name={name}]").fill(value)
                        async with page.expect_response(
                            lambda r: r.url == origin + "/commands" and r.request.method == "POST"
                        ) as submitted:
                            await form.locator("button[type=submit]").click()
                        response = await submitted.value
                        require(response.status == 200, f"UI {kind} HTTP request failed")
                        outcome = await response.json()
                        require(
                            outcome["submission"]["disposition"] == "ACCEPTED",
                            f"UI {kind} rejected: " + str(outcome["submission"].get("reason_code")),
                        )
                        ident = outcome["submission"]["command_id"]
                        deadline = time.monotonic() + 20
                        while time.monotonic() < deadline:
                            result = await context.request.get(
                                origin + "/command-record", params={"command_id": ident}
                            )
                            result = await result.json()
                            if result["status"] == "SUCCEEDED":
                                break
                            require(result["status"] == "ACCEPTED", f"UI {kind} failed")
                            await asyncio.sleep(0.1)
                        else:
                            raise AssertionError(f"UI {kind} completion timed out")
                        record = page.locator(".command-record").filter(has_text=ident)
                        await record.get_by_text("성공 결과 수신", exact=True).wait_for()
                        return ident, response.request.post_data_json, response.request.headers

                    await command("LOAD_JOB")
                    print("Browser: waiting for physical preheat", flush=True)
                    await wait_state(
                        lambda s: s["state"]["state"] == "READY", "Real preheat timed out"
                    )
                    start_id, original, original_headers = await command("START")
                    print(
                        "Browser: signed persistent test sensor bias and independent physical heat",
                        flush=True,
                    )
                    offset = page.locator("#inputs-form [name=temperature_offset_c]")
                    for bias in (-15, 20):
                        await offset.fill(str(bias))
                        async with page.expect_response(
                            lambda r: r.url == origin + "/inputs" and r.request.method == "POST"
                        ) as applied:
                            await page.locator("#inputs-form button[type=submit]").click()
                        require(
                            (await applied.value).status == 200, "Signed sensor bias input failed"
                        )
                        biased = await snapshot()
                        require(
                            biased["state"]["inputs"]["temperature_offset_c"] == bias
                            and abs(
                                biased["state"]["inputs"]["temperature_c"]
                                - max(0, biased["process"]["temperature_c"] + bias)
                            )
                            < 1e-6,
                            "Sensor bias was not applied once to the real process temperature",
                        )
                    first_tick = int(biased["process"]["tick"])
                    await page.wait_for_timeout(5500)
                    sustained = await snapshot()
                    require(
                        int(sustained["process"]["tick"]) - first_tick >= 50
                        and sustained["state"]["state"] == "RUNNING"
                        and sustained["state"]["inputs"]["run_feedback"]
                        and sustained["state"]["inputs"]["temperature_offset_c"] == 20
                        and abs(
                            sustained["state"]["inputs"]["temperature_c"]
                            - sustained["process"]["temperature_c"]
                            - 20
                        )
                        < 1e-6,
                        "Real 100ms ticker did not sustain the running sensor-bias scenario",
                    )
                    require(
                        "적용 중" in await page.locator("#temperature-offset-status").inner_text(),
                        "Active test bias was not visible in the console",
                    )
                    await page.locator("#clear-temperature-offset").click()
                    require(await offset.input_value() == "0", "Bias clear did not reset the form")
                    require(
                        (await snapshot())["state"]["inputs"]["temperature_offset_c"] == 20,
                        "Form clear unexpectedly changed the live PLC inputs",
                    )
                    async with page.expect_response(
                        lambda r: r.url == origin + "/inputs" and r.request.method == "POST"
                    ) as cleared:
                        await page.locator("#inputs-form button[type=submit]").click()
                    require((await cleared.value).status == 200, "Sensor-bias clear failed")
                    restored = await snapshot()
                    require(
                        restored["state"]["inputs"]["temperature_offset_c"] == 0
                        and restored["state"]["inputs"]["temperature_c"]
                        == restored["process"]["temperature_c"],
                        "Zero bias did not immediately restore the physical measurement",
                    )
                    print(
                        "Browser: Sealer field overlay, lost-response GET recovery, live process",
                        flush=True,
                    )
                    field_form = page.locator("#field-fault-form")
                    await field_form.locator("[name=group]").select_option("Sealer")
                    await field_form.locator("[name=mode]").select_option("FROZEN")
                    posted = [0]

                    async def lose_field_response(route):
                        if route.request.method == "POST":
                            posted[0] += 1
                            committed = await route.fetch()
                            require(
                                committed.status == 200,
                                "Field mode did not commit before response loss",
                            )
                            await route.abort("failed")
                        else:
                            await route.continue_()

                    await page.route("**/field-faults/Sealer", lose_field_response)
                    await field_form.locator("button[type=submit]").click()
                    await (
                        page.locator("#field-fault-result")
                        .filter(has_text="확정하지 않았습니다")
                        .wait_for()
                    )
                    require(
                        await page.locator("#field-fault-apply").is_disabled(),
                        "Uncertain field input allowed another mutation before GET recovery",
                    )
                    await page.unroute("**/field-faults/Sealer", lose_field_response)
                    await page.wait_for_timeout(400)
                    require(posted[0] == 1, "Field input was automatically reposted")
                    async with page.expect_response(
                        lambda r: r.url == origin + "/field-faults" and r.request.method == "GET"
                    ) as observed:
                        await page.locator("#field-fault-refresh").click()
                    require(
                        (await observed.value).status == 200,
                        "Passive field configuration recovery failed",
                    )

                    def sealer_field(snapshot):
                        return next(
                            row
                            for row in snapshot["field_channels"]
                            if row["configured"]["group_id"] == "Sealer"
                        )

                    frozen = await wait_state(
                        lambda s: (
                            sealer_field(s)["configured"]["mode"] == "FROZEN"
                            and not sealer_field(s)["pending"]
                        ),
                        "Frozen field frame was not observed",
                    )
                    frozen_fields = sealer_field(frozen)["published"]["fields"]
                    frozen_heartbeat = next(
                        row for row in frozen_fields if row["tag_id"] == "Sealer.LinkHeartbeat"
                    )
                    frozen_tick = int(frozen["process"]["tick"])
                    await page.wait_for_timeout(5500)
                    during_fault = await snapshot()
                    require(
                        int(during_fault["process"]["tick"]) - frozen_tick >= 50
                        and during_fault["state"]["state"] == "RUNNING"
                        and during_fault["state"]["inputs"]["run_feedback"]
                        and sealer_field(during_fault)["published"]["fields"] == frozen_fields,
                        "Field freeze changed its original frame or stopped the real ticker",
                    )
                    require(
                        str(during_fault["tags"]["Line.Heartbeat"])
                        != str(frozen_heartbeat["reading"]["value"]),
                        "Sealer field freeze also froze the Line heartbeat",
                    )
                    for mode, code in (
                        ("BAD", 2156658688),
                        ("UNCERTAIN", 1083375616),
                        ("NORMAL", 0),
                    ):
                        await field_form.locator("[name=mode]").select_option(mode)
                        async with page.expect_response(
                            lambda r: (
                                r.url == origin + "/field-faults/Sealer"
                                and r.request.method == "POST"
                            )
                        ) as changed_field:
                            await field_form.locator("button[type=submit]").click()
                        require(
                            (await changed_field.value).status == 200,
                            "Scoped field quality input failed",
                        )
                        effective = await wait_state(
                            lambda s, requested_mode=mode: (
                                sealer_field(s)["configured"]["mode"] == requested_mode
                                and not sealer_field(s)["pending"]
                            ),
                            "Requested field publication was not observed",
                        )
                        require(
                            all(
                                item["status_code"] == code
                                for item in sealer_field(effective)["published"]["fields"]
                            ),
                            "Selected group did not retain the exact field quality code",
                        )
                        require(
                            effective["state"]["state"] == "RUNNING"
                            and effective["state"]["latched_fault_code"] == 0,
                            "A field publication fault changed physical PLC safety state",
                        )
                        require(
                            all(
                                row["configured"]["mode"] == "NORMAL"
                                for row in effective["field_channels"]
                                if row["configured"]["group_id"] != "Sealer"
                            ),
                            "A Sealer field input modified another equipment group",
                        )
                    # Failure modes retain their original NORMAL evidence while OPC stays live.
                    for failure_mode, failure_code in (
                        ("READ_FAILURE", 2147811328),
                        ("LINK_DOWN", 2156527616),
                    ):
                        await field_form.locator("[name=mode]").select_option(failure_mode)
                        if failure_mode == "READ_FAILURE":
                            posted[0] = 0
                            await page.route("**/field-faults/Sealer", lose_field_response)
                            await field_form.locator("button[type=submit]").click()
                            await (
                                page.locator("#field-fault-result")
                                .filter(has_text="확정하지 않았습니다")
                                .wait_for()
                            )
                            require(
                                await page.locator("#field-fault-apply").is_disabled(),
                                "Lost failure response allowed another mutation",
                            )
                            await page.unroute("**/field-faults/Sealer", lose_field_response)
                            await page.wait_for_timeout(400)
                            require(posted[0] == 1, "Failure input was automatically reposted")
                            async with page.expect_response(
                                lambda r: (
                                    r.url == origin + "/field-faults" and r.request.method == "GET"
                                )
                            ) as observed_failure:
                                await page.locator("#field-fault-refresh").click()
                            require(
                                (await observed_failure.value).status == 200,
                                "Failure response recovery did not use passive GET",
                            )
                        else:
                            async with page.expect_response(
                                lambda r: (
                                    r.url == origin + "/field-faults/Sealer"
                                    and r.request.method == "POST"
                                )
                            ) as entered_failure:
                                await field_form.locator("button[type=submit]").click()
                            require(
                                (await entered_failure.value).status == 200,
                                "Field link-down input failed",
                            )
                        failed = await wait_state(
                            lambda current, requested=failure_mode: (
                                sealer_field(current)["configured"]["mode"] == requested
                                and not sealer_field(current)["pending"]
                            ),
                            "Failure publication was not observed",
                        )
                        checkpoint = sealer_field(failed)["configured"]["normal_checkpoint"]
                        original_fields = {item["tag_id"]: item for item in checkpoint["fields"]}
                        failed_fields = sealer_field(failed)["published"]["fields"]
                        require(
                            all(item["status_code"] == 0 for item in checkpoint["fields"]),
                            "Actual CLI NORMAL source quality was changed in checkpoint",
                        )
                        for item in failed_fields:
                            source_field = original_fields[item["tag_id"]]
                            require(
                                item["reading"] == source_field["reading"]
                                and item["source_timestamp"] == source_field["source_timestamp"]
                                and item["status_code"] == failure_code,
                                "Failure output changed original evidence or exact code",
                            )
                        failed_tick = int(failed["process"]["tick"])
                        await page.wait_for_timeout(5500)
                        continuous = await snapshot()
                        require(
                            int(continuous["process"]["tick"]) - failed_tick >= 50
                            and continuous["state"]["state"] == "RUNNING"
                            and continuous["state"]["inputs"]["run_feedback"]
                            and continuous["state"]["latched_fault_code"] == 0
                            and sealer_field(continuous)["published"]["fields"] == failed_fields,
                            "Failure overlay stopped production or refreshed its held frame",
                        )
                        require(
                            all(
                                row["configured"]["mode"] == "NORMAL"
                                for row in continuous["field_channels"]
                                if row["configured"]["group_id"] != "Sealer"
                            ),
                            "Failure overlay changed another equipment group",
                        )
                        require(
                            "원본 품질"
                            in await page.locator('[data-field-group="Sealer"]').inner_text(),
                            "Original quality was not separately visible",
                        )
                        await field_form.locator("[name=mode]").select_option("NORMAL")
                        async with page.expect_response(
                            lambda r: (
                                r.url == origin + "/field-faults/Sealer"
                                and r.request.method == "POST"
                            )
                        ) as normal_response:
                            await field_form.locator("button[type=submit]").click()
                        require(
                            (await normal_response.value).status == 200,
                            "Failure NORMAL restoration failed",
                        )
                        fresh = await wait_state(
                            lambda current: (
                                sealer_field(current)["configured"]["mode"] == "NORMAL"
                                and not sealer_field(current)["pending"]
                            ),
                            "Fresh NORMAL restoration was not published",
                        )
                        require(
                            all(
                                item["status_code"] == 0
                                and datetime.fromisoformat(item["source_timestamp"])
                                > datetime.fromisoformat(
                                    original_fields[item["tag_id"]]["source_timestamp"]
                                )
                                for item in sealer_field(fresh)["published"]["fields"]
                            ),
                            "NORMAL restored cached rather than fresh acquisition evidence",
                        )
                        print(
                            f"Browser: {failure_mode}: 50 live ticks; fresh NORMAL",
                            flush=True,
                        )
                    print(
                        "Browser: DELAY6s real CLI, original evidence; controlled gateway3/5",
                        flush=True,
                    )
                    from apps.gateway.opcua_client import GatewayOPCUAClient, GatewayOPCUAConfig
                    from apps.simulator.certificates import GATEWAY_URI, SERVER_URI

                    # Only the gateway freshness threshold clock is controlled. The real
                    # CLI publisher's six-second queue, UTC timestamps and production
                    # ticker use actual clocks. No fake elapsed production is claimed.
                    gateway_mono = [0.0]
                    collector = GatewayOPCUAClient(
                        GatewayOPCUAConfig(
                            endpoint=f"opc.tcp://127.0.0.1:{opc_port}/browser/",
                            application_uri=GATEWAY_URI,
                            client_certificate=certs / "gateway.der",
                            client_private_key=certs / "gateway.pem",
                            trusted_server_certificate=certs / "server.der",
                            server_application_uri=SERVER_URI,
                        ),
                        monotonic_clock=lambda: gateway_mono[0],
                    )
                    try:
                        await collector.connect()
                        require(
                            (await collector.read_tags()).health.state == "GOOD",
                            "NORMAL OPC heartbeat baseline was not Good",
                        )
                        await field_form.locator("[name=mode]").select_option("DELAY")
                        await field_form.locator("[name=delay_ms]").fill("6000")
                        posted[0] = 0
                        await page.route("**/field-faults/Sealer", lose_field_response)
                        await field_form.locator("button[type=submit]").click()
                        await (
                            page.locator("#field-fault-result")
                            .filter(has_text="확정하지 않았습니다")
                            .wait_for()
                        )
                        require(
                            await page.locator("#field-fault-apply").is_disabled(),
                            "Lost DELAY response allowed another mutation",
                        )
                        await page.unroute("**/field-faults/Sealer", lose_field_response)
                        async with page.expect_response(
                            lambda r: (
                                r.url == origin + "/field-faults" and r.request.method == "GET"
                            )
                        ) as delay_recovery:
                            await page.locator("#field-fault-refresh").click()
                        require(
                            (await delay_recovery.value).status == 200 and posted[0] == 1,
                            "DELAY recovery did not use one POST then GET only",
                        )
                        print("Browser DELAY: response lost and GET recovered", flush=True)
                        waiting = await wait_state(
                            lambda current: (
                                sealer_field(current)["configured"]["mode"] == "DELAY"
                                and sealer_field(current)["publication_runtime"] is not None
                                and sealer_field(current)["publication_runtime"][
                                    "waiting_for_first_completion"
                                ]
                            ),
                            "Actual DELAY first completion Waiting was not visible",
                        )
                        channel = sealer_field(waiting)
                        require(
                            channel["configured"]["delay_ms"] == 6000 and not channel["pending"],
                            "Configured/published DELAY was confused with first-read Waiting",
                        )
                        require(
                            channel["publication_runtime"]["read_in_flight"],
                            "Single-flight read was not exposed separately",
                        )
                        require(
                            "첫 읽기 완료 대기"
                            in await page.locator('[data-field-group="Sealer"]').inner_text(),
                            "Console did not show first completion Waiting",
                        )
                        # Actual OPC Waiting quality must survive the typed gateway read.
                        raw_waiting = await collector.read_tags()
                        sealer_waiting = [
                            sample
                            for sample in raw_waiting.samples
                            if sample.tag_id.startswith("Sealer.")
                        ]
                        require(
                            all(sample.status_code >> 30 == 2 for sample in sealer_waiting),
                            "First completion Waiting was replaced by Good raw quality",
                        )
                        gateway_mono[0] = 3.0
                        require(
                            (await collector.read_tags()).health.state == "DELAYED",
                            "Controlled monotonic three-second heartbeat boundary was not DELAYED",
                        )
                        gateway_mono[0] = 5.0
                        require(
                            (await collector.read_tags()).health.state == "ALARM",
                            "Controlled monotonic five-second heartbeat boundary was not ALARM",
                        )
                        print(
                            "Browser DELAY: Waiting raw quality and controlled3/5 passed",
                            flush=True,
                        )
                        physical_tick = int(waiting["process"]["tick"])
                        completed = await wait_state(
                            lambda current: (
                                sealer_field(current)["configured"]["mode"] == "DELAY"
                                and sealer_field(current)["publication_runtime"] is not None
                                and not sealer_field(current)["publication_runtime"][
                                    "waiting_for_first_completion"
                                ]
                            ),
                            "Real six-second delayed acquisition never completed",
                            timeout=30,
                        )
                        publication = sealer_field(completed)["published"]
                        heartbeat = next(
                            item
                            for item in publication["fields"]
                            if item["tag_id"] == "Sealer.LinkHeartbeat"
                        )
                        require(
                            datetime.fromisoformat(publication["published_at"])
                            - datetime.fromisoformat(heartbeat["source_timestamp"])
                            >= timedelta(seconds=5.5),
                            "Completed DELAY repainted the original acquisition timestamp",
                        )
                        require(
                            completed["state"]["state"] == "RUNNING"
                            and completed["state"]["inputs"]["run_feedback"]
                            and completed["state"]["latched_fault_code"] == 0
                            and int(completed["process"]["tick"]) > physical_tick,
                            "Actual physical production did not continue through DELAY",
                        )
                        gateway_mono[0] = 6.0
                        delivered = await collector.read_tags()
                        actual_heartbeat = next(
                            sample
                            for sample in delivered.samples
                            if sample.tag_id == "Sealer.LinkHeartbeat"
                        )
                        require(
                            actual_heartbeat.reading.model_dump(mode="json") == heartbeat["reading"]
                            and actual_heartbeat.source_timestamp
                            == datetime.fromisoformat(heartbeat["source_timestamp"])
                            and actual_heartbeat.status_code == heartbeat["status_code"],
                            "TLS OPC did not preserve the completed acquisition value/time/quality",
                        )
                        require(
                            delivered.health.state == "GOOD",
                            "New completed heartbeat did not recover controlled freshness",
                        )
                        # UI cannot change duration in-place; the real backend CAS also
                        # rejects it. Obtain a genuine authenticated request template,
                        # without committing it, then use exact explicit JSON requests.
                        print(
                            "Browser DELAY: real completion, HB/time and live production passed",
                            flush=True,
                        )
                        template = {}

                        async def capture_delay_request(route):
                            template["headers"] = {
                                key: value
                                for key, value in route.request.headers.items()
                                if key.lower() not in {"content-length", "host", "connection"}
                            }
                            template["body"] = route.request.post_data_json
                            await route.abort("failed")

                        await page.route("**/field-faults/Sealer", capture_delay_request)
                        await field_form.locator("[name=delay_ms]").fill("6000")
                        await field_form.locator("button[type=submit]").click()
                        await (
                            page.locator("#field-fault-result")
                            .filter(has_text="확정하지 않았습니다")
                            .wait_for()
                        )
                        await page.unroute("**/field-faults/Sealer", capture_delay_request)
                        require(
                            template["body"]["delay_ms"] == 6000,
                            "DELAY HTTP input was not a strict integer",
                        )

                        async def field_json(body):
                            response = await context.request.post(
                                origin + "/field-faults/Sealer",
                                headers=template["headers"],
                                data=body,
                            )
                            return response.status, await response.json()

                        print("Browser DELAY: original HTTP template captured", flush=True)
                        current_revision = sealer_field(await snapshot())["configured"]["revision"]
                        status, _ = await field_json(
                            {
                                "mode": "DELAY",
                                "expected_revision": current_revision,
                                "delay_ms": 6100,
                            }
                        )
                        require(status == 409, "Duration changed without NORMAL actual publication")
                        stale = str(int(current_revision) - 1)
                        status, _ = await field_json({"mode": "NORMAL", "expected_revision": stale})
                        require(status == 409, "Stale field CAS was accepted")
                        # Clear uncertainty only with the existing passive GET control.
                        await page.locator("#field-fault-refresh").click()
                        # Respect the console's strict CSP; use locator state rather
                        # than a polling Function constructor in the page context.
                        await page.locator("#field-fault-apply:not(:disabled)").wait_for(
                            state="visible"
                        )
                        await field_form.locator("[name=mode]").select_option("NORMAL")
                        async with page.expect_response(
                            lambda r: (
                                r.url == origin + "/field-faults/Sealer"
                                and r.request.method == "POST"
                            )
                        ) as normal_delay:
                            await field_form.locator("button[type=submit]").click()
                        require(
                            (await normal_delay.value).status == 200,
                            "Explicit NORMAL restoration failed",
                        )
                        normal = await wait_state(
                            lambda current: (
                                sealer_field(current)["configured"]["mode"] == "NORMAL"
                                and not sealer_field(current)["pending"]
                            ),
                            "Actual NORMAL publication not confirmed",
                        )
                        require(
                            all(
                                item["status_code"] == 0
                                for item in sealer_field(normal)["published"]["fields"]
                            ),
                            "NORMAL restoration did not retain actual Good readings",
                        )
                        print(
                            "Browser: DELAY Waiting -> completion; original HB/time; "
                            "production; GET-only loss; NORMAL/CAS",
                            flush=True,
                        )
                    finally:
                        await collector.disconnect()
                    await wait_state(
                        lambda s: (
                            int(s["process"]["good_quantity"])
                            + int(s["process"]["reject_quantity"])
                            > 0
                        ),
                        "Real server ticker produced no completed item",
                    )
                    await command("PAUSE")
                    paused = await wait_state(
                        lambda s: (
                            s["state"]["state"] == "PAUSED"
                            and not s["state"]["inputs"]["run_feedback"]
                        ),
                        "PAUSE did not confirm stopped feedback",
                    )
                    print("Browser: original-ID replay while paused", flush=True)
                    # Real browser same-origin replay, with the original ID and exact body.
                    # No new request ID is invented and no transport response is mocked.
                    duplicate = await page.evaluate(
                        """async ({body, csrf}) => {
                          const response = await fetch('/commands', {
                            method: 'POST', credentials: 'same-origin',
                            headers: {'Content-Type':'application/json','X-CSRF-Token':csrf},
                            body: JSON.stringify(body)
                          });
                          return {status: response.status, body: await response.json()};
                        }""",
                        {"body": original, "csrf": original_headers["x-csrf-token"]},
                    )
                    require(duplicate["status"] == 200, "Same-ID replay HTTP request failed")
                    require(not duplicate["body"]["created"], "Same ID scheduled new work")
                    after = await snapshot()
                    require(
                        after["state"]["control_version"] == paused["state"]["control_version"]
                        and after["process"]["input_quantity"]
                        == paused["process"]["input_quantity"],
                        "Same-ID replay changed control state or admitted new material",
                    )
                    await page.locator("#lookup-id").fill(start_id)
                    await page.locator("#lookup-form button").click()
                    await command("END_RUN")
                    summary_response = await context.request.get(
                        origin + "/run-summary", params={"execution_id": "browser/A"}
                    )
                    final = await summary_response.json()
                    require(
                        final["final"] and final["wip_quantity"] == 0,
                        "END lacks final boundary",
                    )
                    require(
                        int(final["input_quantity"])
                        == int(final["good_quantity"])
                        + int(final["reject_quantity"])
                        + int(final["scrap_quantity"]),
                        "Final quantities do not conserve material",
                    )
                    await page.locator("#settle-run").click()
                    print("Browser: local final-quantity settlement", flush=True)
                    await wait_state(
                        lambda s: s["state"]["previous_execution_settled"],
                        "UI settlement did not attest the final run",
                    )
                    await command("LOAD_JOB", "browser-B")
                    second = await snapshot()
                    require(
                        second["process"]["input_quantity"] == "0", "Second job inherited counts"
                    )
                    preserved = await context.request.get(
                        origin + "/run-summary", params={"execution_id": "browser/A"}
                    )
                    require(
                        await preserved.json() == final,
                        "Second job changed the original final summary",
                    )
                    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as verification:
                        events = [
                            json.loads(row[0])
                            for row in verification.execute(
                                "SELECT body FROM source_events ORDER BY seq"
                            )
                        ]
                        produced = [
                            event for event in events if event["kind"].startswith("PRODUCTION_")
                        ]
                        require(
                            len(produced)
                            == int(final["good_quantity"]) + int(final["reject_quantity"])
                            and all(event["execution_id"] == "browser/A" for event in produced),
                            "Durable production events lost their original execution identity",
                        )
                    await page.locator("#refresh").click()
                    await page.locator("#execution-label").filter(has_text="browser-B").wait_for()
                    screenshot.parent.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(screenshot), full_page=True)
                    print("Browser: logout", flush=True)
                    async with page.expect_response(
                        lambda r: r.url == origin + "/auth/logout"
                    ) as response:
                        await page.locator("#logout").click()
                    require((await response.value).status == 200, "UI logout failed")
                    await page.locator("#login-panel").wait_for(state="visible")
                    require(
                        (await context.request.get(origin + "/snapshot")).status == 401,
                        "Logged-out browser retained equipment access",
                    )
                    require(not errors, "Browser raised an uncaught JavaScript exception")
                except Exception:
                    safe_frames = [
                        f"{frame.name}:{frame.lineno}"
                        for frame in traceback.extract_tb(sys.exc_info()[2])
                        if Path(frame.filename).resolve() == Path(__file__).resolve()
                    ]
                    print("Browser script frames: " + ", ".join(safe_frames), file=sys.stderr)
                    print(
                        "Browser notice: " + await page.locator("#notice").inner_text(),
                        file=sys.stderr,
                    )
                    print("Browser JavaScript error types: " + repr(errors), file=sys.stderr)
                    raise
                finally:
                    await browser.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            require(process.returncode == 0, "Simulator did not shut down cleanly")
    logs = log_path.read_bytes()
    require(
        all(secret.encode() not in logs for secret in (initial, changed)),
        "Server logs exposed a test credential",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", default=os.environ.get("CHROMIUM_EXECUTABLE", "chromium"))
    parser.add_argument("--screenshot", type=Path, default=ROOT / ".local/qa/simulator-console.png")
    args = parser.parse_args()
    executable = shutil.which(args.chromium)
    if executable is None:
        print("FAIL: Chromium is required; provide --chromium /path/to/chromium", file=sys.stderr)
        return 1
    try:
        with tempfile.TemporaryDirectory(prefix="hmi-mes-browser-") as directory:
            asyncio.run(exercise(Path(directory), executable, args.screenshot.resolve()))
    except ImportError:
        print(
            "FAIL: Install project development dependencies, including Playwright", file=sys.stderr
        )
        return 1
    except Exception as error:
        # Do not print browser exception objects that may contain filled credentials.
        message = str(error) if type(error) is AssertionError else type(error).__name__
        print("FAIL: " + message, file=sys.stderr)
        return 1
    print("PASS: Real browser authentication, recovery, production, pause/end, same-ID replay,")
    print("      final settlement, second-job isolation and logout; clean server shutdown.")
    print("Screenshot: " + str(args.screenshot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
