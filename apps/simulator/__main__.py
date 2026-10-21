"""Explicit local simulator lifecycle: provision, bootstrap, or serve."""

import argparse
import asyncio
import logging
import os
import signal
import stat
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from apps.simulator.auth import SimulatorAuth
from apps.simulator.certificates import SERVER_URI, provision_development_certificates
from apps.simulator.controller import SimulatorController
from apps.simulator.storage import RecoveryRequired, RetentionBlocked, SimulatorStorage
from contracts.models import UINT64_MAX

_RETENTION_INTERVAL = 60.0
_RETENTION_REASONS = {
    "STARTUP_HOLD",
    "CLOCK_FROZEN",
    "RECOVERY_REQUIRED",
    "UNRESOLVED_FAULT",
    "NOT_STOPPED",
    "UNRESOLVED_COMMAND",
    "UNFINISHED_EXECUTION",
}


async def _retention_maintenance(
    controller: SimulatorController,
    *,
    should_exit: Callable[[], bool],
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Opt-in same-loop maintenance, at most one bounded call per 60 seconds.

    Clock injection is internal test scheduling only; storage remains the sole
    authority for recording age and its seven-day owner hold.
    """
    logger = logging.getLogger(__name__)
    next_poll = monotonic()
    last_block = None
    while not should_exit():
        remaining = next_poll - monotonic()
        if remaining > 0:
            await sleep(remaining)
            continue
        try:
            result = controller.prune_events(limit=1000)
        except (RetentionBlocked, RecoveryRequired) as error:
            code = (
                "RECOVERY_REQUIRED"
                if isinstance(error, RecoveryRequired)
                else str(error).partition(":")[0]
            )
            if code not in _RETENTION_REASONS:
                code = "BLOCKED"
            if code != last_block:
                logger.warning("Simulator retention blocked (%s)", code)
            last_block = code
        else:
            last_block = None
            logger.info(
                "Simulator retention pruned=%s through=%s",
                result.pruned_count,
                result.pruned_through_seq,
            )
        # Wait a full interval after a completed poll; do not catch up in bursts.
        next_poll = monotonic() + _RETENTION_INTERVAL


def _password_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise ValueError("Password file must be a regular owner-only file")
        raw = handle.read(1026)
    return raw.decode("utf-8").removesuffix("\n")


async def serve(args) -> None:
    import uvicorn

    from apps.simulator.console import ConsoleConfig, create_app
    from apps.simulator.field_channels import FieldChannels
    from apps.simulator.opcua_server import OPCUAServerConfig, SimulatorOPCUAServer

    # Direct launcher serves loopback HTTP only; remote TLS deployment is separate.
    if args.host not in {"127.0.0.1", "::1"}:
        raise ValueError("Local launcher binds only explicit loopback addresses")
    origin = urlsplit(args.origin)
    if origin.scheme != "http" or origin.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Local launcher requires an explicit loopback HTTP origin")
    with SimulatorStorage(args.database) as storage:
        controller = SimulatorController(storage, now=datetime.now(UTC))
        auth = SimulatorAuth(storage.connection, allowed_origins={args.origin})
        with storage.transaction():
            auth.initialize_schema()
        field_channels = FieldChannels(storage)
        app = create_app(
            controller,
            auth,
            ConsoleConfig(origin=args.origin, allow_insecure_loopback=True),
            field_channels=field_channels,
        )
        opc = SimulatorOPCUAServer(
            controller,
            OPCUAServerConfig(
                endpoint=args.opc_endpoint,
                application_uri=SERVER_URI,
                server_certificate=args.server_certificate,
                server_private_key=args.server_private_key,
                trusted_client_certificate=args.trusted_client_certificate,
            ),
            field_channels=field_channels,
        )

        class LocalServer(uvicorn.Server):
            @contextmanager
            def capture_signals(self):
                # Root lifecycle owns shutdown so OPC and SQLite always close too.
                yield

        http = LocalServer(
            uvicorn.Config(
                app,
                host=args.host,
                port=args.port,
                access_log=False,
                log_level="warning",
                proxy_headers=False,
            )
        )

        async def cycle():
            heartbeat = 0
            while not http.should_exit:
                started = asyncio.get_running_loop().time()
                now = datetime.now(UTC)
                if not storage.recovery_required and not controller.state.recovery_required:
                    controller.tick(now=now)
                if heartbeat == UINT64_MAX:
                    raise ValueError("Heartbeat exhausted")
                heartbeat += 1
                await opc.publish_snapshot(acquired_at=now, heartbeat=heartbeat)
                await asyncio.sleep(max(0, 0.1 - (asyncio.get_running_loop().time() - started)))

        loop = asyncio.get_running_loop()
        for shutdown_signal in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(shutdown_signal, setattr, http, "should_exit", True)
        await opc.start()
        ticker = asyncio.create_task(cycle())
        web = asyncio.create_task(http.serve())
        maintenance = None
        if getattr(args, "enable_retention", False):
            maintenance = asyncio.create_task(
                _retention_maintenance(
                    controller,
                    should_exit=lambda: http.should_exit,
                )
            )
        tasks = (ticker, web) if maintenance is None else (ticker, web, maintenance)
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            http.should_exit = True
            ticker.cancel()
            background = (ticker,)
            if maintenance is not None:
                maintenance.cancel()
                background = (ticker, maintenance)
            await asyncio.gather(*background, return_exceptions=True)
            try:
                await asyncio.wait_for(web, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                web.cancel()
                await asyncio.gather(web, return_exceptions=True)
            finally:
                await opc.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Separate local HMI-MES equipment simulator")
    commands = parser.add_subparsers(dest="command", required=True)
    certs = commands.add_parser("init-dev-certificates")
    certs.add_argument("--directory", type=Path, required=True)
    bootstrap = commands.add_parser("bootstrap-user")
    bootstrap.add_argument("--database", type=Path, required=True)
    bootstrap.add_argument("--origin", required=True)
    bootstrap.add_argument("--login", required=True)
    bootstrap.add_argument("--password-file", type=Path, required=True)
    bootstrap.add_argument("--role", choices=["viewer", "simulator_operator"], required=True)
    server = commands.add_parser("serve")
    server.add_argument("--database", type=Path, required=True)
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8001)
    server.add_argument("--origin", default="http://127.0.0.1:8001")
    server.add_argument("--opc-endpoint", default="opc.tcp://127.0.0.1:4840/hmi-mes/")
    server.add_argument("--server-certificate", type=Path, required=True)
    server.add_argument("--server-private-key", type=Path, required=True)
    server.add_argument("--trusted-client-certificate", type=Path, required=True)
    server.add_argument(
        "--enable-retention",
        action="store_true",
        help="Opt in to stopped seven-day source-event maintenance every 60 seconds",
    )
    args = parser.parse_args()
    try:
        if args.command == "init-dev-certificates":
            provision_development_certificates(args.directory)
        elif args.command == "bootstrap-user":
            password = _password_file(args.password_file)
            with SimulatorStorage(args.database) as storage:
                auth = SimulatorAuth(storage.connection, allowed_origins={args.origin})
                with storage.transaction():
                    auth.initialize_schema()
                    auth.bootstrap_user(args.login, password, role=args.role)
        else:
            asyncio.run(serve(args))
    except (Exception, KeyboardInterrupt):
        # Never serialize exception objects containing configuration, SQL or credentials.
        logging.getLogger(__name__).error(
            "Simulator operation failed; check configuration and state"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
