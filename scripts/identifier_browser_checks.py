"""Real browser identity-query round trips using actual registered MES records."""

from scripts.browser_identity_urls import work_action


async def exercise_identifier_roundtrips(
    browser, origin, submit_login, producer_password, checkpoint, require, errors, screenshot
):
    from playwright.async_api import expect

    matrix = ("identifier-A", "identifier/A", ".", "..", "identifier%2FA", "식별-é-Ａ")
    context = await browser.new_context(viewport={"width": 1440, "height": 1000})
    page = await context.new_page()
    page.set_default_timeout(15_000)
    page.on("pageerror", lambda error: errors.append(type(error).__name__))
    posts = []
    page.on(
        "request", lambda request: posts.append(request.url) if request.method == "POST" else None
    )
    try:
        await page.goto(origin, wait_until="domcontentloaded")
        session = await submit_login(
            producer_password,
            target_page=page,
            login_name="browser-planner",
            roles=("production_manager",),
        )
        await page.locator("#workspace-drafts").click()
        for index, identity in enumerate(matrix):
            checkpoint("identity query browser round trip " + str(index + 1) + "/6")

            async def lose_registration(route):
                received = await route.fetch()
                require(received.status == 200, "Identity fixture UI registration did not commit")
                await route.abort("failed")

            await page.route("**/api/v1/recipe-versions", lose_registration)
            try:
                for field, value in {
                    "id": identity,
                    "family": "identity-family-" + str(index),
                    "version": "1",
                    "item": "browser-item-A",
                    "temperature": "150",
                    "speed": "6",
                }.items():
                    await page.locator("#mes-recipe-" + field).fill(value)
                await page.locator("#mes-recipe-submit").click()
                await expect(page.locator("#mes-recipe-status")).to_have_attribute(
                    "data-state", "uncertain"
                )
            finally:
                await page.unroute("**/api/v1/recipe-versions", lose_registration)
            await page.locator("#mes-recipe-lookup").click()
            await expect(page.locator("#mes-recipe-status")).to_have_attribute(
                "data-state", "confirmed"
            )
            await expect(page.locator("#mes-recipe-original-id")).to_have_text(identity)
            await expect(page.locator("#mes-recipe-list")).to_be_visible()
            select = page.get_by_role(
                "button", name=identity + " 공정 설정을 작업지시에 사용", exact=True
            )
            for _ in range(4):
                if await select.count():
                    break
                await page.locator("#mes-recipes-next").click()
                await expect(page.locator("#mes-recipe-list")).to_be_visible()
            await select.click()
            await page.route("**/api/v1/work-orders", lose_registration)
            try:
                for field, value in {"id": identity, "target": "4", "date": "2026-10-25"}.items():
                    await page.locator("#mes-order-" + field).fill(value)
                await page.locator("#mes-order-submit").click()
                await expect(page.locator("#mes-order-status")).to_have_attribute(
                    "data-state", "uncertain"
                )
            finally:
                await page.unroute("**/api/v1/work-orders", lose_registration)
            await page.locator("#mes-order-lookup").click()
            await expect(page.locator("#mes-order-status")).to_have_attribute(
                "data-state", "confirmed"
            )
            await expect(page.locator("#mes-order-original-id")).to_have_text(identity)
            await expect(page.locator("#mes-order-list")).to_be_visible()
            candidate = page.locator('[data-cancel-order="' + identity + '"]')
            for _ in range(4):
                if await candidate.count():
                    break
                await page.locator("#mes-orders-next").click()
                await expect(page.locator("#mes-order-list")).to_be_visible()
            await candidate.click()
            await page.locator("#cancellation-submit").click()
            await expect(page.locator("#cancellation-status")).to_have_attribute(
                "data-state", "CONFIRMED"
            )
            await expect(page.locator("#cancellation-order")).to_have_text(identity)
            await page.locator("#cancellation-acknowledge").click()
            # JSON can already contain every valid original ID. Recover it through
            # the same UI aliases, without asking the browser to POST another intent.
            protected = {"Origin": origin, "X-CSRF-Token": session["csrf_token"]}
            for kind in ("cancel", "release"):
                target = "identity-original-" + kind + "-" + str(index)
                created = await context.request.post(
                    origin + "/api/v1/work-orders",
                    data={
                        "id": target,
                        "item_id": "browser-item-A",
                        "line_id": "packaging-line-1",
                        "recipe_version_id": identity,
                        "target_quantity": 4,
                        "planned_date": "2026-10-25",
                    },
                    headers=protected,
                )
                require(created.status == 200, "Original identity candidate registration failed")
                body = {
                    "expected_row_version": "1",
                    "cancellation_id" if kind == "cancel" else "release_id": identity,
                }
                stored = await context.request.post(
                    work_action(origin, kind, target), data=body, headers=protected
                )
                require(stored.status == 200, "Original identity receipt did not commit")
                before = len(posts)
                if kind == "cancel":
                    await page.locator("#cancellation-recovery-order").fill(target)
                    await page.locator("#cancellation-recovery-id").fill(identity)
                    await page.locator("#cancellation-recovery-submit").click()
                    await expect(page.locator("#cancellation-status")).to_have_attribute(
                        "data-state", "CONFIRMED"
                    )
                    await expect(page.locator("#cancellation-original-id")).to_have_text(identity)
                    await page.locator("#cancellation-acknowledge").click()
                else:
                    await page.locator("#workflow-recovery-kind").select_option("release")
                    await page.locator("#workflow-recovery-order").fill(target)
                    await page.locator("#workflow-recovery-id").fill(identity)
                    await page.locator("#workflow-recovery-submit").click()
                    await expect(page.locator("#workflow-status")).to_have_attribute(
                        "data-state", "confirmed"
                    )
                    await expect(page.locator("#workflow-original-id")).to_have_text(identity)
                    await page.locator("#workflow-acknowledge").click()
                require(len(posts) == before, "Identity passive recovery generated another POST")
            await page.locator("#mes-recipe-reset").click()
            await page.locator("#mes-order-reset").click()
        await page.locator("#cancellation-history").evaluate("node => { node.open = true }")
        await page.locator("#cancellation-panel").screenshot(
            path=str(screenshot.with_stem(screenshot.stem + "-identifiers"))
        )
        require(
            await page.evaluate(
                "localStorage.length === 0 && sessionStorage.length === 0 && document.cookie === ''"
            ),
            "Identity UI persisted credentials or workflow records",
        )
    finally:
        await context.close()
