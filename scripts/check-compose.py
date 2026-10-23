#!/usr/bin/env python3
"""Build/start/check/remove only an isolated, randomly named local Compose project."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def good_snapshot(payload):
    samples = payload["screen"]["view"]["samples"]
    return (
        len(samples) == 48
        and len({sample["tag_id"] for sample in samples}) == 48
        and all(sample["status_code"] >> 30 == 0 for sample in samples)
        and len(payload["evidence"]) == 48
        and all(evidence["fresh"] for evidence in payload["evidence"])
        and payload["health"]["state"] == "CAPTURE_FRESH"
        and payload["health"]["heartbeat"] == "GOOD"
        and not payload["missing_tag_ids"]
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-file", action="append", type=Path, default=[])
    parser.add_argument(
        "--no-build", action="store_true", help="Use explicit cached images supplied by an override"
    )
    args = parser.parse_args()
    project = "cx07-check-" + secrets.token_hex(6)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    environment = os.environ.copy()
    for name in (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
        "COMPOSE_FILE",
        "COMPOSE_PROJECT_NAME",
    ):
        environment.pop(name, None)
    environment["HMI_COMPOSE_PORT"] = str(port)
    command = [
        "docker",
        "--host=unix:///var/run/docker.sock",
        "compose",
        "-p",
        project,
        "-f",
        str(ROOT / "compose.yaml"),
    ]
    for path in args.compose_file:
        command += ["-f", str(path.resolve())]

    def compose(*arguments, timeout=600, input_text=None):
        result = subprocess.run(
            [*command, *arguments],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
        )
        if result.returncode:
            if arguments[0] == "exec":
                try:
                    diagnostic = json.loads(result.stdout)
                    phase = diagnostic["failed_stage"]
                    kind = diagnostic["error_type"]
                    if all(value.replace(" ", "").isalpha() for value in (phase, kind)):
                        reason = diagnostic.get("reason", "operation failed")
                        # The helper emits only its fixed RuntimeError messages here.
                        if not isinstance(reason, str) or len(reason) > 200:
                            reason = "operation failed"
                        raise RuntimeError(
                            f"Compose acceptance failed at {phase} ({kind}): {reason}"
                        )
                except (ValueError, KeyError, TypeError):
                    pass
            # Raw tool output could contain a future accidentally logged secret.
            raise RuntimeError("Compose operation failed: " + arguments[0])
        return result.stdout

    try:
        compose("up", "-d", "--no-build" if args.no_build else "--build")
        # Release completed one-shot root filesystems; their volume state remains.
        compose("rm", "-f", "secrets", "init")
        origin = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 90
        with httpx.Client(base_url=origin, trust_env=False, timeout=5) as client:
            while True:
                try:
                    response = client.post("/auth/guest", headers={"Origin": origin}, json={})
                    response.raise_for_status()
                    break
                except (httpx.HTTPError, ValueError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Guest login did not become ready") from None
                    time.sleep(1)
            while True:
                response = client.get(
                    "/api/v1/snapshot",
                    params={
                        "line_id": "packaging-line-1",
                        "source_id": "demo-packaging-source-v1",
                        "stream_id": "events-main",
                    },
                )
                if response.status_code == 200 and good_snapshot(response.json()):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("48 fresh GOOD tags did not arrive")
                time.sleep(1)
            web = client.get("/")
            if web.status_code != 200 or "/assets/" not in web.text:
                raise RuntimeError("Built web entrypoint unavailable")
        evidence = compose(
            "exec",
            "-T",
            "api",
            "python",
            "-",
            timeout=180,
            input_text=(ROOT / "scripts/compose_acceptance.py").read_text(),
        )
        print("Actual Compose process evidence: " + evidence.strip())
        # Re-run init on persistent state; guest/certs/secrets/SQLite must survive.
        compose("run", "--no-deps", "--rm", "init")
        print(
            "PASS: guest, analytics, 48 fresh GOOD tags, GOOD trends, "
            "actual RUNNING and settlement, web, repeat init"
        )
    finally:
        compose("down", "-v", "--remove-orphans", timeout=120)
        print("Cleaned isolated project " + project)


if __name__ == "__main__":
    main()
