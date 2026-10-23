#!/usr/bin/env python3
"""Local live acceptance: browser -> API -> PostgreSQL -> gateway -> encrypted OPC UA.

Uses disposable PostgreSQL and real CLI processes with default 100ms physics.
No injected telemetry, command journal, simulated clock, or production events.
Requires a built apps/web/dist (build with VITE_SCREEN_TRANSPORT=polling), Chromium,
Playwright and the integration PostgreSQL Docker image. This proves the local
live path, not a public deployment. All servers/container are cleaned in finally.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_spec = importlib.util.spec_from_file_location(
    "app_a_browser_helpers", ROOT / "scripts/check-app-a-browser.py"
)
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
require = _helpers.require
STAGE = "setup"


def checkpoint(value):
    global STAGE
    STAGE = value
    print("Live local: " + value, flush=True)


def protected(directory, name, value):
    path = directory / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(value + "\n")
    return path


async def exercise(
    directory,
    executable,
    web_dir,
    screenshot,
    evidence,
    capture_dir=None,
    analytics_dir=None,
    guest_dir=None,
):
    import httpx
    from alembic import command
    from playwright.async_api import async_playwright, expect

    from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
    from apps.api.database import create_database_engine, migration_config
    from apps.gateway.buffer import GatewayBuffer
    from apps.simulator.certificates import (
        GATEWAY_URI,
        SERVER_URI,
        provision_development_certificates,
    )
    from apps.simulator.storage import SimulatorStorage

    children, handles = [], []
    credentials = []
    sim_port, opc_port, intake_port, user_port = [_helpers.free_port() for _ in range(4)]
    require(
        len({sim_port, opc_port, intake_port, user_port}) == 4, "Distinct listener ports required"
    )
    sim_origin, origin = f"http://127.0.0.1:{sim_port}", f"http://127.0.0.1:{user_port}"
    opc_endpoint = f"opc.tcp://127.0.0.1:{opc_port}/live-acceptance/"
    certs = directory / "certificates"
    provision_development_certificates(certs)
    sim_db = directory / "simulator.sqlite3"
    queue = directory / "gateway.sqlite3"
    sim_initial, sim_changed, initial, changed, token = [
        secrets.token_urlsafe(32) for _ in range(5)
    ]
    credentials.extend([sim_initial, sim_changed, initial, changed, token])
    sim_secret = protected(directory, "sim-password", sim_initial)
    user_secret = protected(directory, "user-password", initial)
    token_file = protected(directory, "service-token", token)
    base = [sys.executable, "-m"]

    def run_cli(args, env=None):
        result = subprocess.run(base + args, cwd=ROOT, env=env, capture_output=True, timeout=30)
        require(result.returncode == 0, "Explicit CLI provisioning failed")
        require(
            all(value.encode() not in result.stdout + result.stderr for value in credentials),
            "CLI exposed a credential",
        )

    def launch(name, args, env=None):
        handle = (directory / (name + ".log")).open("wb")
        handles.append(handle)
        child = subprocess.Popen(base + args, cwd=ROOT, env=env, stdout=handle, stderr=handle)
        children.append(child)
        return child

    async def ready(url, process):
        async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                require(process.poll() is None, "A live server exited before readiness")
                try:
                    response = await client.get(url + "/auth/me")
                    require(response.status_code == 401, "Anonymous session was not denied")
                    return
                except httpx.TransportError:
                    await asyncio.sleep(0.1)
        require(False, "HTTP readiness timed out")

    async def wait_json(getter, predicate, timeout, description):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            require(
                all(child.poll() is None for child in children),
                "A live CLI process stopped unexpectedly",
            )
            value = await getter()
            if predicate(value):
                return value
            await asyncio.sleep(0.25)
        require(False, description)

    @contextmanager
    def running_processes():
        try:
            yield
        finally:
            for child in reversed(children):
                if child.poll() is None:
                    child.terminate()
            for child in reversed(children):
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)

    try:
        with _helpers.disposable_postgres(directory) as url, running_processes():
            env = os.environ.copy()
            env["HMI_DATABASE_DSN"] = url.render_as_string(hide_password=False)
            credentials.append(env["HMI_DATABASE_DSN"])
            engine = create_database_engine(url)
            try:
                with engine.connect() as connection:
                    command.upgrade(migration_config(connection), "head")
                bootstrap_reference_data(engine)
            finally:
                engine.dispose()
            checkpoint("provision disposable users, certificates and source identity")
            run_cli(
                [
                    "apps.simulator",
                    "bootstrap-user",
                    "--database",
                    str(sim_db),
                    "--origin",
                    sim_origin,
                    "--login",
                    "live-operator",
                    "--password-file",
                    str(sim_secret),
                    "--role",
                    "simulator_operator",
                ]
            )
            # Read the provisioned identity before starting the real simulator; no runtime mutation.
            storage = SimulatorStorage(sim_db)
            identity = storage.identity
            storage.close()
            buffer = GatewayBuffer(queue, gateway_id="live-gateway")
            generation = buffer.capture_position[0]
            buffer.close()
            scope_args = ["--source-id", identity.source_id, "--stream-id", identity.stream_id]
            run_cli(
                [
                    "apps.api",
                    "configure-source",
                    *scope_args,
                    "--gateway-id",
                    "live-gateway",
                    "--capture-generation",
                    str(generation),
                    "--line-id",
                    LINE_ID,
                ],
                env,
            )
            run_cli(
                [
                    "apps.api",
                    "bootstrap-user",
                    "--login",
                    "live-operator",
                    "--role",
                    "operator",
                    "--role",
                    "production_manager",
                    "--origin",
                    origin,
                    "--password-file",
                    str(user_secret),
                ],
                env,
            )
            if guest_dir is not None:
                run_cli(["apps.api", "bootstrap-guest", "--origin", origin], env)
            sim_secret.unlink()
            user_secret.unlink()
            simulator = launch(
                "simulator",
                [
                    "apps.simulator",
                    "serve",
                    "--database",
                    str(sim_db),
                    "--port",
                    str(sim_port),
                    "--origin",
                    sim_origin,
                    "--opc-endpoint",
                    opc_endpoint,
                    "--server-certificate",
                    str(certs / "server.der"),
                    "--server-private-key",
                    str(certs / "server.pem"),
                    "--trusted-client-certificate",
                    str(certs / "gateway.der"),
                ],
            )
            launch(
                "internal-api",
                [
                    "apps.api",
                    "serve",
                    *scope_args,
                    "--gateway-id",
                    "live-gateway",
                    "--capture-generation",
                    str(generation),
                    "--key-id",
                    "live-service",
                    "--token-file",
                    str(token_file),
                    "--expires-at",
                    (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    "--port",
                    str(intake_port),
                ],
                env,
            )
            users = launch(
                "user-api",
                [
                    "apps.api",
                    "serve-user",
                    "--origin",
                    origin,
                    "--port",
                    str(user_port),
                    "--web-dir",
                    str(web_dir),
                    *(["--enable-guest-viewer"] if guest_dir is not None else []),
                ],
                env,
            )
            await ready(sim_origin, simulator)
            await ready(origin, users)
            launch(
                "gateway",
                [
                    "apps.gateway",
                    "serve",
                    *scope_args,
                    "--gateway-id",
                    "live-gateway",
                    "--queue-file",
                    str(queue),
                    "--opc-endpoint",
                    opc_endpoint,
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
                    f"http://127.0.0.1:{intake_port}",
                    "--token-file",
                    str(token_file),
                    "--allow-loopback-http",
                    "--enable-commands",
                    "--enable-settlement",
                    "--poll-interval",
                    ".25",
                ],
            )
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    executable_path=executable,
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                try:
                    sim_context = await browser.new_context(
                        viewport={"width": 1440, "height": 1080}
                    )
                    sim = await sim_context.new_page()
                    sim.set_default_timeout(20_000)
                    sim.on("dialog", lambda dialog: dialog.accept())
                    checkpoint("simulator browser login and mandatory password change")
                    await sim.goto(sim_origin)
                    await sim.locator("#login-form [name=login]").fill("live-operator")
                    await sim.locator("#login-form [name=password]").fill(sim_initial)
                    await sim.locator("#login-form button").click()
                    await sim.locator("#password-form [name=password]").fill(sim_changed)
                    await sim.locator("#password-form [name=confirm]").fill(sim_changed)
                    await sim.locator("#password-form button").click()
                    await sim.locator("#password-panel").wait_for(state="hidden")

                    async def sim_snapshot():
                        response = await sim_context.request.get(sim_origin + "/snapshot")
                        require(response.status == 200, "Simulator snapshot failed")
                        return await response.json()

                    initial_state = await sim_snapshot()
                    require(
                        initial_state["state"]["recovery_required"]
                        and not initial_state["state"]["run_output"],
                        "Startup did not require stopped-output recovery",
                    )
                    checkpoint("five explicit operator recovery checks; select REMOTE/AUTO")
                    for name in (
                        "outputs_stopped",
                        "positions_verified",
                        "storage_reconciled",
                        "commands_reconciled",
                        "operator_confirmed",
                    ):
                        await sim.locator(f"#recovery-form [name={name}]").check()
                    async with sim.expect_response(
                        lambda r: r.url == sim_origin + "/recover"
                    ) as recovered:
                        await sim.locator("#recovery-form button[type=submit]").click()
                    require((await recovered.value).status == 200, "Operator recovery failed")
                    checkpoint("recovery response received; select REMOTE/AUTO")
                    await sim.locator("#mode-form [name=control_mode]").select_option("REMOTE")
                    await sim.locator("#mode-form [name=operation_mode]").select_option("AUTO")
                    async with sim.expect_response(
                        lambda r: r.url == sim_origin + "/inputs"
                    ) as selected:
                        await sim.locator("#mode-form button").click()
                    require(
                        (await selected.value).status == 200,
                        "Operator REMOTE/AUTO selection failed",
                    )
                    checkpoint("REMOTE/AUTO response received")
                    context = await browser.new_context(
                        viewport={"width": 1440, "height": 1080}, locale="ko-KR"
                    )
                    page = await context.new_page()
                    page.set_default_timeout(25_000)
                    errors = []
                    page.on("pageerror", lambda e: errors.append(type(e).__name__))
                    websocket_urls = []
                    page.on("websocket", lambda ws: websocket_urls.append(ws.url))
                    checkpoint("App A browser login and mandatory password change")
                    await page.goto(origin)
                    await page.locator("#login").fill("live-operator")
                    await page.locator("#login-password").fill(initial)
                    await page.locator("#login-submit").click()
                    await page.locator("#new-password").fill(changed)
                    await page.locator("#confirm-password").fill(changed)
                    await page.locator("#password-submit").click()
                    await page.locator("#workspace-drafts").click()
                    checkpoint("browser registers 150 C / 10 m/min recipe and target-20 work order")
                    for field, value in {
                        "id": "live-recipe-v1",
                        "family": "live-recipe",
                        "version": "v1",
                        "item": "live-item",
                        "temperature": "150",
                        "speed": "10",
                    }.items():
                        await page.locator("#mes-recipe-" + field).fill(value)
                    await page.locator("#mes-recipe-submit").click()
                    checkpoint("recipe submitted; await confirmed registration")
                    await expect(page.locator("#mes-recipe-status")).to_have_attribute(
                        "data-state", "confirmed"
                    )
                    for field, value in {
                        "id": "live-order",
                        "target": "20",
                        "date": datetime.now(UTC).date().isoformat(),
                    }.items():
                        await page.locator("#mes-order-" + field).fill(value)
                    await page.locator("#mes-order-line").select_option(LINE_ID)
                    checkpoint("work order form prepared")
                    await page.locator("#mes-order-submit").click()
                    checkpoint("work order submitted; await confirmed registration")
                    await expect(page.locator("#mes-order-status")).to_have_attribute(
                        "data-state", "confirmed"
                    )
                    checkpoint("release registered work order")
                    await page.locator('[data-release-order="live-order"]').click()
                    await page.locator("#release-submit").click()
                    await expect(page.locator("#workflow-status")).to_have_attribute(
                        "data-state", "confirmed"
                    )
                    checkpoint("acknowledge confirmed release tracker")
                    await page.locator("#workflow-acknowledge").click()
                    await (
                        page.locator("section.mes-card")
                        .filter(has=page.locator("#mes-orders-heading"))
                        .get_by_role("button", name="목록 새로 조회", exact=True)
                        .click()
                    )
                    await page.locator('[data-bind-order="live-order"]').click()
                    checkpoint("wait for actual source readiness before binding")
                    await expect(page.locator("#binding-prepare")).to_be_enabled()
                    await page.locator("#binding-prepare").click()
                    async with page.expect_response(
                        lambda r: (
                            "/api/v1/work-order-actions/bind" in r.url
                            and r.request.method == "POST"
                        )
                    ) as bound:
                        await page.locator("#binding-submit").click()
                    response = await bound.value
                    require(response.status == 200, "Managed binding failed")
                    receipt = await response.json()
                    execution_id, load_id = receipt["execution_id"], receipt["command_id"]
                    await expect(page.locator("#load-command-id")).to_have_text(load_id)
                    await expect(page.locator("#load-command-status")).to_have_attribute(
                        "data-status", "SUCCEEDED", timeout=30_000
                    )
                    checkpoint(
                        "wait for real default-physics preheat to READY (bounded 45 seconds)"
                    )
                    await wait_json(
                        sim_snapshot,
                        lambda s: s["state"]["state"] == "READY",
                        45,
                        "Default physical warmup did not reach READY",
                    )
                    await expect(page.locator("#command-START")).to_be_enabled()
                    commands = {"LOAD_JOB": load_id}

                    async def command_ui(kind):
                        await expect(page.locator("#command-" + kind)).to_be_enabled()
                        await page.locator("#command-" + kind).click()
                        if kind == "END_RUN":
                            await page.locator("#command-END_RUN-confirm").click()
                        await expect(page.locator("#command-status")).to_have_attribute(
                            "data-status", "SUCCEEDED", timeout=35_000
                        )
                        commands[kind] = await page.locator("#original-command-id").inner_text()
                        if kind != "END_RUN":
                            await page.locator("#command-acknowledge").click()

                    checkpoint("START through browser; wait for actual good output >= 2")
                    await command_ui("START")
                    running = await wait_json(
                        sim_snapshot,
                        lambda s: int(s["tags"]["Line.GoodTotal"]) >= 2,
                        20,
                        "Actual physical process produced no good output",
                    )
                    require(
                        running["state"]["execution_id"] == execution_id,
                        "Production changed execution identity",
                    )
                    if capture_dir is not None:
                        # Optional README media from the real running line; no effect on checks.
                        capture_dir.mkdir(parents=True, exist_ok=True)
                        await page.evaluate("window.scrollTo(0, 0)")
                        await page.wait_for_timeout(2500)
                        await page.screenshot(path=str(capture_dir / "line-monitoring-running.png"))
                    checkpoint("PAUSE through browser and verify actual PLC PAUSED")
                    await command_ui("PAUSE")
                    await wait_json(
                        sim_snapshot,
                        lambda s: s["state"]["state"] == "PAUSED",
                        10,
                        "PLC did not confirm PAUSED",
                    )
                    # The END_RUN confirmation pins the control version shown on screen, so wait
                    # until the screen reflects the PAUSE result before opening it.
                    await expect(page.locator("#line-state")).to_have_text(
                        "일시 정지", timeout=10_000
                    )
                    checkpoint("confirmed END_RUN; wait for original execution final settlement")
                    await command_ui("END_RUN")
                    scope = {
                        "line_id": LINE_ID,
                        "source_id": identity.source_id,
                        "stream_id": identity.stream_id,
                        "execution_id": execution_id,
                    }

                    async def execution():
                        response = await context.request.get(
                            origin + "/api/v1/execution-record?" + urlencode(scope)
                        )
                        require(response.status == 200, "Original execution GET failed")
                        return await response.json()

                    record = await wait_json(
                        execution,
                        lambda r: r["settlement"]["status"] == "SETTLED",
                        35,
                        "Actual gateway did not settle the ended execution",
                    )
                    final_response = await sim_context.request.get(
                        sim_origin + "/run-summary?" + urlencode({"execution_id": execution_id})
                    )
                    require(final_response.status == 200, "Simulator final summary GET failed")
                    final = await final_response.json()
                    require(
                        final["final"]
                        and final["execution_id"] == execution_id
                        and int(final["good_quantity"]) >= 2,
                        "Simulator final summary was not the actual completed execution",
                    )

                    def normalized(v):
                        return {k: value for k, value in v.items() if k != "as_of"}

                    require(
                        normalized(record["settlement"]["summary"]) == normalized(final),
                        "MES final summary differs from simulator final summary",
                    )
                    for key in ("good_quantity", "reject_quantity", "scrap_quantity"):
                        require(
                            int(record["totals"][key]) == int(final[key]),
                            "MES actual event totals differ from simulator final summary",
                        )
                    require(not errors, "Browser raised a runtime error")
                    require(
                        not websocket_urls, "Acceptance requires the deployed polling web build"
                    )
                    await page.locator("#execution-refresh").click()
                    await expect(page.locator('[data-settlement-status="SETTLED"]')).to_be_visible()
                    screenshot.parent.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(screenshot), full_page=True)
                    evidence.parent.mkdir(parents=True, exist_ok=True)
                    evidence.write_text(
                        json.dumps(
                            {
                                "scope": "local live CLI acceptance; not public deployment",
                                "transport": "polling",
                                "physics": "default 100ms ticker; no overrides",
                                "execution_id": execution_id,
                                "commands": commands,
                                "settlement": record["settlement"]["status"],
                                "totals": record["totals"],
                                "simulator_final_summary": final,
                                "completed_at": datetime.now(UTC).isoformat(),
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    )
                    checkpoint("SETTLED: browser/API totals match real simulator final summary")
                    if analytics_dir is not None:
                        await exercise_analytics(page, origin, analytics_dir, execution_id, record)
                    if guest_dir is not None:
                        await exercise_guest(browser, origin, guest_dir)
                finally:
                    if sys.exc_info()[0] is not None and "page" in locals():
                        if await page.locator("#workspace-drafts").is_visible():
                            screenshot.parent.mkdir(parents=True, exist_ok=True)
                            await page.screenshot(
                                path=str(screenshot.with_stem(screenshot.stem + "-failure")),
                                full_page=True,
                            )
                    await browser.close()
    finally:
        for handle in handles:
            handle.close()
        for log in directory.glob("*.log"):
            require(
                all(secret.encode() not in log.read_bytes() for secret in credentials),
                "Live process log exposed a credential",
            )


ANALYTICS_PAGES = ("dashboard", "trends", "reports", "spc")


async def exercise_analytics(page, origin, out, execution_id, record):
    """Open the reporting pages on the settled run and keep the real API responses.

    Requires a web build with VITE_ANALYTICS_PAGES=enabled. A page passes when its real
    responses parse in the UI (no error notice) and the dashboard/report quantities match
    the settled execution.
    """
    from playwright.async_api import expect

    checkpoint("analytics pages render real reporting API responses")
    out.mkdir(parents=True, exist_ok=True)
    responses = {}

    async def keep(response):
        path = response.url[len(origin) :].split("?", 1)[0]
        if path.startswith(
            ("/api/v1/dashboard", "/api/v1/trends", "/api/v1/reports", "/api/v1/spc")
        ):
            try:
                responses.setdefault(
                    path, {"status": response.status, "body": await response.json()}
                )
            except Exception:
                responses.setdefault(path, {"status": response.status, "body": None})

    page.on("response", keep)
    totals = record["totals"]
    for name in ANALYTICS_PAGES:
        button = page.locator(f"#workspace-{name}")
        require(await button.is_enabled(), "Analytics pages require VITE_ANALYTICS_PAGES=enabled")
        await button.click()
        await page.locator(f'[data-page="{name}"]').wait_for()
        api = {
            "dashboard": "/api/v1/dashboard",
            "trends": "/api/v1/trends",
            "reports": "/api/v1/reports/executions",
            "spc": "/api/v1/spc/xbar-r",
        }[name]
        ready = {
            "dashboard": ".kpi-grid",
            "trends": ".chart svg",
            "reports": ".data-table .link-button",
            "spc": ".kpi-grid",
        }[name]
        await page.locator(f'[data-page="{name}"] {ready}').first.wait_for(timeout=15_000)
        for _ in range(50):  # the response listener reads bodies asynchronously
            if api in responses:
                break
            await asyncio.sleep(0.1)
        require(api in responses, "Reporting page did not call its real API: " + api)
        if name == "reports":
            await page.locator(".data-table .link-button").first.click()
            await page.locator(".timeline-segment").first.wait_for(timeout=15_000)
            href = await page.locator('[data-page="reports"] a.download').get_attribute("href")
            csv = await page.context.request.get(origin + href)
            body = await csv.body()
            require(
                csv.status == 200
                and csv.headers.get("content-type", "").startswith("text/csv")
                and body.startswith(b"\xef\xbb\xbf")
                and execution_id.encode() in body,
                "Execution CSV download did not return the settled execution",
            )
        await expect(page.locator(f'[data-page="{name}"] .page-notice.bad')).to_have_count(0)
        await page.screenshot(path=str(out / f"{name}.png"), full_page=True)
    page.remove_listener("response", keep)
    dashboard = next(
        (item["body"] for path, item in responses.items() if path == "/api/v1/dashboard"), None
    )
    require(dashboard is not None, "Dashboard API was not called")
    require(
        int(dashboard["production"]["good"]) == int(totals["good_quantity"])
        and int(dashboard["production"]["reject"]) == int(totals["reject_quantity"]),
        "Dashboard production differs from the settled execution",
    )
    reports = responses.get("/api/v1/reports/executions", {}).get("body") or {}
    item = next((x for x in reports.get("items", []) if x["execution_id"] == execution_id), None)
    require(item is not None, "Execution report does not list the settled execution")
    require(
        item["settlement_status"] == "SETTLED"
        and int(item["totals"]["good"]) == int(totals["good_quantity"]),
        "Execution report differs from the settled execution",
    )
    (out / "responses.json").write_text(
        json.dumps(responses, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )


async def exercise_guest(browser, origin, out):
    """Enter through the guest button and confirm the session is read-only."""
    from playwright.async_api import expect

    checkpoint("guest button opens a read-only viewer session")
    out.mkdir(parents=True, exist_ok=True)
    context = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="ko-KR")
    try:
        page = await context.new_page()
        await page.goto(origin, wait_until="domcontentloaded")
        async with page.expect_response(lambda r: r.url == origin + "/auth/guest") as received:
            await page.locator("#guest-login").click()
        response = await received.value
        require(response.status == 200, "Guest login failed: HTTP " + str(response.status))
        data = await response.json()
        require(data["roles"] == ["viewer"], "Guest session was not viewer-only")
        await expect(page.locator("#current-roles")).to_have_text("조회 사용자")
        await expect(page.locator(".guest-banner")).to_be_visible()
        await expect(page.locator("#command-START")).to_have_count(0)
        await expect(page.locator("#line-state")).not_to_have_text("불명", timeout=15_000)
        # A well-formed write the guest is not allowed to make; the spec requires 403.
        denied = await context.request.post(
            origin + "/auth/password",
            headers={"Origin": origin, "X-CSRF-Token": data["csrf_token"], "X-HMI-Request": "1"},
            data={"new_password": "guest-cannot-change-this"},
        )
        require(denied.status == 403, "Guest write was not rejected: HTTP " + str(denied.status))
        await page.screenshot(path=str(out / "guest.png"))
    finally:
        await context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chromium", default=shutil.which("chromium") or shutil.which("chromium-browser")
    )
    parser.add_argument("--web-dir", type=Path, default=ROOT / "apps/web/dist")
    parser.add_argument("--screenshot", type=Path, default=ROOT / ".local/operable-system.png")
    parser.add_argument("--evidence", type=Path, default=ROOT / ".local/operable-system.json")
    parser.add_argument(
        "--capture-dir", type=Path, help="Also save a viewport screenshot while the line runs"
    )
    parser.add_argument(
        "--guest-dir",
        type=Path,
        help="Also provision the guest viewer and enter through the guest button",
    )
    parser.add_argument(
        "--analytics-dir",
        type=Path,
        help="Also check the reporting pages on the settled run (analytics-enabled build)",
    )
    args = parser.parse_args()
    if not args.chromium or not (args.web_dir / "index.html").is_file():
        print("FAIL: Chromium and a built polling web directory are required", file=sys.stderr)
        return 1
    try:
        with tempfile.TemporaryDirectory(prefix="hmi-operable-system-") as directory:
            asyncio.run(
                exercise(
                    Path(directory),
                    args.chromium,
                    args.web_dir.resolve(),
                    args.screenshot.resolve(),
                    args.evidence.resolve(),
                    args.capture_dir.resolve() if args.capture_dir else None,
                    args.analytics_dir.resolve() if args.analytics_dir else None,
                    args.guest_dir.resolve() if args.guest_dir else None,
                )
            )
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        message = str(error) if type(error) is _helpers.BrowserCheckFailed else type(error).__name__
        print("FAIL: " + message + " (" + STAGE + ")", file=sys.stderr)
        return 1
    print(
        "PASS: local live browser -> PostgreSQL/API -> gateway -> encrypted OPC UA -> "
        "default physics; final SETTLED totals matched."
    )
    print("Screenshot: " + str(args.screenshot))
    print("Evidence: " + str(args.evidence))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
