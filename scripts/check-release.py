#!/usr/bin/env python3
"""Reproduce a committed release in an isolated clone and disposable Compose project.

Default is preparation only (exit 2). --run performs actual no-cache builds and
runtime checks; no cached-image substitute can turn an unexecuted phase green.
Build output is hashed, never printed or persisted, because proxy diagnostics
may contain credentials. The optional override may alter build configuration
and build-only secrets, but cannot change runtime services or bind current code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]
PHASES = (
    "clean_checkout",
    "no_cache_build",
    "image_manifest",
    "compose_whole",
    "data_preserving_restart",
    "independent_cli",
    "cx17_bundle",
    "cleanup",
)

# Read only fixed nonsecret state, while simulator and gateway writers are stopped.
INVENTORY = """
import hashlib,json,sqlite3
from pathlib import Path
from sqlalchemy import text
from deploy.compose_runtime import database
from apps.api.database import create_database_engine

database()
engine=create_database_engine()
def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),default=str)
def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()
result={}
with engine.connect() as db:
    for table in ("production_executions","execution_totals","production_events",
                  "command_requests","alarm_instances","stream_progress","stream_gaps"):
        rows=db.execute(text("SELECT * FROM "+table)).mappings()
        result["pg:"+table]=sorted(canonical(dict(row)) for row in rows)
sim_tables=("simulator_identity","executions","source_events","command_journal","runtime_runs")
for name,tables in (("simulator",sim_tables),
                    ("gateway",("gateway_identity","source_progress","missing_ranges"))):
    path=Path("/var/lib/hmi/runtime")/(name+".sqlite3")
    with sqlite3.connect(path.as_uri()+"?mode=ro",uri=True) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
        for table in tables:
            query="SELECT * FROM "+table
            if table=="simulator_identity":
                query=("SELECT source_id,stream_id,counter_epoch,active_execution_id "
                       "FROM simulator_identity")
            elif table=="gateway_identity":
                query="SELECT gateway_id,capture_generation FROM gateway_identity"
            rows=db.execute(query)
            result[name+":"+table]=sorted(canonical(row) for row in rows)
        if name=="simulator":
            query="SELECT snapshot_json FROM runtime_state WHERE singleton=1"
            raw=db.execute(query).fetchone()[0]
            state=json.loads(raw)
            result["simulator:recovery_required"]=state["recovery_required"]
            result["simulator:run_output"]=state["run_output"]
print(json.dumps(result))
"""


# Separate child wrapper preserves the Claude-owned original and all other CLI
# invocations. It reuses or declares the actual same-host clock assumption.
INDEPENDENT_DRIVER_WRAPPER = r"""
import hashlib,importlib.util,json,subprocess,sys
from pathlib import Path

driver_path=Path(sys.argv[1]).resolve()
proof_path=Path(sys.argv[2]).resolve()
arguments=sys.argv[3:]
original_sha=hashlib.sha256(driver_path.read_bytes()).hexdigest()
spec=importlib.util.spec_from_file_location("release_original_operable",driver_path)
driver=importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
original_popen=subprocess.Popen
extensions=[]
def guarded_popen(arguments,*args,**kwargs):
    if isinstance(arguments,(list,tuple)) and list(arguments[:4])==[
        sys.executable,"-m","apps.api","serve"
    ]:
        if kwargs.get("shell"):
            raise RuntimeError("Internal API clock extension refuses shell execution")
        policies=[index for index,value in enumerate(arguments)
                  if isinstance(value,str) and (value.startswith("--trend-clock-policy")
                  or (value.startswith("--") and len(value)>2
                      and "--trend-clock-policy".startswith(value.split("=",1)[0])))]
        if len(policies)>1:
            raise RuntimeError("Internal API clock policy is duplicated")
        action="appended"
        appended=["--trend-clock-policy","same-host"]
        if policies:
            index=policies[0]
            option=arguments[index]
            if option=="--trend-clock-policy":
                value=arguments[index+1] if index+1<len(arguments) else None
            elif option.startswith("--trend-clock-policy="):
                value=option.split("=",1)[1]
            else:
                value=None
            if value!="same-host":
                raise RuntimeError("Internal API clock policy conflicts or is malformed")
            action="reused"
            appended=[]
        else:
            arguments=[*arguments,*appended]
        extensions.append({"module":"apps.api","subcommand":"serve",
                           "action":action,"policy":"same-host","appended":appended})
    return original_popen(arguments,*args,**kwargs)
subprocess.Popen=guarded_popen
sys.argv=[str(driver_path),*arguments]
code=1
try:
    code=driver.main()
    if code==0 and len(extensions)!=1:
        code=1
finally:
    subprocess.Popen=original_popen
    unchanged=hashlib.sha256(driver_path.read_bytes()).hexdigest()==original_sha
    if not unchanged:
        code=1
    proof_path.write_text(json.dumps({"original_driver_sha256":original_sha,
        "original_driver_unchanged":unchanged,"extensions":extensions,"exit_code":code},indent=2))
raise SystemExit(code)
"""


def now():
    return datetime.now(UTC).isoformat()


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def environment():
    value = os.environ.copy()
    for name in (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
        "COMPOSE_FILE",
        "COMPOSE_PROJECT_NAME",
    ):
        value.pop(name, None)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="HEAD", help="Committed Git revision")
    parser.add_argument("--output", required=True, type=Path, help="New evidence directory")
    parser.add_argument(
        "--compose-file",
        action="append",
        default=[],
        type=Path,
        help="External build proxy/CA override; runtime changes rejected",
    )
    parser.add_argument("--run", action="store_true", help="Execute build and runtime phases")
    parser.add_argument("--minimum-free-gb", type=float, default=8)
    parser.add_argument(
        "--chromium", default=shutil.which("chromium") or shutil.which("chromium-browser")
    )
    parser.add_argument(
        "--remove-images",
        action="store_true",
        help="Remove only this run's image tags after checks",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    project = "cx22-release-" + secrets.token_hex(6)
    env = environment()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        env["HMI_COMPOSE_PORT"] = str(sock.getsockname()[1])
    record = {
        "started_at_utc": now(),
        "runner_sha256": sha256(Path(__file__).read_bytes()),
        "project": project,
        "phases": {phase: {"outcome": "NOT_RUN"} for phase in PHASES},
        "commands": [],
        "images": [],
        "mode": "run" if args.run else "prepare",
        "override_sha256": {
            str(path.resolve()): sha256(path.read_bytes()) for path in args.compose_file
        },
        "hardware": {
            "cpu_count": os.cpu_count(),
            "free_bytes": shutil.disk_usage(output).free,
            "ssd_verified": False,
            "ram_requirement_verified": False,
        },
    }
    compose = None
    clone = None
    code = 1
    current_phase = "clean_checkout"

    def execute(phase, command, *, cwd=None, timeout=600, input_text=None):
        nonlocal current_phase
        current_phase = phase
        started = now()
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                capture_output=True,
                timeout=timeout,
                input=input_text.encode() if input_text else None,
            )
            combined = result.stdout + result.stderr
            entry = {
                "phase": phase,
                "argv": [str(arg) for arg in command],
                "started_at_utc": started,
                "ended_at_utc": now(),
                "exit_code": result.returncode,
                "output_sha256": sha256(combined),
                "output_bytes": len(combined),
            }
            if b"no space left on device" in combined.lower():
                entry["known_failure"] = "ENOSPC"
            record["commands"].append(entry)
            if result.returncode:
                raise RuntimeError("Command failed in " + phase)
            return result.stdout.decode()
        except subprocess.TimeoutExpired as error:
            record["commands"].append(
                {
                    "phase": phase,
                    "argv": [str(arg) for arg in command],
                    "started_at_utc": started,
                    "ended_at_utc": now(),
                    "exit_code": 124,
                    "failure": "TIMEOUT",
                }
            )
            raise RuntimeError("Command timed out in " + phase) from error

    def mark(phase, **details):
        record["phases"][phase] = {"outcome": "PASSED", "at_utc": now(), **details}

    def ready():
        origin = "http://127.0.0.1:" + env["HMI_COMPOSE_PORT"]
        deadline = time.monotonic() + 120
        with httpx.Client(base_url=origin, trust_env=False, timeout=3) as client:
            while time.monotonic() < deadline:
                try:
                    response = client.get("/auth/config")
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
        raise RuntimeError("Release API did not become ready")

    try:
        target = execute(
            "clean_checkout", ["git", "rev-parse", args.target + "^{commit}"], cwd=ROOT
        ).strip()
        record["target_sha"] = target
        record["source_dirty"] = bool(
            execute("clean_checkout", ["git", "status", "--porcelain"], cwd=ROOT).strip()
        )
        # A local clone uses ordinary .git storage, never a Git worktree or shared index.
        clone = Path(tempfile.mkdtemp(prefix="hmi-release-")) / "checkout"
        execute(
            "clean_checkout",
            ["git", "clone", "--no-hardlinks", "--no-checkout", str(ROOT), str(clone)],
        )
        execute("clean_checkout", ["git", "checkout", "--detach", target], cwd=clone)
        if execute("clean_checkout", ["git", "status", "--porcelain"], cwd=clone).strip():
            raise RuntimeError("Fresh release clone is dirty")
        mark("clean_checkout", target_sha=target)
        required = [
            "compose.yaml",
            "scripts/check-operable-system.py",
            "tests/e2e/test_core_acceptance.py",
        ]
        if any(not (clone / path).exists() for path in required):
            raise RuntimeError("Committed target lacks required release acceptance runners")
        record["tracked_files_sha256"] = {
            name: sha256((clone / name).read_bytes())
            for name in execute("clean_checkout", ["git", "ls-files"], cwd=clone).splitlines()
            if (clone / name).is_file()
        }
        compose = [*DOCKER, "compose", "-p", project, "-f", str(clone / "compose.yaml")]
        baseline = json.loads(
            execute("configuration", [*compose, "config", "--format", "json"], cwd=clone)
        )
        for path in args.compose_file:
            compose += ["-f", str(path.resolve())]
        configured = json.loads(
            execute("configuration", [*compose, "config", "--format", "json"], cwd=clone)
        )

        def runtime(configuration):
            return {
                key: {name: value for name, value in service.items() if name != "build"}
                for key, service in configuration["services"].items()
            }

        if (
            runtime(configured) != runtime(baseline)
            or configured.get("volumes") != baseline.get("volumes")
            or configured.get("networks") != baseline.get("networks")
        ):
            raise RuntimeError("Build override changes runtime; clean-source verification refused")
        for service_name, service in configured["services"].items():
            if "build" not in service:
                continue
            base_build = baseline["services"][service_name]["build"]
            build = service["build"]
            if {key: value for key, value in build.items() if key not in {"args", "secrets"}} != {
                key: value for key, value in base_build.items() if key not in {"args", "secrets"}
            }:
                raise RuntimeError("Build override changes source context or Dockerfile")
            if set(build.get("args", {})) - {
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
                "http_proxy",
                "https_proxy",
                "no_proxy",
            }:
                raise RuntimeError("Only proxy build arguments are allowed")
            if any(secret.get("source") != "proxy_ca" for secret in build.get("secrets", [])):
                raise RuntimeError("Only the proxy_ca build secret is allowed")
        if set(configured.get("secrets", {})) - {"proxy_ca"}:
            raise RuntimeError("Only the proxy_ca external build secret is allowed")
        if not args.run:
            record["reason"] = "Prepared committed source only; build/runtime not requested"
            code = 2
            return code
        if record["hardware"]["free_bytes"] < args.minimum_free_gb * 1024**3:
            raise RuntimeError("Insufficient free space for the requested build guard")
        daemon = json.loads(
            execute("environment", [*DOCKER, "info", "--format", "{{json .}}"], cwd=clone)
        )
        docker_root = Path(daemon["DockerRootDir"])
        record["docker"] = {
            "storage_driver": daemon["Driver"],
            "root_directory": str(docker_root),
            "server_version": daemon["ServerVersion"],
            "free_bytes": shutil.disk_usage(docker_root).free,
        }
        if record["docker"]["free_bytes"] < args.minimum_free_gb * 1024**3:
            raise RuntimeError("Insufficient Docker data-root free space for requested build guard")
        execute("no_cache_build", [*compose, "build", "--no-cache"], cwd=clone, timeout=3600)
        mark("no_cache_build")
        tags = sorted(
            {value["image"] for value in configured["services"].values() if "build" in value}
        )
        for tag in tags:
            value = json.loads(
                execute("image_manifest", [*DOCKER, "image", "inspect", tag], cwd=clone)
            )[0]
            record["images"].append(
                {"tag": tag, "id": value["Id"], "repo_digests": value.get("RepoDigests", [])}
            )
        mark("image_manifest")
        execute("compose_whole", [*compose, "up", "-d", "--no-build"], cwd=clone)
        ready()
        evidence = json.loads(
            execute(
                "compose_whole",
                [*compose, "exec", "-T", "api", "python", "-"],
                cwd=clone,
                timeout=300,
                input_text=(clone / "scripts/compose_acceptance.py").read_text(),
            )
        )
        if (
            evidence.get("settlement") != "SETTLED"
            or not evidence.get("good_trend_points")
            or evidence.get("running_seconds", 0) <= 0
        ):
            raise RuntimeError("Actual Compose production evidence is incomplete")
        mark("compose_whole", evidence=evidence)
        execute("data_preserving_restart", [*compose, "stop", "gateway", "simulator"], cwd=clone)
        before = json.loads(
            execute(
                "data_preserving_restart",
                [*compose, "exec", "-T", "api", "python", "-"],
                cwd=clone,
                input_text=INVENTORY,
            )
        )
        execute("data_preserving_restart", [*compose, "down"], cwd=clone)
        execute("data_preserving_restart", [*compose, "up", "-d", "--no-build"], cwd=clone)
        ready()
        execute("data_preserving_restart", [*compose, "stop", "gateway", "simulator"], cwd=clone)
        after = json.loads(
            execute(
                "data_preserving_restart",
                [*compose, "exec", "-T", "api", "python", "-"],
                cwd=clone,
                input_text=INVENTORY,
            )
        )
        mutable = {
            "simulator:source_events",
            "simulator:recovery_required",
            "simulator:run_output",
            "pg:stream_progress",
            "gateway:source_progress",
        }
        if any(before[key] != after[key] for key in before if key not in mutable):
            raise RuntimeError("Restart changed preserved production/commands/alarms/cursors")
        if (
            not set(before["simulator:source_events"]) <= set(after["simulator:source_events"])
            or not after["simulator:recovery_required"]
            or after["simulator:run_output"]
        ):
            raise RuntimeError("Restart lost original events or automatically restarted")
        for key in ("pg:stream_progress", "gateway:source_progress"):
            if len(before[key]) != len(after[key]):
                raise RuntimeError("Restart changed cursor scope cardinality")
        old_progress = [json.loads(row) for row in before["pg:stream_progress"]]
        new_progress = [json.loads(row) for row in after["pg:stream_progress"]]
        for old, new in zip(old_progress, new_progress, strict=True):
            for field in ("verified_through_seq", "latest_ingested_seq"):
                if field in old and int(new[field]) < int(old[field]):
                    raise RuntimeError("Restart rewound the original event cursor")
        for old_raw, new_raw in zip(
            before["gateway:source_progress"], after["gateway:source_progress"], strict=True
        ):
            old, new = json.loads(old_raw), json.loads(new_raw)
            if old[:2] != new[:2] or int(new[2]) < int(old[2]):
                raise RuntimeError("Restart changed gateway scope or rewound read cursor")
            old_api, new_api = json.loads(old[3]), json.loads(new[3])
            for field in ("verified_through_seq", "latest_ingested_seq"):
                if int(new_api[field]) < int(old_api[field]):
                    raise RuntimeError("Restart rewound gateway API cursor")
        mark(
            "data_preserving_restart",
            inventory_before_sha256=sha256(json.dumps(before, sort_keys=True).encode()),
            inventory_after_sha256=sha256(json.dumps(after, sort_keys=True).encode()),
            original_events_preserved=True,
            recovery_required=True,
            automatic_start=False,
        )
        web = clone / "apps/web/dist"
        web.mkdir(parents=True, exist_ok=True)
        container = execute("independent_cli", [*compose, "ps", "-q", "api"], cwd=clone).strip()
        execute("independent_cli", [*DOCKER, "cp", container + ":/app/web/.", str(web)], cwd=clone)
        execute("cleanup", [*compose, "down", "-v", "--remove-orphans"], cwd=clone)
        wrapper = output / "independent-driver-wrapper.py"
        wrapper.write_text(INDEPENDENT_DRIVER_WRAPPER)
        driver = clone / "scripts/check-operable-system.py"
        record["independent_driver"] = {
            "original_sha256": sha256(driver.read_bytes()),
            "wrapper_sha256": sha256(wrapper.read_bytes()),
            "extension": {
                "module": "apps.api",
                "subcommand": "serve",
                "policy": "same-host",
                "strategy": "reuse-existing-or-append-if-absent",
            },
        }
        extension_proof = output / "independent-driver-extension.json"
        command = [
            sys.executable,
            str(wrapper),
            str(driver),
            str(extension_proof),
            "--web-dir",
            str(web),
            "--evidence",
            str(output / "independent-cli.json"),
            "--screenshot",
            str(output / "independent-cli.png"),
            "--analytics-dir",
            str(output / "independent-analytics"),
        ]
        if args.chromium:
            command += ["--chromium", args.chromium]
        execute("independent_cli", command, cwd=clone, timeout=900)
        proof = json.loads(extension_proof.read_text())
        if (
            not proof.get("original_driver_unchanged")
            or len(proof.get("extensions", [])) != 1
            or proof.get("exit_code") != 0
        ):
            raise RuntimeError("Independent driver clock policy was not verified exactly once")
        record["independent_driver"]["extension"] = proof["extensions"][0]
        mark("independent_cli", extension_proof=proof)
        env["HMI_ACCEPTANCE_ARTIFACTS"] = str(output / "cx17")
        execute(
            "cx17_bundle",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/e2e",
                "-q",
                "--junitxml=" + str(output / "cx17.xml"),
            ],
            cwd=clone,
            timeout=1800,
        )
        mark("cx17_bundle")
        code = 0
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        if (
            current_phase in record["phases"]
            and record["phases"][current_phase]["outcome"] != "PASSED"
        ):
            record["phases"][current_phase] = {"outcome": "FAILED", "at_utc": now()}
        record["failure"] = {
            "type": type(error).__name__,
            "reason": str(error)
            if isinstance(error, RuntimeError)
            else "release preparation or verification failed",
        }
        for phase in PHASES:
            if record["phases"][phase]["outcome"] == "NOT_RUN":
                record["phases"][phase]["reason"] = (
                    "Not reached after failure or missing prerequisites"
                )
    finally:
        if compose and args.run and clone:
            try:
                execute(
                    "cleanup", [*compose, "down", "-v", "--remove-orphans"], cwd=clone, timeout=120
                )
                if args.remove_images:
                    for item in record["images"]:
                        execute("cleanup", [*DOCKER, "image", "rm", item["tag"]], cwd=clone)
                mark("cleanup")
            except (OSError, RuntimeError):
                record["phases"]["cleanup"] = {"outcome": "FAILED"}
                code = 1
        record.update(
            ended_at_utc=now(),
            exit_code=code,
            outcome="PASSED" if code == 0 else "INCOMPLETE" if code == 2 else "FAILED",
        )
        if clone and record["phases"]["cleanup"]["outcome"] == "FAILED":
            record["retained_checkout_for_cleanup"] = str(clone)
        (output / "release.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n"
        )
        if clone and record["phases"]["cleanup"]["outcome"] != "FAILED":
            shutil.rmtree(clone.parent)
        elif clone:
            record["retained_checkout_for_cleanup"] = str(clone)
        print(
            json.dumps(
                {
                    "manifest": str(output / "release.json"),
                    "outcome": record["outcome"],
                    "exit_code": code,
                }
            )
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
