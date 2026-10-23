"""Combined source → encrypted OPC → gateway HTTP → PostgreSQL acceptance.

T10/T11 explicitly use deterministic boundary seams; they do not claim an
actual 30 minute outage or an 8 hour run.
"""

import csv
import io
import secrets
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text
from test_gateway_pipeline import (
    assert_final_target,
    drive_request,
    journal_network_command,
    journal_result,
    register,
)

from apps.api.bootstrap import LINE_ID
from apps.api.reports import read_execution_reports
from apps.gateway.commands import CommandTransport, CommandWorker
from apps.gateway.opcua_client import GatewayMethodUncertain
from apps.gateway.settlement import RunSummaryTransport, RunSummaryWorker
from contracts.models import CommandRequest, RunSummaryRequest
from contracts.transport import BatchBody, GatewayBatch, batch_digest


async def complete_job(pipeline, evidence, *, response_loss=False):
    runtime, server, client, pump, service = pipeline
    register(service, runtime)
    transport = CommandTransport(pump.intake.config)
    worker = CommandWorker(client, transport)
    original = client.submit_command
    original_get = client.get_command_result
    lost_queries = set()
    submissions = []

    async def get_result(request):
        result = await original_get(request)
        if response_loss and request.command_id not in lost_queries:
            lost_queries.add(request.command_id)
            raise GatewayMethodUncertain(
                "GetCommandResult", request.command_id, delivery_possible=True
            )
        return result

    async def submit(request):
        submissions.append(request.command_id)
        result = await original(request)
        if response_loss:
            raise GatewayMethodUncertain(
                "SubmitCommand", request.command_id, delivery_possible=True
            )
        return result

    client.submit_command = submit
    client.get_command_result = get_result
    requests = []
    try:
        for index, kind in enumerate(("LOAD_JOB", "START", "END_RUN"), 1):
            request = journal_network_command(
                service, runtime, evidence.data["case_id"] + kind, kind
            )
            requests.append(request.model_dump(mode="json"))
            first = await worker.run_once()
            evidence.check(kind + " dispatched original ID", first.command_id == request.command_id)
            if response_loss:
                evidence.check(
                    kind + " transient uncertainty persisted",
                    first.state == "uncertain"
                    and journal_result(service, request.command_id)["status"] == "OUTCOME_UNKNOWN",
                )
            drive_request(runtime, request)
            await server.publish_snapshot(acquired_at=datetime.now(UTC), heartbeat=index + 1)
            await worker.run_once()
            evidence.check(
                kind + " resolved original ID",
                journal_result(service, request.command_id)["status"] == "SUCCEEDED",
            )
        summary = await client.get_run_summary(RunSummaryRequest(execution_id="run-A"))
        evidence.check(
            "exactly one OPC submission per command",
            submissions == [r["command_id"] for r in requests],
            submissions=submissions,
        )
        evidence.record(
            commands=requests,
            executions=[summary.model_dump(mode="json")],
            final_sequences={"run-A": summary.last_event_seq},
        )
        return summary
    finally:
        client.submit_command = original
        client.get_command_result = original_get
        await transport.close()


async def settle(pipeline, evidence, summary):
    runtime, server, client, pump, service = pipeline
    summaries = RunSummaryTransport(pump.intake.config)
    collector = RunSummaryWorker(
        client, summaries, source_id=pump.source_id, stream_id=pump.stream_id
    )
    try:
        evidence.check(
            "PLC final summary reported over HTTP", (await collector.run_once()).reported == 1
        )
        with service.engine.connect() as db:
            settlement = db.execute(
                text("SELECT status FROM run_reconciliations WHERE execution_id='run-A'")
            ).scalar_one()
            events = [
                dict(row)
                for row in db.execute(
                    text(
                        "SELECT event_id,seq,execution_id,kind,body,content_sha256 "
                        "FROM event_inbox ORDER BY seq"
                    )
                ).mappings()
            ]
            reports = read_execution_reports(
                db,
                LINE_ID,
                datetime.now(UTC) - timedelta(days=1),
                datetime.now(UTC) + timedelta(seconds=1),
                now=datetime.now(UTC) + timedelta(seconds=1),
            )
        totals = service.get_totals("run-A")
        evidence.check(
            "source and ingestion three quantities match",
            all(
                totals[k] == getattr(summary, k)
                for k in ("good_quantity", "reject_quantity", "scrap_quantity")
            ),
            totals=totals,
        )
        evidence.check(
            "final sequence verified",
            pump.buffer.progress(pump.source_id, pump.stream_id).api_progress.verified_through_seq
            == summary.last_event_seq,
        )
        evidence.check("settled", settlement == "SETTLED")
        report = next(row for row in reports["items"] if row["execution_id"] == "run-A")
        evidence.check(
            "report quantities equal final PLC",
            report["totals"]
            == {
                "good": str(summary.good_quantity),
                "reject": str(summary.reject_quantity),
                "scrap": str(summary.scrap_quantity),
            },
        )
        evidence.record(events=events, reports=reports)
    finally:
        await summaries.close()


@pytest.mark.parametrize("case_id,response_loss", [("T01", False), ("T03", True)])
async def test_normal_and_response_loss_original_execution(
    pipeline, acceptance_evidence, case_id, response_loss
):
    evidence = acceptance_evidence(case_id)
    evidence.record(
        scope="real OPC/HTTP/PostgreSQL; accelerated small physical geometry; no browser assertion"
    )
    summary = await complete_job(pipeline, evidence, response_loss=response_loss)
    runtime, server, client, pump, service = pipeline
    capture = await pump.capture_once()
    evidence.check("48 actual OPC tags captured", capture.sample_count == 48)
    leased = pump.buffer.lease_next()
    sample_tags = [sample.tag_id for sample in leased.batch.body.samples]
    pump.buffer.retry(leased.batch.body.batch_id)
    evidence.record(sample_tags=sample_tags)
    evidence.check("actual HTTP batch committed", await pump.deliver_once())
    await settle(pipeline, evidence, summary)
    equipment = {tag.split(".")[0] for tag in evidence.data["sample_tags"]}
    evidence.check(
        "all three equipment captured over OPC",
        {"Infeed", "Sealer", "Outfeed"} <= equipment,
        equipment=sorted(equipment),
    )
    evidence.check(
        "pinned recipe and final quantities",
        summary.recipe_version == "recipe-A"
        and (summary.good_quantity, summary.reject_quantity, summary.scrap_quantity) == (4, 1, 0),
    )


async def test_duplicate_delayed_reverse_events_combined(pipeline, acceptance_evidence):
    evidence = acceptance_evidence("T07")
    summary = await complete_job(pipeline, evidence)
    runtime, server, client, pump, service = pipeline
    await pump.capture_once()
    leased = pump.buffer.lease_next()
    source = leased.batch
    pump.buffer.retry(source.body.batch_id)
    events = list(source.body.events)
    evidence.check("source contains multiple events", len(events) > 2)

    def fragment(suffix, selected, seq):
        body = BatchBody.model_validate(
            source.body.model_dump(mode="python")
            | {
                "batch_id": evidence.data["acceptance_id"] + suffix,
                "capture_seq": seq,
                "samples": [],
                "events": selected,
            }
        )
        return GatewayBatch(body=body, content_sha256=batch_digest(body))

    late = fragment("-late", events[1:], source.body.capture_seq + 1)
    ack = await pump.intake.deliver(late)
    evidence.check("hole prevents contiguous verification", ack.progress.verified_through_seq == 0)
    before = service.get_totals("run-A")
    await pump.intake.deliver(late)
    evidence.check("same immutable batch counted once", service.get_totals("run-A") == before)
    await pump.intake.deliver(fragment("-first", events[:1], source.body.capture_seq + 2))
    before = service.get_totals("run-A")
    await pump.intake.deliver(fragment("-duplicate", events, source.body.capture_seq + 3))
    evidence.check("new batch duplicate events counted once", service.get_totals("run-A") == before)
    evidence.check("original gateway queue receipt restored", await pump.deliver_once())
    await settle(pipeline, evidence, summary)


async def test_a_to_b_then_original_a_replay(pipeline, acceptance_evidence):
    """User publication/binding -> real gateway HTTP/OPC -> exact prior settlement."""
    import httpx
    from sqlalchemy import text

    from apps.api.auth import UserAuth
    from apps.api.user_http import UserHTTPConfig, create_user_app
    from apps.gateway.commands import CommandTransport, CommandWorker
    from apps.gateway.settlement import RunSummaryTransport, RunSummaryWorker

    evidence = acceptance_evidence("T08")
    runtime, server, client, pump, service = pipeline
    origin = "https://managed.example.test"
    auth = UserAuth(service.engine, allowed_origins={origin})
    password = secrets.token_urlsafe(32)
    auth.bootstrap_user("planner", password, roles={"production_manager", "operator"})
    challenge = auth.begin_login()
    session = auth.login(
        "planner",
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=origin,
    )
    session = auth.change_password(
        session.token, secrets.token_urlsafe(32), csrf_token=session.csrf_token, origin=origin
    )
    protection = {"Origin": origin, "X-CSRF-Token": session.csrf_token}
    app = create_user_app(auth, UserHTTPConfig(origin=origin))
    commands = CommandTransport(pump.intake.config)
    worker = CommandWorker(client, commands)
    summaries = RunSummaryTransport(pump.intake.config)
    collector = RunSummaryWorker(
        client, summaries, source_id=pump.source_id, stream_id=pump.stream_id
    )
    original_submit = client.submit_command
    submitted = []
    heartbeat = 1
    captured_batches = []

    async def track_submit(request):
        submitted.append(request.command_id)
        return await original_submit(request)

    async def capture():
        nonlocal heartbeat
        heartbeat += 1
        await server.publish_snapshot(acquired_at=datetime.now(UTC), heartbeat=heartbeat)
        assert (await pump.capture_once()).sample_count == 48
        assert client.health().state == "GOOD"
        leased = pump.buffer.lease_next()
        captured_batches.append(leased.batch)
        pump.buffer.retry(leased.batch.body.batch_id)
        assert await pump.deliver_once()

    def bind_intent(command):
        return {
            "command_id": command,
            "line_id": LINE_ID,
            "source_id": pump.source_id,
            "stream_id": pump.stream_id,
            "expected_work_order_version": "2",
            "expected_control_version": str(runtime.state.control_version),
        }

    async def dispatch(request):
        assert (await worker.run_once()).command_id == request.command_id
        drive_request(runtime, request)
        await capture()
        assert (await worker.run_once()).command_id == request.command_id
        assert journal_result(service, request.command_id)["status"] == "SUCCEEDED"

    client.submit_command = track_submit
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=origin,
            cookies={"hmi_session": session.token},
        ) as browser:
            recipe = await browser.post(
                "/api/v1/recipe-versions",
                headers=protection,
                json={
                    "id": "managed-v1",
                    "recipe_id": "managed",
                    "version": "v1",
                    "item_id": "item",
                    "parameters": {"temperature_c": 150.0, "speed_m_per_min": 6.0},
                },
            )
            assert recipe.status_code == 200
            for order in ("managed-A", "managed-B"):
                created = await browser.post(
                    "/api/v1/work-orders",
                    headers=protection,
                    json={
                        "id": order,
                        "item_id": "item",
                        "line_id": LINE_ID,
                        "recipe_version_id": "managed-v1",
                        "target_quantity": 4,
                        "planned_date": "2026-10-24",
                    },
                )
                assert created.status_code == 200 and created.json()["status"] == "DRAFT"
                released = await browser.post(
                    f"/api/v1/work-orders/{order}/release",
                    headers=protection,
                    json={
                        "release_id": "release-" + order,
                        "expected_row_version": "1",
                    },
                )
                assert released.status_code == 200
            await capture()
            first = await browser.post(
                "/api/v1/work-orders/managed-A/bind",
                headers=protection,
                json=bind_intent("managed-load-A"),
            )
            assert first.status_code == 200, first.text
            run_a = first.json()["execution_id"]
            await dispatch(CommandRequest.model_validate(first.json()["load_request"]))
            for kind in ("START", "END_RUN"):
                now = datetime.now(UTC)
                request = CommandRequest(
                    command_id="managed-" + kind,
                    target_id="Line",
                    execution_id=run_a,
                    requested_by="planner",
                    requested_at=now,
                    expires_at=now + timedelta(seconds=5),
                    expected_control_version=runtime.state.control_version,
                    command={"kind": kind},
                )
                accepted = await browser.post(
                    "/api/v1/commands",
                    headers=protection,
                    json={
                        "line_id": LINE_ID,
                        "source_id": pump.source_id,
                        "stream_id": pump.stream_id,
                        "request": request.model_dump(mode="json"),
                    },
                )
                assert accepted.status_code == 200, accepted.text
                await dispatch(request)
            final = await client.get_run_summary(RunSummaryRequest(execution_id=run_a))
            assert_final_target(final)
            original_final = final.model_dump(mode="json", exclude={"as_of"})
            saved_a_batches = [
                batch
                for batch in captured_batches
                if any(
                    event.execution_id == run_a
                    and event.kind in {"PRODUCTION_GOOD", "PRODUCTION_REJECT", "WIP_SCRAP"}
                    for event in batch.body.events
                )
            ]
            evidence.check(
                "saved A replay contains actual production events", bool(saved_a_batches)
            )
            second_intent = bind_intent("managed-load-B")
            blocked = await browser.post(
                "/api/v1/work-orders/managed-B/bind", headers=protection, json=second_intent
            )
            evidence.check(
                "B binding blocked before exact A settlement", blocked.status_code == 409
            )
            with service.engine.connect() as db:
                assert (
                    db.execute(
                        text("SELECT execution_id FROM line_execution_reservations")
                    ).scalar_one()
                    == run_a
                )
                assert (
                    db.execute(text("SELECT count(*) FROM production_executions")).scalar_one() == 1
                )
            assert (await collector.run_once()).reported == 1
            await capture()
            second = await browser.post(
                "/api/v1/work-orders/managed-B/bind", headers=protection, json=second_intent
            )
            evidence.check(
                "B binding permitted after exact A settlement",
                second.status_code == 200,
                status=second.status_code,
            )
            replay = await browser.post(
                "/api/v1/work-orders/managed-B/bind", headers=protection, json=second_intent
            )
            assert replay.json() == second.json()
            run_b = second.json()["execution_id"]
            assert run_b != run_a
            await dispatch(CommandRequest.model_validate(second.json()["load_request"]))
            assert submitted == [
                "managed-load-A",
                "managed-START",
                "managed-END_RUN",
                "managed-load-B",
            ]
            assert runtime.state.execution_id == run_b
            before_a, before_b = service.get_totals(run_a), service.get_totals(run_b)
            receipts = [await pump.intake.deliver(batch) for batch in saved_a_batches]
            evidence.check("A replay does not mutate B", service.get_totals(run_b) == before_b)
            evidence.check(
                "A replay does not double count A", service.get_totals(run_a) == before_a
            )
            evidence.check("B remains PLC active execution", runtime.state.execution_id == run_b)
            evidence.record(
                commands=submitted,
                executions=[run_a, run_b],
                events=[
                    event.model_dump(mode="json")
                    for batch in saved_a_batches
                    for event in batch.body.events
                ],
                final_sequences={run_a: final.last_event_seq},
                reports={"A": before_a, "B": before_b},
                replay_receipts=[receipt.model_dump(mode="json") for receipt in receipts],
            )
            after = await client.get_run_summary(RunSummaryRequest(execution_id=run_a))
            evidence.check(
                "original A final summary immutable after B and replay",
                after.model_dump(mode="json", exclude={"as_of"}) == original_final,
            )
            with service.engine.connect() as db:
                assert (
                    db.execute(
                        text("SELECT execution_id FROM line_execution_reservations")
                    ).scalar_one()
                    == run_b
                )
                assert (
                    db.execute(
                        text("SELECT status FROM run_reconciliations WHERE execution_id=:id"),
                        {"id": run_a},
                    ).scalar_one()
                    == "SETTLED"
                )
                audit = db.execute(
                    text(
                        "SELECT actor,action FROM command_request_audit "
                        "WHERE command_id='managed-load-B' ORDER BY id"
                    )
                ).all()
                assert audit[0] == ("planner", "REQUESTED")
                assert {actor for actor, action in audit if action != "REQUESTED"} == {"gateway-1"}
    finally:
        client.submit_command = original_submit
        await summaries.close()
        await commands.close()


async def test_midnight_quality_three_quantities_and_csv(
    state, tmp_path, monkeypatch, acceptance_evidence
):
    from datetime import date
    from uuid import uuid4

    from acceptance_runtime import boundary_network_runtime
    from test_alarm_consumer import AT

    import apps.simulator.opcua_server as opc_module
    from apps.api.reports import read_daily_report, read_execution_timeline
    from contracts.models import ReadEventsRequest

    evidence = acceptance_evidence("T11")
    midnight = AT + timedelta(hours=15)

    class Clock:
        utc = midnight - timedelta(seconds=1)
        mono = 1000.0

        def now(self):
            return self.utc

        def monotonic(self):
            return self.mono

        def advance(self, seconds):
            self.utc += timedelta(seconds=seconds)
            self.mono += seconds

    clock = Clock()

    class SourceDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now()

    # Only the server clock seam changes; durable source events are never edited.
    monkeypatch.setattr(opc_module, "datetime", SourceDateTime)
    evidence.record(
        scope=(
            "actual simulator/SQLite → encrypted OPC → gateway HTTP → PG → authenticated CSV; "
            "deterministic source/gateway/API timestamps at Seoul midnight, no elapsed-soak claim"
        )
    )
    async with boundary_network_runtime(
        tmp_path, state.engine, clock=clock, service=state.service
    ) as values:
        runtime, server, client, pump, service = values
        epoch = client.clock_reading()[0]
        policy = service.trend_clock_policy.model_copy(update={"clock_epoch": epoch})
        service.trend_clock_policy = policy
        service.alarm_consumer.config = service.alarm_consumer.config.model_copy(
            update={"clock_policy": policy}
        )
        requests = []
        heartbeat = 1

        async def capture(*, publish=True):
            nonlocal heartbeat
            if publish:
                heartbeat += 1
                await server.publish_snapshot(acquired_at=clock.now(), heartbeat=heartbeat)
            result = await pump.capture_once()
            evidence.check("actual OPC capture has all 48 tags", result.sample_count == 48)
            evidence.check("actual authenticated HTTP batch commits", await pump.deliver_once())

        async def command(kind, args=None):
            request = CommandRequest(
                command_id="T11-" + str(len(requests)) + kind,
                target_id="Line",
                execution_id="run",
                requested_by="acceptance-operator",
                requested_at=clock.now(),
                expires_at=clock.now() + timedelta(minutes=2),
                expected_control_version=runtime.state.control_version,
                command={"kind": kind, "args": args or {}},
            )
            requests.append(request.model_dump(mode="json"))
            submission = await client.submit_command(request)
            evidence.check(
                kind + " accepted by real PLC OPC method", submission.disposition == "ACCEPTED"
            )
            for _ in range(100):
                if runtime.get_result(request.command_id).status == "SUCCEEDED":
                    break
                clock.advance(0.1)
                runtime.tick(now=clock.now())
            evidence.check(
                kind + " completed original command",
                runtime.get_result(request.command_id).status == "SUCCEEDED",
            )

        await command(
            "LOAD_JOB",
            {
                "work_order_id": "run",
                "recipe_version": "recipe",
                "target_quantity": 4,
                "parameters": {"temperature_c": 150.0, "speed_m_per_min": 6.0},
            },
        )
        await command("START")
        clock.advance(0.1)
        runtime.tick(now=clock.now())
        await command("PAUSE")
        process = runtime.storage.load_execution()
        items = [item.item_id for item in process.slots if item is not None]
        evidence.check("real WIP available for explicit scrap", bool(items))
        runtime.scrap_wip(
            (min(items),),
            reason="Acceptance verified damaged item",
            positions_verified=True,
            now=clock.now(),
        )
        await command("START")
        await capture()
        clock.advance(2)
        await capture()
        clock.advance(5)
        await capture(publish=False)  # Real repeated OPC heartbeat ages to ALARM.
        clock.advance(2)
        client._clock_epoch = str(uuid4())  # Explicit unknown epoch time-quality seam.
        await capture()
        for _ in range(100):
            process = runtime.storage.load_execution()
            if process.good_quantity == 4 and process.wip_quantity == 0:
                break
            clock.advance(0.1)
            runtime.tick(now=clock.now())
        await command("END_RUN")
        await capture()
        summary = await client.get_run_summary(RunSummaryRequest(execution_id="run"))
        evidence.check(
            "actual PLC final has all three quantities",
            summary.final
            and (summary.good_quantity, summary.reject_quantity, summary.scrap_quantity)
            == (4, 1, 1),
        )
        transport = RunSummaryTransport(pump.intake.config)
        collector = RunSummaryWorker(client, transport, source_id="source", stream_id="stream")
        try:
            evidence.check(
                "actual PLC final committed by HTTP", (await collector.run_once()).reported == 1
            )
        finally:
            await transport.close()
        original = await client.read_events(
            ReadEventsRequest(source_id="source", stream_id="stream", after_seq=0)
        )
        events = original.events
        with state.engine.connect() as db:
            previous = read_daily_report(
                db, LINE_ID, date(2026, 10, 25), now=midnight + timedelta(days=1)
            )
            following = read_daily_report(
                db, LINE_ID, date(2026, 10, 26), now=midnight + timedelta(days=1)
            )
            timeline = read_execution_timeline(
                db, "run", LINE_ID, "source", "stream", now=clock.now()
            )
            status = db.execute(
                text("SELECT status FROM run_reconciliations WHERE execution_id='run'")
            ).scalar_one()
            durable = db.execute(text("SELECT body FROM event_inbox ORDER BY seq")).scalars().all()
        evidence.check(
            "source event originals survive OPC/HTTP unchanged",
            durable == [event.model_dump(mode="json") for event in events],
        )
        evidence.check("original execution settles", status == "SETTLED")
        kinds = {"PRODUCTION_GOOD": "good", "PRODUCTION_REJECT": "reject", "WIP_SCRAP": "scrap"}
        expected = [{"good": 0, "reject": 0, "scrap": 0}, {"good": 0, "reject": 0, "scrap": 0}]
        for event in events:
            if event.kind in kinds:
                expected[int(event.occurred_at >= midnight)][kinds[event.kind]] += (
                    event.payload.quantity
                )
        evidence.check(
            "source production crosses Seoul midnight",
            any(event.occurred_at < midnight for event in events if event.kind in kinds)
            and any(event.occurred_at >= midnight for event in events if event.kind in kinds),
        )
        for index, report in enumerate((previous, following)):
            evidence.check(
                "daily quantities equal original source event sum",
                report["totals"] == {key: str(value) for key, value in expected[index].items()},
            )
        reasons = {segment.get("unknown_reason") for segment in timeline["segments"]}
        evidence.check(
            "communication and clock errors remain unknown",
            {"COMMUNICATION_LOST", "TIME_UNKNOWN"} <= reasons,
            reasons=list(reasons),
        )
        for report in (previous, following, timeline):
            duration = report["durations_seconds"]
            evidence.check(
                "time equation",
                abs(
                    duration["running"]
                    + duration["stopped"]
                    + duration["unknown"]
                    - duration["total"]
                )
                < 0.000001,
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=state.app),
            base_url="https://reports.test",
            cookies={"hmi_session": state.session.token},
        ) as http:
            params = {
                "line_id": LINE_ID,
                "from": AT.isoformat(),
                "to": (AT + timedelta(days=1)).isoformat(),
            }
            response = await http.get("/api/v1/reports/executions.csv", params=params)
            evidence.check("authenticated CSV export", response.status_code == 200)
            rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
            row = next(row for row in rows[1:] if row[0] == "run")
            evidence.check(
                "CSV equals original PLC final three quantities",
                row[5:8]
                == [
                    str(summary.good_quantity),
                    str(summary.reject_quantity),
                    str(summary.scrap_quantity),
                ],
                csv_row=row,
            )
        evidence.record(
            commands=requests,
            executions=[summary.model_dump(mode="json")],
            events=[event.model_dump(mode="json") for event in events],
            final_sequences={"run": summary.last_event_seq},
            reports={
                "previous": previous,
                "following": following,
                "timeline": timeline,
                "csv": rows,
            },
        )


def test_buffer_and_source_retention_combined_boundary(tmp_path, acceptance_evidence):
    from test_gateway_buffer import AT, make_batch
    from test_simulator_retention import WEEK, Clock, allowed, control, open_store, read

    from apps.gateway.buffer import BufferLimits, GatewayBuffer

    evidence = acceptance_evidence("T10")
    evidence.record(
        scope=(
            "deterministic 30min/byte/7day boundary seams; actual SQLite; "
            "real 30minute outage remains CX19"
        )
    )
    first = make_batch()
    with GatewayBuffer(tmp_path / "measure.sqlite3", gateway_id="gateway-A") as measure:
        measure.enqueue(first, now=AT)
        one = measure.status(now=AT).queue_bytes
    losses = []
    for name, limits, elapsed in [
        ("age", BufferLimits(), timedelta(minutes=30)),
        ("bytes", BufferLimits(max_bytes=one), timedelta(seconds=1)),
    ]:
        with GatewayBuffer(
            tmp_path / (name + ".sqlite3"), gateway_id="gateway-A", limits=limits
        ) as queue:
            queue.enqueue(first, now=AT)
            result = queue.enqueue(make_batch(2), now=AT + elapsed)
            evidence.check(
                name + " first limit creates exact gap",
                len(result.losses) == 1 and result.losses[0].batch_id == first.body.batch_id,
            )
            evidence.check(
                name + " loss never advances verified cursor",
                queue.progress("source-A", "stream-A").api_progress.verified_through_seq == 0,
            )
            losses.extend(result.losses)
    clock = Clock()
    with open_store(tmp_path / "source.sqlite3", clock) as source:
        source.acknowledge_recovery()
        original = control(source)
        clock.advance(WEEK)
        pruned = source.prune_events(controller_guard=allowed)
        missing = read(source)
        evidence.check(
            "seven-day original cannot be recovered",
            pruned.pruned_count == 1
            and missing.code == "RETENTION_GAP"
            and missing.missing_from_seq == original.seq
            and missing.missing_through_seq == original.seq,
        )
    evidence.record(
        events=[original.model_dump(mode="json")],
        losses=losses,
        final_sequences={"verified_through_seq": 0},
        reports={"source_read": missing.model_dump(mode="json")},
    )
