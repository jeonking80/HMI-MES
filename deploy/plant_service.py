"""Persistent hosted process supervisor; public endpoint exposes liveness only."""

from __future__ import annotations

import hmac
import json
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.api.internal import _token  # noqa: E402
from deploy.plant_runtime import protected  # noqa: E402


def gateway_secret(directory: Path, token: str) -> Path:
    """Initial installation only; changed tokens require explicit rotation tooling."""
    _token(token)
    path = directory / "gateway-token"
    if path.exists() or path.is_symlink():
        if not hmac.compare_digest(protected(path), token):
            raise ValueError("Existing gateway credential differs; never overwrite")
        return path
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(token + "\n")
    return path


def runtime_command(directory: Path, *arguments: str) -> list[str]:
    return [
        sys.executable,
        str(ROOT / "deploy/plant_runtime.py"),
        "--directory",
        str(directory),
        *arguments,
    ]


def main() -> int:
    directory = Path(os.environ.get("HMI_PLANT_RUNTIME_DIR", "/var/data/runtime"))
    port = int(os.environ.get("PORT", "10000"))
    if not directory.is_absolute() or not 1024 <= port <= 65535:
        raise ValueError("Explicit persistent directory and unprivileged port required")
    token = os.environ.pop("HMI_GATEWAY_TOKEN")
    _token(token)
    if directory.exists() or directory.is_symlink():
        command = runtime_command(directory, "prepare")
    else:
        if os.environ.get("HMI_PLANT_ALLOW_INITIALIZE") != "true":
            raise ValueError("New runtime requires explicit initialization setting")
        command = runtime_command(directory, "prepare", "--initialize")
    subprocess.run(command, check=True)
    token_file = gateway_secret(directory, token)
    del token
    child = subprocess.Popen(
        runtime_command(
            directory,
            "serve",
            "--token-file",
            str(token_file),
            "--api-url",
            os.environ.get("HMI_PUBLIC_API_URL", "https://hmi-mes.vercel.app"),
        ),
        stdin=subprocess.DEVNULL,
    )
    stopping = threading.Event()

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/health/live":
                self.send_error(404)
                return
            alive = child.poll() is None and not stopping.is_set()
            body = json.dumps({"alive": alive}).encode()
            self.send_response(200 if alive else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = None
    thread = None

    def stop(*args):
        stopping.set()
        if child.poll() is None:
            child.terminate()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stop)
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), Health)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        result = child.wait()
        return 0 if stopping.is_set() else (result or 1)
    finally:
        stop()
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("Plant service failed; inspect protected runtime configuration", file=sys.stderr)
        raise SystemExit(1) from None
