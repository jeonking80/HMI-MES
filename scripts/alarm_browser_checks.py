"""Actual PostgreSQL AlarmStore fixtures + browser alarm-center checks.

This helper exercises the UI/HTTP/store boundary. Trusted qualified conditions
are deliberate fixtures; these checks do not claim real OPC alarm acquisition.
The parent launcher owns the disposable PostgreSQL, real CLI and browser lifecycle.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4


def _fixture(url, origin):
    from apps.api.alarm_lifecycle import AlarmEvidence, AlarmRule, AlarmScope, QualifiedCondition
    from apps.api.alarm_qualification import PressureRuleConfig, TemperatureRuleConfig
    from apps.api.alarm_store import apply_condition, install_rule
    from apps.api.auth import Principal, UserAuth
    from apps.api.bootstrap import LINE_ID
    from apps.api.database import create_database_engine

    engine = create_database_engine(url)
    password, initial = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    admin = "browser-alarm-admin"
    scope = AlarmScope(
        line_id=LINE_ID,
        source_id="browser-source",
        stream_id="browser-stream",
        asset_id="Sealer",
    )
    at = datetime.now(UTC) - timedelta(seconds=60)
    try:
        auth = UserAuth(engine, allowed_origins={origin})
        auth.bootstrap_user(admin, initial, roles={"equipment_admin"})
        challenge = auth.begin_login()
        session = auth.login(
            admin,
            initial,
            challenge_token=challenge.token,
            csrf_token=challenge.csrf_token,
            origin=origin,
        )
        auth.change_password(session.token, password, csrf_token=session.csrf_token, origin=origin)
        principal = Principal(admin, frozenset({"equipment_admin"}), False)
        pressure_rule, temperature_rule = "browser-ui-pressure", "browser-ui-temperature"
        with engine.begin() as db:
            install_rule(
                db,
                scope,
                PressureRuleConfig(
                    rule=AlarmRule(rule_id=pressure_rule, version="v1", severity="HIGH")
                ),
                input_id="browser-ui-install-pressure",
                principal=principal,
                at=at,
            )
            install_rule(
                db,
                scope,
                TemperatureRuleConfig(
                    rule=AlarmRule(rule_id=temperature_rule, version="v1", severity="WARNING"),
                    setpoint_c=150.0,
                ),
                input_id="browser-ui-install-temperature",
                principal=principal,
                at=at,
            )
            for rule_id, position, condition, alarm_id in (
                (pressure_rule, 1, "ACTIVE", "browser-ui-old"),
                (pressure_rule, 2, "NORMAL", None),
                (pressure_rule, 3, "ACTIVE", "browser-ui-new"),
                (temperature_rule, 1, "ACTIVE", "browser-ui-temperature"),
            ):
                identity = f"{rule_id}-{position}"
                apply_condition(
                    db,
                    rule_id,
                    QualifiedCondition(
                        event_id=identity,
                        scope=scope,
                        rule_version="v1",
                        position=position,
                        condition=condition,
                        new_alarm_id=alarm_id,
                        evidence=AlarmEvidence(
                            observed_at=at + timedelta(seconds=position * 5),
                            references=(identity,),
                        ),
                    ),
                )
        return scope.model_dump(), admin, password, initial
    finally:
        engine.dispose()


async def exercise_alarm_center(
    browser,
    origin,
    url,
    submit_login,
    operator_password,
    viewer_password,
    checkpoint,
    require,
    sensitive,
    errors,
    screenshot: Path | None = None,
):
    """A bounded actual browser slice; never synthesizes an alarm-instance SQL row."""
    from playwright.async_api import expect

    scope, admin, admin_password, admin_initial = _fixture(url, origin)
    sensitive.extend((admin_password, admin_initial))
    context = await browser.new_context(viewport={"width": 1440, "height": 1000})
    posts = []
    lost_receipts = []
    try:
        page = await context.new_page()
        page.set_default_timeout(15_000)
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        await page.goto(origin, wait_until="domcontentloaded")
        await submit_login(operator_password, target_page=page)
        await page.locator(".alarm-center").wait_for(state="visible")
        checkpoint("alarm center actual stored fixtures, severity filter and unknown coverage")
        await expect(page.locator(".alarm-list [data-alarm-id]")).to_have_count(3)
        await expect(page.locator(".alarm-center")).to_contain_text("수집 실행 상태 불명")
        await page.get_by_label("심각도 코드 (정확히 일치)").fill("HIGH")
        await page.get_by_role("button", name="필터 적용", exact=True).click()
        await expect(page.locator(".alarm-list [data-alarm-id]")).to_have_count(2)
        await expect(page.locator('[data-alarm-id="browser-ui-temperature"]')).to_have_count(0)
        await page.get_by_label("심각도 코드 (정확히 일치)").fill("NO_SUCH_SEVERITY")
        await page.get_by_role("button", name="필터 적용", exact=True).click()
        await expect(page.locator("#alarm-empty")).to_contain_text("조회된 알람 없음")
        await expect(page.locator(".alarm-center")).to_contain_text(
            "현재 정상 또는 전체 알람 감시를 보장하지 않습니다"
        )
        await page.get_by_label("심각도 코드 (정확히 일치)").fill("HIGH")
        await page.get_by_role("button", name="필터 적용", exact=True).click()
        await page.locator('[data-alarm-id="browser-ui-old"] button').click()
        await expect(page.locator(".alarm-detail")).to_contain_text("복귀 미확인")

        loss_kind = ["ACKNOWLEDGE"]

        async def lose_after_commit(route):
            if route.request.method != "POST":
                await route.continue_()
                return
            intent = route.request.post_data_json
            posts.append(intent)
            response = await route.fetch()
            require(response.status == 200, "Alarm browser mutation did not persist successfully")
            if intent["kind"] in loss_kind:
                loss_kind.remove(intent["kind"])
                lost_receipts.append(await response.json())
                await route.abort("failed")
            else:
                await route.fulfill(response=response)

        await page.route("**/api/v1/alarm-actions", lose_after_commit)
        checkpoint("old returned alarm ACK response loss keeps one original immutable action")
        await page.locator("#alarm-acknowledge").click()
        await expect(page.locator("#alarm-action-phase")).to_have_attribute("data-phase", "UNKNOWN")
        await expect(page.locator("#alarm-recover")).to_be_enabled()
        original_id = await page.locator("#alarm-original-id").inner_text()
        require(len(posts) == 1, "Alarm ACK generated more than one POST")
        require(posts[0]["action_id"] == original_id, "Alarm ACK lost original identity")

        async def missing_receipt(route):
            await route.fulfill(
                status=404, content_type="application/json", body='{"detail":"not found"}'
            )

        await page.route("**/api/v1/alarm-action-receipts?*", missing_receipt)
        await page.locator("#alarm-recover").click()
        await expect(page.locator(".alarm-ledger")).to_contain_text() if False else None
        await expect(page.locator(".alarm-ledger")).to_contain_text(
            "저장되지 않았다고 단정할 수 없습니다"
        )
        await expect(page.locator("#alarm-action-phase")).to_have_attribute("data-phase", "UNKNOWN")
        await expect(page.locator("#alarm-original-id")).to_have_text(original_id)
        await expect(page.locator("#alarm-acknowledge")).to_be_disabled()
        require(len(posts) == 1, "Missing receipt caused an automatic alarm resubmission")
        await page.unroute("**/api/v1/alarm-action-receipts?*", missing_receipt)

        checkpoint(
            "alarm ledger survives logout and passive viewer recovery preserves original actor"
        )
        await page.get_by_role("button", name="로그아웃", exact=True).click()
        await page.locator("#login-form").wait_for(state="visible")
        viewer = await submit_login(
            viewer_password, target_page=page, login_name="browser-viewer", roles=("viewer",)
        )
        await expect(page.locator("#alarm-original-id")).to_have_text(original_id)
        await expect(page.locator(".alarm-ledger")).to_contain_text("browser-operator")
        await page.locator("#alarm-recover").click()
        await expect(page.locator("#alarm-action-phase")).to_have_attribute(
            "data-phase", "CONFIRMED"
        )
        require(len(posts) == 1, "Passive relogin recovery posted another alarm action")
        from urllib.parse import urlencode

        query = urlencode(scope | {"rule_id": "browser-ui-pressure"})
        recovered = await context.request.get(
            origin + f"/api/v1/alarms/browser-ui-old/action-receipts/{original_id}?{query}"
        )
        require(
            recovered.status == 200
            and (await recovered.json())["intent"] == posts[0]
            and (await recovered.json())["recorded_at"] == lost_receipts[0]["recorded_at"],
            "Alarm recovery changed original intent or timestamp",
        )
        for identity, expected in (
            ("browser-ui-old", "RETURNED_ACKNOWLEDGED"),
            ("browser-ui-new", "ACTIVE_UNACKNOWLEDGED"),
        ):
            response = await context.request.get(origin + f"/api/v1/alarms/{identity}?{query}")
            require(
                response.status == 200 and (await response.json())["status"] == expected,
                "Old-instance acknowledgement affected the new active instance",
            )
        await page.get_by_role("button", name="기록 확인 · 다음 조치 준비", exact=True).click()
        await page.locator('[data-alarm-id="browser-ui-new"] button').click()
        await expect(page.locator("#alarm-acknowledge, .alarm-note")).to_have_count(0)
        denied_intent = posts[0] | {"action_id": str(uuid4()), "actor": "browser-viewer"}
        denial = await context.request.post(
            origin + "/api/v1/alarms/browser-ui-old/actions",
            data=denied_intent,
            headers={"Origin": origin, "X-CSRF-Token": viewer["csrf_token"]},
        )
        require(denial.status == 403, "Viewer posted an alarm action through the actual API")

        await page.get_by_role("button", name="로그아웃", exact=True).click()
        await page.locator("#login-form").wait_for(state="visible")
        await submit_login(operator_password, target_page=page)
        await page.locator('[data-alarm-id="browser-ui-new"] button').click()
        checkpoint("alarm note add/edit keeps exact parent and before/after through response loss")
        await page.locator("#alarm-note").fill("browser initial note")
        await page.get_by_role("button", name="조치 메모 추가 요청", exact=True).click()
        await expect(page.locator("#alarm-action-phase")).to_have_attribute(
            "data-phase", "CONFIRMED"
        )
        await page.get_by_role("button", name="기록 확인 · 다음 조치 준비", exact=True).click()
        await page.get_by_role(
            "button", name="이 기록을 기준으로 메모 수정", exact=True
        ).first.click()
        await page.locator("#alarm-note").fill("browser corrected note")
        loss_kind.append("NOTE_EDIT")
        await page.get_by_role("button", name="메모 수정 기록 요청", exact=True).click()
        await expect(page.locator("#alarm-action-phase")).to_have_attribute("data-phase", "UNKNOWN")
        await expect(page.locator("#alarm-recover")).to_be_enabled()
        edit_id = await page.locator("#alarm-original-id").inner_text()
        await page.locator("#alarm-recover").click()
        await expect(page.locator("#alarm-action-phase")).to_have_attribute(
            "data-phase", "CONFIRMED"
        )
        require(
            len(posts) == 3
            and posts[2]["kind"] == "NOTE_EDIT"
            and posts[2]["action_id"] == edit_id
            and posts[2]["note_id"] == posts[1]["note_id"]
            and posts[2]["expected_previous_action_id"] == posts[1]["action_id"],
            "Alarm note recovery changed note identity, parent or original action ID",
        )
        await expect(page.locator(".alarm-history")).to_contain_text(
            "수정 전: browser initial note"
        )
        await expect(page.locator(".alarm-history")).to_contain_text(
            "조치 내용: browser corrected note"
        )
        await page.get_by_role("button", name="기록 확인 · 다음 조치 준비", exact=True).click()
        await page.locator("#alarm-acknowledge").click()
        await expect(page.locator("#alarm-action-phase")).to_have_attribute(
            "data-phase", "CONFIRMED"
        )
        await expect(page.locator('[data-alarm-id="browser-ui-new"]')).to_have_attribute(
            "data-alarm-status", "ACTIVE_ACKNOWLEDGED"
        )
        await expect(page.locator(".alarm-detail")).to_contain_text("발생 확인")
        await page.get_by_role("button", name="기록 확인 · 다음 조치 준비", exact=True).click()
        await page.set_viewport_size({"width": 360, "height": 780})
        require(
            await page.evaluate("document.documentElement.scrollWidth <= innerWidth"),
            "Alarm center caused mobile horizontal page overflow",
        )
        await page.set_viewport_size({"width": 1440, "height": 1000})

        artifact = screenshot or Path(__file__).resolve().parents[1] / ".local/qa/app-a-alarm.png"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        await page.locator(".alarm-center").screenshot(path=str(artifact))

        checkpoint("equipment-admin alone reads alarm history but cannot acknowledge or add notes")
        await page.get_by_role("button", name="로그아웃", exact=True).click()
        await page.locator("#login-form").wait_for(state="visible")
        admin_session = await submit_login(
            admin_password, target_page=page, login_name=admin, roles=("equipment_admin",)
        )
        await page.locator('[data-alarm-id="browser-ui-new"] button').click()
        await expect(page.locator("#alarm-acknowledge, .alarm-note")).to_have_count(0)
        denied_intent = posts[0] | {"action_id": str(uuid4()), "actor": admin}
        denial = await context.request.post(
            origin + "/api/v1/alarms/browser-ui-old/actions",
            data=denied_intent,
            headers={"Origin": origin, "X-CSRF-Token": admin_session["csrf_token"]},
        )
        require(denial.status == 403, "Equipment-admin inherited operator alarm-action permission")
        require(
            await page.evaluate(
                "localStorage.length === 0 && sessionStorage.length === 0 && document.cookie === ''"
            ),
            "Alarm UI persisted readable authentication or intent storage",
        )
    finally:
        await context.close()
