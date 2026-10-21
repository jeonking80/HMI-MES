"""STAGED ONLY: actual UserAuth/Ingestion/fault-store/browser boundary.

This helper has not run. It is not a real OPC acquisition or whole T05 claim.
The established parent launcher owns the disposable DB, real serve-user CLI,
initial operator password rotation, Chromium and credential-safe cleanup.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlparse


async def _fixture(url, origin, sensitive, require):
    from sqlalchemy import text

    from apps.api.alarm_consumer import ConfiguredClockPolicy
    from apps.api.alarm_coordinator import MixedAlarmChannel, MixedAlarmConsumerConfig
    from apps.api.alarm_lifecycle import AlarmRule, AlarmScope
    from apps.api.alarm_qualification import PressureRuleConfig
    from apps.api.alarm_rules import PLCFaultRuleConfig
    from apps.api.alarm_runtime import (
        MixedRuleOperation,
        MixedRuleProvisioningConfig,
        configure_alarm_rules,
        start_alarm_runtime,
    )
    from apps.api.auth import UserAuth
    from apps.api.bootstrap import LINE_ID
    from apps.api.database import create_database_engine
    from apps.api.ingestion import IngestionService
    from contracts.models import FaultEvent, Parameters, StateEvent
    from contracts.tags.validate import load_contract
    from contracts.transport import BatchBody, GatewayBatch, batch_digest

    engine = create_database_engine(url)
    admin, initial, password = (
        "browser-line-admin",
        secrets.token_urlsafe(32),
        secrets.token_urlsafe(32),
    )
    sensitive.extend((initial, password))
    source_id, stream_id, gateway_id = (
        "zz-browser-line-alarm-source",
        "browser-line-alarm-stream",
        "browser-line-alarm-gateway",
    )
    scope = AlarmScope(line_id=LINE_ID, source_id=source_id, stream_id=stream_id, asset_id="Line")
    execution = "browser-line-alarm-execution"
    source = dict(
        gateway_id=gateway_id, source_id=source_id, stream_id=stream_id, capture_generation=1
    )
    auth = UserAuth(engine, allowed_origins={origin})
    session = None
    try:
        service = IngestionService(engine)
        service.configure_source(**source, line_id=LINE_ID, now=datetime.now(UTC))
        service.register_execution(
            execution_id=execution,
            source_id=source_id,
            stream_id=stream_id,
            work_order_id="browser-line-alarm-legacy-order",
            recipe_version="browser-line-alarm-recipe",
            parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
            counter_epoch="browser-line-alarm-counter",
            target_quantity=1,
            now=datetime.now(UTC),
        )
        auth.bootstrap_user(admin, initial, roles={"equipment_admin"})
        challenge = auth.begin_login()
        session = auth.login(
            admin,
            initial,
            challenge_token=challenge.token,
            csrf_token=challenge.csrf_token,
            origin=origin,
        )
        session = auth.change_password(
            session.token, password, csrf_token=session.csrf_token, origin=origin
        )
        auth.revoke(session.token, csrf_token=session.csrf_token, origin=origin)
        session = None
        fault = PLCFaultRuleConfig(
            rule=AlarmRule(rule_id="browser-line-fault", version="f1", severity="HIGH")
        )
        pressure = PressureRuleConfig(
            rule=AlarmRule(rule_id="browser-line-pressure", version="p1", severity="WARNING")
        )
        provision = MixedRuleProvisioningConfig(
            kind="MIXED_ALARM_RULES",
            line_id=LINE_ID,
            **source,
            operations=(
                MixedRuleOperation(
                    asset_id="Line",
                    operation_id="browser-line-install-fault",
                    expected_version=None,
                    config=fault,
                ),
                MixedRuleOperation(
                    asset_id="Sealer",
                    operation_id="browser-line-install-pressure",
                    expected_version=None,
                    config=pressure,
                ),
            ),
        )
        configure_alarm_rules(auth, login=admin, password=password, origin=origin, config=provision)
        at = datetime.now(UTC)
        policy = ConfiguredClockPolicy(
            policy_id="browser-same-process-utc",
            policy_version="v1",
            reference_id="browser-fixture-kernel",
            method="CONFIGURED_API_REFERENCE",
            guarantee_kind="SAME_HOST_SHARED_KERNEL_UTC",
            guarantee_basis=(
                "Controlled captures in the same host process; not an OPC clock verification"
            ),
            **source,
            clock_epoch="browser-line-capture-epoch",
            valid_from_api_utc=at - timedelta(minutes=1),
            valid_until_api_utc=at + timedelta(minutes=20),
            offset_lower_seconds=-0.01,
            offset_upper_seconds=0.01,
        )
        runtime_config = MixedAlarmConsumerConfig(
            kind="MIXED_ALARM_RUNTIME",
            line_id=LINE_ID,
            **source,
            channels=(
                MixedAlarmChannel(asset_id="Line", config=fault),
                MixedAlarmChannel(asset_id="Sealer", config=pressure),
            ),
            clock_policy=policy,
        )
        runtime = start_alarm_runtime(engine, runtime_config, args=SimpleNamespace(**source))
        service = IngestionService(engine, alarm_consumer=runtime)
        tags = load_contract()["tags"]
        capture_seq = 0

        async def emit(*, faulted=False, raw_code=None):
            nonlocal capture_seq
            capture_seq += 1
            captured = datetime.now(UTC)
            running = not faulted
            code = (123 if faulted else 0) if raw_code is None else raw_code
            ceiling = 3 if faulted else 1
            values = {
                "Line.State": 5 if faulted else 2,
                "Sealer.State": 0 if faulted else 2,
                "Line.OperationMode": 1,
                "Line.ControlMode": 1,
                "Line.ControlVersion": 2 if faulted else 1,
                "Line.EventSeq": ceiling,
                "Line.FaultCode": code,
                "Line.ActiveExecutionId": execution,
                "Line.CounterEpoch": "browser-line-alarm-counter",
                "Line.RecipeVersion": "browser-line-alarm-recipe",
                "Sealer.RunFeedback": running,
                "Sealer.AirPressure": 3.0,
            }
            samples = []
            for tag in tags:
                name, kind = tag["logical_name"], tag["data_type"]
                default = (
                    False
                    if kind == "Boolean"
                    else ""
                    if kind == "String"
                    else 0.0
                    if kind == "Double"
                    else 0
                )
                samples.append(
                    dict(
                        sample_id=f"browser-line-sample-{capture_seq}-{name}",
                        tag_id=name,
                        source_timestamp=captured,
                        server_timestamp=captured,
                        acquired_at=captured,
                        status_code=0,
                        reading={"data_type": kind, "value": values.get(name, default)},
                    )
                )
            common = dict(
                source_id=source_id,
                stream_id=stream_id,
                equipment_id="Line",
                execution_id=execution,
                occurred_at=captured,
            )
            events = ()
            if capture_seq == 1:
                events = (
                    StateEvent(
                        kind="STATE_CHANGED",
                        event_id="browser-line-state-running",
                        seq=1,
                        payload={
                            "previous_state": "READY",
                            "state": "RUNNING",
                            "control_version": 1,
                        },
                        **common,
                    ),
                )
            elif capture_seq == 6:
                events = (
                    StateEvent(
                        kind="STATE_CHANGED",
                        event_id="browser-line-state-fault",
                        seq=2,
                        payload={
                            "previous_state": "RUNNING",
                            "state": "FAULT",
                            "control_version": 2,
                        },
                        **common,
                    ),
                    FaultEvent(
                        kind="FAULT_CHANGED",
                        event_id="browser-line-fault-active",
                        seq=3,
                        payload={"fault_code": 123, "active": True},
                        **common,
                    ),
                )
            body = BatchBody(
                schema_version="1.2",
                batch_id=f"browser-line-batch-{capture_seq}",
                **source,
                capture_seq=capture_seq,
                captured_at=captured,
                samples=samples,
                events=events,
                capture_time={
                    "clock_epoch": "browser-line-capture-epoch",
                    "monotonic_seconds": time.monotonic(),
                    "utc_read_span_seconds": 0.001,
                },
                communication={
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
                },
            )
            await asyncio.sleep(0.05)  # Real receive age clears the declared read-span interval.
            service.ingest(
                GatewayBatch(body=body, content_sha256=batch_digest(body)),
                authenticated_gateway_id=gateway_id,
                authenticated_source_id=source_id,
                authenticated_stream_id=stream_id,
                now=datetime.now(UTC),
            )

        await asyncio.sleep(0.05)  # First capture interval follows committed installed-rule time.
        for number in range(5):
            if number:
                await asyncio.sleep(1.0)
            await emit()
        await emit(faulted=True)
        # An original code0 sample without a source RESET pair must not invent a return.
        await emit(faulted=True, raw_code=0)
        with engine.connect() as db:
            instances = (
                db.execute(
                    text(
                        "SELECT i.alarm_id,c.asset_id,i.payload FROM alarm_instances i "
                        "JOIN alarm_channels c USING(channel_id) "
                        "WHERE c.source_id=:s AND c.stream_id=:t"
                    ),
                    {"s": source_id, "t": stream_id},
                )
                .mappings()
                .all()
            )
            require(
                len(instances) == 2, "Normal capture fixture did not qualify both actual channels"
            )
            identities = {row["asset_id"]: row["alarm_id"] for row in instances}
            require(set(identities) == {"Line", "Sealer"}, "Fault fixture target identities differ")
            decisions = db.execute(
                text(
                    "SELECT count(*) FROM alarm_capture_decisions "
                    "WHERE source_id=:s AND stream_id=:t"
                ),
                {"s": source_id, "t": stream_id},
            ).scalar_one()
            require(
                decisions == 14, "Mixed capture processing did not commit both channels atomically"
            )
        return SimpleNamespace(
            engine=engine,
            scope=scope,
            source=source,
            ids=identities,
            admin=admin,
            password=password,
        )
    except BaseException:
        engine.dispose()
        raise
    finally:
        if session is not None:
            auth.revoke(session.token, csrf_token=session.csrf_token, origin=origin)


def _records(fixture):
    """Read-only assertions; never seed or repair production/alarm rows with SQL."""
    from sqlalchemy import text

    with fixture.engine.connect() as db:
        return {
            "actions": [
                dict(row)
                for row in db.execute(
                    text(
                        "SELECT a.* FROM alarm_actions a JOIN alarm_channels c USING(channel_id) "
                        "WHERE c.source_id=:s AND c.stream_id=:t ORDER BY a.action_id"
                    ),
                    {"s": fixture.scope.source_id, "t": fixture.scope.stream_id},
                ).mappings()
            ],
            "mechanical": tuple(
                db.execute(
                    text(
                        "SELECT (SELECT count(*) FROM command_requests),"
                        "(SELECT count(*) FROM event_inbox), "
                        "(SELECT count(*) FROM production_events)"
                    )
                ).one()
            ),
        }


async def exercise_line_alarm_center(
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
    """Bounded browser extension; caller keeps established launch/auth/cleanup boundaries."""
    from playwright.async_api import expect

    fixture = await _fixture(url, origin, sensitive, require)
    context = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="ko-KR")
    posts, gets = [], []
    pending = []
    try:
        page = await context.new_page()
        page.set_default_timeout(15000)
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        page.on(
            "request",
            lambda request: require(
                not any(value in request.url for value in sensitive),
                "Credential appeared in Line browser URL",
            ),
        )
        page.on(
            "request",
            lambda request: (
                gets.append(request.url)
                if request.method == "GET" and "/api/v1/alarm" in request.url
                else None
            ),
        )
        await page.add_init_script("""window.__lineAlarmCspViolations = 0;
            document.addEventListener('securitypolicyviolation', () => {
                window.__lineAlarmCspViolations += 1;
            });""")
        await page.goto(origin, wait_until="domcontentloaded")
        login_data = await submit_login(operator_password, target_page=page)
        for cookie in await context.cookies():
            if cookie["name"] in {"hmi_session", "hmi_preauth"}:
                require(
                    cookie["httpOnly"] and cookie["sameSite"] == "Strict",
                    "Line browser cookie protections missing",
                )
                sensitive.append(cookie["value"])
        await page.locator("#source-selector").select_option(
            label=await page.locator("#source-selector option")
            .filter(has_text=fixture.scope.source_id)
            .inner_text()
        )
        await expect(page.locator(".alarm-list [data-alarm-id]")).to_have_count(1)
        await expect(page.locator(".alarm-list")).to_contain_text(fixture.ids["Sealer"])
        await page.get_by_label("알람 대상", exact=True).select_option("Line")
        await expect(page.locator(".alarm-list")).to_contain_text(fixture.ids["Line"])
        await expect(page.locator(".alarm-center")).to_contain_text("수집 실행 상태 불명")
        await expect(page.locator(".alarm-list")).to_contain_text("원래 발생 고장 코드 123")
        await page.locator(f'[data-alarm-id="{fixture.ids["Line"]}"] button').click()
        await expect(page.locator(".alarm-fault-code")).to_contain_text("123")
        await expect(page.locator(".alarm-detail")).to_contain_text("복귀 기록 없음")

        checkpoint("Line/Sealer late original GETs cannot overwrite the selected scope")
        for delayed, selected in (("Line", "Sealer"), ("Sealer", "Line")):
            intercepted, release, done = asyncio.Event(), asyncio.Event(), asyncio.Event()
            pending.append(release)
            received, completed, routed, matched, fetch_errors = [], [], [], [], []
            await page.locator(f'[data-alarm-id="{fixture.ids[delayed]}"] button').click()
            await expect(page.locator(".alarm-detail .alarm-facts")).to_have_count(1)

            async def delay(
                route,
                _request,
                *,
                delayed=delayed,
                intercepted=intercepted,
                release=release,
                done=done,
                received=received,
                completed=completed,
                routed=routed,
                matched=matched,
                fetch_errors=fetch_errors,
            ):
                routed.append(True)
                query = parse_qs(urlparse(route.request.url).query)
                if query.get("asset_id") != [delayed]:
                    await route.continue_()
                    return
                matched.append(True)
                try:
                    response = await route.fetch()
                except Exception as error:
                    fetch_errors.append(type(error).__name__)
                    intercepted.set()
                    return
                require(response.status == 200, "Delayed original scope GET did not succeed")
                received.append(route.request.url)
                if len(received) == 2:
                    intercepted.set()
                try:
                    await release.wait()
                    await route.fulfill(response=response)
                except Exception:
                    # Aborting an old useEffect fetch is the expected lifecycle too.
                    pass
                finally:
                    completed.append(True)
                    if len(completed) == 2:
                        done.set()

            await page.route("**/api/v1/alarms?*", delay)
            await page.route("**/api/v1/alarm-instance?*", delay)
            await page.get_by_role("button", name="기록 새로 조회", exact=True).click()
            try:
                await asyncio.wait_for(intercepted.wait(), 5)
            except TimeoutError:
                require(
                    False,
                    "Late GET counts routed/matched/fetched/errors: "
                    + str((len(routed), len(matched), len(received), len(fetch_errors))),
                )
            require(not fetch_errors, "Late GET upstream fetch failed: " + ",".join(fetch_errors))
            checkpoint("Held original list/detail; switching to " + selected)
            await page.get_by_label("알람 대상", exact=True).select_option(selected)
            await expect(page.locator(".alarm-list")).to_contain_text(fixture.ids[selected])
            release.set()
            try:
                await asyncio.wait_for(done.wait(), 5)
            except TimeoutError:
                require(
                    False,
                    "Late GET harness did not finish original responses: " + str(len(completed)),
                )
            checkpoint("Released original list/detail; selected scope stays " + selected)
            await page.unroute("**/api/v1/alarms?*", delay)
            await page.unroute("**/api/v1/alarm-instance?*", delay)
            await expect(page.locator(".alarm-list [data-alarm-id]")).to_have_count(1)
            await expect(page.locator(".alarm-list")).to_contain_text(fixture.ids[selected])
            require(
                await page.locator(".alarm-detail").count() == 0, "Scope switch retained old detail"
            )
        await page.locator(f'[data-alarm-id="{fixture.ids["Line"]}"] button').click()
        await expect(page.locator("#alarm-acknowledge")).to_be_enabled()
        baseline = _records(fixture)
        loss_kinds = ["ACKNOWLEDGE", "NOTE_ADD", "NOTE_EDIT"]

        async def lose_committed(route):
            if route.request.method != "POST":
                await route.continue_()
                return
            intent = route.request.post_data_json
            posts.append(intent)
            response = await route.fetch()
            require(response.status == 200, "Actual Line action was not committed")
            if intent["kind"] in loss_kinds:
                loss_kinds.remove(intent["kind"])
                await route.abort("failed")
            else:
                await route.fulfill(response=response)

        await page.route("**/api/v1/alarm-actions", lose_committed)
        checkpoint("One actual ACK survives lost response and passive recovery from Sealer scope")
        await page.locator("#alarm-acknowledge").click()
        await expect(page.locator("#alarm-action-phase")).to_have_attribute("data-phase", "UNKNOWN")
        original = await page.locator("#alarm-original-id").inner_text()
        require(
            len(posts) == 1 and posts[0]["action_id"] == original,
            "Line ACK changed or repeated its ID",
        )
        await page.get_by_label("알람 대상", exact=True).select_option("Sealer")
        await expect(page.locator(".alarm-list")).to_contain_text(fixture.ids["Sealer"])
        await expect(page.locator(".alarm-ledger")).to_contain_text("Line")
        await page.locator(f'[data-alarm-id="{fixture.ids["Sealer"]}"] button').click()
        await expect(page.locator("#alarm-acknowledge")).to_be_disabled()

        async def missing(route):
            await route.fulfill(
                status=404, content_type="application/json", body='{"detail":"not found"}'
            )

        await page.route("**/api/v1/alarm-action-receipts?*", missing)
        await page.locator("#alarm-recover").click()
        await expect(page.locator(".alarm-ledger")).to_contain_text(
            "현재 원래 ID의 기록을 찾지 못했습니다"
        )
        await expect(page.locator("#alarm-action-phase")).to_have_attribute("data-phase", "UNKNOWN")
        await page.unroute("**/api/v1/alarm-action-receipts?*", missing)
        await page.locator("#alarm-recover").click()
        await expect(page.locator("#alarm-action-phase")).to_have_attribute(
            "data-phase", "CONFIRMED"
        )
        require(len(posts) == 1, "Cross-scope passive recovery created another action POST")
        recoveries = [
            parse_qs(urlparse(value).query) for value in gets if "/alarm-action-receipts?" in value
        ]
        require(
            bool(recoveries)
            and all(
                all(q.get(key) == [str(value)] for key, value in fixture.scope.model_dump().items())
                and q.get("action_id") == [original]
                and q.get("alarm_id") == [fixture.ids["Line"]]
                and q.get("rule_id") == ["browser-line-fault"]
                for q in recoveries
            ),
            "Recovery used the selected scope instead of original Line identity",
        )
        require(
            _records(fixture)["mechanical"] == baseline["mechanical"],
            "ACK affected command or production evidence",
        )
        await page.get_by_role("button", name="기록 확인 · 다음 조치 준비", exact=True).click()
        await page.get_by_label("알람 대상", exact=True).select_option("Line")
        await page.locator(f'[data-alarm-id="{fixture.ids["Line"]}"] button').click()
        await expect(page.locator(".alarm-detail")).to_contain_text("복귀 기록 없음")
        for editing in (False, True):
            if editing:
                await page.get_by_role(
                    "button", name="이 기록을 기준으로 메모 수정", exact=True
                ).click()
            await page.locator("#alarm-note").fill(
                "Line note corrected" if editing else "Line original note"
            )
            await page.locator(".alarm-note button[type=submit]").click()
            await expect(page.locator("#alarm-action-phase")).to_have_attribute(
                "data-phase", "UNKNOWN"
            )
            expected_id = await page.locator("#alarm-original-id").inner_text()
            await page.get_by_label("알람 대상", exact=True).select_option("Sealer")
            await page.locator("#alarm-recover").click()
            await expect(page.locator("#alarm-action-phase")).to_have_attribute(
                "data-phase", "CONFIRMED"
            )
            require(posts[-1]["action_id"] == expected_id, "Note recovery replaced original ID")
            await page.get_by_role("button", name="기록 확인 · 다음 조치 준비", exact=True).click()
            await page.get_by_label("알람 대상", exact=True).select_option("Line")
            await page.locator(f'[data-alarm-id="{fixture.ids["Line"]}"] button').click()
        require(
            len(posts) == 3, "ACK and two explicit note actions did not submit exactly once each"
        )
        require(
            posts[2]["note_id"] == posts[1]["note_id"]
            and posts[2]["expected_previous_action_id"] == posts[1]["action_id"],
            "Note edit lost original parent",
        )
        await expect(page.locator(".alarm-history")).to_contain_text("Line note corrected")
        await expect(page.locator(".alarm-history")).to_contain_text("Line original note")
        await expect(page.locator(".alarm-fault-code")).to_contain_text("123")
        await expect(page.locator(".alarm-detail")).to_contain_text("복귀 기록 없음")
        stored = _records(fixture)
        require(
            len(stored["actions"]) == 3 and stored["mechanical"] == baseline["mechanical"],
            "Line actions changed mechanical evidence or duplicated records",
        )
        saved_actions = {row["action_id"]: row["payload"] for row in stored["actions"]}
        require(
            saved_actions[posts[2]["action_id"]]["previous_action_id"] == posts[1]["action_id"]
            and saved_actions[posts[2]["action_id"]]["before"] == posts[1]["text"]
            and saved_actions[posts[2]["action_id"]]["after"] == posts[2]["text"],
            "Stored Line note parent/before/after differ from original explicit requests",
        )
        if screenshot:
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(
                path=str(screenshot.with_stem(screenshot.stem + "-line-alarm")), full_page=True
            )
        await page.locator("#logout").click()
        await page.locator("#login-form").wait_for(state="visible")
        viewer = await submit_login(
            viewer_password, target_page=page, login_name="browser-viewer", roles=("viewer",)
        )
        await page.locator("#source-selector").select_option(
            label=await page.locator("#source-selector option")
            .filter(has_text=fixture.scope.source_id)
            .inner_text()
        )
        await page.get_by_label("알람 대상", exact=True).select_option("Line")
        await page.locator(f'[data-alarm-id="{fixture.ids["Line"]}"] button').click()
        require(
            await page.locator("#alarm-acknowledge,.alarm-note").count() == 0,
            "Viewer received Line action controls",
        )
        query = urlencode(
            fixture.scope.model_dump()
            | {"rule_id": "browser-line-fault", "alarm_id": fixture.ids["Line"]}
        )
        forbidden = dict(posts[0], action_id="browser-line-denied-viewer", actor="browser-viewer")
        response = await context.request.post(
            origin + "/api/v1/alarm-actions",
            data=forbidden,
            headers={"Origin": origin, "X-CSRF-Token": viewer["csrf_token"]},
        )
        require(response.status == 403, "Viewer submitted actual Line action")
        recovered = await context.request.get(
            origin + "/api/v1/alarm-action-receipts?" + query + "&action_id=" + original
        )
        require(
            recovered.status == 200
            and (await recovered.json())["intent"]["actor"] == login_data["login"],
            "Viewer passive recovery changed original operator identity",
        )
        require(_records(fixture) == stored, "Read-only viewer access modified Line records")
        await page.locator("#logout").click()
        await page.locator("#login-form").wait_for(state="visible")
        admin = await submit_login(
            fixture.password, target_page=page, login_name=fixture.admin, roles=("equipment_admin",)
        )
        await page.locator("#source-selector").select_option(
            label=await page.locator("#source-selector option")
            .filter(has_text=fixture.scope.source_id)
            .inner_text()
        )
        await page.get_by_label("알람 대상", exact=True).select_option("Line")
        await page.locator(f'[data-alarm-id="{fixture.ids["Line"]}"] button').click()
        require(
            await page.locator("#alarm-acknowledge,.alarm-note").count() == 0,
            "Equipment admin inherited operator Line action controls",
        )
        denial = await context.request.post(
            origin + "/api/v1/alarm-actions",
            data=dict(posts[0], action_id="browser-line-denied-admin", actor=fixture.admin),
            headers={"Origin": origin, "X-CSRF-Token": admin["csrf_token"]},
        )
        require(denial.status == 403, "Equipment admin inherited actual operator action grant")
        require(_records(fixture) == stored, "Read-only equipment admin changed Line records")
        require(
            await page.evaluate(
                "localStorage.length===0 && sessionStorage.length===0 "
                "&& document.cookie==='' && window.__lineAlarmCspViolations===0"
            ),
            "Line alarm UI exposed browser-readable credentials or violated CSP",
        )
    finally:
        for release in pending:
            release.set()
        await context.close()
        fixture.engine.dispose()
