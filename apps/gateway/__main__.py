"""Separate OPC UA collector and durable HTTP delivery process."""

import argparse
import asyncio
import base64
import json
import os
import re
import signal
import stat
import sys
from pathlib import Path

from apps.gateway.buffer import GatewayBuffer
from apps.gateway.commands import CommandTransport, CommandWorker
from apps.gateway.http_client import IntakeClient, IntakeConfig
from apps.gateway.opcua_client import GatewayOPCUAClient, GatewayOPCUAConfig
from apps.gateway.settlement import RunSummaryTransport, RunSummaryWorker
from apps.gateway.worker import GatewayPump


def _token_file(path: Path) -> str:
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or not 43 <= info.st_size <= 88
        ):
            raise ValueError("Protected owner-only token file required")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw = handle.read(89)
        value = raw.removesuffix(b"\n").removesuffix(b"\r").decode("ascii")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43,86}", value):
            raise ValueError("Invalid token encoding")
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        if not 32 <= len(decoded) <= 64 or (
            base64.urlsafe_b64encode(decoded).rstrip(b"=").decode() != value
        ):
            raise ValueError("Invalid token encoding")
        return value
    except (OSError, UnicodeError, ValueError):
        raise ValueError("Required protected service token file unavailable or invalid") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    serve = result.add_subparsers(dest="command", required=True).add_parser("serve")
    for name in (
        "gateway-id",
        "source-id",
        "stream-id",
        "opc-endpoint",
        "application-uri",
        "server-application-uri",
        "api-url",
    ):
        serve.add_argument("--" + name, required=True)
    for name in (
        "queue-file",
        "client-certificate",
        "client-private-key",
        "trusted-server-certificate",
        "token-file",
    ):
        serve.add_argument("--" + name, required=True, type=Path)
    serve.add_argument("--poll-interval", type=float, default=1.0)
    serve.add_argument("--allow-loopback-http", action="store_true")
    serve.add_argument("--clock-measured-round-trip", action="store_true")
    serve.add_argument("--enable-commands", action="store_true")
    serve.add_argument("--enable-settlement", action="store_true")
    return result


async def _command_loop(worker: CommandWorker, stop: asyncio.Event) -> None:
    while not stop.is_set():
        result = await worker.run_once()
        print(json.dumps({"operation": "command", "state": result.state}), flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.2 if result.state == "reported" else 1.0)
        except TimeoutError:
            pass


async def _settlement_loop(worker: RunSummaryWorker, stop: asyncio.Event) -> None:
    while not stop.is_set():
        result = await worker.run_once()
        print(json.dumps({"operation": "settlement", "state": result.state}), flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.2 if result.state == "polled" else 1.0)
        except TimeoutError:
            pass


async def _run_workers(pump, command_worker, stop, *, settlement_worker=None) -> None:
    def status(value):
        output = {"operation": value["operation"], "state": value["state"]}
        error = _error_class(value.get("error"))
        if error is not None:
            output["error"] = error
        print(json.dumps(output), flush=True)

    tasks = [
        asyncio.create_task(pump.run(stop, on_status=status)),
        asyncio.create_task(stop.wait()),
    ]
    if command_worker is not None:
        tasks.append(asyncio.create_task(_command_loop(command_worker, stop)))
    if settlement_worker is not None:
        tasks.append(asyncio.create_task(_settlement_loop(settlement_worker, stop)))
    try:
        completed, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in completed:
            task.result()
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _serve(args) -> None:
    token = _token_file(args.token_file)
    config = GatewayOPCUAConfig(
        endpoint=args.opc_endpoint,
        application_uri=args.application_uri,
        client_certificate=args.client_certificate,
        client_private_key=args.client_private_key,
        trusted_server_certificate=args.trusted_server_certificate,
        server_application_uri=args.server_application_uri,
        poll_interval_seconds=args.poll_interval,
    )
    intake_config = IntakeConfig(
        base_url=args.api_url, token=token, allow_loopback_http=args.allow_loopback_http
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM)
    for sig in signals:
        loop.add_signal_handler(sig, stop.set)
    opc, intake, buffer, commands, settlement = None, None, None, None, None
    try:
        # GatewayBuffer rejects another application's existing SQLite schema.
        buffer = GatewayBuffer(args.queue_file, gateway_id=args.gateway_id)
        opc = GatewayOPCUAClient(config)
        intake = IntakeClient(intake_config)
        pump = GatewayPump(
            args.source_id,
            args.stream_id,
            opc=opc,
            buffer=buffer,
            intake=intake,
            measured_clock=args.clock_measured_round_trip,
        )
        command_worker = None
        if args.enable_commands:
            commands = CommandTransport(intake_config)
            command_worker = CommandWorker(opc, commands)
        settlement_worker = None
        if args.enable_settlement:
            settlement = RunSummaryTransport(intake_config)
            settlement_worker = RunSummaryWorker(
                opc, settlement, source_id=args.source_id, stream_id=args.stream_id
            )
        await _run_workers(pump, command_worker, stop, settlement_worker=settlement_worker)
    finally:
        try:
            if settlement is not None:
                await settlement.close()
        finally:
            try:
                if commands is not None:
                    await commands.close()
            finally:
                try:
                    if intake is not None:
                        await intake.close()
                finally:
                    try:
                        if opc is not None:
                            await opc.disconnect()
                    finally:
                        if buffer is not None:
                            buffer.close()
                        for sig in signals:
                            loop.remove_signal_handler(sig)


def _error_class(value):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", value):
        return value
    return None


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        asyncio.run(_serve(args))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        # Transport/driver errors can contain endpoint details; emit no exception body.
        output = {"operation": "gateway", "state": "stopped"}
        name = _error_class(type(error).__name__)
        if name is not None:
            output["error"] = name
        print(
            json.dumps(output),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
