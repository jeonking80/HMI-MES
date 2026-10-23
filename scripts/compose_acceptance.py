"""Stream into the disposable Compose api container; never run on a public plant.

Only explicit account provisioning touches storage directly. All commissioning,
work registration, commands and settlement checks use the existing HTTP APIs.
No simulator SQL, telemetry injection or direct PLC state mutation is used.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

PHASE = "setup"


def wait(getter, predicate, description, seconds=45):
    deadline = time.monotonic() + seconds
    while True:
        value = getter()
        if predicate(value):
            return value
        if time.monotonic() >= deadline:
            raise RuntimeError(description)
        time.sleep(0.25)


def protected(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(value)
    return path


def exercise():
    global PHASE
    # This module is streamed only to a new isolated project's api container.
    from deploy.compose_runtime import ORIGIN, ROOT, database
    from deploy.plant_runtime import CHECKS, IDENTITY, LOGIN, commission
    from deploy.plant_runtime import protected as read_secret

    if ROOT != Path("/var/lib/hmi") or not (ROOT / "runtime/runtime.json").is_file():
        raise RuntimeError("Disposable Compose runtime required")
    os.umask(0o077)
    initial = secrets.token_urlsafe(32)
    changed = secrets.token_urlsafe(32)
    initial_file = protected(ROOT / "acceptance-operator-initial", initial)
    protected(ROOT / "acceptance-operator-password", changed)
    PHASE = "operator bootstrap"
    database()  # Select only this project's local postgres, ignoring deployment DSNs.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "apps.api",
            "bootstrap-user",
            "--login",
            "compose.acceptance",
            "--role",
            "operator",
            "--role",
            "production_manager",
            "--origin",
            ORIGIN,
            "--password-file",
            str(initial_file),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    runtime = ROOT / "runtime"
    PHASE = "simulator commissioning"
    commission(
        runtime,
        password_file=runtime / "console-initial-password",
        new_password_file=runtime / "console-password",
        checks=dict.fromkeys(CHECKS, True),
    )
    scope = {key: IDENTITY[key] for key in ("line_id", "source_id", "stream_id")}
    with (
        httpx.Client(base_url=ORIGIN, timeout=10, trust_env=False) as api,
        httpx.Client(
            base_url="http://127.0.0.1:8011",
            timeout=10,
            trust_env=False,
        ) as simulator,
    ):

        def post(client, origin, path, body, csrf="", extra=None):
            response = client.post(
                path, json=body, headers={"Origin": origin, "X-CSRF-Token": csrf, **(extra or {})}
            )
            if response.status_code != 200:
                raise RuntimeError(f"HTTP mutation rejected: {path} ({response.status_code})")
            return response.json()

        def get(path, params=None):
            response = api.get(path, params=params)
            if response.status_code != 200:
                raise RuntimeError(f"HTTP read rejected: {path} ({response.status_code})")
            return response.json()

        PHASE = "operator authentication"
        challenge = post(api, ORIGIN, "/auth/challenge", {}, extra={"X-HMI-Request": "1"})
        session = post(
            api,
            ORIGIN,
            "/auth/login",
            {"login": "compose.acceptance", "password": initial},
            challenge["csrf_token"],
        )
        if not session["must_change_password"]:
            raise RuntimeError("Disposable operator must require initial password change")
        session = post(
            api, ORIGIN, "/auth/password", {"new_password": changed}, session["csrf_token"]
        )
        csrf = session["csrf_token"]
        PHASE = "simulator authentication"
        sim_origin = "http://127.0.0.1:8011"
        challenge = post(
            simulator, sim_origin, "/auth/challenge", {}, extra={"X-Simulator-Request": "1"}
        )
        post(
            simulator,
            sim_origin,
            "/auth/login",
            {"login": LOGIN, "password": read_secret(runtime / "console-password")},
            challenge["csrf_token"],
        )

        def sim():
            response = simulator.get("/snapshot")
            response.raise_for_status()
            return response.json()

        def snapshot():
            return get("/api/v1/snapshot", scope)

        def values():
            return {
                sample["tag_id"]: sample["reading"]["value"]
                for sample in snapshot()["screen"]["view"]["samples"]
            }

        PHASE = "analytics config"
        if get("/auth/config").get("analytics") is not True:
            raise RuntimeError("Analytics are unavailable in the composed app")
        PHASE = "recipe registration"
        post(
            api,
            ORIGIN,
            "/api/v1/recipe-versions",
            {
                "id": "compose-recipe-v1",
                "recipe_id": "compose-recipe",
                "version": "v1",
                "item_id": "compose-item",
                "parameters": {"temperature_c": 150.0, "speed_m_per_min": 10.0},
            },
            csrf,
        )
        PHASE = "order registration"
        order = post(
            api,
            ORIGIN,
            "/api/v1/work-orders",
            {
                "id": "compose-order",
                "item_id": "compose-item",
                "line_id": scope["line_id"],
                "recipe_version_id": "compose-recipe-v1",
                "target_quantity": 20,
                "planned_date": datetime.now(UTC).date().isoformat(),
            },
            csrf,
        )
        PHASE = "order release"
        released = post(
            api,
            ORIGIN,
            "/api/v1/work-orders/compose-order/release",
            {
                "release_id": "compose-release",
                "expected_row_version": order["row_version"],
            },
            csrf,
        )
        # Ensure the normal gateway has observed the explicit simulator commissioning.
        wait(
            values,
            lambda v: v["Line.ControlMode"] == 1 and v["Line.OperationMode"] == 1,
            "Gateway did not observe REMOTE/AUTO",
        )
        PHASE = "order binding"
        bound = post(
            api,
            ORIGIN,
            "/api/v1/work-orders/compose-order/bind",
            {
                **scope,
                "command_id": "compose-load",
                "expected_work_order_version": released["row_version"],
                "expected_control_version": values()["Line.ControlVersion"],
            },
            csrf,
        )
        execution = bound["execution_id"]

        def command_done(identity):
            def read():
                return get("/api/v1/commands/" + identity, scope)["result"]

            result = wait(
                read,
                lambda r: r["status"] in {"SUCCEEDED", "FAILED", "REJECTED", "EXPIRED"},
                "Gateway command did not complete",
            )
            if result["status"] != "SUCCEEDED":
                raise RuntimeError("PLC command did not succeed: " + identity)

        PHASE = "load result and preheat"
        command_done("compose-load")
        wait(sim, lambda s: s["state"]["state"] == "READY", "Actual physics did not reach READY")
        wait(values, lambda v: v["Line.State"] == 1, "API did not observe READY")

        def command(kind):
            at = datetime.now(UTC)
            identity = "compose-" + kind.lower()
            post(
                api,
                ORIGIN,
                "/api/v1/commands",
                {
                    **scope,
                    "request": {
                        "schema_version": "1.0",
                        "command_id": identity,
                        "target_id": "Line",
                        "execution_id": execution,
                        "command": {"kind": kind, "args": {}},
                        "requested_by": "compose.acceptance",
                        "requested_at": at.isoformat(),
                        "expires_at": (at + timedelta(seconds=5)).isoformat(),
                        "expected_control_version": values()["Line.ControlVersion"],
                    },
                },
                csrf,
            )
            command_done(identity)

        PHASE = "start and production"
        command("START")
        wait(
            sim,
            lambda s: int(s["tags"]["Line.GoodTotal"]) >= 2,
            "Actual process produced fewer than two good items",
            seconds=30,
        )
        # Keep enough real RUNNING captures for the report's bounded interval evidence.
        time.sleep(3)
        PHASE = "pause"
        command("PAUSE")
        wait(sim, lambda s: s["state"]["state"] == "PAUSED", "PLC did not pause")
        wait(values, lambda v: v["Line.State"] == 3, "API did not observe PAUSED")
        PHASE = "end and settlement"
        command("END_RUN")
        record = wait(
            lambda: get("/api/v1/execution-record", {**scope, "execution_id": execution}),
            lambda r: r["settlement"]["status"] == "SETTLED",
            "Execution did not settle",
        )
        PHASE = "timeline"
        timeline = get(f"/api/v1/reports/executions/{execution}/timeline", scope)
        running = timeline["durations_seconds"]["running"]
        unknown = {s.get("unknown_reason") for s in timeline["segments"] if s["class"] == "UNKNOWN"}
        if running <= 0 or not unknown <= {"NO_EVIDENCE"}:
            raise RuntimeError("Actual execution report lacks proved RUNNING intervals")
        end = datetime.now(UTC)
        PHASE = "trend"
        trends = get(
            "/api/v1/trends",
            {
                **scope,
                "tag": "Sealer.TemperaturePV",
                "from": (end - timedelta(minutes=3)).isoformat(),
                "to": end.isoformat(),
                "resolution": "second",
            },
        )
        good = sum(point["quality"] == "GOOD" for point in trends["series"][0]["points"])
        if not good:
            raise RuntimeError("Same-host Compose capture produced no GOOD trend points")
        if int(record["settlement"]["summary"]["good_quantity"]) < 2:
            raise RuntimeError("Settled run does not contain actual good production")
        return {
            "analytics": True,
            "good_trend_points": good,
            "running_seconds": running,
            "unknown_reasons": sorted(unknown),
            "settlement": "SETTLED",
        }


if __name__ == "__main__":
    try:
        print(json.dumps(exercise()))
    except Exception as error:
        # Only fixed phase/type identifiers; never echo request bodies or secrets.
        print(
            json.dumps(
                {
                    "failed_stage": PHASE,
                    "error_type": type(error).__name__,
                    "reason": str(error) if type(error) is RuntimeError else "operation failed",
                }
            )
        )
        raise SystemExit(1) from None
