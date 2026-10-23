#!/usr/bin/env python3
"""Build/start/check/remove only an isolated, randomly named local Compose project."""

from __future__ import annotations

import argparse
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

    def compose(*arguments, timeout=600):
        result = subprocess.run(
            [*command, *arguments],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode:
            # Raw tool output could contain a future accidentally logged secret.
            raise RuntimeError("Compose operation failed: " + arguments[0])

    try:
        compose("up", "-d", "--build")
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
        # Re-run init on persistent state; guest/certs/secrets/SQLite must survive.
        compose("run", "--rm", "init")
        print("PASS: guest login, 48 fresh GOOD tags, built web, repeat init")
    finally:
        compose("down", "-v", "--remove-orphans", timeout=120)
        print("Cleaned isolated project " + project)


if __name__ == "__main__":
    main()
