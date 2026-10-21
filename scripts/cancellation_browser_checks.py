"""C1 actual browser/API/DB checks; no PLC activity or connection cancellation.

Prepared separately while the repository is frozen. The parent launcher owns the
disposable database, real user server, login pacing and browser lifecycle.
"""

import asyncio
import secrets
from uuid import uuid4

from scripts.browser_identity_urls import work_action, work_receipt


def _admin_fixture(url, origin):
    from apps.api.auth import UserAuth
    from apps.api.database import create_database_engine

    initial, password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    engine = create_database_engine(url)
    try:
        auth = UserAuth(engine, allowed_origins={origin})
        auth.bootstrap_user("browser-cancel-admin", initial, roles={"equipment_admin"})
        challenge = auth.begin_login()
        session = auth.login(
            "browser-cancel-admin",
            initial,
            challenge_token=challenge.token,
            csrf_token=challenge.csrf_token,
            origin=origin,
        )
        auth.change_password(session.token, password, csrf_token=session.csrf_token, origin=origin)
    finally:
        engine.dispose()
    return password, initial


async def exercise_cancellation_center(
    browser,
    origin,
    url,
    submit_login,
    producer_password,
    operator_password,
    viewer_password,
    checkpoint,
    require,
    sensitive,
    errors,
    screenshot,
):
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import expect
    from sqlalchemy import text

    from apps.api.bootstrap import LINE_ID
    from apps.api.database import create_database_engine

    admin_password, admin_initial = _admin_fixture(url, origin)
    sensitive.extend((admin_password, admin_initial))
    engine = create_database_engine(url)
    contexts = []
    cancel_posts = []
    credential_urls = []

    def machine_state():
        with engine.connect() as db:
            return {
                "commands": list(
                    db.execute(
                        text(
                            "SELECT command_id,request,canonical_payload,result,status,"
                            "collision_latched "
                            "FROM command_requests ORDER BY command_id"
                        )
                    ).mappings()
                ),
                "links": list(
                    db.execute(
                        text("SELECT * FROM work_order_execution_links ORDER BY execution_id")
                    ).mappings()
                ),
                "slots": list(
                    db.execute(
                        text("SELECT * FROM line_execution_reservations ORDER BY line_id")
                    ).mappings()
                ),
                "executions": list(
                    db.execute(
                        text("SELECT * FROM production_executions ORDER BY execution_id")
                    ).mappings()
                ),
                "plans": list(
                    db.execute(
                        text("SELECT * FROM execution_plans ORDER BY execution_id")
                    ).mappings()
                ),
                "totals": list(
                    db.execute(
                        text("SELECT * FROM execution_totals ORDER BY execution_id")
                    ).mappings()
                ),
                "events": db.execute(text("SELECT count(*) FROM production_events")).scalar(),
            }

    def records(order):
        with engine.connect() as db:
            return {
                "order": db.execute(text("SELECT * FROM work_orders WHERE id=:id"), {"id": order})
                .mappings()
                .one(),
                "receipts": list(
                    db.execute(
                        text("SELECT * FROM work_order_cancellations WHERE work_order_id=:id"),
                        {"id": order},
                    ).mappings()
                ),
                "audit": list(
                    db.execute(
                        text(
                            "SELECT actor,action,evidence FROM mes_management_audit "
                            "WHERE work_order_id=:id ORDER BY id"
                        ),
                        {"id": order},
                    ).mappings()
                ),
            }

    async def login_context(password, login="browser-planner", role="production_manager"):
        context = await browser.new_context(viewport={"width": 1440, "height": 1000})
        contexts.append(context)
        await context.add_init_script("""window.__hmiCspViolations = 0;
            document.addEventListener('securitypolicyviolation', () => {
                window.__hmiCspViolations += 1;
            });""")
        page = await context.new_page()
        page.set_default_timeout(15_000)
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        page.on(
            "request",
            lambda request: credential_urls.append(
                any(value in request.url for value in sensitive)
            ),
        )
        page.on(
            "request",
            lambda request: (
                cancel_posts.append(
                    {
                        "url": request.url,
                        "body": request.post_data_json,
                    }
                )
                if request.method == "POST" and "/api/v1/work-order-actions/cancel?" in request.url
                else None
            ),
        )
        await page.goto(origin, wait_until="domcontentloaded")
        session = await submit_login(password, target_page=page, login_name=login, roles=(role,))
        return context, page, session

    async def request_fixture(context, session, order, released=False):
        definition = {
            "id": order,
            "item_id": "browser-item-A",
            "line_id": LINE_ID,
            "recipe_version_id": "browser-managed-v1",
            "target_quantity": 7,
            "planned_date": "2026-10-25",
        }
        headers = {"Origin": origin, "X-CSRF-Token": session["csrf_token"]}
        created = await context.request.post(
            origin + "/api/v1/work-orders", data=definition, headers=headers
        )
        require(created.status == 200, "Cancellation candidate registration failed")
        if not released:
            return None
        release = await context.request.post(
            work_action(origin, "release", order),
            data={"release_id": order + "-release", "expected_row_version": "1"},
            headers=headers,
        )
        require(release.status == 200, "Cancellation released candidate fixture failed")
        return await release.json()

    async def load_original(page, order, command):
        await page.locator("#workflow-recovery-kind").select_option("bind")
        await page.locator("#workflow-recovery-order").fill(order)
        await page.locator("#workflow-recovery-id").fill(command)
        await page.locator("#workflow-recovery-submit").click()
        await expect(page.locator("#workflow-status")).to_have_attribute("data-state", "confirmed")
        await expect(page.locator("#load-command-id")).to_have_text(command)

    async def direct_denial(context, session, role):
        denied = await context.request.post(
            work_action(origin, "cancel", "browser-cancel-denial"),
            data={
                "cancellation_id": "denied-cancel-" + role,
                "expected_row_version": "1",
            },
            headers={"Origin": origin, "X-CSRF-Token": session["csrf_token"]},
        )
        require(
            denied.status == 403,
            "Read-only role gained work-order cancellation permission",
        )

    async def protected_state(page, context):
        require(
            await page.evaluate("""() => localStorage.length === 0
            && sessionStorage.length === 0 && document.cookie === ''
            && window.__hmiCspViolations === 0"""),
            "Cancellation UI leaked storage or violated CSP",
        )
        for cookie in await context.cookies():
            if cookie["name"] in {"hmi_session", "hmi_preauth"}:
                require(
                    cookie["httpOnly"] and cookie["sameSite"] == "Strict",
                    "Cancellation cookies lost authentication protections",
                )
                sensitive.append(cookie["value"])

    try:
        before_machine = machine_state()
        with engine.connect() as db:
            original_load = db.execute(
                text(
                    "SELECT load_command_id FROM work_order_execution_links "
                    "WHERE work_order_id='browser-next-order'"
                )
            ).scalar_one()
            legacy_start = db.execute(
                text(
                    "SELECT command_id FROM command_requests WHERE kind='START' "
                    "ORDER BY created_at LIMIT 1"
                )
            ).scalar_one()
        context, page, session = await login_context(producer_password)
        for order in (
            "browser-cancel-draft",
            "browser-cancel-denial",
            "browser-cancel-stale",
            "browser-cancel-shared",
        ):
            await request_fixture(context, session, order)
        release_receipt = await request_fixture(context, session, "browser-cancel-released", True)
        await load_original(page, "browser-next-order", original_load)
        await page.locator("#command-lookup-id").fill(legacy_start)
        await page.locator("#command-lookup-submit").click()
        await expect(page.locator("#original-command-id")).to_have_text(legacy_start)
        await expect(page.locator("#command-status")).to_have_attribute("data-status", "EXPIRED")
        await page.locator("#workspace-drafts").click()
        checkpoint("C1 metadata cancellation remains independent of another original LOAD")
        await expect(page.locator('[data-cancel-order="browser-next-order"]')).to_be_disabled()
        draft = "browser-cancel-draft"
        await expect(page.locator('[data-cancel-order="' + draft + '"]')).to_be_enabled()
        committed, late_release, late_finished = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        draft_receipts = []

        async def late_committed_response(route):
            response = await route.fetch()
            require(response.status == 200, "Actual DRAFT cancellation failed to commit")
            draft_receipts.append(await response.json())
            committed.set()
            try:
                await late_release.wait()
                await route.fulfill(response=response)
            except PlaywrightError:
                # Explicit session logout aborts the browser fetch after real commit.
                pass
            finally:
                late_finished.set()

        draft_path = work_action(origin, "cancel", draft)
        await page.route(draft_path, late_committed_response)
        try:
            await page.locator('[data-cancel-order="' + draft + '"]').click()
            await expect(page.locator("#cancellation-confirmation")).to_contain_text(
                "설비에 연결되지 않은 작업만 취소할 수 있습니다"
            )
            await page.locator("#cancellation-submit").click()
            await asyncio.wait_for(committed.wait(), 10)
            cancel_id = draft_receipts[0]["cancellation_id"]
            await expect(page.locator("#cancellation-original-id")).to_have_text(cancel_id)
            await expect(page.locator("#cancellation-status")).to_have_attribute(
                "data-state", "SENDING"
            )
            await expect(page.locator('[data-release-order="' + draft + '"]')).to_be_disabled()
            await page.locator("#workspace-operations").click()
            await expect(page.locator("#original-command-id")).to_have_text(legacy_start)
            await expect(page.locator("#workflow-original-id")).to_have_text(original_load)
            await expect(page.locator("#load-command-id")).to_have_text(original_load)
            await page.locator("#workspace-drafts").click()
            await expect(page.locator("#cancellation-original-id")).to_have_text(cancel_id)
            await page.locator("#logout").click()
            viewer = await submit_login(
                viewer_password,
                target_page=page,
                login_name="browser-viewer",
                roles=("viewer",),
            )
            late_release.set()
            await asyncio.wait_for(late_finished.wait(), 10)
            await expect(page.locator("#cancellation-status")).to_have_attribute(
                "data-state", "UNKNOWN"
            )
            require(
                await page.locator("#cancellation-prepare-again").count() == 0,
                "Uncertain cancellation exposed new preparation",
            )
            await expect(page.locator("#cancellation-original-id")).to_have_text(cancel_id)
            await expect(page.locator("#cancellation-actor")).to_have_text("browser-planner")
            await expect(page.locator("#workflow-original-id")).to_have_text(original_load)
            await expect(page.locator("#original-command-id")).to_have_text(legacy_start)
            recovery_path = work_receipt(origin, "cancellation", draft, cancel_id)

            async def invisible_receipt(route):
                response = await route.fetch()
                require(
                    response.status == 200,
                    "Committed cancellation receipt was not stored",
                )
                await route.fulfill(status=404, json={"detail": "receipt visibility fault"})

            await page.route(recovery_path, invisible_receipt)
            try:
                await page.locator("#cancellation-lookup").click()
                await expect(page.locator("#cancellation-status")).to_have_attribute(
                    "data-state", "UNKNOWN"
                )
                require(
                    await page.locator("#cancellation-acknowledge").count() == 0,
                    "404 cancellation exposed a replacement shortcut",
                )
            finally:
                await page.unroute(recovery_path, invisible_receipt)
            await page.locator("#cancellation-lookup").click()
            await expect(page.locator("#cancellation-status")).to_have_attribute(
                "data-state", "CONFIRMED"
            )
            await expect(page.locator("#cancellation-actor")).to_have_text("browser-planner")
            await expect(page.locator("#cancellation-receipt")).to_contain_text(
                "취소됨 (CANCELLED)"
            )
            require(
                await page.locator("#cancellation-acknowledge").count() == 0,
                "Different account gained ownership of the original cancellation",
            )
            await page.locator("#workspace-drafts").click()
            require(
                await page.locator("[data-cancel-order], #cancellation-submit").count() == 0,
                "Viewer received cancellation mutation controls",
            )
            await direct_denial(context, viewer, "viewer")
            await page.locator("#logout").click()
            session = await submit_login(
                producer_password,
                target_page=page,
                login_name="browser-planner",
                roles=("production_manager",),
            )
            await expect(page.locator("#cancellation-original-id")).to_have_text(cancel_id)
            await page.locator("#cancellation-acknowledge").click()
            await expect(
                page.locator('[data-cancellation-history-id="' + cancel_id + '"]')
            ).to_have_count(1)
            saved = records(draft)
            require(
                saved["order"]["status"] == "CANCELLED"
                and str(saved["order"]["row_version"]) == "2"
                and len(saved["receipts"]) == 1
                and [row["action"] for row in saved["audit"]]
                == ["REGISTER_WORK_ORDER", "CANCEL_WORK_ORDER"]
                and saved["receipts"][0]["original_intent"]["actor"] == "browser-planner",
                "DRAFT cancellation lost its original actor or duplicated its audit",
            )
            require(
                len([item for item in cancel_posts if item["url"] == draft_path]) == 1,
                "Late cancellation response/logout/GET caused another POST",
            )
        finally:
            late_release.set()
            await page.unroute(draft_path, late_committed_response)

        checkpoint(
            "C1 RELEASED cancellation preserves the exact prior release and all machine records"
        )
        await page.locator("#workspace-drafts").click()
        released = "browser-cancel-released"
        released_path = work_action(origin, "cancel", released)
        released_receipts = []

        async def lose_released_response(route):
            response = await route.fetch()
            require(response.status == 200, "Actual RELEASED cancellation did not commit")
            released_receipts.append(await response.json())
            await route.abort("failed")

        await page.route(released_path, lose_released_response)
        try:
            await page.locator('[data-cancel-order="' + released + '"]').click()
            await page.locator("#cancellation-submit").click()
            await expect(page.locator("#cancellation-status")).to_have_attribute(
                "data-state", "UNKNOWN"
            )
            released_id = released_receipts[0]["cancellation_id"]
            await page.locator("#cancellation-lookup").click()
            await expect(page.locator("#cancellation-status")).to_have_attribute(
                "data-state", "CONFIRMED"
            )
            await expect(page.locator("#cancellation-original-id")).to_have_text(released_id)
            await expect(page.locator("#workflow-original-id")).to_have_text(original_load)
            saved = records(released)
            require(
                saved["order"]["status"] == "CANCELLED"
                and str(saved["order"]["row_version"]) == "3"
                and len(saved["receipts"]) == 1
                and [row["action"] for row in saved["audit"]]
                == ["REGISTER_WORK_ORDER", "RELEASE_WORK_ORDER", "CANCEL_WORK_ORDER"],
                "RELEASED cancellation duplicated metadata or changed the wrong version",
            )
            original_release = await context.request.get(
                work_receipt(origin, "release", released, release_receipt["release_id"])
            )
            require(
                original_release.status == 200
                and (await original_release.json())["receipt"] == release_receipt,
                "Cancellation rewrote the original release receipt",
            )
            require(
                machine_state() == before_machine,
                "C1 cancellation changed execution/LOAD/slot/totals",
            )
            require(
                len([item for item in cancel_posts if item["url"] == released_path]) == 1,
                "RELEASED cancellation recovery auto-resubmitted",
            )
            await page.locator("#cancellation-acknowledge").click()
            row = page.locator("#mes-order-list tbody tr").filter(has_text=released)
            await expect(row).to_contain_text("취소됨 (CANCELLED)")
            require(
                await row.locator(
                    "[data-release-order], [data-bind-order], [data-cancel-order]"
                ).count()
                == 0,
                "CANCELLED order remained selectable for a mutation",
            )
            await page.screenshot(
                path=str(screenshot.with_stem(screenshot.stem + "-cancellation")),
                full_page=True,
            )
            await protected_state(page, context)
        finally:
            await page.unroute(released_path, lose_released_response)

        checkpoint("C1 stale cached version rejects once and preserves the original ID")
        stale_context, stale_page, stale_session = await login_context(producer_password)
        await stale_page.locator("#workspace-drafts").click()
        stale = "browser-cancel-stale"
        await expect(stale_page.locator('[data-cancel-order="' + stale + '"]')).to_be_enabled()
        newer = await stale_context.request.post(
            work_action(origin, "release", stale),
            data={
                "release_id": "browser-cancel-stale-release",
                "expected_row_version": "1",
            },
            headers={"Origin": origin, "X-CSRF-Token": stale_session["csrf_token"]},
        )
        require(newer.status == 200, "Stale-version fixture release failed")
        before = records(stale)
        await load_original(stale_page, "browser-next-order", original_load)
        async with stale_page.expect_response(
            lambda response: response.url == work_action(origin, "cancel", stale)
        ) as rejected:
            await stale_page.locator('[data-cancel-order="' + stale + '"]').click()
            await stale_page.locator("#cancellation-submit").click()
        require(
            (await rejected.value).status == 409,
            "Stale cancellation version was not rejected",
        )
        await expect(stale_page.locator("#cancellation-status")).to_have_attribute(
            "data-state", "REJECTED"
        )
        stale_id = await stale_page.locator("#cancellation-original-id").inner_text()
        await stale_page.locator("#cancellation-lookup").click()
        await expect(stale_page.locator("#cancellation-status")).to_have_attribute(
            "data-state", "UNKNOWN"
        )
        await expect(stale_page.locator("#cancellation-original-id")).to_have_text(stale_id)
        require(records(stale) == before, "Stale/404 cancellation changed metadata or audit")

        checkpoint("C1 confirmed rejection survives GET404 and explicit fresh-detail preparation")
        await expect(stale_page.locator("#cancellation-rejection-evidence")).to_be_visible()
        await expect(stale_page.locator("#cancellation-prepare-again")).to_be_visible()
        stale_post_count = len(cancel_posts)
        await stale_page.locator("#cancellation-prepare-again").click()
        await expect(stale_page.locator("#cancellation-confirmation")).to_be_visible()
        await expect(stale_page.locator("#cancellation-status")).to_have_count(0)
        require(
            len(cancel_posts) == stale_post_count,
            "Archiving rejection auto-submitted a cancellation",
        )
        await expect(
            stale_page.locator('[data-cancellation-rejected-id="' + stale_id + '"]')
        ).to_have_count(1)
        await stale_page.locator("#cancellation-rejected-history").evaluate(
            "node => { node.open = true }"
        )
        await expect(stale_page.locator("#cancellation-rejected-history")).to_contain_text(
            "이 조회에서 저장 기록을 찾지 못함"
        )
        await expect(stale_page.locator("#workflow-original-id")).to_have_text(original_load)
        await stale_page.locator("#cancellation-submit").click()
        await expect(stale_page.locator("#cancellation-status")).to_have_attribute(
            "data-state", "CONFIRMED"
        )
        fresh_id = await stale_page.locator("#cancellation-original-id").inner_text()
        require(fresh_id != stale_id, "Explicit new intent reused the rejected cancellation ID")
        require(
            len(cancel_posts) == stale_post_count + 1,
            "Explicit confirmation sent more than one new cancellation",
        )
        newest = next(item for item in cancel_posts if item["body"]["cancellation_id"] == fresh_id)
        require(
            newest["body"]["expected_row_version"] == "2",
            "New cancellation reused the stale row version",
        )
        current = records(stale)
        require(
            current["order"]["status"] == "CANCELLED"
            and str(current["order"]["row_version"]) == "3"
            and len(current["receipts"]) == 1
            and len(current["audit"]) == 3,
            "Reprepared cancellation changed rejected metadata or duplicated audit",
        )
        await expect(stale_page.locator("#workflow-original-id")).to_have_text(original_load)
        await stale_page.locator("#workspace-operations").click()
        await expect(stale_page.locator("#load-command-id")).to_have_text(original_load)
        await protected_state(stale_page, stale_context)
        await stale_page.screenshot(
            path=str(screenshot.with_stem(screenshot.stem + "-reprepare")), full_page=True
        )

        checkpoint(
            "C1 unknown local link remains server-authoritative; existing LOAD tracker survives409"
        )
        bound_context, bound_page, _ = await login_context(producer_password)
        await load_original(bound_page, "browser-next-order", original_load)
        await bound_page.locator("#workspace-drafts").click()
        await expect(
            bound_page.locator('[data-cancel-order="browser-next-order"]')
        ).to_be_disabled()
        bound = "browser-managed-order"
        await expect(bound_page.locator('[data-cancel-order="' + bound + '"]')).to_be_enabled()
        before = records(bound)
        async with bound_page.expect_response(
            lambda response: response.url == work_action(origin, "cancel", bound)
        ) as rejected:
            await bound_page.locator('[data-cancel-order="' + bound + '"]').click()
            await bound_page.locator("#cancellation-submit").click()
        require(
            (await rejected.value).status == 409,
            "Unobserved managed link was inferred unbound",
        )
        await expect(bound_page.locator("#cancellation-status")).to_have_attribute(
            "data-state", "REJECTED"
        )
        await expect(bound_page.locator("#workflow-original-id")).to_have_text(original_load)
        await bound_page.locator("#workspace-operations").click()
        await expect(bound_page.locator("#load-command-id")).to_have_text(original_load)
        require(
            records(bound) == before and machine_state() == before_machine,
            "Bound cancellation rejection changed original execution or reservation",
        )
        await protected_state(bound_page, bound_context)
        checkpoint(
            "C1 bound rejection archive grants no cancellation eligibility or LOAD clearance"
        )
        bound_id = await bound_page.locator("#cancellation-original-id").inner_text()
        before_bound_posts = len(cancel_posts)
        await bound_page.locator("#cancellation-prepare-again").click()
        await expect(bound_page.locator("#cancellation-confirmation")).to_be_visible()
        require(
            len(cancel_posts) == before_bound_posts, "Bound rejection archive submitted an intent"
        )
        await expect(bound_page.locator("#workflow-original-id")).to_have_text(original_load)
        async with bound_page.expect_response(
            lambda response: response.url == work_action(origin, "cancel", bound)
        ) as rejected_again:
            await bound_page.locator("#cancellation-submit").click()
        require(
            (await rejected_again.value).status == 409,
            "Bound rejection archive bypassed server link guard",
        )
        await expect(bound_page.locator("#cancellation-status")).to_have_attribute(
            "data-state", "REJECTED"
        )
        second_bound_id = await bound_page.locator("#cancellation-original-id").inner_text()
        require(
            second_bound_id != bound_id
            and records(bound) == before
            and machine_state() == before_machine,
            "Bound recheck changed execution or reused the rejected intent",
        )
        await bound_page.locator("#logout").click()
        await submit_login(
            producer_password,
            target_page=bound_page,
            login_name="browser-planner",
            roles=("production_manager",),
        )
        require(
            await bound_page.locator("#cancellation-prepare-again").count() == 0,
            "A new login inherited rejection preparation authority",
        )
        await bound_page.locator("#cancellation-lookup").click()
        await expect(bound_page.locator("#cancellation-status")).to_have_attribute(
            "data-state", "UNKNOWN"
        )
        await expect(bound_page.locator("#cancellation-original-id")).to_have_text(second_bound_id)
        require(
            await bound_page.locator("#cancellation-prepare-again").count() == 0,
            "GET404 restored another login generation's preparation authority",
        )

        checkpoint(
            "C1 passive recovery retains additive same-work-order guards and both original ledgers"
        )
        _, guard_page, _ = await login_context(producer_password)
        await guard_page.locator("#workspace-drafts").click()
        await guard_page.locator('[data-release-order="browser-cancel-shared"]').click()
        await guard_page.locator("#release-submit").click()
        await expect(guard_page.locator("#workflow-status")).to_have_attribute(
            "data-state", "confirmed"
        )
        release_id = await guard_page.locator("#workflow-original-id").inner_text()
        missing_id = str(uuid4())
        await guard_page.locator("#cancellation-recovery-order").fill("browser-cancel-shared")
        await guard_page.locator("#cancellation-recovery-id").fill(missing_id)
        await guard_page.locator("#cancellation-recovery-submit").click()
        await expect(guard_page.locator("#cancellation-status")).to_have_attribute(
            "data-state", "UNKNOWN"
        )
        await expect(guard_page.locator("#workflow-original-id")).to_have_text(release_id)
        await expect(guard_page.locator("#cancellation-original-id")).to_have_text(missing_id)
        await expect(guard_page.locator("#cancellation-actor")).to_have_text("저장 기록 확인 전")
        await guard_page.locator("#workflow-acknowledge").click()
        await (
            guard_page.locator("section.mes-card")
            .filter(has=guard_page.locator("#mes-orders-heading"))
            .get_by_role("button", name="목록 새로 조회", exact=True)
            .click()
        )
        await expect(
            guard_page.locator('[data-bind-order="browser-cancel-shared"]')
        ).to_be_disabled()
        await expect(guard_page.locator("#cancellation-original-id")).to_have_text(missing_id)
        require(
            await guard_page.locator("#cancellation-prepare-again").count() == 0,
            "Manual GET404 exposed rejection preparation",
        )
        require(
            not any(item["body"]["cancellation_id"] == missing_id for item in cancel_posts),
            "Passive reference recovery submitted a cancellation",
        )

        checkpoint(
            "C1 operator/equipment-admin read the original actor but cannot cancel through UI/API"
        )
        for login, password, role in (
            ("browser-operator", operator_password, "operator"),
            ("browser-cancel-admin", admin_password, "equipment_admin"),
        ):
            read_context, read_page, read_session = await login_context(password, login, role)
            await read_page.locator("#workspace-drafts").click()
            require(
                await read_page.locator("[data-cancel-order], #cancellation-submit").count() == 0,
                "Non-production-manager received C1 mutation controls",
            )
            await read_page.locator("#cancellation-recovery-order").fill(draft)
            await read_page.locator("#cancellation-recovery-id").fill(cancel_id)
            await read_page.locator("#cancellation-recovery-submit").click()
            await expect(read_page.locator("#cancellation-status")).to_have_attribute(
                "data-state", "CONFIRMED"
            )
            await expect(read_page.locator("#cancellation-actor")).to_have_text("browser-planner")
            await direct_denial(read_context, read_session, role)
            await protected_state(read_page, read_context)
        require(
            machine_state() == before_machine,
            "Cancellation tests mutated machine execution state",
        )
        require(
            not any(credential_urls),
            "Cancellation credential appeared in a request URL",
        )
    finally:
        for context in reversed(contexts):
            await context.close()
        engine.dispose()
