"""Profile full FIFO replay of 1,800 preloaded 48-tag captures with live 1Hz OPC.

Preloading historical immutable captures is a performance fixture, not a real
30 minute outage. No queued repair bypasses FIFO delivery or committed ACKs.
"""

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import text
from test_gateway_pipeline import free_port, register
from test_gateway_pipeline import pipeline as pipeline

from apps.api.auth import UserAuth
from apps.api.bootstrap import LINE_ID
from apps.gateway.http_client import IntakeClient, IntakeConfig
from contracts.models import CommandRequest, ReadEventsRequest, RunSummaryRequest
from contracts.transport import BatchBody, CaptureTime, GatewayBatch, batch_digest

CHILD = r"""
import json,sys,time
from pathlib import Path
from sqlalchemy import event
from sqlalchemy.engine import Engine
from apps.api.ingestion import IngestionService
from apps.api import capture_clock
from datetime import timedelta
stats={"ingest":{"calls":0,"seconds":0,"max":0},"sql":{}}
output=Path(sys.argv.pop(1))
def save():
    output.write_text(json.dumps(stats,indent=2))
original=IngestionService.ingest
def ingest(self,*args,**kwargs):
    started=time.perf_counter()
    try:
        return original(self,*args,**kwargs)
    finally:
        elapsed=time.perf_counter()-started
        value=stats["ingest"]
        value["calls"]+=1;value["seconds"]+=elapsed;value["max"]=max(value["max"],elapsed)
        if value["calls"]%50==0:save()
IngestionService.ingest=ingest
@event.listens_for(Engine,"before_cursor_execute")
def before(conn,cursor,statement,parameters,context,executemany):
    context.profile_started=time.perf_counter()
@event.listens_for(Engine,"after_cursor_execute")
def after(conn,cursor,statement,parameters,context,executemany):
    names=("analog_history_raw","analog_history_minutes","capture_clock_evidence",
           "capture_clock_state","ingest_batches","event_inbox","stream_gaps",
           "stream_progress","snapshot")
    if "FROM lines" in statement:
        kind="read"
        if "FOR UPDATE" in statement:kind="update"
        elif "FOR SHARE" in statement:kind="share"
        key="line_lock_"+kind
    else:
        key=next((name for name in names if name in statement),"other")
        key+="."+statement.lstrip().split(None,1)[0].upper()
    value=stats["sql"].setdefault(key,{"calls":0,"seconds":0})
    value["calls"]+=1;value["seconds"]+=time.perf_counter()-context.profile_started
# Preserve the real CLI policy validity window: historical backlog stays UNKNOWN.
import apps.api.trend_history as trend_history
import apps.api.capture_clock as capture_clock
stats["functions"]={}
def profile_function(module,name):
    fn=getattr(module,name)
    key=module.__name__+"."+name
    value=stats["functions"].setdefault(key,{"calls":0,"seconds":0.0})
    def measured(*args,**kwargs):
        started=time.perf_counter()
        try:return fn(*args,**kwargs)
        finally:
            value["calls"]+=1;value["seconds"]+=time.perf_counter()-started
    setattr(module,name,measured)
for name in ("capture_trends","raw_observations","points_for","cleanup_trends",
             "fence_incomplete_reversal_minutes"):
    profile_function(trend_history,name)
profile_function(capture_clock,"record_capture_clock")
from apps.api.__main__ import main
try:
    code=main()
finally:
    save()
raise SystemExit(code)
"""


class ProfileOutcome:
    """Finalize the artifact only after pytest has reported fixture teardown."""

    def __init__(self, request, artifact):
        self.request, self.artifact = request, artifact
        self.phases = {"setup": "passed"}

    def pytest_runtest_logreport(self, report):
        if report.nodeid != self.request.node.nodeid:
            return
        self.phases[report.when] = report.outcome
        if self.artifact.exists():
            value = json.loads(self.artifact.read_text())
            value["pytest_phases"] = self.phases
            if report.failed:
                value["failure"] = str(report.longrepr)
            if report.when == "teardown":
                value["phase_outcome"] = value.get("outcome")
                value["outcome"] = (
                    "passed"
                    if all(phase == "passed" for phase in self.phases.values())
                    else "failed"
                )
                value["exit_code"] = 0 if value["outcome"] == "passed" else 1
            self.artifact.write_text(json.dumps(value, indent=2, default=str))
        if report.when == "teardown":
            self.request.config.pluginmanager.unregister(self)


async def test_1800_capture_fifo_drain_with_live_collection(pipeline, tmp_path, request):
    runtime, server, opc, pump, service = pipeline
    artifact = Path(os.environ.get("HMI_RECOVERY_PROFILE", str(tmp_path / "profile.json")))
    artifact.parent.mkdir(parents=True, exist_ok=True)
    request.config.pluginmanager.register(
        ProfileOutcome(request, artifact), "fifo-profile-" + uuid4().hex
    )
    child_metrics = artifact.with_suffix(".api.json")

    async def source_command(kind, args=None):
        at = datetime.now(UTC)
        value = CommandRequest(
            command_id="fifo-" + kind,
            target_id="Line",
            execution_id="run-A",
            requested_by="profile-operator",
            requested_at=at,
            expires_at=at + timedelta(minutes=5),
            expected_control_version=runtime.state.control_version,
            command={"kind": kind, "args": args or {}},
        )
        assert (await opc.submit_command(value)).disposition == "ACCEPTED"
        for _ in range(100):
            runtime.tick(now=datetime.now(UTC))
            if runtime.get_result(value.command_id).status == "SUCCEEDED":
                return
        raise AssertionError("Source command did not complete")

    await source_command(
        "LOAD_JOB",
        {
            "work_order_id": "order-A",
            "recipe_version": "recipe-A",
            "target_quantity": 10000,
            "parameters": {"temperature_c": 150.0, "speed_m_per_min": 6.0},
        },
    )
    await source_command("START")
    for _ in range(20):
        if runtime.storage._bounds()[1] >= 4:
            break
        runtime.tick(now=datetime.now(UTC))
    await server.publish_snapshot(acquired_at=datetime.now(UTC), heartbeat=4)
    register(service, runtime)

    async def read_source_events():
        result = []
        after = 0
        while True:
            page = await opc.read_events(
                ReadEventsRequest(
                    source_id=pump.source_id, stream_id=pump.stream_id, after_seq=after
                )
            )
            result.extend(page.events)
            after = page.page_last_seq
            if not page.has_more:
                return result

    events = await read_source_events()
    source_head = events[-1].seq
    observation = await opc.read_tags()
    port = free_port()
    env = os.environ.copy()
    env["HMI_DATABASE_DSN"] = service.engine.url.render_as_string(hide_password=False)
    command = [
        sys.executable,
        "-c",
        CHILD,
        str(child_metrics),
        "serve",
        "--source-id",
        pump.source_id,
        "--stream-id",
        pump.stream_id,
        "--gateway-id",
        pump.buffer.gateway_id,
        "--capture-generation",
        "1",
        "--key-id",
        "fifo-profile",
        "--token-file",
        str(tmp_path / "service-token"),
        "--expires-at",
        (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "--port",
        str(port),
        "--trend-clock-policy",
        "same-host",
    ]
    log = (tmp_path / "api-profile.log").open("wb")
    child = subprocess.Popen(
        command, cwd=Path(__file__).resolve().parents[2], env=env, stdout=log, stderr=log
    )
    intake = IntakeClient(
        IntakeConfig(f"http://127.0.0.1:{port}", pump.intake.config.token, allow_loopback_http=True)
    )
    user_port = free_port()
    origin = f"http://127.0.0.1:{user_port}"
    auth = UserAuth(service.engine, allowed_origins={origin})
    password = secrets.token_urlsafe(32)
    auth.bootstrap_user("fifo-viewer", password, roles={"viewer"})
    challenge = auth.begin_login()
    session = auth.login(
        "fifo-viewer",
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=origin,
    )
    changed_password = secrets.token_urlsafe(32)
    session = auth.change_password(
        session.token, changed_password, csrf_token=session.csrf_token, origin=origin
    )
    web = Path(
        os.environ.get(
            "HMI_FIFO_WEB_DIR", str(Path(__file__).resolve().parents[2] / "apps/web/dist")
        )
    )
    assert (web / "index.html").exists(), "Built polling web fixture required"
    user_metrics = artifact.with_suffix(".user.json")
    user_log = (tmp_path / "user-profile.log").open("wb")
    user_command = [
        sys.executable,
        "-c",
        CHILD,
        str(user_metrics),
        "serve-user",
        "--origin",
        origin,
        "--port",
        str(user_port),
        "--web-dir",
        str(web),
    ]
    user_child = subprocess.Popen(
        user_command,
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        stdout=user_log,
        stderr=user_log,
    )
    user_http = httpx.AsyncClient(
        base_url=origin, trust_env=False, cookies={"hmi_session": session.token}
    )
    browser = playwright = None
    original_intake = pump.intake
    pump.intake = intake
    measurements = {}
    sql_observations = []
    wrappers = []

    def timed(owner, name):
        original = getattr(owner, name)
        value = measurements.setdefault(name, {"calls": 0, "seconds": 0.0, "max": 0.0})

        def record(started):
            elapsed = time.perf_counter() - started
            value["calls"] += 1
            value["seconds"] += elapsed
            value["max"] = max(value["max"], elapsed)

        if name == "read_tags":
            value["starts"] = []
        if asyncio.iscoroutinefunction(original):

            async def wrapped(*args, **kwargs):
                started = time.perf_counter()
                if name == "read_tags":
                    value["starts"].append(started)
                try:
                    return await original(*args, **kwargs)
                finally:
                    record(started)
        else:

            def wrapped(*args, **kwargs):
                started = time.perf_counter()
                try:
                    return original(*args, **kwargs)
                finally:
                    record(started)

        wrappers.append((owner, name, original))
        setattr(owner, name, wrapped)

    validated = False
    stop = asyncio.Event()
    run = publisher = None
    report = {
        "preloaded": 1800,
        "scope": "synthetic historical preload; real FIFO HTTP/PG and live OPC48tags1Hz",
        "deadline_seconds": 300,
        "outcome": "running",
        "time_quality_policy": "unaltered CLI startup validity; historical backlog UNKNOWN",
        "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "user_http_sha256": hashlib.sha256(
            (Path(__file__).resolve().parents[2] / "apps/api/user_http.py").read_bytes()
        ).hexdigest(),
    }
    try:
        async with httpx.AsyncClient(trust_env=False) as http:
            for _ in range(100):
                assert child.poll() is None
                try:
                    if (await http.get(f"http://127.0.0.1:{port}/not-found")).status_code == 404:
                        break
                except httpx.TransportError:
                    await asyncio.sleep(0.1)
            else:
                raise AssertionError("Profile API CLI did not start")
        for _ in range(100):
            assert user_child.poll() is None
            try:
                if (await user_http.get("/auth/me")).status_code == 200:
                    break
            except httpx.TransportError:
                await asyncio.sleep(0.1)
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            executable_path=shutil.which("chromium"), headless=True, args=["--no-sandbox"]
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 1080}, locale="ko-KR"
        )
        page = await context.new_page()
        browser_polls = []
        page.on(
            "response",
            lambda response: (
                browser_polls.append(time.perf_counter())
                if response.url.startswith(origin + "/api/v1/screen?") and response.status == 200
                else None
            ),
        )
        await page.goto(origin, wait_until="domcontentloaded")
        await page.locator("#login").fill("fifo-viewer")
        await page.locator("#login-password").fill(changed_password)
        await page.locator("#login-submit").click()
        await page.locator("#current-login").wait_for(state="visible", timeout=10000)
        assert await page.locator("#current-login").text_content() == "fifo-viewer"
        anchor = observation.acquired_at - timedelta(seconds=1800)
        # Synthetic history has its own epoch and positive read brackets, even
        # when this kernel has been up for less than 1,800 seconds. Sharing the
        # real collector's epoch would create a reversal when live reads resume.
        preload_epoch = "fifo-preload-" + uuid4().hex
        preload_span = observation.capture_time.utc_read_span_seconds
        mono = preload_span + 1.0

        def body(seq, selected=(), gaps=()):
            at = anchor + timedelta(seconds=seq - 1)
            samples = [
                sample.model_copy(
                    update={
                        "sample_id": f"fifo-{seq}-{sample.tag_id}",
                        "acquired_at": at,
                        "source_timestamp": sample.source_timestamp + (at - observation.acquired_at)
                        if sample.source_timestamp
                        else None,
                        "server_timestamp": sample.server_timestamp + (at - observation.acquired_at)
                        if sample.server_timestamp
                        else None,
                    }
                )
                for sample in observation.samples
            ]
            return BatchBody(
                schema_version="1.2",
                batch_id=f"fifo-{seq}",
                gateway_id=pump.buffer.gateway_id,
                source_id=pump.source_id,
                stream_id=pump.stream_id,
                capture_generation=1,
                capture_seq=seq,
                captured_at=at,
                capture_time=CaptureTime(
                    clock_epoch=preload_epoch,
                    monotonic_seconds=mono + seq - 1,
                    utc_read_span_seconds=preload_span,
                ),
                communication={
                    "state": "GOOD",
                    "connected": True,
                    "read_age_seconds": 0.0,
                    "heartbeat_ages_seconds": dict(observation.health.heartbeat_ages_seconds),
                },
                samples=samples,
                events=selected,
                gaps=gaps,
            )

        initial = body(1, events[:4])
        pump.buffer.enqueue(
            GatewayBatch(body=initial, content_sha256=batch_digest(initial)),
            now=datetime.now(UTC),
            latest_read_seq=source_head,
        )
        assert await pump.deliver_once()
        preflight_started = time.perf_counter()
        for _ in range(20):
            rendered = await page.locator("[data-tag-id]").count()
            if rendered == 48 and len(browser_polls) >= 2:
                break
            await asyncio.sleep(0.5)
        assert rendered == 48 and len(browser_polls) >= 2, (
            "Normal browser login/tag rendering/polling preflight failed"
        )
        report["browser_preflight"] = {
            "login": "fifo-viewer",
            "rendered_tags": rendered,
            "screen_responses": len(browser_polls),
            "seconds": time.perf_counter() - preflight_started,
        }
        # Only after the short browser preflight, build representative original
        # production history and the full synthetic 1,800-capture backlog.
        for _ in range(20000):
            if runtime.storage._bounds()[1] >= 2927:
                break
            runtime.tick(now=datetime.now(UTC))
        await source_command("END_RUN")
        summary = await opc.get_run_summary(RunSummaryRequest(execution_id="run-A"))
        assert summary.final
        await server.publish_snapshot(acquired_at=datetime.now(UTC), heartbeat=5)
        events = await read_source_events()
        assert len(events) >= 2927
        source_head = events[-1].seq
        event_buckets = [[] for _ in range(1800)]
        for index, event in enumerate(events[10:]):
            event_buckets[index * 1800 // len(events[10:])].append(event)
        observation = await opc.read_tags()
        recovery_gap = (
            {
                "from_seq": 5,
                "through_seq": 10,
                "irrecoverable": False,
                "detected_at": datetime.now(UTC),
                "reason_code": "BUFFER_AGE_LIMIT",
                "reason": "Profile original source history replay required",
            },
        )
        preload_started = time.perf_counter()
        for seq in range(2, 1802):
            value = body(seq, event_buckets[seq - 2], recovery_gap if seq == 2 else ())
            pump.buffer.enqueue(
                GatewayBatch(body=value, content_sha256=batch_digest(value)),
                now=datetime.now(UTC),
                latest_read_seq=source_head,
            )
        report["preload_seconds"] = time.perf_counter() - preload_started
        for owner, name in (
            (pump.buffer, "enqueue"),
            (pump.buffer, "lease_next"),
            (pump.buffer, "acknowledge"),
            (intake, "deliver"),
            (opc, "read_tags"),
        ):
            timed(owner, name)
        opc.config = replace(opc.config, poll_interval_seconds=1.0)

        async def publish():
            heartbeat = 10
            while not stop.is_set():
                heartbeat += 1
                await server.publish_snapshot(acquired_at=datetime.now(UTC), heartbeat=heartbeat)
                await asyncio.sleep(0.1)

        publisher = asyncio.create_task(publish())
        run = asyncio.create_task(pump.run(stop))
        started = time.perf_counter()
        deadline = started + 300
        while time.perf_counter() < deadline:

            def observe():
                observe_started = time.perf_counter()
                with sqlite3.connect(f"file:{pump.buffer.path}?mode=ro", uri=True) as db:
                    sample_counts = db.execute(
                        "SELECT DISTINCT json_array_length(body,'$.body.samples') "
                        "FROM outbound_batches"
                    ).fetchall()
                    assert all(row[0] == 48 for row in sample_counts)
                    db.execute(
                        "SELECT status,count(*) FROM outbound_batches GROUP BY status"
                    ).fetchall()
                    db.execute(
                        "SELECT min(queued_at) FROM batch_ledger "
                        "JOIN outbound_batches USING(batch_id)"
                    ).fetchone()
                with service.engine.connect() as db:
                    db.execute(
                        text(
                            "SELECT pg_database_size(current_database()),"
                            "(SELECT coalesce(sum(size),0) FROM pg_ls_waldir())"
                        )
                    ).one()
                return time.perf_counter() - observe_started

            sql_observations.append(await asyncio.to_thread(observe))
            status = pump.buffer.status(now=datetime.now(UTC))
            snapshot_started = time.perf_counter()
            response = await user_http.get(
                "/api/v1/snapshot",
                params={
                    "line_id": LINE_ID,
                    "source_id": pump.source_id,
                    "stream_id": pump.stream_id,
                },
            )
            assert response.status_code == 200
            usable = await page.locator("[data-tag-id].usable").count()
            sample_value = measurements.setdefault(
                "user_snapshot_dom", {"calls": 0, "seconds": 0.0, "max": 0.0}
            )
            sample_value["calls"] += 1
            elapsed = time.perf_counter() - snapshot_started
            sample_value["seconds"] += elapsed
            sample_value["max"] = max(sample_value["max"], elapsed)
            progress = pump.buffer.progress(pump.source_id, pump.stream_id).api_progress
            report.update(
                seconds=time.perf_counter() - started,
                queue=status.__dict__,
                progress=progress.model_dump(mode="json"),
                measurements=measurements,
                observer_seconds=sum(sql_observations),
                observer_calls=len(sql_observations),
                source_head=source_head,
            )
            checkpoints = report.setdefault("checkpoints", [])
            sent = measurements.get("deliver", {}).get("calls", 0)
            if not checkpoints or sent >= checkpoints[-1]["sent"] + 150:
                checkpoints.append(
                    {
                        "sent": sent,
                        "seconds": report["seconds"],
                        "queue_batches": status.pending_batches + status.inflight_batches,
                        "queue_bytes": status.queue_bytes,
                        "verified": progress.verified_through_seq,
                        "source_head": source_head,
                    }
                )
            artifact.write_text(json.dumps(report, indent=2, default=str))
            if (
                usable == 48
                and status.pending_batches <= 1
                and status.inflight_batches <= 1
                and progress.verified_through_seq == source_head
                and not progress.unresolved_gaps
            ):
                report["outcome"] = "passed"
                break
            cutoff = int(os.environ.get("HMI_FIFO_PROFILE_BATCHES", "0"))
            if cutoff and measurements.get("deliver", {}).get("calls", 0) >= cutoff:
                report["outcome"] = "profiled_incomplete"
                break
            if run.done():
                run.result()
                raise AssertionError("Gateway stopped before draining FIFO")
            await asyncio.sleep(1)
        report.update(
            seconds=time.perf_counter() - started,
            queue=status.__dict__,
            progress=progress.model_dump(mode="json"),
            measurements=measurements,
            observer_seconds=sum(sql_observations),
            observer_calls=len(sql_observations),
        )
        if report["outcome"] == "running":
            report["outcome"] = "failed_deadline"
        assert report["outcome"] == "passed", {
            key: value for key, value in report.items() if key != "measurements"
        }
        assert report["seconds"] <= 300
        starts = measurements["read_tags"]["starts"]
        intervals = [right - left for left, right in zip(starts, starts[1:], strict=False)]
        report["browser_screen_responses"] = len(browser_polls)
        report["collection"] = {
            "configured_interval_seconds": opc.config.poll_interval_seconds,
            "captures": len(starts),
            "first_monotonic": starts[0],
            "last_monotonic": starts[-1],
            "interval_mean_seconds": sum(intervals) / len(intervals),
            "interval_max_seconds": max(intervals),
        }
        assert opc.config.poll_interval_seconds == 1.0 and intervals
        with service.engine.connect() as db:
            stored = db.execute(text("SELECT body FROM event_inbox ORDER BY seq")).scalars().all()
        assert stored == [event.model_dump(mode="json") for event in events]
        assert service.get_totals("run-A") == {
            key: getattr(summary, key)
            for key in ("good_quantity", "reject_quantity", "scrap_quantity")
        }
        request.node.user_properties.append(("recovery_profile", str(artifact)))
        validated = True
    except BaseException as error:
        report["phase_outcome"] = report["outcome"]
        report["outcome"] = "failed"
        report["failure_type"] = type(error).__name__
        raise
    finally:
        if report["outcome"] == "passed" and not validated:
            report["outcome"] = "failed_final_validation"
        stop.set()
        if run:
            await asyncio.wait_for(run, 10)
        if publisher:
            await publisher
        for owner, name, original in wrappers:
            setattr(owner, name, original)
        pump.intake = original_intake
        await intake.close()
        await user_http.aclose()
        if browser:
            await browser.close()
        if playwright:
            await playwright.stop()
        user_child.terminate()
        try:
            user_child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            user_child.kill()
            user_child.wait(timeout=5)
        user_log.close()
        if user_metrics.exists():
            report["user_api"] = json.loads(user_metrics.read_text())
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        log.close()
        if child_metrics.exists():
            report["api"] = json.loads(child_metrics.read_text())
        artifact.write_text(json.dumps(report, indent=2, default=str))
