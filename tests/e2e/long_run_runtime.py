"""Real-clock CLI long-run acceptance. Imports the existing live driver read-only.

The guarded AST extension pauses that driver's normal workflow while real services
continue running. No simulator clock, physical geometry or collection clock is mocked.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from acceptance_runtime import ROOT, AcceptanceEvidence, load_operable_script


class PrerequisiteBlocked(RuntimeError):
    """Required environment or announcement absent; no acceptance run started."""


def redact(value, credentials=()):
    for credential in credentials:
        value = value.replace(credential, "[REDACTED]")
    return re.sub(r"(?im)(cookie|authorization|x-csrf-token):[^\n]+", r"\1: [REDACTED]", value)


def finish(evidence, outcome, failure=None):
    evidence.finish(outcome, failure=failure)
    suite = ET.Element(
        "testsuite",
        name=evidence.data["case_id"],
        tests="1",
        failures="1" if outcome == "failed" else "0",
        skipped="1" if outcome == "blocked" else "0",
    )
    case = ET.SubElement(suite, "testcase", name=evidence.data["case_id"])
    if outcome != "passed":
        ET.SubElement(
            case, "skipped" if outcome == "blocked" else "failure", message=failure or outcome
        ).text = failure
    ET.ElementTree(suite).write(
        evidence.path.with_suffix(".xml"), encoding="utf-8", xml_declaration=True
    )


def checkpoint(evidence):
    """Durable in-progress metadata; process interruption cannot look like a pass."""
    evidence.path.write_text(
        json.dumps(
            {**evidence.data, "outcome": "running", "exit_code": None},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        + "\n"
    )


def distribution(values):
    ordered = sorted(values)
    if not ordered or any(not math.isfinite(v) or v < 0 for v in ordered):
        raise ValueError("A latency distribution requires finite nonnegative observations")
    return {
        "samples": len(ordered),
        "p50": ordered[math.ceil(len(ordered) * 0.5) - 1],
        "p95": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "max": ordered[-1],
    }


def extended_driver(hook, evidence):
    module = load_operable_script()
    path = ROOT / "scripts/check-operable-system.py"
    source = path.read_text()
    replacements = {
        '"target": "20"': '"target": "4294967295"',
        "timedelta(hours=1)": "timedelta(hours=12)",
        '".25"': '"1.0"',
    }
    for before, after in replacements.items():
        if source.count(before) != 1:
            raise RuntimeError("Live driver changed: long-run source anchor is ambiguous")
        source = source.replace(before, after)
    tree = ast.parse(source, filename=str(path))
    inserted, clock_policies = 0, 0
    for node in ast.walk(tree):
        if isinstance(node, ast.List) and any(
            isinstance(item, ast.Constant) and item.value == "live-service" for item in node.elts
        ):
            if any(
                isinstance(item, ast.Constant) and item.value == "--trend-clock-policy"
                for item in node.elts
            ):
                raise RuntimeError("Live driver clock policy anchor changed")
            node.elts.extend([ast.Constant("--trend-clock-policy"), ast.Constant("same-host")])
            clock_policies += 1
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            if (
                isinstance(call.func, ast.Name)
                and call.func.id == "checkpoint"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and call.args[0].value == "PAUSE through browser and verify actual PLC PAUSED"
            ):
                node.value = ast.Await(
                    value=ast.Call(
                        func=ast.Name(id="long_run_hook", ctx=ast.Load()),
                        args=[
                            ast.Call(
                                func=ast.Name(id="locals", ctx=ast.Load()), args=[], keywords=[]
                            )
                        ],
                        keywords=[],
                    )
                )
                inserted += 1
    if inserted != 1 or clock_policies != 1:
        raise RuntimeError("Live driver changed: running-production hook not found exactly once")
    module.long_run_hook = hook
    exec(compile(ast.fix_missing_locations(tree), str(path), "exec"), module.__dict__)
    evidence.record(
        driver_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        driver_extensions={
            **replacements,
            "running_checkpoint": "await long_run_hook(locals())",
            "internal_api_clock_policy": "same-host: actual same-kernel CLI stack",
        },
        physics="unaltered CLI default 100ms; collection 48 tags at 1Hz",
        credential_policy="disposable local service token expires after 12h",
    )
    return module


class Observation:
    def __init__(self, live, output):
        from apps.api.database import create_database_engine

        self.live, self.output = live, output
        self.engine = create_database_engine(live["url"])
        self.previous_cpu = None
        self.previous_cgroup_cpu = None
        self.expected_unavailable = None

    def sql(self, statement):
        from sqlalchemy import text

        with self.engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(text(statement))]

    def source(self):
        with sqlite3.connect(f"file:{self.live['sim_db']}?mode=ro", uri=True) as db:
            return [
                json.loads(row[0])
                for row in db.execute("SELECT body FROM source_events ORDER BY seq")
            ]

    def sample(self):
        # The runtime owns the buffer lock. Never reopen GatewayBuffer (which writes
        # recovery state); inspect the WAL-consistent SQLite snapshot read-only.
        with sqlite3.connect(f"file:{self.live['queue']}?mode=ro", uri=True) as db:
            counts = dict(
                db.execute("SELECT status,count(*) FROM outbound_batches GROUP BY status")
            )
            status = {
                "pending_batches": counts.get("PENDING", 0),
                "inflight_batches": counts.get("INFLIGHT", 0),
                "queue_bytes": db.execute(
                    "SELECT coalesce(sum(accounted_bytes),0) FROM outbound_batches"
                ).fetchone()[0],
            }
            first = db.execute(
                "SELECT min(queued_at) FROM batch_ledger JOIN outbound_batches USING(batch_id)"
            ).fetchone()[0]
            status["oldest_age_seconds"] = (
                (datetime.now(UTC) - datetime.fromisoformat(first)).total_seconds() if first else 0
            )
            capture = [
                int(v)
                for v in db.execute(
                    "SELECT capture_generation,last_capture_seq FROM gateway_identity"
                ).fetchone()
            ]
            losses = [json.loads(row[0]) for row in db.execute("SELECT body FROM missing_ranges")]
            status["loss_records"] = len(losses)
            sample_counts = db.execute(
                "SELECT DISTINCT json_array_length(body,'$.body.samples') FROM outbound_batches"
            ).fetchall()
            status["outbound_sample_counts"] = [row[0] for row in sample_counts]
            if any(row[0] != 48 for row in sample_counts):
                raise AssertionError("An actual outbound capture did not contain 48 tags")
            for suffix, name in (("", "database_bytes"), ("-wal", "wal_bytes")):
                path = Path(str(self.live["queue"]) + suffix)
                status[name] = path.stat().st_size if path.exists() else 0
        cpu, rss = 0, 0
        for child in self.live["children"]:
            if child.poll() is not None:
                if child is self.expected_unavailable:
                    continue
                raise RuntimeError("A supervised CLI service exited")
            fields = Path(f"/proc/{child.pid}/stat").read_text().split(") ", 1)[1].split()
            cpu += (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
            rss += int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
        now = time.monotonic()
        percent = (
            None
            if self.previous_cpu is None
            else 100 * (cpu - self.previous_cpu[1]) / (now - self.previous_cpu[0])
        )
        self.previous_cpu = (now, cpu)
        with sqlite3.connect(f"file:{self.live['sim_db']}?mode=ro", uri=True) as db:
            source_progress = dict(
                zip(
                    ("events", "head_seq"),
                    db.execute("SELECT count(*),max(seq) FROM source_events").fetchone(),
                    strict=True,
                )
            )
            source_progress["process_tick"] = max(
                int(row[0]) for row in db.execute("SELECT tick FROM executions")
            )
        # Session-cgroup counters include runner/browser children. Host meminfo is
        # recorded separately, since a shared host is not the test's memory quota.
        cgroup = Path("/sys/fs/cgroup")
        cgroup_usage = dict(line.split() for line in (cgroup / "cpu.stat").read_text().splitlines())
        cgroup_cpu = int(cgroup_usage["usage_usec"]) / 1_000_000
        cgroup_percent = (
            None
            if self.previous_cgroup_cpu is None
            else (
                100
                * (cgroup_cpu - self.previous_cgroup_cpu[1])
                / (now - self.previous_cgroup_cpu[0])
            )
        )
        self.previous_cgroup_cpu = (now, cgroup_cpu)
        cgroup_metrics = {
            "cpu_percent": cgroup_percent,
            "memory_current_bytes": int((cgroup / "memory.current").read_text()),
            "memory_peak_bytes": int((cgroup / "memory.peak").read_text()),
        }
        sizes = self.sql(
            "SELECT pg_database_size(current_database()) AS database_bytes, "
            "(SELECT coalesce(sum(size),0) FROM pg_ls_waldir()) AS wal_bytes"
        )[0]
        value = {
            "utc": datetime.now(UTC).isoformat(),
            "monotonic": now,
            "cpu_percent_cli_processes": percent,
            "rss_bytes_cli_processes": rss,
            "gateway": status,
            "capture_position": capture,
            "losses": losses,
            "postgres": sizes,
            "source_progress": source_progress,
            "session_cgroup": cgroup_metrics,
            "host_meminfo": Path("/proc/meminfo").read_text(),
        }
        with (self.output / "resources.jsonl").open("a") as handle:
            handle.write(json.dumps(value, default=str) + "\n")
        if status["queue_bytes"] > 100_000_000:
            raise AssertionError("Gateway exceeded unchanged 100MB queue budget")
        if status["oldest_age_seconds"] > 1802:
            raise AssertionError(
                "Gateway retained an outbound payload beyond 30min + 2s scan margin"
            )
        return value

    def reconcile(self, target):
        source = self.source()
        inbox = self.sql(
            "SELECT event_id,seq,execution_id,body,applied_at FROM event_inbox ORDER BY seq"
        )
        by_id = {row["event_id"]: row for row in inbox}
        errors = [
            item["event_id"]
            for item in source
            if int(item["seq"]) <= target
            and (
                item["event_id"] not in by_id
                or by_id[item["event_id"]]["body"] != item
                or by_id[item["event_id"]]["applied_at"] is None
            )
        ]
        cursors = self.sql("SELECT * FROM stream_progress")
        gaps = self.sql("SELECT * FROM stream_gaps")
        result = {
            "source_events": source,
            "inbox_events": inbox,
            "cursors": cursors,
            "gaps": gaps,
            "mismatched_original_ids": errors,
            "target_sequence": target,
        }
        (self.output / "reconciliation.json").write_text(json.dumps(result, indent=2, default=str))
        return (
            not errors
            and len(cursors) == 1
            and int(cursors[0]["verified_through_seq"]) >= target
            and not gaps
        )


def recovery_elapsed(evidence, started):
    elapsed = time.monotonic() - started
    evidence.check("recovery_completed_within_300_seconds", elapsed <= 300, seconds=elapsed)
    return elapsed


async def outage(live, evidence, output):
    observer = Observation(live, output)
    try:
        await _outage(live, evidence, output, observer)
    except BaseException:
        # This is the already-owned engine and source path, before driver cleanup.
        # No credential discovery or additional environment is needed.
        try:
            source = observer.source()
            target = max(int(event["seq"]) for event in source)
            complete = observer.reconcile(target)
            with sqlite3.connect(f"file:{live['queue']}?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                ack = [dict(row) for row in db.execute("SELECT * FROM source_progress")]
            evidence.record(
                failure_reconciliation_complete=complete,
                failure_reconciliation_path=str(output / "reconciliation.json"),
                failure_gateway_ack_progress=ack,
            )
        except BaseException as error:
            evidence.record(failure_snapshot_error=redact(f"{type(error).__name__}: {error}"))
        raise
    finally:
        observer.engine.dispose()


async def _outage(live, evidence, output, observer):
    api = next(
        child for child in live["children"] if "serve" in child.args and "apps.api" in child.args
    )
    first = observer.sample()
    api_index = live["children"].index(api)
    api_log = next(
        handle for handle in live["handles"] if Path(handle.name).name == "internal-api.log"
    )
    observer.expected_unavailable = api
    try:
        api.terminate()
        try:
            await asyncio.to_thread(api.wait, timeout=10)
        except subprocess.TimeoutExpired:
            api.kill()
            await asyncio.to_thread(api.wait, timeout=5)
        # Start the real 30min only after the old process has exited and its TCP
        # listener refuses a fresh connection. TIME_WAIT is not a live listener.
        try:
            connection = socket.create_connection(("127.0.0.1", live["intake_port"]), timeout=0.5)
        except ConnectionRefusedError:
            pass
        else:
            connection.close()
            raise AssertionError("Disposable API port remained open after process exit")
        started = time.monotonic()
        evidence.record(
            outage_started_at_utc=datetime.now(UTC).isoformat(),
            outage_method="terminate disposable internal API; TCP listener confirmed closed",
        )
        checkpoint(evidence)
        while time.monotonic() - started < 1800:
            await asyncio.sleep(min(5, 1800 - (time.monotonic() - started)))
            observer.sample()
            simulator_session = await live["sim_context"].request.get(
                live["sim_origin"] + "/snapshot", headers={"Connection": "close"}, max_retries=2
            )
            if simulator_session.status != 200:
                raise AssertionError("Active simulator operator session expired during outage")
            if not (await simulator_session.json())["state"]["run_output"]:
                raise AssertionError("Actual simulator stopped production during API outage")
            session = await live["context"].request.get(
                live["origin"] + "/auth/me", headers={"Connection": "close"}, max_retries=2
            )
            if session.status != 200:
                raise AssertionError("Active operator session expired during outage")
        disconnected = time.monotonic() - started
        last = observer.sample()
        source = observer.source()
        target = max(int(event["seq"]) for event in source)
        evidence.check(
            "actual_outage_at_least_30_minutes", disconnected >= 1800, seconds=disconnected
        )
        evidence.check(
            "collection_continued_at_1Hz",
            last["capture_position"][1] - first["capture_position"][1] >= 1780,
            first=first["capture_position"],
            last=last["capture_position"],
        )
        evidence.check(
            "real_default_process_continued",
            last["source_progress"]["process_tick"] - first["source_progress"]["process_tick"] > 0,
            first=first["source_progress"],
            last=last["source_progress"],
        )
    finally:
        recovered = time.monotonic()
        if api.poll() is not None:
            # Same command/environment/token/DB and same open log handle; replace
            # the child in the driver's owned list so its cleanup supervises it.
            live["children"][api_index] = subprocess.Popen(
                api.args, cwd=ROOT, env=live["env"], stdout=api_log, stderr=api_log
            )
        observer.expected_unavailable = None
    evidence.record(reconnected_at_utc=datetime.now(UTC).isoformat())
    checkpoint(evidence)
    while time.monotonic() - recovered <= 300:
        sample = observer.sample()
        response = await live["context"].request.get(
            live["origin"] + "/api/v1/snapshot",
            params={
                "line_id": live["LINE_ID"],
                "source_id": live["identity"].source_id,
                "stream_id": live["identity"].stream_id,
            },
        )
        snapshot = await response.json() if response.status == 200 else {}
        # Freshness is independently checked against all 48 rendered API tag samples.
        latest = json.dumps(snapshot)
        if (
            sample["gateway"]["pending_batches"] <= 1
            and sample["gateway"]["inflight_batches"] <= 1
            and observer.reconcile(target)
            and response.status == 200
            and len(snapshot.get("evidence", [])) == 48
            and all(item["fresh"] for item in snapshot["evidence"])
            and await live["page"].locator("[data-tag-id].usable").count() == 48
        ):
            elapsed = recovery_elapsed(evidence, recovered)
            evidence.record(
                recovery_seconds=elapsed,
                latest_snapshot=snapshot,
                outage_seconds=disconnected,
                losses_at_reconnect=last["losses"],
            )
            evidence.check(
                "latest_48_tag_screen_recovered",
                await live["page"].locator("[data-tag-id].usable").count() == 48,
                snapshot_status=response.status,
                snapshot_bytes=len(latest),
            )
            return
        await asyncio.sleep(1)
    raise AssertionError(
        "Original events/cursor/gaps, backlog and latest state did not recover in 300 real seconds"
    )


async def endurance(live, evidence, output):
    observer = Observation(live, output)
    contexts, pages = [], []
    screen_latencies, alarm_latencies = [], []

    def latency_record(kind, identifier, original_at, displayed_at, session, **fields):
        with (output / "latency-observations.jsonl").open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "kind": kind,
                        "original_id": identifier,
                        "original_at": str(original_at),
                        "display_observed_at": str(displayed_at),
                        "session_index": session,
                        "seconds_upper_bound": (displayed_at - original_at).total_seconds(),
                        **fields,
                    }
                )
                + "\n"
            )

    async def login_page():
        context = await live["browser"].new_context(locale="ko-KR")
        try:
            page = await context.new_page()
            await page.goto(live["origin"])
            await page.locator("#login").fill("live-operator")
            await page.locator("#login-password").fill(live["changed"])
            await page.locator("#login-submit").click()
            await page.locator('[data-process-value="Line.GoodTotal"]').wait_for()
            return context, page
        except BaseException:
            await context.close()
            raise

    try:
        for _ in range(10):
            context, page = await login_page()
            contexts.append(context)
            pages.append(page)
        started = time.monotonic()
        renew_at = started + 14400
        first_sample = observer.sample()
        previous_alarm_ids = {
            row["alarm_id"] for row in observer.sql("SELECT alarm_id FROM alarm_instances")
        }
        while time.monotonic() - started < 28800:
            cycle = time.monotonic()
            if cycle >= renew_at:
                # Honor the real 8h absolute authentication policy. Establish a
                # replacement before retiring each old page, keeping >=10 alive.
                for index in range(10):
                    replacement, page = await login_page()
                    await contexts[index].close()
                    contexts[index], pages[index] = replacement, page
                await live["context"].add_cookies(await contexts[0].cookies())
                await live["page"].reload()
                sim_context = await live["browser"].new_context()
                try:
                    sim_page = await sim_context.new_page()
                    await sim_page.goto(live["sim_origin"])
                    await sim_page.locator("#login-form [name=login]").fill("live-operator")
                    await sim_page.locator("#login-form [name=password]").fill(live["sim_changed"])
                    await sim_page.locator("#login-form button").click()
                    await sim_page.locator("#login-panel").wait_for(state="hidden")
                    await live["sim_context"].add_cookies(await sim_context.cookies())
                    await live["sim"].reload()
                finally:
                    await sim_context.close()
                evidence.record(authentication_renewed_at_utc=datetime.now(UTC).isoformat())
                renew_at += 14400
            observer.sample()
            active = await live["context"].request.get(live["origin"] + "/auth/me")
            if active.status != 200:
                raise AssertionError("Original command operator session expired")
            snapshot = await live["sim_snapshot"]()
            source = observer.source()
            goods = [item for item in source if item["kind"] == "PRODUCTION_GOOD"]
            if not goods:
                raise AssertionError("No original PLC production events")
            value = int(snapshot["tags"]["Line.GoodTotal"])
            total, selected = 0, None
            for event in goods:
                total += int(event["payload"]["quantity"])
                if total == value:
                    selected = event
                    break
            if selected is None:
                raise AssertionError("Displayed target count has no exact original PLC event")
            occurred = datetime.fromisoformat(selected["occurred_at"].replace("Z", "+00:00"))

            async def screen(page, index, value=value, occurred=occurred, selected=selected):
                await page.wait_for_function(
                    "v => Number(document.querySelector("
                    "'[data-process-value=\"Line.GoodTotal\"]')"
                    ".textContent.replaceAll(',', '')) >= v",
                    arg=value,
                    timeout=10000,
                )
                displayed = datetime.now(UTC)
                screen_latencies.append((displayed - occurred).total_seconds())
                latency_record(
                    "PLC_TO_DOM",
                    selected["event_id"],
                    occurred,
                    displayed,
                    index,
                    original_seq=selected["seq"],
                    target_good_total=value,
                )
                response = await page.request.get(live["origin"] + "/api/v1/sources")
                if response.status != 200:
                    raise AssertionError("Concurrent authenticated query failed")
                active = await page.request.get(live["origin"] + "/auth/me")
                if active.status != 200:
                    raise AssertionError("Concurrent active browser session expired")

            await asyncio.gather(*(screen(page, index) for index, page in enumerate(pages)))
            # Real sensor offset crosses the installed analog rule without changing physical heat.
            sim = live["sim"]
            await sim.locator("#inputs-form [name=temperature_offset_c]").fill("30")
            await sim.locator("#inputs-form button[type=submit]").click()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                rows = observer.sql(
                    "SELECT alarm_id,occurred_at FROM alarm_instances ORDER BY occurred_at DESC"
                )
                new = [row for row in rows if row["alarm_id"] not in previous_alarm_ids]
                if new:
                    row = new[0]

                    async def alarm(page, index, row=row):
                        await page.locator(f'[data-alarm-id="{row["alarm_id"]}"]').wait_for(
                            timeout=10000
                        )
                        displayed = datetime.now(UTC)
                        alarm_latencies.append((displayed - row["occurred_at"]).total_seconds())
                        latency_record(
                            "QUALIFIED_ALARM_TO_DOM",
                            row["alarm_id"],
                            row["occurred_at"],
                            displayed,
                            index,
                        )

                    await asyncio.gather(*(alarm(page, index) for index, page in enumerate(pages)))
                    previous_alarm_ids.update(item["alarm_id"] for item in rows)
                    break
                await asyncio.sleep(0.2)
            else:
                raise AssertionError("Actual analog condition produced no qualified alarm")
            await sim.locator("#inputs-form [name=temperature_offset_c]").fill("0")
            await sim.locator("#inputs-form button[type=submit]").click()
            (output / "latencies.json").write_text(
                json.dumps(
                    {
                        "plc_to_dom_upper_bound_seconds": screen_latencies,
                        "qualified_alarm_to_dom_upper_bound_seconds": alarm_latencies,
                    }
                )
            )
            await asyncio.sleep(
                max(0, min(60 - (time.monotonic() - cycle), 28800 - (time.monotonic() - started)))
            )
        elapsed = time.monotonic() - started
        last_sample = observer.sample()
        target = max(int(event["seq"]) for event in observer.source())
        evidence.check("real_8_hours", elapsed >= 28800, seconds=elapsed)
        evidence.check(
            "collection_maintained_1Hz",
            last_sample["capture_position"][1] - first_sample["capture_position"][1] >= 28400,
            first=first_sample["capture_position"],
            last=last_sample["capture_position"],
        )
        evidence.check("original_event_cursor_gap_reconciliation", observer.reconcile(target))
        for name, values, maximum in (
            ("plc_to_dom", screen_latencies, 2),
            ("qualified_alarm_to_dom", alarm_latencies, 3),
        ):
            measured = distribution(values)
            evidence.record(**{name: measured})
            evidence.check(name + "_p95", measured["p95"] <= maximum, **measured)
    finally:
        for context in contexts:
            await context.close()
        observer.engine.dispose()


def main(case):
    parser = argparse.ArgumentParser(
        description="Real-clock disposable local " + case + " acceptance"
    )
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--chromium", default=shutil.which("chromium"))
    parser.add_argument("--web-dir", type=Path, default=ROOT / "apps/web/dist")
    parser.add_argument("--hardware-evidence", type=Path)
    parser.add_argument("--start-announced-at-utc")
    args = parser.parse_args()
    output = args.artifacts.resolve()
    output.mkdir(parents=True, exist_ok=True)
    evidence = AcceptanceEvidence(case, output)
    evidence.record(
        fixed_criteria={
            "real_duration_seconds": 1800 if case == "CX-19" else 28800,
            "collection_tags": 48,
            "collection_period_seconds": 1,
            "default_physical_tick_seconds": 0.1,
            "recovery_deadline_seconds": 300,
            "queue_max_age_seconds": 1800,
            "queue_max_bytes": 100_000_000,
            "browser_sessions": 10 if case == "CX-21" else 1,
            "plc_to_dom_p95_seconds": 2,
            "qualified_alarm_to_dom_p95_seconds": 3,
        }
    )
    try:
        if case == "CX-21":
            hardware = (
                json.loads(args.hardware_evidence.read_text()) if args.hardware_evidence else {}
            )
            evidence.record(hardware=hardware, user_start_announcement=args.start_announced_at_utc)
            try:
                evidence.check(
                    "hardware_requirement_established",
                    hardware.get("effective_cpu_cores", 0) >= 4
                    and hardware.get("effective_memory_bytes", 0) >= 8_589_934_592
                    and hardware.get("ssd_proven") is True,
                    cpu_cores=hardware.get("effective_cpu_cores"),
                    memory_bytes=hardware.get("effective_memory_bytes"),
                    ssd_proven=hardware.get("ssd_proven"),
                )
                evidence.check("start_announced_to_user", bool(args.start_announced_at_utc))
            except AssertionError as error:
                raise PrerequisiteBlocked(str(error)) from error
        if not args.chromium or not (args.web_dir / "index.html").is_file():
            raise RuntimeError("Chromium and prebuilt polling web distribution required")

        live_credentials = []

        async def hook(live):
            live_credentials.extend(live["credentials"])
            evidence.record(
                executions=[live["execution_id"]],
                source_id=live["identity"].source_id,
                stream_id=live["identity"].stream_id,
                commands=dict(live["commands"]),
            )
            await (outage if case == "CX-19" else endurance)(live, evidence, output)

        driver = extended_driver(hook, evidence)
        checkpoint(evidence)
        with tempfile.TemporaryDirectory(prefix="hmi-long-run-") as runtime:
            try:
                asyncio.run(
                    driver.exercise(
                        Path(runtime),
                        args.chromium,
                        args.web_dir.resolve(),
                        output / "final.png",
                        output / "final-settlement.json",
                    )
                )
            finally:
                evidence.record(driver_stage=driver.STAGE)
                if live_credentials:
                    for log in Path(runtime).glob("*.log"):
                        safe = redact(log.read_text(errors="replace"), live_credentials)
                        (output / log.name).write_text(safe)
        settled = json.loads((output / "final-settlement.json").read_text())
        evidence.record(
            commands=settled["commands"],
            reports=settled,
            final_sequences=settled["simulator_final_summary"],
        )
        finish(evidence, "passed")
        return 0
    except BaseException as error:
        safe_failure = redact(
            f"{type(error).__name__}: {error}", locals().get("live_credentials", [])
        )
        finish(
            evidence,
            "blocked" if isinstance(error, PrerequisiteBlocked) else "failed",
            failure=safe_failure,
        )
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        status = "BLOCKED" if isinstance(error, PrerequisiteBlocked) else "FAIL"
        print(f"{status}: {case}: {safe_failure}")
        return 1
