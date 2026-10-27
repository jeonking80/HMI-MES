"""Plant host agent: follow the operator's on/off request from the deployed API.

Runs on the always-on plant host (see deploy/oracle-cloud-init.sh). Every few seconds
it reports its state to /internal/v1/runtime-control/agent and reads the request.
ON starts deploy/plant_runtime.py serve and the explicit recovery commissioning; OFF,
or the request's own expiry, stops it with SIGTERM. The agent never issues production
commands and never starts equipment without a current, unexpired request.
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
API = "https://hmi-mes.vercel.app"
POLL_SECONDS = 5
READY_SECONDS = 90
RETRY_SECONDS = 60
CHECKS = [
    "--outputs-stopped",
    "--positions-verified",
    "--storage-reconciled",
    "--commands-reconciled",
    "--operator-confirmed",
]


def host_load(previous):
    """CPU busy/steal share since the previous call and available memory, from /proc."""
    try:
        fields = [int(v) for v in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
        memory = {
            line.split(":")[0]: int(line.split()[1])
            for line in Path("/proc/meminfo").read_text().splitlines()
        }
        load = Path("/proc/loadavg").read_text().split()[0]
    except (OSError, ValueError, IndexError):
        return previous, None
    if previous is None:
        return fields, None
    delta = [now - old for now, old in zip(fields, previous, strict=True)]
    total = sum(delta[:8]) or 1
    idle = delta[3] + delta[4]
    steal = delta[7] if len(delta) > 7 else 0
    summary = (
        f"cpu {100 * (total - idle - steal) // total}% steal {100 * steal // total}%"
        f" load {load} mem {memory.get('MemAvailable', 0) // 1024}MB"
    )
    return fields, summary


class Agent:
    def __init__(self, directory: Path, token_file: Path, api_url: str):
        self.directory = directory
        self.token_file = token_file
        self.token = token_file.read_text().strip()
        self.api_url = api_url
        self.child: subprocess.Popen | None = None
        self.state = "OFF"
        self.detail: str | None = None
        self.last_failure = 0.0
        self.stopping = False
        self.cpu = None

    def runtime(self, *args):
        return [
            sys.executable,
            str(ROOT / "deploy/plant_runtime.py"),
            "--directory",
            str(self.directory),
            *args,
        ]

    def exchange(self) -> str:
        self.cpu, load = host_load(self.cpu)
        # An error keeps its reason; otherwise report host load for diagnosis.
        detail = self.detail if self.state == "ERROR" else load
        response = httpx.post(
            self.api_url + "/internal/v1/runtime-control/agent",
            json={"state": self.state, "detail": detail},
            headers={"Authorization": "Bearer " + self.token},
            timeout=15,
        )
        response.raise_for_status()
        return response.json()["desired"]

    def prepare(self):
        if not self.directory.exists():
            subprocess.run(self.runtime("prepare", "--initialize"), check=True, cwd=ROOT)

    def start(self):
        self.state, self.detail = "STARTING", None
        self.child = subprocess.Popen(
            self.runtime(
                "serve",
                "--token-file",
                str(self.token_file),
                "--api-url",
                self.api_url,
                "--clock-measured-round-trip",
            ),
            cwd=ROOT,
        )
        marker = self.directory / ".commissioned"
        passwords = (
            ["--password-file", str(self.directory / "console-password")]
            if marker.exists()
            else [
                "--password-file",
                str(self.directory / "console-initial-password"),
                "--new-password-file",
                str(self.directory / "console-password"),
            ]
        )
        deadline = time.monotonic() + READY_SECONDS
        while time.monotonic() < deadline:
            if self.child.poll() is not None:
                return self.fail("runtime exited while starting")
            time.sleep(3)
            done = subprocess.run(self.runtime("commission", *passwords, *CHECKS), cwd=ROOT)
            if done.returncode == 0:
                marker.touch(exist_ok=True)
                self.state = "RUNNING"
                return
        self.fail("runtime did not become ready")

    def stop(self):
        if self.child is not None and self.child.poll() is None:
            self.state = "STOPPING"
            self.child.send_signal(signal.SIGTERM)
            try:
                self.child.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=10)
        self.child = None
        if self.state != "ERROR":
            self.state, self.detail = "OFF", None

    def fail(self, detail: str):
        self.stop()
        self.state, self.detail = "ERROR", detail
        self.last_failure = time.monotonic()

    def step(self):
        try:
            desired = self.exchange()
        except (httpx.HTTPError, KeyError, ValueError):
            return  # Keep the current state; an unreachable API is not a stop request.
        running = self.child is not None and self.child.poll() is None
        if self.child is not None and not running:
            self.fail("runtime exited")
        elif desired == "ON" and not running:
            if time.monotonic() - self.last_failure >= RETRY_SECONDS:
                self.start()
        elif desired == "OFF":
            if running:
                self.stop()
            elif self.state == "ERROR":
                self.state, self.detail = "OFF", None

    def run(self):
        self.prepare()
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stopping", True))
        try:
            while not self.stopping:
                self.step()
                time.sleep(POLL_SECONDS)
        finally:
            self.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--api-url", default=API)
    args = parser.parse_args()
    Agent(args.directory.absolute(), args.token_file, args.api_url).run()


if __name__ == "__main__":
    raise SystemExit(main())
