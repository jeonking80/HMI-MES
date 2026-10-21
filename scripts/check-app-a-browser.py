#!/usr/bin/env python3
"""Exercise App A's real CLI, PostgreSQL, auth, commands and MES draft registration.

Run separately from pytest with .venv/bin/python scripts/check-app-a-browser.py.
The disposable PostgreSQL fixture owns its generated credentials and container;
this script never selects an existing database. Browser and server exceptions
are reduced to safe static summaries, never filled-input diagnostics.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

CHECK_STAGE = "setup"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class BrowserCheckFailed(RuntimeError):
    """Only fixed, credential-free check descriptions may be printed."""


def checkpoint(message: str) -> None:
    global CHECK_STAGE
    CHECK_STAGE = message
    print("Browser: " + message, flush=True)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BrowserCheckFailed(message)


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@contextmanager
def disposable_postgres(directory: Path):
    """Reuse the pinned local-Docker fixture and its mandatory cleanup verbatim."""
    spec = importlib.util.spec_from_file_location(
        "hmi_browser_disposable_postgres", ROOT / "tests/integration/conftest.py"
    )
    require(spec is not None and spec.loader is not None, "PostgreSQL fixture unavailable")
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    factory = SimpleNamespace(
        mktemp=lambda name: Path(tempfile.mkdtemp(prefix=name + "-", dir=directory))
    )
    # Calling the underlying fixture as a context manager preserves its finally
    # cleanup, generated password, pinned image and local-socket-only Docker use.
    with contextmanager(fixtures.postgres_url.__wrapped__)(factory) as url:
        yield url


async def assert_screen_unavailable(page):
    from playwright.async_api import expect

    await expect(page.locator("#screen-connection")).not_to_have_attribute("data-state", "synced")
    await expect(page.locator("#line-state")).to_have_text("불명")
    await expect(page.locator('[data-process-value="Line.GoodTotal"]')).to_have_text("—")
    for machine in ("Infeed", "Sealer", "Outfeed"):
        await expect(page.locator(f'[data-equipment="{machine}"]')).to_have_attribute(
            "data-state", "UNKNOWN"
        )
    require(
        await page.locator(
            "#command-START:enabled, #command-PAUSE:enabled, "
            "#command-END_RUN:enabled, #command-RESET_FAULT:enabled"
        ).count()
        == 0,
        "Unsynchronized screen exposed a mutation command",
    )


async def screen_recovery_checks(page, context, origin, snapshot_path, bridges):
    """Interrupt actual server delivery while a separate ingestion task stays fresh."""
    from playwright.async_api import expect

    checkpoint(
        "WS disconnect immediately masks controls/process values and reconnects with new sync"
    )
    await expect(page.locator("#command-START")).to_be_enabled()
    previous = await page.locator("#screen-connection").get_attribute("data-sync")
    await bridges[-1]["client"].close(code=1011)
    await assert_screen_unavailable(page)
    await expect(page.locator("#screen-connection")).to_have_attribute("data-state", "synced")
    require(
        await page.locator("#screen-connection").get_attribute("data-sync") != previous,
        "Screen reconnect reused the previous synchronization session",
    )
    await expect(page.locator("#command-START")).to_be_enabled()

    checkpoint("WS watchdog detects frozen delivery despite fresh actual API evidence")
    previous = await page.locator("#screen-connection").get_attribute("data-sync")
    bridges[-1]["mode"] = "drop"
    await assert_screen_unavailable(page)
    fresh = await context.request.get(origin + snapshot_path)
    require(
        fresh.status == 200 and (await fresh.json())["health"]["heartbeat"] == "GOOD",
        "Watchdog test did not retain fresh underlying communication evidence",
    )
    await expect(page.locator("#screen-connection")).to_have_attribute("data-state", "synced")
    require(
        await page.locator("#screen-connection").get_attribute("data-sync") != previous,
        "Watchdog did not obtain a new snapshot synchronization session",
    )
    await expect(page.locator("#command-START")).to_be_enabled()
    for machine in ("Infeed", "Sealer", "Outfeed"):
        await expect(page.locator(f'[data-equipment="{machine}"]')).to_have_attribute(
            "data-state", "STOPPED"
        )
    await page.locator('[data-equipment="Sealer"] a').click()
    require(
        await page.evaluate("location.hash === '#group-Sealer'"),
        "Process diagram anchor did not select the actual equipment detail",
    )
    await page.set_viewport_size({"width": 360, "height": 780})
    require(
        await page.evaluate("document.documentElement.scrollWidth <= innerWidth"),
        "Mobile process diagram caused horizontal page overflow",
    )
    await page.set_viewport_size({"width": 1440, "height": 1000})


async def read_only_mes(page, context, origin, csrf, role, recipe, order, recovery_ids):
    """Use real lists and deny both valid mutation bodies for a read-only role."""
    from playwright.async_api import expect

    from scripts.browser_identity_urls import work_action, work_receipt

    await page.locator("#workspace-drafts").click()
    await expect(
        page.locator("#mes-recipe-list").get_by_role("rowheader", name=recipe["id"], exact=True)
    ).to_be_visible()
    row = page.locator("#mes-order-list tbody tr").filter(has_text=order["id"])
    await expect(row).to_contain_text("배포됨 (RELEASED)")
    require(
        await page.locator("#mes-recipe-form, #mes-order-form").count() == 0,
        "Read-only role was offered MES registration forms",
    )
    for path, definition in (
        ("recipe-versions", recipe | {"id": recipe["id"] + "-" + role}),
        ("work-orders", order | {"id": order["id"] + "-" + role}),
    ):
        denied = await context.request.post(
            origin + "/api/v1/" + path,
            data=definition,
            headers={"Origin": origin, "X-CSRF-Token": csrf},
        )
        require(denied.status == 403, "Read-only role registered MES data through the API")
    require(
        await page.locator("[data-release-order]").count() == 0,
        "Read-only production role was offered release mutation",
    )
    denied = await context.request.post(
        work_action(origin, "release", order["id"]),
        data={"release_id": "denied-release-" + role, "expected_row_version": "3"},
        headers={"Origin": origin, "X-CSRF-Token": csrf},
    )
    require(denied.status == 403, "Non-production-manager could release through API")
    if role == "viewer":
        require(
            await page.locator("[data-bind-order], #binding-prepare").count() == 0,
            "Viewer was offered execution binding",
        )
        denied = await context.request.post(
            work_action(origin, "bind", order["id"]),
            data={
                "command_id": "denied-bind-viewer",
                "line_id": order["line_id"],
                "source_id": "browser-source",
                "stream_id": "browser-stream",
                "expected_work_order_version": "3",
                "expected_control_version": "42",
            },
            headers={"Origin": origin, "X-CSRF-Token": csrf},
        )
        require(denied.status == 403, "Viewer could bind through API")
    else:
        require(
            await page.locator("[data-bind-order]").count() >= 1,
            "Operator cannot prepare an authorized released order",
        )
    released = await context.request.get(
        work_receipt(origin, "release", order["id"], recovery_ids["release"])
    )
    require(
        released.status == 200 and (await released.json())["intent"]["actor"] == "browser-planner",
        "Read-only release recovery lost its original actor",
    )
    await page.locator("#workflow-recovery-kind").select_option("bind")
    await page.locator("#workflow-recovery-order").fill(order["id"])
    await page.locator("#workflow-recovery-id").fill(recovery_ids["binding"])
    await page.locator("#workflow-recovery-submit").click()
    await expect(page.locator("#workflow-status")).to_have_attribute("data-state", "confirmed")
    await expect(page.locator("#workflow-actor")).to_have_text("browser-planner")
    require(
        await page.evaluate(
            "() => localStorage.length === 0 && sessionStorage.length === 0 "
            "&& document.cookie === ''"
        ),
        "MES reader exposed credentials through browser-readable storage",
    )
    await page.locator("#workspace-operations").click()


async def exercise_mes(
    browser,
    origin,
    url,
    producer_password,
    submit_login,
    sensitive,
    errors,
    credential_urls,
    screenshot,
    start_binding_capture,
    viewer_password,
    ingest_capture,
):
    """Actual production-manager registration and post-commit response-loss checks."""
    from playwright.async_api import expect
    from sqlalchemy import text

    from apps.api.bootstrap import LINE_ID
    from apps.api.database import create_database_engine
    from scripts.browser_identity_urls import work_action, work_detail, work_receipt

    recipe = {
        "id": "browser-managed-v1",
        "recipe_id": "browser-managed",
        "version": "v1",
        "item_id": "browser-item-A",
        "parameters": {"temperature_c": 150.0, "speed_m_per_min": 6.0},
    }
    order = {
        "id": "browser-managed-order",
        "item_id": recipe["item_id"],
        "line_id": LINE_ID,
        "recipe_version_id": recipe["id"],
        "target_quantity": 4294967295,
        "planned_date": "2026-10-23",
    }

    def records():
        query_engine = create_database_engine(url)
        try:
            with query_engine.connect() as connection:
                return {
                    "recipes": list(
                        connection.execute(
                            text("SELECT * FROM recipe_versions WHERE id=:id"),
                            {"id": recipe["id"]},
                        ).mappings()
                    ),
                    "orders": list(
                        connection.execute(
                            text("SELECT * FROM work_orders WHERE id=:id"),
                            {"id": order["id"]},
                        ).mappings()
                    ),
                    "audit": list(
                        connection.execute(
                            text(
                                "SELECT * FROM mes_management_audit "
                                "WHERE recipe_version_id=:recipe OR work_order_id=:order "
                                "ORDER BY id"
                            ),
                            {"recipe": recipe["id"], "order": order["id"]},
                        ).mappings()
                    ),
                    "machine_counts": tuple(
                        connection.execute(
                            text(
                                "SELECT (SELECT count(*) FROM command_requests),"
                                "(SELECT count(*) FROM production_executions),"
                                "(SELECT count(*) FROM execution_plans),"
                                "(SELECT count(*) FROM production_events)"
                            )
                        ).one()
                    ),
                }
        finally:
            query_engine.dispose()

    baseline = records()
    context = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="ko-KR")
    await context.add_init_script("""window.__hmiCspViolations = 0;
        document.addEventListener('securitypolicyviolation', () => {
            window.__hmiCspViolations += 1;
        });""")
    try:
        page = await context.new_page()
        page.set_default_timeout(15_000)
        browser_order_posts = []
        page.on(
            "request",
            lambda request: (
                browser_order_posts.append(request.post_data_json)
                if request.method == "POST" and request.url == origin + "/api/v1/work-orders"
                else None
            ),
        )
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        page.on(
            "request",
            lambda request: credential_urls.append(
                any(value in request.url for value in sensitive)
            ),
        )
        checkpoint("MES production-manager login and immutable recipe registration")
        await page.goto(origin, wait_until="domcontentloaded")
        session = await submit_login(
            producer_password,
            target_page=page,
            login_name="browser-planner",
            roles=("production_manager",),
        )
        require(
            not session["must_change_password"],
            "MES fixture retained its initial-password gate",
        )
        await page.locator("#workspace-drafts").click()
        await page.locator("#mes-recipe-form").wait_for(state="visible")
        for field, value in {
            "id": recipe["id"],
            "family": recipe["recipe_id"],
            "version": recipe["version"],
            "item": recipe["item_id"],
            "temperature": "150",
            "speed": "6",
        }.items():
            await page.locator("#mes-recipe-" + field).fill(value)
        async with page.expect_response(
            lambda response: (
                response.url == origin + "/api/v1/recipe-versions"
                and response.request.method == "POST"
            )
        ) as received:
            await page.locator("#mes-recipe-submit").click()
        require((await received.value).status == 200, "Actual recipe registration failed")
        await expect(page.locator("#mes-recipe-status")).to_have_attribute(
            "data-state", "confirmed"
        )
        await expect(page.locator("#mes-order-recipe")).to_have_value(recipe["id"])
        await expect(page.locator("#mes-order-item")).to_have_value(recipe["item_id"])
        await expect(page.locator("#mes-order-item")).to_have_attribute("readonly", "")
        saved = records()
        require(
            len(saved["recipes"]) == 1
            and not saved["orders"]
            and len(saved["audit"]) == 1
            and saved["recipes"][0]["created_by"] == "browser-planner"
            and saved["recipes"][0]["temperature_c"] == 150.0
            and saved["recipes"][0]["speed_m_per_min"] == 6.0,
            "Recipe definition and actor audit were not stored together",
        )

        checkpoint("MES DRAFT post-commit response loss keeps exact original form and ID")
        for field, value in {
            "id": order["id"],
            "target": str(order["target_quantity"]),
            "date": order["planned_date"],
        }.items():
            await page.locator("#mes-order-" + field).fill(value)
        await page.locator("#mes-order-line").select_option(order["line_id"])
        intercepted = []

        async def lose_committed_order_response(route):
            if route.request.method != "POST":
                await route.continue_()
                return
            intercepted.append(route.request.post_data_json)
            receipt = await route.fetch()
            require(receipt.status == 200, "Actual DRAFT registration did not commit")
            payload = await receipt.json()
            require(
                payload["status"] == "DRAFT" and payload["id"] == order["id"],
                "DRAFT registration falsely claimed a different workflow state",
            )
            saved = records()
            require(
                len(saved["orders"]) == 1 and len(saved["audit"]) == 2,
                "Lost registration response did not leave one DRAFT and one audit",
            )
            await route.abort("failed")

        await page.route("**/api/v1/work-orders", lose_committed_order_response)
        try:
            await page.locator("#mes-order-submit").click()
            await expect(page.locator("#mes-order-status")).to_have_attribute(
                "data-state", "uncertain"
            )
            require(
                intercepted == [order],
                "Uncertain DRAFT submission changed its request DTO",
            )
            await expect(page.locator("#mes-order-id")).to_have_value(order["id"])
            await expect(page.locator("#mes-order-target")).to_have_value(
                str(order["target_quantity"])
            )
            await expect(page.locator("#mes-order-id")).to_be_disabled()
            require(
                await page.locator("#mes-order-reset").count() == 0,
                "Uncertain DRAFT exposed a shortcut to a different registration",
            )
            await page.locator("#workspace-operations").click()
            await page.locator("#workspace-drafts").click()
            await expect(page.locator("#mes-order-status")).to_have_attribute(
                "data-state", "uncertain"
            )
            for field, value in {
                "id": order["id"],
                "recipe": recipe["id"],
                "item": recipe["item_id"],
                "line": order["line_id"],
                "target": str(order["target_quantity"]),
                "date": order["planned_date"],
            }.items():
                await expect(page.locator("#mes-order-" + field)).to_have_value(value)
            await asyncio.sleep(1.2)
            require(len(intercepted) == 1, "Uncertain DRAFT was automatically posted again")
            await page.screenshot(
                path=str(screenshot.with_stem(screenshot.stem + "-mes-uncertain")),
                full_page=True,
            )
        finally:
            await page.unroute("**/api/v1/work-orders", lose_committed_order_response)
        async with page.expect_response(
            lambda response: (
                response.url == work_detail(origin, order["id"])
                and response.request.method == "GET"
            )
        ) as lookup:
            await page.locator("#mes-order-lookup").click()
        require((await lookup.value).status == 200, "Original-ID DRAFT lookup failed")
        await expect(page.locator("#mes-order-status")).to_have_attribute("data-state", "confirmed")
        await expect(page.locator("#mes-order-original-id")).to_have_text(order["id"])
        await expect(page.locator("#mes-order-list tbody tr")).to_contain_text("작성 중 (DRAFT)")
        saved = records()
        require(
            len(saved["orders"]) == 1
            and len(saved["audit"]) == 2
            and saved["orders"][0]["created_by"] == "browser-planner"
            and saved["orders"][0]["status"] == "DRAFT"
            and str(saved["orders"][0]["row_version"]) == "1"
            and saved["orders"][0]["target_quantity"] == order["target_quantity"]
            and saved["orders"][0]["planned_date"].isoformat() == order["planned_date"]
            and [row["action"] for row in saved["audit"]]
            == ["REGISTER_RECIPE", "REGISTER_WORK_ORDER"]
            and all(row["actor"] == "browser-planner" for row in saved["audit"])
            and saved["audit"][0]["evidence"] == recipe
            and saved["audit"][1]["evidence"] == order
            and saved["machine_counts"] == baseline["machine_counts"],
            "DRAFT lookup duplicated registration/audit or changed execution/command data",
        )
        require(
            await page.locator(
                "#mes-drafts #command-START, #mes-drafts #command-PAUSE, "
                "#mes-drafts #command-END_RUN, #mes-drafts #command-RESET_FAULT"
            ).count()
            == 0,
            "DRAFT registration exposed machine controls inside MES",
        )
        await page.screenshot(
            path=str(screenshot.with_stem(screenshot.stem + "-mes")), full_page=True
        )

        checkpoint("MES conflicting immutable ID cannot overwrite original settings")
        await page.locator("#mes-recipe-reset").click()
        for field, value in {
            "id": recipe["id"],
            "family": recipe["recipe_id"],
            "version": recipe["version"],
            "item": recipe["item_id"],
            "temperature": "151",
            "speed": "6",
        }.items():
            await page.locator("#mes-recipe-" + field).fill(value)
        async with page.expect_response(
            lambda response: (
                response.url == origin + "/api/v1/recipe-versions"
                and response.request.method == "POST"
            )
        ) as conflict:
            await page.locator("#mes-recipe-submit").click()
        require(
            (await conflict.value).status == 409,
            "Immutable recipe conflict was not rejected",
        )
        await expect(page.locator("#mes-recipe-status")).to_have_attribute("data-state", "conflict")
        await page.locator("#mes-recipe-lookup").click()
        await expect(page.locator("#mes-recipe-result .mes-result-facts")).to_contain_text("150 ℃")
        await expect(page.locator("#mes-recipe-status")).to_have_attribute("data-state", "conflict")
        await expect(page.locator("#mes-recipe-temperature")).to_have_value("151")
        saved = records()
        require(
            saved["recipes"][0]["temperature_c"] == 150.0 and len(saved["audit"]) == 2,
            "Conflicting definition overwrote original recipe or added a duplicate audit",
        )

        checkpoint("MES lists use real bounded cursor pagination")
        for index in range(1, 21):
            identity = f"zz-browser-page-{index:02}"
            extra = recipe | {"id": identity, "recipe_id": identity}
            response = await context.request.post(
                origin + "/api/v1/recipe-versions",
                data=extra,
                headers={"Origin": origin, "X-CSRF-Token": session["csrf_token"]},
            )
            require(response.status == 200, "Pagination fixture registration failed")
        await (
            page.locator("section.mes-card")
            .filter(has=page.locator("#mes-recipes-heading"))
            .get_by_role("button", name="목록 새로 조회", exact=True)
            .click()
        )
        await expect(page.locator("#mes-recipe-list tbody tr")).to_have_count(20)
        async with page.expect_response(
            lambda response: (
                response.url.startswith(origin + "/api/v1/recipe-versions?")
                and "after_id=" in response.url
            )
        ) as next_page:
            await page.locator("#mes-recipes-next").click()
        page_data = await (await next_page.value).json()
        require(
            len(page_data["items"]) == 1 and page_data["next_cursor"] is None,
            "MES cursor pagination did not bound the next response",
        )
        await expect(page.locator("#mes-recipe-list tbody tr")).to_have_count(1)
        await expect(page.locator("#mes-recipe-list")).to_contain_text("zz-browser-page-20")
        await expect(page.locator("#mes-recipes-next")).to_be_disabled()
        await page.locator("#mes-recipes-first").click()
        await expect(page.locator("#mes-recipe-list tbody tr")).to_have_count(20)
        await expect(page.locator("#mes-orders-next")).to_be_disabled()
        checkpoint("MES actual release remains publication state in refreshed lists")
        release_posts = []

        async def lose_release_response(route):
            release_posts.append(route.request.post_data_json)
            response = await route.fetch()
            require(response.status == 200, "Actual release did not commit")
            require(
                (await response.json())["status"] == "RELEASED",
                "Release claimed execution",
            )
            await route.abort("failed")

        release_path = work_action(origin, "release", order["id"])
        await page.route(release_path, lose_release_response)
        try:
            await page.locator('[data-release-order="' + order["id"] + '"]').click()
            await page.locator("#release-submit").click()
            await expect(page.locator("#workflow-status")).to_have_attribute(
                "data-state", "uncertain"
            )
            release_id = release_posts[0]["release_id"]
            await expect(page.locator("#workflow-original-id")).to_have_text(release_id)
            await page.locator("#workspace-operations").click()
            await page.locator("#workspace-drafts").click()
            await page.locator("#logout").click()
            session = await submit_login(
                producer_password,
                target_page=page,
                login_name="browser-planner",
                roles=("production_manager",),
            )
            await expect(page.locator("#workflow-original-id")).to_have_text(release_id)
            require(
                len(release_posts) == 1,
                "Unknown release was automatically posted again",
            )
            recovery_path = work_receipt(origin, "release", order["id"], release_id)

            async def lose_lookup_visibility(route):
                response = await route.fetch()
                require(response.status == 200, "Committed release receipt was not stored")
                await route.fulfill(status=404, json={"detail": "receipt visibility fault"})

            await page.route(recovery_path, lose_lookup_visibility)
            try:
                await page.locator("#workflow-lookup").click()
                await expect(page.locator("#workflow-status")).to_have_attribute(
                    "data-state", "uncertain"
                )
                await expect(page.locator("#workflow-status")).to_contain_text("뒤늦게")
                require(
                    await page.locator("#workflow-acknowledge").count() == 0,
                    "Unknown 404 exposed a replacement intent shortcut",
                )
            finally:
                await page.unroute(recovery_path, lose_lookup_visibility)
            await page.locator("#workflow-lookup").click()
            await expect(page.locator("#workflow-status")).to_have_attribute(
                "data-state", "confirmed"
            )
            await expect(page.locator("#workflow-actor")).to_have_text("browser-planner")
            require(len(release_posts) == 1, "Passive release lookup re-posted an intent")
            await page.locator("#workflow-acknowledge").click()
            await page.locator("#workspace-drafts").click()
        finally:
            await page.unroute(release_path, lose_release_response)
        await (
            page.locator("section.mes-card")
            .filter(has=page.locator("#mes-orders-heading"))
            .get_by_role("button", name="목록 새로 조회", exact=True)
            .click()
        )
        await expect(page.locator("#mes-order-list tbody tr")).to_contain_text("배포됨 (RELEASED)")
        saved = records()
        require(
            saved["orders"][0]["status"] == "RELEASED"
            and str(saved["orders"][0]["row_version"]) == "2"
            and [row["action"] for row in saved["audit"]]
            == ["REGISTER_RECIPE", "REGISTER_WORK_ORDER", "RELEASE_WORK_ORDER"]
            and saved["machine_counts"] == baseline["machine_counts"],
            "Publication changed execution data or lost its actual state/audit",
        )
        for cookie in await context.cookies():
            if cookie["name"] in {"hmi_session", "hmi_preauth"}:
                require(
                    cookie["httpOnly"] and cookie["sameSite"] == "Strict",
                    "MES browser cookies lost authentication protections",
                )
                sensitive.append(cookie["value"])
        require(
            await page.evaluate("""() => localStorage.length === 0
                && sessionStorage.length === 0 && document.cookie === ''
                && window.__hmiCspViolations === 0"""),
            "MES registration leaked storage or violated content security policy",
        )
        require(
            records()["machine_counts"] == baseline["machine_counts"],
            "MES registration changed machine execution data",
        )
        require(
            browser_order_posts == [order],
            "MES uncertainty or original-ID lookup automatically resubmitted the original form",
        )
        checkpoint(
            "MES fresh same-source binding commits once; lost response recovers original LOAD"
        )
        await page.locator('[data-bind-order="' + order["id"] + '"]').click()
        await expect(page.locator("#binding-prepare")).to_be_disabled()
        capture = start_binding_capture()
        try:
            await expect(page.locator("#binding-prepare")).to_be_enabled()
            binding_posts = []
            bind_path = work_action(origin, "bind", order["id"])

            async def lose_binding_response(route):
                binding_posts.append(route.request.post_data_json)
                response = await route.fetch()
                require(
                    response.status == 200,
                    "Actual managed binding did not commit: HTTP " + str(response.status),
                )
                receipt = await response.json()
                require(
                    receipt["load_request"]["command"]["kind"] == "LOAD_JOB",
                    "Binding receipt lost its original LOAD request",
                )
                from apps.api.commands import claim_next

                claim_engine = create_database_engine(url)
                try:
                    with claim_engine.begin() as connection:
                        work = claim_next(
                            connection,
                            authenticated_gateway_id="browser-gateway",
                            authenticated_source_id="browser-source",
                            authenticated_stream_id="browser-stream",
                            now=datetime.now(UTC),
                        )
                        require(
                            work is not None
                            and work.action == "SUBMIT"
                            and work.request.command_id == receipt["command_id"],
                            "Original LOAD was not reserved exactly once before response loss",
                        )
                finally:
                    claim_engine.dispose()
                await route.abort("failed")

            await page.route(bind_path, lose_binding_response)
            try:
                await page.locator("#binding-prepare").click()
                await page.locator("#binding-submit").click()
                await expect(page.locator("#workflow-status")).to_have_attribute(
                    "data-state", "uncertain"
                )
                command_id = binding_posts[0]["command_id"]
                await expect(page.locator("#workflow-original-id")).to_have_text(command_id)
                await expect(page.locator("#binding-prepare")).to_be_disabled()
                await page.locator("#workspace-drafts").click()
                await page.locator("#workspace-operations").click()
                await page.locator("#logout").click()
                await submit_login(
                    viewer_password,
                    target_page=page,
                    login_name="browser-viewer",
                    roles=("viewer",),
                )
                await expect(page.locator("#workflow-original-id")).to_have_text(command_id)
                await expect(page.locator("#workflow-actor")).to_have_text("browser-planner")
                require(
                    await page.locator("#binding-prepare").count() == 0,
                    "Different viewer login gained mutation access to preserved bind intent",
                )
                await page.locator("#workflow-lookup").click()
                await expect(page.locator("#workflow-status")).to_have_attribute(
                    "data-state", "confirmed"
                )
                await expect(page.locator("#load-command-id")).to_have_text(command_id)
                await expect(page.locator("#load-command-status")).to_have_attribute(
                    "data-status", "OUTCOME_UNKNOWN"
                )
                await page.locator("#logout").click()
                session = await submit_login(
                    producer_password,
                    target_page=page,
                    login_name="browser-planner",
                    roles=("production_manager",),
                )
                await expect(page.locator("#workflow-original-id")).to_have_text(command_id)
                await asyncio.sleep(1.2)
                require(
                    len(binding_posts) == 1,
                    "Binding recovery generated a second LOAD POST",
                )
                saved = records()
                require(
                    saved["machine_counts"]
                    == tuple(
                        n + 1 if i < 3 else n for i, n in enumerate(baseline["machine_counts"])
                    ),
                    "Managed binding did not create exactly one command/execution/plan",
                )
                require(
                    str(saved["orders"][0]["row_version"]) == "3"
                    and [row["action"] for row in saved["audit"]]
                    == [
                        "REGISTER_RECIPE",
                        "REGISTER_WORK_ORDER",
                        "RELEASE_WORK_ORDER",
                        "BIND_EXECUTION",
                    ],
                    "Binding duplicated its original audit or work-order transition",
                )
                require(
                    await page.locator("#workflow-acknowledge").count() == 0,
                    "LOAD reservation exposed an automatic replacement shortcut",
                )
                await page.screenshot(
                    path=str(screenshot.with_stem(screenshot.stem + "-binding")),
                    full_page=True,
                )
            finally:
                await page.unroute(bind_path, lose_binding_response)
        finally:
            capture.cancel()
            await asyncio.gather(capture, return_exceptions=True)
        from scripts.execution_browser_checks import exercise_execution_archive

        await exercise_execution_archive(
            page,
            context,
            origin,
            url,
            session,
            order,
            command_id,
            ingest_capture,
            checkpoint,
            require,
            screenshot,
        )
    finally:
        await context.close()
    return recipe, order, {"release": release_id, "binding": command_id}


async def exercise(
    directory: Path,
    executable: str,
    web_dir: Path,
    screenshot: Path,
    *,
    focus_line_alarms: bool = False,
) -> None:
    import httpx
    from alembic import command
    from playwright.async_api import async_playwright, expect
    from sqlalchemy import text

    from apps.api.auth import UserAuth
    from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
    from apps.api.database import create_database_engine, migration_config
    from apps.api.ingestion import IngestionService
    from contracts.models import UINT64_MAX, Parameters
    from contracts.tags.validate import load_contract
    from contracts.transport import BatchBody, GatewayBatch, batch_digest

    fixture_tasks = []
    initial, changed = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    viewer_initial, viewer_password = (
        secrets.token_urlsafe(32),
        secrets.token_urlsafe(32),
    )
    producer_initial, producer_password = (
        secrets.token_urlsafe(32),
        secrets.token_urlsafe(32),
    )
    sensitive = [
        initial,
        changed,
        viewer_initial,
        viewer_password,
        producer_initial,
        producer_password,
    ]
    http_port = free_port()
    origin = f"http://127.0.0.1:{http_port}"
    password_file = directory / "initial-password"
    descriptor = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(initial + "\n")
    base = [sys.executable, "-m", "apps.api"]

    with disposable_postgres(directory) as url:
        sensitive.append(url.password)
        engine = create_database_engine(url)
        try:
            with engine.connect() as connection:
                command.upgrade(migration_config(connection), "head")
            bootstrap_reference_data(engine)
            IngestionService(engine).configure_source(
                source_id="browser-source",
                stream_id="browser-stream",
                gateway_id="browser-gateway",
                line_id=LINE_ID,
                capture_generation=1,
                now=datetime.now(UTC),
            )
            IngestionService(engine).register_execution(
                execution_id="browser-run",
                source_id="browser-source",
                stream_id="browser-stream",
                work_order_id="browser-order",
                recipe_version="browser-recipe",
                parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
                counter_epoch="browser-epoch",
                target_quantity=10,
                now=datetime.now(UTC),
            )
            fixture_auth = UserAuth(engine, allowed_origins={origin})
            for login_name, first_password, next_password, role in (
                ("browser-viewer", viewer_initial, viewer_password, "viewer"),
                (
                    "browser-planner",
                    producer_initial,
                    producer_password,
                    "production_manager",
                ),
            ):
                fixture_auth.bootstrap_user(login_name, first_password, roles={role})
                challenge = fixture_auth.begin_login()
                fixture_session = fixture_auth.login(
                    login_name,
                    first_password,
                    challenge_token=challenge.token,
                    csrf_token=challenge.csrf_token,
                    origin=origin,
                )
                fixture_auth.change_password(
                    fixture_session.token,
                    next_password,
                    csrf_token=fixture_session.csrf_token,
                    origin=origin,
                )
            with engine.connect() as connection:
                line_name = connection.execute(
                    text("SELECT name FROM lines WHERE id=:id"), {"id": LINE_ID}
                ).scalar_one()
        finally:
            engine.dispose()

        def ingest_snapshot_fixture(
            *,
            seq=1,
            legacy=False,
            future_seconds=0,
            controls=False,
            binding=False,
            control_overrides=None,
        ):
            # Controlled test observations enter through the real transactional
            # ingestion service. This is DB→API→browser evidence, not an OPC run.
            fixture_engine = create_database_engine(url)
            try:
                now = datetime.now(UTC)
                captured = now + timedelta(seconds=future_seconds)
                tags = {tag["logical_name"]: tag for tag in load_contract()["tags"]}
                values = [
                    ("Line.GoodTotal", UINT64_MAX, 0),
                    ("Line.RejectTotal", None, 0x80000000),
                    ("Line.ScrapTotal", 7, 0x80000000),
                    ("Line.State", 2, 0),
                ]
                if controls:
                    ready = {
                        "Line.State": 1,
                        "Line.ControlMode": 1,
                        "Line.OperationMode": 1,
                        "Line.ControlVersion": 42,
                        "Line.EventSeq": 0,
                        "Line.ActiveExecutionId": "browser-run",
                        "Line.CounterEpoch": "browser-epoch",
                        "Line.RecipeVersion": "browser-recipe",
                        "Line.Permissive": True,
                        "Line.GuardClosed": True,
                        "Sealer.GuardClosed": True,
                        "Sealer.Ready": True,
                    }
                    if binding:
                        ready.update(
                            {
                                "Line.State": 0,
                                "Line.ActiveExecutionId": "",
                                "Line.RecipeVersion": "",
                            }
                        )
                    ready.update(control_overrides or {})
                    defaults = {
                        "Boolean": False,
                        "String": "",
                        "Double": 0.0,
                        "Int32": 0,
                        "UInt32": 0,
                        "UInt64": 0,
                    }
                    values = [
                        (name, ready.get(name, defaults[tag["data_type"]]), 0)
                        for name, tag in tags.items()
                    ]
                samples = [
                    {
                        "sample_id": f"browser-sample-{seq}-" + tag,
                        "tag_id": tag,
                        "source_timestamp": captured - timedelta(days=1)
                        if controls and not tag.endswith("Heartbeat")
                        else captured,
                        "server_timestamp": captured,
                        "acquired_at": captured,
                        "status_code": status,
                        "reading": {
                            "data_type": tags[tag]["data_type"],
                            "value": value,
                        },
                    }
                    for tag, value, status in values
                ]
                extra = (
                    {}
                    if legacy
                    else {
                        "communication": {
                            "state": "GOOD",
                            "connected": True,
                            "read_age_seconds": 0.0,
                            "heartbeat_ages_seconds": {
                                name: 0.0
                                for name in (
                                    "Line.Heartbeat",
                                    "Infeed.LinkHeartbeat",
                                    "Sealer.LinkHeartbeat",
                                    "Outfeed.LinkHeartbeat",
                                )
                            },
                        }
                    }
                )
                body = BatchBody(
                    schema_version="1.0" if legacy else "1.1",
                    batch_id=f"browser-fixture-batch-{seq}",
                    gateway_id="browser-gateway",
                    source_id="browser-source",
                    stream_id="browser-stream",
                    capture_generation=1,
                    capture_seq=seq,
                    captured_at=captured,
                    samples=samples,
                    **extra,
                )
                IngestionService(fixture_engine).ingest(
                    GatewayBatch(body=body, content_sha256=batch_digest(body)),
                    authenticated_gateway_id=body.gateway_id,
                    authenticated_source_id=body.source_id,
                    authenticated_stream_id=body.stream_id,
                    now=now,
                )
                return captured
            finally:
                fixture_engine.dispose()

        def command_rows():
            query_engine = create_database_engine(url)
            try:
                with query_engine.connect() as connection:
                    return [
                        dict(row)
                        for row in connection.execute(
                            text(
                                "SELECT command_id,actor,status,request FROM command_requests "
                                "ORDER BY command_id"
                            )
                        ).mappings()
                    ]
            finally:
                query_engine.dispose()

        child_env = os.environ.copy()
        # This DSN is exclusively the newly generated disposable test container.
        # It is inherited by the CLI through the environment, never arguments/logs.
        child_env["HMI_DATABASE_DSN"] = url.render_as_string(hide_password=False)
        provisioned = subprocess.run(
            base
            + [
                "bootstrap-user",
                "--origin",
                origin,
                "--login",
                "browser-operator",
                "--role",
                "operator",
                "--password-file",
                str(password_file),
            ],
            cwd=ROOT,
            env=child_env,
            capture_output=True,
            timeout=20,
        )
        require(provisioned.returncode == 0, "Explicit browser-user provisioning failed")
        require(
            all(
                value.encode() not in provisioned.stdout + provisioned.stderr for value in sensitive
            ),
            "Provisioning exposed a test credential",
        )
        password_file.unlink()
        log_path = directory / "server.log"
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                base
                + [
                    "serve-user",
                    "--origin",
                    origin,
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(http_port),
                    "--web-dir",
                    str(web_dir),
                ],
                cwd=ROOT,
                env=child_env,
                stdout=log,
                stderr=log,
            )
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
                    deadline = time.monotonic() + 20
                    while True:
                        require(process.poll() is None, "User API exited before readiness")
                        try:
                            response = await client.get(origin + "/auth/me")
                            require(
                                response.status_code == 401,
                                "Anonymous browser was accepted",
                            )
                            response = await client.get(origin + "/")
                            require(response.status_code == 200, "Built UI was not served")
                            break
                        except httpx.TransportError:
                            require(
                                time.monotonic() < deadline,
                                "User API readiness timed out",
                            )
                            await asyncio.sleep(0.1)
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(
                        executable_path=executable,
                        headless=True,
                        args=["--no-sandbox", "--disable-dev-shm-usage"],
                    )
                    try:
                        context = await browser.new_context(
                            viewport={"width": 1440, "height": 1000}, locale="ko-KR"
                        )
                        page = await context.new_page()
                        page.set_default_timeout(15_000)
                        bridges = []
                        rest_screen_reads = []
                        page.on(
                            "request",
                            lambda request: (
                                rest_screen_reads.append(True)
                                if "/api/v1/snapshot?" in request.url
                                else None
                            ),
                        )

                        def bridge_screen(client):
                            # Keep real cookie/Origin/CSRF authentication and DB projections.
                            # Only selected network deliveries/revisions are fault-injected.
                            server = client.connect_to_server()
                            bridge = {
                                "client": client,
                                "server": server,
                                "mode": "pass",
                                "subscriptions": 0,
                                "changed": False,
                            }
                            bridges.append(bridge)
                            require(
                                not any(value in client.url for value in sensitive),
                                "A credential appeared in the screen WebSocket URL",
                            )

                            def subscribe(message):
                                payload = json.loads(message)
                                bridge["subscriptions"] += 1
                                require(
                                    bridge["subscriptions"] == 1
                                    and set(payload)
                                    == {
                                        "line_id",
                                        "source_id",
                                        "stream_id",
                                        "csrf_token",
                                    }
                                    and payload["line_id"] == LINE_ID
                                    and payload["source_id"] == "browser-source"
                                    and payload["stream_id"] == "browser-stream"
                                    and len(payload["csrf_token"]) == 43,
                                    "Browser sent an invalid or repeated screen subscription",
                                )
                                sensitive.append(payload["csrf_token"])
                                server.send(message)

                            def receive(message):
                                if bridge["mode"] == "drop":
                                    return
                                payload = json.loads(message)
                                if (
                                    bridge["mode"] == "gap"
                                    and payload["screen"]["kind"] == "UPDATE"
                                ):
                                    payload["screen"]["base_revision"] = str(
                                        int(payload["screen"]["base_revision"]) + 1
                                    )
                                    payload["screen"]["revision"] = str(
                                        int(payload["screen"]["revision"]) + 1
                                    )
                                    bridge["mode"], bridge["changed"] = "pass", True
                                    client.send(json.dumps(payload))
                                else:
                                    client.send(message)

                            client.on_message(subscribe)
                            server.on_message(receive)

                        await page.route_web_socket("**/ws/v1/screen", bridge_screen)
                        errors: list[str] = []
                        credential_urls: list[bool] = []
                        page.on(
                            "pageerror",
                            lambda error: errors.append(type(error).__name__),
                        )
                        page.on(
                            "request",
                            lambda request: credential_urls.append(
                                any(value in request.url for value in sensitive)
                            ),
                        )
                        await page.add_init_script("""window.__hmiCspViolations = 0;
                            document.addEventListener('securitypolicyviolation', () => {
                                window.__hmiCspViolations += 1;
                            });""")

                        async def protected_storage():
                            cookies = await context.cookies()
                            for cookie in cookies:
                                if cookie["name"] in {"hmi_session", "hmi_preauth"}:
                                    require(
                                        cookie["httpOnly"] and cookie["sameSite"] == "Strict",
                                        "Browser cookie protections were missing",
                                    )
                                    sensitive.append(cookie["value"])
                            storage = await page.evaluate("""() => ({
                                local: {...localStorage}, session: {...sessionStorage},
                                cookie: document.cookie,
                                csp: window.__hmiCspViolations
                            })""")
                            require(
                                not storage["local"] and not storage["session"],
                                "Authentication UI persisted browser storage",
                            )
                            require(
                                not storage["cookie"],
                                "Session cookie was readable by JavaScript",
                            )
                            require(
                                storage["csp"] == 0,
                                "Browser content security policy was violated",
                            )

                        login_times = []

                        async def submit_login(
                            password: str,
                            *,
                            target_page=None,
                            login_name="browser-operator",
                            roles=("operator",),
                        ):
                            # Respect the real five-logins/minute IP limit as this
                            # suite now exercises more logout/actor transitions.
                            while True:
                                recent = [at for at in login_times if time.monotonic() - at < 61]
                                if len(recent) < 5:
                                    break
                                await asyncio.sleep(0.5)
                            login_times.append(time.monotonic())
                            target_page = target_page or page
                            form = target_page.locator("#login-form")
                            await form.locator("#login").fill(login_name)
                            await form.locator("#login-password").fill(password)
                            async with target_page.expect_response(
                                lambda response: response.url == origin + "/auth/login"
                            ) as received:
                                await form.locator("button[type=submit]").click()
                            response = await received.value
                            require(
                                response.status == 200,
                                "Real browser login failed: HTTP " + str(response.status),
                            )
                            data = await response.json()
                            sensitive.append(data["csrf_token"])
                            require(
                                data["login"] == login_name and data["roles"] == list(roles),
                                "Browser login identity/roles were incorrect",
                            )
                            return data

                        print(
                            "Browser: App A login and initial password change",
                            flush=True,
                        )
                        await page.goto(origin, wait_until="domcontentloaded")
                        data = await submit_login(initial)
                        require(
                            data["must_change_password"],
                            "Initial password bypassed rotation",
                        )
                        await protected_storage()
                        form = page.locator("#password-form")
                        await form.locator("#new-password").fill(changed)
                        await form.locator("#confirm-password").fill(changed)
                        async with page.expect_response(
                            lambda response: response.url == origin + "/auth/password"
                        ) as received:
                            await form.locator("button[type=submit]").click()
                        response = await received.value
                        require(
                            response.status == 200,
                            "Browser initial password change failed",
                        )
                        data = await response.json()
                        require(
                            not data["must_change_password"],
                            "Password-change gate remained",
                        )
                        sensitive.append(data["csrf_token"])
                        await (
                            page.locator("#current-login")
                            .filter(has_text="browser-operator")
                            .wait_for()
                        )
                        await page.locator("#current-roles").filter(has_text="작업자").wait_for()
                        await protected_storage()
                        me = await context.request.get(origin + "/auth/me")
                        require(
                            me.status == 200 and (await me.json())["roles"] == ["operator"],
                            "Real HTTP identity did not match the rendered browser identity",
                        )
                        if focus_line_alarms:
                            from scripts.line_alarm_browser_checks import exercise_line_alarm_center

                            await exercise_line_alarm_center(
                                browser,
                                origin,
                                url,
                                submit_login,
                                changed,
                                viewer_password,
                                checkpoint,
                                require,
                                sensitive,
                                errors,
                                screenshot,
                            )
                        else:
                            print(
                                "Browser: registered source with no observations, "
                                "then fixture ingest",
                                flush=True,
                            )
                            checkpoint("registered source / never-observed rendering")
                            selector = page.locator("#source-selector")
                            await expect(selector).to_have_value("0")
                            selected = await selector.locator("option:checked").inner_text()
                            require(
                                line_name in selected
                                and "browser-source / browser-stream" in selected,
                                "Browser source label did not match registered PostgreSQL binding",
                            )
                            await expect(page.locator("#snapshot-health")).to_have_text(
                                "수집 이력 없음"
                            )
                            await expect(page.locator("#line-state")).to_have_text("불명")
                            good = page.locator('[data-tag-id="Line.GoodTotal"]')
                            bad_null = page.locator('[data-tag-id="Line.RejectTotal"]')
                            bad_value = page.locator('[data-tag-id="Line.ScrapTotal"]')
                            missing = page.locator('[data-tag-id="Line.TargetQuantity"]')
                            await expect(good.locator("dd > strong")).to_have_text("—")
                            await expect(good.locator(".quality")).to_have_text("미수신 · 불명")
                            require(
                                await page.locator(
                                    "#command-START:enabled, #command-PAUSE:enabled, "
                                    "#command-END_RUN:enabled, #command-RESET_FAULT:enabled"
                                ).count()
                                == 0,
                                "Incomplete snapshot exposed an enabled mutation control",
                            )
                            await expect(page.locator("#control-restricted")).to_be_visible()
                            checkpoint(
                                "legacy capture cannot assert healthy heartbeat or line state"
                            )
                            ingest_snapshot_fixture(legacy=True)
                            heartbeat = page.locator("#heartbeat-state")
                            await expect(good.locator("dd > strong")).to_have_text(
                                str(UINT64_MAX) + " 개"
                            )
                            await expect(heartbeat).to_have_attribute("data-state", "UNKNOWN")
                            await expect(page.locator("#line-state")).to_have_text("불명")
                            checkpoint("schema 1.1 GOOD heartbeat and fresh exact UInt64 rendering")
                            ingest_snapshot_fixture(seq=2)
                            await expect(heartbeat).to_have_attribute("data-state", "GOOD")
                            await expect(heartbeat).to_have_text("정상")
                            await expect(good.locator("dd > strong")).to_have_text(
                                str(UINT64_MAX) + " 개"
                            )
                            await expect(good.locator(".quality")).to_have_text("Good")
                            await expect(page.locator("#line-state")).to_have_text("운전 중")
                            checkpoint("Bad/null and missing-value rendering")
                            await expect(bad_null.locator("dd > strong")).to_have_text("—")
                            await expect(bad_null.locator(".quality")).to_have_text("Bad")
                            await expect(
                                page.locator('[data-process-value="Line.GoodTotal"]')
                            ).to_have_text(str(UINT64_MAX))
                            await expect(
                                page.locator('[data-process-value="Line.RejectTotal"]')
                            ).to_have_text("—")
                            for machine in ("Infeed", "Sealer", "Outfeed"):
                                await expect(
                                    page.locator(f'[data-equipment="{machine}"]')
                                ).to_have_attribute("data-state", "UNKNOWN")
                            await expect(bad_null.locator(".last-observation")).to_have_text(
                                "마지막 관측: 값 불명"
                            )
                            await expect(bad_value.locator("dd > strong")).to_have_text("—")
                            await expect(bad_value.locator(".last-observation")).to_have_text(
                                "마지막 관측: 7"
                            )
                            await expect(missing.locator("dd > strong")).to_have_text("—")
                            await expect(missing.locator(".quality")).to_have_text("미수신 · 불명")
                            checkpoint("actual snapshot API evidence")
                            snapshot_path = (
                                "/api/v1/snapshot?line_id="
                                + LINE_ID
                                + "&source_id=browser-source&stream_id=browser-stream"
                            )
                            actual = await context.request.get(origin + snapshot_path)
                            require(actual.status == 200, "Actual snapshot API read failed")
                            payload = await actual.json()
                            values = {
                                item["tag_id"]: item
                                for item in payload["screen"]["view"]["samples"]
                            }
                            require(
                                values["Line.GoodTotal"]["reading"]["value"] == str(UINT64_MAX)
                                and values["Line.RejectTotal"]["reading"]["value"] is None,
                                "Actual snapshot API lost exact UInt64/null evidence",
                            )
                            require(
                                payload["screen"]["view"]["control_restricted"]
                                and payload["health"]["heartbeat"] == "GOOD",
                                "Fixture communication health or control restriction was incorrect",
                            )
                            screenshot.parent.mkdir(parents=True, exist_ok=True)
                            await page.screenshot(path=str(screenshot), full_page=True)
                            print(
                                "Browser: stored observations expire without replacement",
                                flush=True,
                            )
                            checkpoint("stored observation expiry")
                            await expect(good.locator(".quality")).to_have_text(
                                "Good · 오래된 관측 / 불명", timeout=10_000
                            )
                            await expect(good.locator("dd > strong")).to_have_text("—")
                            await expect(good.locator(".last-observation")).to_have_text(
                                "마지막 관측: " + str(UINT64_MAX)
                            )
                            await expect(page.locator("#line-state")).to_have_text("불명")
                            await expect(page.locator("#snapshot-health")).to_have_text("수집 지연")
                            await expect(heartbeat).to_have_attribute("data-state", "DELAYED")
                            await expect(heartbeat).to_have_text("지연")
                            actual = await context.request.get(origin + snapshot_path)
                            require(
                                (await actual.json())["health"]["state"] == "STALE",
                                "Expired stored capture remained fresh in actual API",
                            )
                            await page.screenshot(
                                path=str(screenshot.with_stem(screenshot.stem + "-stale")),
                                full_page=True,
                            )

                            checkpoint("five-second communication alarm preserves raw UInt64")
                            await expect(heartbeat).to_have_attribute(
                                "data-state", "ALARM", timeout=5000
                            )
                            await expect(heartbeat).to_have_text("통신 경보")
                            await expect(page.locator("#line-state")).to_have_text("불명")
                            actual = await context.request.get(origin + snapshot_path)
                            alarm = await actual.json()
                            require(
                                alarm["health"]["heartbeat"] == "ALARM"
                                and alarm["screen"]["view"]["state"] == "UNKNOWN"
                                and any(
                                    item["tag_id"] == "Line.GoodTotal"
                                    and item["reading"]["value"] == str(UINT64_MAX)
                                    for item in alarm["screen"]["view"]["samples"]
                                ),
                                "Alarm did not retain exact raw evidence and unknown derived state",
                            )
                            checkpoint("future gateway clock cannot restore GOOD on catch-up")
                            future_capture = ingest_snapshot_fixture(seq=3, future_seconds=10)
                            await expect(heartbeat).to_have_attribute("data-state", "UNKNOWN")
                            deadline = (
                                time.monotonic()
                                + max(0, (future_capture - datetime.now(UTC)).total_seconds())
                                + 1.5
                            )
                            while time.monotonic() < deadline:
                                require(
                                    await heartbeat.get_attribute("data-state") != "GOOD",
                                    "Future gateway clock restored browser GOOD on catch-up",
                                )
                                actual = await context.request.get(origin + snapshot_path)
                                clock_read = await actual.json()
                                require(
                                    clock_read["health"]["heartbeat"] != "GOOD",
                                    "Future gateway clock restored API GOOD on catch-up",
                                )
                                require(
                                    clock_read["screen"]["view"]["state"] == "UNKNOWN",
                                    "Future gateway clock restored derived line state",
                                )
                                await asyncio.sleep(0.25)
                            await expect(heartbeat).to_have_attribute("data-state", "ALARM")
                            await expect(page.locator("#line-state")).to_have_text("불명")
                            actual = await context.request.get(origin + snapshot_path)
                            require(
                                (await actual.json())["health"]["heartbeat"] == "ALARM",
                                "Stored receipt age failed to retain alarm after clock catch-up",
                            )

                            checkpoint(
                                "stable old source times permit START; "
                                "lost POST response retains original ID"
                            )
                            await page.locator("#command-panel").wait_for(state="visible")
                            for kind in ("START", "PAUSE", "END_RUN", "RESET_FAULT"):
                                require(
                                    await page.locator("#command-" + kind).count() == 1,
                                    "Operator command controls were missing",
                                )
                            ingest_snapshot_fixture(seq=4, controls=True)

                            async def maintain_fresh_control_capture():
                                sequence = 5
                                while True:
                                    ingest_snapshot_fixture(seq=sequence, controls=True)
                                    sequence += 1
                                    await asyncio.sleep(0.6)

                            capture_task = asyncio.create_task(maintain_fresh_control_capture())
                            fixture_tasks.append(capture_task)
                            await screen_recovery_checks(
                                page, context, origin, snapshot_path, bridges
                            )
                            intercepted = []

                            async def lose_committed_command_response(route):
                                payload = route.request.post_data_json
                                intercepted.append(payload)
                                # Forward the real browser body/cookies/Origin/CSRF to the
                                # actual API, then drop only its post-commit response.
                                received = await route.fetch()
                                require(
                                    received.status == 200,
                                    "Fresh operator START was not journaled by actual API",
                                )
                                receipt = await received.json()
                                require(
                                    receipt["status"] == "REQUESTED",
                                    "Request storage incorrectly claimed PLC execution success",
                                )
                                rows = command_rows()
                                require(
                                    len(rows) == 1
                                    and rows[0]["command_id"] == receipt["command_id"]
                                    and rows[0]["status"] == "REQUESTED",
                                    "Lost POST did not leave one durable original request",
                                )
                                await route.abort("failed")

                            await page.route("**/api/v1/commands", lose_committed_command_response)
                            try:
                                await expect(page.locator("#command-START")).to_be_enabled()
                                await page.locator("#command-START").click()
                                await expect(page.locator("#command-status")).to_have_attribute(
                                    "data-status", "REQUESTED"
                                )
                                require(
                                    len(intercepted) == 1,
                                    "Uncertain command POST was automatically resubmitted",
                                )
                                original_command_id = intercepted[0]["request"]["command_id"]
                                await expect(page.locator("#original-command-id")).to_have_text(
                                    original_command_id
                                )
                                await expect(page.locator("#command-status")).to_have_text(
                                    "요청 저장 · 전달 대기"
                                )
                                await expect(page.locator("#command-START")).to_be_disabled()
                                require(
                                    await page.locator("#command-acknowledge").count() == 0,
                                    "Unresolved request exposed a next-command shortcut",
                                )
                                # Wait through multiple real GET polls and confirm the ID
                                # and one-row journal are retained without a new POST.
                                for _ in range(6):
                                    await asyncio.sleep(0.25)
                                    require(
                                        len(intercepted) == 1 and len(command_rows()) == 1,
                                        "Uncertain POST created another command request",
                                    )
                                checkpoint(
                                    "WS revision gap blocks new control but preserves "
                                    "original command ID"
                                )
                                previous_sync = await page.locator(
                                    "#screen-connection"
                                ).get_attribute("data-sync")
                                broken = bridges[-1]
                                broken["mode"] = "gap"
                                await assert_screen_unavailable(page)
                                require(
                                    broken["changed"],
                                    "Revision-gap fault was not delivered",
                                )
                                await expect(page.locator("#original-command-id")).to_have_text(
                                    original_command_id
                                )
                                await expect(page.locator("#screen-connection")).to_have_attribute(
                                    "data-state", "synced"
                                )
                                require(
                                    await page.locator("#screen-connection").get_attribute(
                                        "data-sync"
                                    )
                                    != previous_sync
                                    and len(intercepted) == 1
                                    and len(command_rows()) == 1,
                                    "Screen resynchronization replayed or lost "
                                    "the original command",
                                )
                                await expect(page.locator("#original-command-id")).to_have_text(
                                    original_command_id
                                )
                                await page.screenshot(
                                    path=str(screenshot.with_stem(screenshot.stem + "-command")),
                                    full_page=True,
                                )
                            finally:
                                await page.unroute(
                                    "**/api/v1/commands", lose_committed_command_response
                                )
                                capture_task.cancel()
                                await asyncio.gather(capture_task, return_exceptions=True)

                            def start_binding_capture():
                                from apps.api.commands import claim_next

                                with engine.begin() as connection:
                                    claimed = claim_next(
                                        connection,
                                        authenticated_gateway_id="browser-gateway",
                                        authenticated_source_id="browser-source",
                                        authenticated_stream_id="browser-stream",
                                        now=datetime.now(UTC),
                                    )
                                require(
                                    claimed is None and command_rows()[0]["status"] == "EXPIRED",
                                    "Undispatched START did not expire through the real journal",
                                )

                                async def maintain():
                                    sequence = 1000
                                    while True:
                                        ingest_snapshot_fixture(
                                            seq=sequence, controls=True, binding=True
                                        )
                                        sequence += 1
                                        await asyncio.sleep(0.6)

                                task = asyncio.create_task(maintain())
                                fixture_tasks.append(task)
                                return task

                            mes_recipe, mes_order, recovery_ids = await exercise_mes(
                                browser,
                                origin,
                                url,
                                producer_password,
                                submit_login,
                                sensitive,
                                errors,
                                credential_urls,
                                screenshot,
                                start_binding_capture,
                                viewer_password,
                                ingest_snapshot_fixture,
                            )
                            checkpoint(
                                "MES operator reads lists without production registration rights"
                            )
                            operator_me = await context.request.get(origin + "/auth/me")
                            require(
                                operator_me.status == 200,
                                "Operator session expired during MES check",
                            )
                            await read_only_mes(
                                page,
                                context,
                                origin,
                                data["csrf_token"],
                                "operator",
                                mes_recipe,
                                mes_order,
                                recovery_ids,
                            )
                            await expect(page.locator("#original-command-id")).to_have_text(
                                original_command_id
                            )

                            checkpoint(
                                "viewer has no mutation controls and actual API denies direct POST"
                            )
                            viewer_context = await browser.new_context(
                                viewport={"width": 1280, "height": 900}
                            )
                            try:
                                viewer_page = await viewer_context.new_page()
                                viewer_page.on(
                                    "pageerror",
                                    lambda error: errors.append(type(error).__name__),
                                )
                                viewer_sockets = []
                                viewer_page.on(
                                    "websocket",
                                    lambda socket: viewer_sockets.append(socket),
                                )
                                await viewer_page.add_init_script("""window.__hmiWsCloseCodes = [];
                                    const NativeWebSocket = window.WebSocket;
                                    window.WebSocket = class extends NativeWebSocket {
                                        constructor(...args) {
                                            super(...args);
                                            this.addEventListener('close', event => {
                                                window.__hmiWsCloseCodes.push(event.code);
                                            });
                                        }
                                    };""")
                                await viewer_page.goto(origin, wait_until="domcontentloaded")
                                viewer = await submit_login(
                                    viewer_password,
                                    target_page=viewer_page,
                                    login_name="browser-viewer",
                                    roles=("viewer",),
                                )
                                await viewer_page.locator("#command-panel").wait_for(
                                    state="visible"
                                )
                                for kind in ("START", "PAUSE", "END_RUN", "RESET_FAULT"):
                                    require(
                                        await viewer_page.locator("#command-" + kind).count() == 0,
                                        "Viewer was offered a mutation command",
                                    )
                                forged = intercepted[0] | {
                                    "request": intercepted[0]["request"]
                                    | {
                                        "command_id": str(uuid4()),
                                        "requested_by": "browser-viewer",
                                        "requested_at": datetime.now(UTC).isoformat(),
                                        "expires_at": (
                                            datetime.now(UTC) + timedelta(seconds=5)
                                        ).isoformat(),
                                    }
                                }
                                denial = await viewer_context.request.post(
                                    origin + "/api/v1/commands",
                                    data=forged,
                                    headers={
                                        "Origin": origin,
                                        "X-CSRF-Token": viewer["csrf_token"],
                                    },
                                )
                                require(
                                    denial.status == 403 and len(command_rows()) == 3,
                                    "Viewer direct API command was not denied before journal write",
                                )
                                checkpoint(
                                    "MES viewer reads lists and cannot register through UI/API"
                                )
                                await read_only_mes(
                                    viewer_page,
                                    viewer_context,
                                    origin,
                                    viewer["csrf_token"],
                                    "viewer",
                                    mes_recipe,
                                    mes_order,
                                    recovery_ids,
                                )
                                checkpoint(
                                    "WS policy 1008 after actual session logout "
                                    "never auto-reconnects"
                                )
                                socket_count = len(viewer_sockets)
                                revoked = await viewer_context.request.post(
                                    origin + "/auth/logout",
                                    headers={
                                        "Origin": origin,
                                        "X-CSRF-Token": viewer["csrf_token"],
                                    },
                                )
                                require(
                                    revoked.status == 200,
                                    "Viewer session revocation failed",
                                )
                                await viewer_page.locator("#login-form").wait_for(state="visible")
                                require(
                                    await viewer_page.evaluate(
                                        "window.__hmiWsCloseCodes.includes(1008)"
                                    ),
                                    "Revoked live screen did not close with the policy code",
                                )
                                await asyncio.sleep(1.3)
                                require(
                                    len(viewer_sockets) == socket_count,
                                    "Policy-closed screen automatically reconnected without login",
                                )
                                require(
                                    await viewer_page.evaluate(
                                        "() => localStorage.length === 0 "
                                        "&& sessionStorage.length === 0 && document.cookie === ''"
                                    ),
                                    "Viewer credentials leaked into browser-readable storage",
                                )
                            finally:
                                await viewer_context.close()

                            checkpoint("refresh re-login, logout and fresh re-login")
                            print(
                                "Browser: refresh requires fresh login, then logout and re-login",
                                flush=True,
                            )
                            await page.reload(wait_until="domcontentloaded")
                            await page.locator("#login-form").wait_for(state="visible")
                            data = await submit_login(changed)
                            require(
                                not data["must_change_password"],
                                "Refresh lost changed-password state",
                            )
                            await page.locator("#current-login").wait_for(state="visible")
                            await protected_storage()
                            await page.locator("#command-lookup-id").fill(original_command_id)
                            await page.locator("#command-lookup-submit").click()
                            await expect(page.locator("#original-command-id")).to_have_text(
                                original_command_id
                            )
                            await expect(page.locator("#command-status")).to_have_attribute(
                                "data-status", "EXPIRED"
                            )
                            require(
                                len(command_rows()) == 3,
                                "Original-ID recovery created another request",
                            )
                            async with page.expect_response(
                                lambda response: response.url == origin + "/auth/logout"
                            ) as received:
                                await page.locator("#logout").click()
                            require(
                                (await received.value).status == 200,
                                "Browser logout failed",
                            )
                            await page.locator("#login-form").wait_for(state="visible")
                            require(
                                (await context.request.get(origin + "/auth/me")).status == 401,
                                "Logged-out browser retained authentication",
                            )
                            await submit_login(changed)
                            await page.locator("#current-login").wait_for(state="visible")
                            await protected_storage()
                            from scripts.alarm_browser_checks import exercise_alarm_center

                            await exercise_alarm_center(
                                browser,
                                origin,
                                url,
                                submit_login,
                                changed,
                                viewer_password,
                                checkpoint,
                                require,
                                sensitive,
                                errors,
                            )
                            from scripts.cancellation_browser_checks import (
                                exercise_cancellation_center,
                            )

                            await exercise_cancellation_center(
                                browser,
                                origin,
                                url,
                                submit_login,
                                producer_password,
                                changed,
                                viewer_password,
                                checkpoint,
                                require,
                                sensitive,
                                errors,
                                screenshot,
                            )
                            from scripts.identifier_browser_checks import (
                                exercise_identifier_roundtrips,
                            )

                            await exercise_identifier_roundtrips(
                                browser,
                                origin,
                                submit_login,
                                producer_password,
                                checkpoint,
                                require,
                                errors,
                                screenshot,
                            )
                            from scripts.line_alarm_browser_checks import exercise_line_alarm_center

                            await exercise_line_alarm_center(
                                browser,
                                origin,
                                url,
                                submit_login,
                                changed,
                                viewer_password,
                                checkpoint,
                                require,
                                sensitive,
                                errors,
                                screenshot,
                            )
                        require(
                            not rest_screen_reads,
                            "WS screen silently fell back to REST snapshot polling",
                        )
                        require(
                            not errors,
                            "Browser raised an uncaught JavaScript exception",
                        )
                        require(
                            not any(credential_urls),
                            "A credential appeared in a request URL",
                        )
                    finally:
                        # Stop route forwarding before closing the browser so a
                        # queued real WS frame cannot schedule send after teardown.
                        for bridge in bridges:
                            bridge["mode"] = "drop"
                            bridge["client"].on_message(lambda message: None)
                            bridge["server"].on_message(lambda message: None)
                        await asyncio.sleep(0.2)
                        for task in fixture_tasks:
                            task.cancel()
                        await asyncio.gather(*fixture_tasks, return_exceptions=True)
                        await browser.close()
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                require(process.returncode == 0, "User API did not shut down cleanly")
        logs = log_path.read_bytes()
        require(
            all(value.encode() not in logs for value in sensitive),
            "User API logs exposed a test credential",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chromium", default=os.environ.get("CHROMIUM_EXECUTABLE", "/usr/bin/chromium")
    )
    parser.add_argument("--web-dir", type=Path, default=ROOT / "apps/web/dist")
    parser.add_argument("--screenshot", type=Path, default=ROOT / ".local/qa/app-a.png")
    parser.add_argument(
        "--focus-line-alarms",
        action="store_true",
        help="Run original secure setup/password rotation and bounded Line alarm checks",
    )
    args = parser.parse_args()
    executable = shutil.which(args.chromium)
    if executable is None:
        print(
            "FAIL: Chromium is required; provide --chromium /path/to/chromium",
            file=sys.stderr,
        )
        return 1
    if not (args.web_dir / "index.html").is_file():
        print("FAIL: Build apps/web first or provide --web-dir", file=sys.stderr)
        return 1
    try:
        import pytest

        with tempfile.TemporaryDirectory(prefix="hmi-mes-app-a-browser-") as directory:
            asyncio.run(
                exercise(
                    Path(directory),
                    executable,
                    args.web_dir.resolve(),
                    args.screenshot.resolve(),
                    focus_line_alarms=args.focus_line_alarms,
                )
            )
    except ImportError:
        print(
            "FAIL: Install project development dependencies, including Playwright",
            file=sys.stderr,
        )
        return 1
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        # Fixture failures and browser errors may embed environment or filled-input
        # diagnostics. Only our fixed assertion messages are safe to print.
        message = str(error) if type(error) is BrowserCheckFailed else type(error).__name__
        if isinstance(error, pytest.fail.Exception):
            message = "Disposable PostgreSQL fixture failed; verify local Docker and pinned image"
        print("FAIL: " + message + " (" + CHECK_STAGE + ")", file=sys.stderr)
        return 1
    if args.focus_line_alarms:
        print("PASS: Real App A CLI/PG/browser Line+Sealer alarm capture fixtures,")
        print("      exact scope, late GET isolation, original ACK/note recovery and roles.")
        print("      Fixture ingestion/fault-store evidence; OPC and whole T05 not exercised.")
        print("Screenshot: " + str(args.screenshot.with_stem(args.screenshot.stem + "-line-alarm")))
        return 0
    print("PASS: Real Chromium/App A CLI/PostgreSQL login, initial password rotation,")
    print("      identity/roles, registered source, exact UInt64/Bad/null/expired observations,")
    print("      legacy UNKNOWN, 1.1 GOOD→DELAYED→ALARM, future-clock non-resurrection,")
    print("      atomic WS disconnect/watchdog/gap resync, policy1008, process diagram/mobile,")
    print("      operator REQUESTED/original-ID recovery after lost POST, viewer direct API403,")
    print("      MES recipes, DRAFT/release/bind original-ID recovery, LOAD tracking,")
    print("      alarm ACK/note, C1 unbound cancellation/recovery, roles and protected storage.")
    print(
        "      Fixture ingestion, command journal and MES registration; OPC/PLC is not exercised."
    )
    print("Screenshot: " + str(args.screenshot))
    print("Stale screenshot: " + str(args.screenshot.with_stem(args.screenshot.stem + "-stale")))
    print(
        "Command screenshot: " + str(args.screenshot.with_stem(args.screenshot.stem + "-command"))
    )
    print("MES screenshot: " + str(args.screenshot.with_stem(args.screenshot.stem + "-mes")))
    print(
        "MES uncertain screenshot: "
        + str(args.screenshot.with_stem(args.screenshot.stem + "-mes-uncertain"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
