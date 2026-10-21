"""Actual journal/ingestion/settlement fixtures for explicit same-session archive.

These fixtures exercise App A and do not claim PLC or OPC execution.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from scripts.browser_identity_urls import work_action


async def exercise_execution_archive(
    page,
    context,
    origin,
    url,
    session,
    original_order,
    command_id,
    ingest_capture,
    checkpoint,
    require,
    screenshot,
):
    from playwright.async_api import expect
    from sqlalchemy import text

    from apps.api.bootstrap import ASSET_IDS
    from apps.api.commands import apply_result, claim_next
    from apps.api.database import create_database_engine
    from apps.api.ingestion import IngestionService
    from apps.api.settlement import record_final_summary
    from contracts.models import CommandResult, RunSummary
    from contracts.transport import BatchBody, GatewayBatch, batch_digest

    gateway = {
        "authenticated_gateway_id": "browser-gateway",
        "authenticated_source_id": "browser-source",
        "authenticated_stream_id": "browser-stream",
    }
    engine = create_database_engine(url)
    task = None
    posts = []
    page.on(
        "request",
        lambda request: posts.append(request.url) if request.method == "POST" else None,
    )
    try:
        with engine.connect() as db:
            row = (
                db.execute(
                    text("SELECT request,result FROM command_requests WHERE command_id=:id"),
                    {"id": command_id},
                )
                .mappings()
                .one()
            )
            sequence = int(db.execute(text("SELECT max(capture_seq) FROM ingest_batches")).scalar())
        request, old_result = row["request"], row["result"]
        execution_id = request["execution_id"]
        recipe = request["command"]["args"]["recipe_version"]
        parameters = request["command"]["args"]["parameters"]
        epoch = "browser-epoch"

        def next_capture():
            nonlocal sequence
            sequence += 1
            return sequence

        def business_state():
            with engine.connect() as db:
                return (
                    tuple(
                        db.execute(
                            text(
                                "SELECT execution_id FROM line_execution_reservations "
                                "ORDER BY line_id"
                            )
                        ).scalars()
                    ),
                    db.execute(text("SELECT count(*) FROM mes_management_audit")).scalar(),
                    db.execute(text("SELECT count(*) FROM command_requests")).scalar(),
                )

        execution_url = origin + "/api/v1/execution-record"
        checkpoint("execution manual read cannot archive pending or unresolved original LOAD")
        await page.locator("#execution-refresh").click()
        await expect(page.locator('[data-settlement-status="PENDING"]')).to_be_visible()
        await expect(page.locator("#execution-archive")).to_be_disabled()
        require(old_result["status"] == "OUTCOME_UNKNOWN", "Original LOAD was not durably reserved")
        now = datetime.now(UTC)
        with engine.begin() as db:
            apply_result(
                db,
                CommandResult(
                    command_id=command_id,
                    status="SUCCEEDED",
                    updated_at=now,
                    delivered_at=datetime.fromisoformat(old_result["delivered_at"]),
                    completed_at=now,
                    applied_control_version=43,
                    evidence={
                        "state": "READY",
                        "execution_id": execution_id,
                        "recipe_version": recipe,
                    },
                ),
                **gateway,
                now=now,
            )
        template = json.loads(
            (
                Path(__file__).resolve().parents[1] / "contracts/examples/transport-batch.json"
            ).read_text()
        )["body"]["events"][0]

        def production(first, last):
            now = datetime.now(UTC)
            events = []
            for event_seq in range(first, last + 1):
                event = json.loads(json.dumps(template))
                event.update(
                    {
                        "seq": str(event_seq),
                        "event_id": f"browser-archive-{event_seq}",
                        "execution_id": execution_id,
                        "equipment_id": ASSET_IDS["Outfeed"],
                        "source_id": "browser-source",
                        "stream_id": "browser-stream",
                        "occurred_at": now,
                    }
                )
                event["payload"] = {"quantity": "1", "counter_epoch": epoch}
                events.append(event)
            body = BatchBody(
                schema_version="1.0",
                batch_id=f"browser-archive-events-{last}",
                gateway_id="browser-gateway",
                source_id="browser-source",
                stream_id="browser-stream",
                capture_generation=1,
                capture_seq=next_capture(),
                captured_at=now,
                samples=(),
                events=events,
            )
            IngestionService(engine).ingest(
                GatewayBatch(body=body, content_sha256=batch_digest(body)), **gateway, now=now
            )

        def settle(quantity, **changes):
            now = datetime.now(UTC)
            summary = RunSummary(
                **(
                    {
                        "source_id": "browser-source",
                        "stream_id": "browser-stream",
                        "execution_id": execution_id,
                        "state": "ENDED",
                        "as_of": now,
                        "ended_at": now,
                        "counter_epoch": epoch,
                        "recipe_version": recipe,
                        "parameters": parameters,
                        "input_quantity": quantity,
                        "good_quantity": quantity,
                        "reject_quantity": 0,
                        "scrap_quantity": 0,
                        "wip_quantity": 0,
                        "last_event_seq": quantity,
                        "final": True,
                    }
                    | changes
                )
            )
            with engine.begin() as db:
                return record_final_summary(db, summary, **gateway, now=now)

        production(1, 4)
        require(settle(4).status == "SETTLED", "Actual final summary failed to settle")
        await page.locator("#execution-refresh").click()
        await expect(page.locator("#execution-archive")).to_be_enabled()
        checkpoint("incoherent returned settlement/totals cannot archive a stored original record")

        async def mismatch_response(route):
            response = await route.fetch()
            payload = await response.json()
            payload["settlement"]["status"] = "MISMATCH"
            payload["settlement"]["summary"]["counter_epoch"] = "different-observed-epoch"
            await route.fulfill(status=200, json=payload)

        await page.route(execution_url + "?*", mismatch_response)
        try:
            await page.locator("#execution-refresh").click()
            await expect(page.locator('[data-settlement-status="MISMATCH"]')).to_be_visible()
            await expect(page.locator("#execution-archive")).to_be_disabled()
            await expect(page.locator("[data-execution-details]")).to_contain_text(
                "different-observed-epoch"
            )
        finally:
            await page.unroute(execution_url + "?*", mismatch_response)
        await page.locator("#execution-refresh").click()
        await expect(page.locator("#execution-archive")).to_be_enabled()

        async def later_totals_response(route):
            response = await route.fetch()
            payload = await response.json()
            payload["totals"]["good_quantity"] = "5"
            await route.fulfill(status=200, json=payload)

        # Do not poison or replace the immutable actual final summary fixture.
        # Fault-inject a mismatching returned projection after the real GET.
        await page.route(execution_url + "?*", later_totals_response)
        before = business_state()
        try:
            await page.locator("#execution-archive").click()
            await expect(page.locator(".execution-record")).to_contain_text(
                "집계 수량이 달라졌습니다"
            )
            await expect(page.locator("#workflow-original-id")).to_have_text(command_id)
            require(business_state() == before, "Rejected local archive changed business records")
        finally:
            await page.unroute(execution_url + "?*", later_totals_response)
        await page.locator("#execution-refresh").click()
        await expect(page.locator("#execution-archive")).to_be_enabled()
        checkpoint(
            "archive rechecks source flags and request errors without discarding original intent"
        )

        async def source_conflict(route):
            response = await route.fetch()
            require(response.status == 200, "Execution record unexpectedly unavailable")
            payload = await response.json()
            payload["source"]["conflicted"] = payload["source"]["control_restricted"] = True
            await route.fulfill(status=200, json=payload)

        await page.route(execution_url + "?*", source_conflict)
        try:
            await page.locator("#execution-archive").click()
            await expect(page.locator(".execution-record")).to_contain_text("현재 수집원 제한")
            await expect(page.locator("#workflow-original-id")).to_have_text(command_id)
        finally:
            await page.unroute(execution_url + "?*", source_conflict)
        await page.locator("#execution-refresh").click()
        await expect(page.locator("#execution-archive")).to_be_enabled()

        async def missing_response(route):
            response = await route.fetch()
            require(response.status == 200, "Actual execution lookup failed")
            await route.abort("failed")

        await page.route(execution_url + "?*", missing_response)
        try:
            await page.locator("#execution-archive").click()
            await expect(page.locator(".execution-record")).to_contain_text("원래 요청을 유지")
            await expect(page.locator("#workflow-original-id")).to_have_text(command_id)
        finally:
            await page.unroute(execution_url + "?*", missing_response)
        await page.locator("#execution-refresh").click()
        await expect(page.locator("#execution-archive")).to_be_enabled()
        checkpoint(
            "explicit archive preserves original ledger/history without releasing DB reservation"
        )
        before, before_posts = business_state(), len(posts)
        await page.locator("#execution-archive").click()
        await page.locator("#workflow-recovery").wait_for(state="visible")
        archived = page.locator('[data-archived-command="' + command_id + '"]')
        await expect(archived).to_have_count(1)
        await archived.locator("summary").click()
        await expect(archived).to_contain_text(execution_id)
        await expect(archived).to_contain_text("browser-planner")
        require(
            business_state() == before and len(posts) == before_posts,
            "Archive mutated business state or submitted a request",
        )

        values = {
            "Line.State": 0,
            "Line.WipCount": 0,
            "Line.ControlVersion": 43,
            "Line.EventSeq": 4,
            "Line.ActiveExecutionId": execution_id,
            "Line.RecipeVersion": recipe,
            "Line.CounterEpoch": epoch,
            "Line.GoodTotal": 4,
            "Line.RejectTotal": 0,
            "Line.ScrapTotal": 0,
            "Line.TargetQuantity": original_order["target_quantity"],
            "Outfeed.GoodCounter": 4,
            "Sealer.TemperatureSP": parameters["temperature_c"],
        }

        async def fresh_capture():
            while True:
                ingest_capture(
                    seq=next_capture(), controls=True, binding=True, control_overrides=values
                )
                await asyncio.sleep(0.6)

        task = asyncio.create_task(fresh_capture())
        checkpoint("same-session next work order still uses fresh WS and exact previous settlement")
        await page.locator("#workspace-drafts").click()
        await page.locator("#mes-order-id").fill("browser-next-order")
        await page.locator("#mes-order-recipe").select_option(recipe)
        await page.locator("#mes-order-line").select_option(original_order["line_id"])
        await page.locator("#mes-order-target").fill("5")
        await page.locator("#mes-order-date").fill("2026-10-24")
        await page.locator("#mes-order-submit").click()
        await expect(page.locator("#mes-order-status")).to_have_attribute("data-state", "confirmed")
        await page.locator('[data-release-order="browser-next-order"]').click()
        await page.locator("#release-submit").click()
        await expect(page.locator("#workflow-status")).to_have_attribute("data-state", "confirmed")
        await page.locator("#workflow-acknowledge").click()
        await (
            page.locator("section.mes-card")
            .filter(has=page.locator("#mes-orders-heading"))
            .get_by_role("button", name="목록 새로 조회", exact=True)
            .click()
        )
        await page.locator('[data-bind-order="browser-next-order"]').click()
        # A previous revision can still be fresh until the next real WS update.
        # Wait for the new fixture version before capturing this user's intent.
        await expect(page.locator('[data-tag-id="Line.ControlVersion"] dd > strong')).to_have_text(
            "43"
        )
        await expect(
            page.locator('[data-tag-id="Line.ActiveExecutionId"] dd > strong')
        ).to_have_text(execution_id)
        await expect(page.locator("#binding-prepare")).to_be_enabled()
        next_posts = []

        async def lose_next_response(route):
            next_posts.append(route.request.post_data_json)
            response = await route.fetch()
            if response.status != 200:
                from urllib.parse import urlencode

                query = {
                    "line_id": original_order["line_id"],
                    "source_id": "browser-source",
                    "stream_id": "browser-stream",
                }
                observed = await context.request.get(
                    origin + "/api/v1/snapshot?" + urlencode(query)
                )
                snapshot = await observed.json()
                samples = snapshot["screen"]["view"]["samples"]
                current = {item["tag_id"]: item["reading"]["value"] for item in samples}
                with engine.connect() as db:
                    progress = tuple(
                        db.execute(
                            text(
                                "SELECT verified_through_seq,latest_ingested_seq "
                                "FROM stream_progress"
                            )
                        ).one()
                    )
                    gaps = db.execute(text("SELECT count(*) FROM stream_gaps")).scalar()
                    pending = db.execute(
                        text("SELECT count(*) FROM event_inbox WHERE applied_at IS NULL")
                    ).scalar()
                detail = (await response.json()).get("detail", "unavailable")
                diagnostics = {
                    "http": response.status,
                    "detail": detail,
                    "health": snapshot["health"],
                    "tags": len(samples),
                    "good": sum(item["status_code"] >> 30 == 0 for item in samples),
                    "fresh": sum(item["fresh"] for item in snapshot["evidence"]),
                    "missing": snapshot["missing_tag_ids"],
                    "expected_version": next_posts[-1]["expected_control_version"],
                    "observed_version": current["Line.ControlVersion"],
                    "state": current["Line.State"],
                    "wip": current["Line.WipCount"],
                    "event_seq": current["Line.EventSeq"],
                    "frontier": [str(value) for value in progress],
                    "gaps": gaps,
                    "pending": pending,
                }
                # Only public health/state and count diagnostics; no auth or request body.
                raise RuntimeError("Next bind gates " + json.dumps(diagnostics))
            payload = await response.json()
            with engine.begin() as db:
                work = claim_next(db, **gateway, now=datetime.now(UTC))
                require(
                    work is not None
                    and work.action == "SUBMIT"
                    and work.request.command_id == payload["command_id"],
                    "Next LOAD was not reserved exactly once",
                )
            await route.abort("failed")

        bind_url = work_action(origin, "bind", "browser-next-order")
        await page.route(bind_url, lose_next_response)
        try:
            await page.locator("#binding-prepare").click()
            await page.locator("#binding-submit").click()
            await expect(page.locator("#workflow-status")).to_have_attribute(
                "data-state", "uncertain"
            )
            next_id = next_posts[0]["command_id"]
            await page.locator("#workflow-lookup").click()
            await expect(page.locator("#workflow-status")).to_have_attribute(
                "data-state", "confirmed"
            )
            await expect(page.locator("#load-command-id")).to_have_text(next_id)
            await expect(page.locator("#load-command-status")).to_have_attribute(
                "data-status", "OUTCOME_UNKNOWN"
            )
            await expect(archived).to_have_count(1)
            require(len(next_posts) == 1, "Next binding response loss caused an automatic rePOST")
            with engine.connect() as db:
                link = db.execute(
                    text(
                        "SELECT execution_id FROM work_order_execution_links "
                        "WHERE load_command_id=:id"
                    ),
                    {"id": next_id},
                ).scalar_one()
                current = db.execute(
                    text(
                        "SELECT execution_id FROM line_execution_reservations WHERE line_id=:line"
                    ),
                    {"line": original_order["line_id"]},
                ).scalar_one()
                old = db.execute(
                    text("SELECT count(*) FROM work_order_execution_links WHERE execution_id=:id"),
                    {"id": execution_id},
                ).scalar_one()
            require(
                current == link and current != execution_id and old == 1,
                "Next binding lost historical execution or reserved the wrong original slot",
            )
            await page.screenshot(
                path=str(screenshot.with_stem(screenshot.stem + "-execution")), full_page=True
            )
        finally:
            await page.unroute(bind_url, lose_next_response)
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        engine.dispose()
