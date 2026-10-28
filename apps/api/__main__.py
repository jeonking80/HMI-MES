"""Explicit operator CLI and separate loopback service and browser-session APIs."""

import argparse
import asyncio
import ipaddress
import json
import os
import signal
import stat
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from apps.api.alarm_runtime import (
    ALARM_RULES_FILE,
    ALARM_RUNTIME_FILE,
    configure_alarm_rules,
    read_config_file,
    start_alarm_runtime,
)
from apps.api.alarm_store import AlarmStoreUnavailable
from apps.api.auth import ROLES, UserAuth
from apps.api.database import (
    SUPPORTED_AUTH_REVISIONS,
    SUPPORTED_INGESTION_REVISIONS,
    create_database_engine,
)
from apps.api.guest import bootstrap_guest
from apps.api.ingestion import (
    IngestionService,
    IngestionUnavailable,
    SourceBindingError,
    source_streams,
)
from apps.api.internal import InternalConfig, ServiceCredential, create_internal_app
from apps.api.user_http import UserHTTPConfig, create_user_app
from apps.api.web import mount_web
from contracts.models import Parameters


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap-user")
    bootstrap.add_argument("--login", required=True)
    bootstrap.add_argument("--role", choices=sorted(ROLES), action="append", required=True)
    bootstrap.add_argument("--origin", required=True)
    bootstrap.add_argument("--password-file", type=Path, required=True)
    guest = commands.add_parser("bootstrap-guest")
    guest.add_argument("--origin", required=True)
    alarm_rules = commands.add_parser("configure-alarm-rules")
    alarm_rules.add_argument("--login", required=True)
    alarm_rules.add_argument("--password-file", type=Path, required=True)
    alarm_rules.add_argument("--origin", required=True)
    alarm_rules.add_argument("--config-file", type=Path, required=True)
    users = commands.add_parser("serve-user")
    users.add_argument("--origin", required=True)
    users.add_argument("--enable-guest-viewer", action="store_true")
    users.add_argument("--web-dir", type=Path, help="Explicit built web directory")
    users.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "::1"))
    users.add_argument("--port", default=8002, type=int)
    configure = commands.add_parser("configure-source")
    register = commands.add_parser("register-execution")
    status = commands.add_parser("status")
    serve = commands.add_parser("serve")
    for command in (configure, register, status, serve):
        command.add_argument("--source-id", required=True)
        command.add_argument("--stream-id", required=True)
    for command in (configure, serve):
        command.add_argument("--gateway-id", required=True)
        command.add_argument("--capture-generation", type=int, required=True)
    configure.add_argument("--line-id", required=True)
    for name in ("execution-id", "work-order-id", "recipe-version", "counter-epoch"):
        register.add_argument("--" + name, required=True)
    register.add_argument("--temperature-c", required=True, type=float)
    register.add_argument("--speed-m-per-min", required=True, type=float)
    register.add_argument("--air-pressure-bar", type=float)
    register.add_argument("--target-quantity", required=True, type=int)
    serve.add_argument("--key-id", required=True)
    serve.add_argument("--token-file", type=Path, required=True)
    serve.add_argument("--expires-at", required=True, help="ISO timestamp with timezone")
    serve.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "::1"))
    serve.add_argument("--port", default=8000, type=int)
    serve.add_argument("--alarm-config-file", type=Path)
    clocks = serve.add_mutually_exclusive_group()
    clocks.add_argument(
        "--trend-clock-policy",
        choices=("same-host",),
        help="Explicit operator assertion: API and gateway share the host kernel UTC clock",
    )
    clocks.add_argument("--clock-measured-round-trip", action="store_true")
    return result


def _user_http_config(args) -> UserHTTPConfig:
    if not 1 <= args.port <= 65535:
        raise ValueError("Valid explicit listener port required")
    host = f"[{args.host}]" if ":" in args.host else args.host
    expected = f"http://{host}:{args.port}"
    if args.origin != expected:
        raise ValueError("Origin must exactly match the loopback HTTP listener")
    return UserHTTPConfig(
        origin=args.origin, allow_insecure_loopback=True, guest_viewer=args.enable_guest_viewer
    )


def _password_file(path: Path) -> str:
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or not 1 <= info.st_size <= 1026
        ):
            raise ValueError("Protected password file required")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw = handle.read(1027)
        value = raw.removesuffix(b"\n").removesuffix(b"\r")
        if not 12 <= len(value) <= 1024 or b"\x00" in value:
            raise ValueError("Invalid password bounds")
        return value.decode("utf-8")
    except (OSError, UnicodeError, ValueError):
        raise ValueError("Required protected password file unavailable or invalid") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _ready(engine) -> None:
    with engine.connect() as connection:
        revisions = (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
        )
        if len(revisions) != 1 or revisions[0] not in SUPPORTED_INGESTION_REVISIONS:
            raise ValueError("Apply a supported ingestion schema migration first")


def _verify_binding(engine, args) -> None:
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(source_streams).where(
                    source_streams.c.source_id == args.source_id,
                    source_streams.c.stream_id == args.stream_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or (row["gateway_id"], int(row["capture_generation"])) != (
            args.gateway_id,
            args.capture_generation,
        ):
            raise ValueError("Explicit source registration must match service binding")


async def _serve(app, args) -> None:
    class LocalServer(uvicorn.Server):
        @contextmanager
        def capture_signals(self):
            yield

    server = LocalServer(
        uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            proxy_headers=False,
            access_log=False,
            ws_max_size=4096,
            ws_max_queue=1,
        )
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, setattr, server, "should_exit", True)
    try:
        await server.serve()
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    engine = None
    try:
        credential = None
        user_config = _user_http_config(args) if args.command == "serve-user" else None
        password = (
            _password_file(args.password_file)
            if args.command in {"bootstrap-user", "configure-alarm-rules"}
            else None
        )
        alarm_rules_config = (
            read_config_file(args.config_file, ALARM_RULES_FILE)
            if args.command == "configure-alarm-rules"
            else None
        )
        alarm_config = (
            read_config_file(args.alarm_config_file, ALARM_RUNTIME_FILE)
            if args.command == "serve" and args.alarm_config_file is not None
            else None
        )
        if args.command == "serve":
            # Validate secrets before connecting; startup never provisions a credential or source.
            expiry = datetime.fromisoformat(args.expires_at)
            if expiry.tzinfo is None or expiry <= datetime.now(UTC):
                raise ValueError("Future timezone-aware expiry required")
            if not 1 <= args.port <= 65535 or not ipaddress.ip_address(args.host).is_loopback:
                raise ValueError("Loopback listener required")
            credential = ServiceCredential.from_secret_file(
                key_id=args.key_id,
                gateway_id=args.gateway_id,
                source_id=args.source_id,
                stream_id=args.stream_id,
                secret_file=args.token_file,
                expires_at=expiry,
            )
        engine = create_database_engine()
        if args.command in {
            "bootstrap-user",
            "bootstrap-guest",
            "serve-user",
            "configure-alarm-rules",
        }:
            with engine.connect() as connection:
                revisions = (
                    connection.execute(text("SELECT version_num FROM alembic_version"))
                    .scalars()
                    .all()
                )
                if len(revisions) != 1 or revisions[0] not in SUPPORTED_AUTH_REVISIONS:
                    raise ValueError("Apply explicit user authentication migration first")
            auth = UserAuth(
                engine,
                allowed_origins={args.origin},
                guest_viewer=getattr(args, "enable_guest_viewer", False),
            )
            if args.command == "bootstrap-user":
                auth.bootstrap_user(args.login, password, roles=set(args.role))
                print("App A user provisioning committed; initial password change required")
            elif args.command == "bootstrap-guest":
                bootstrap_guest(auth)
                print("Dedicated guest viewer provisioning committed")
            elif args.command == "configure-alarm-rules":
                configure_alarm_rules(
                    auth,
                    login=args.login,
                    password=password,
                    origin=args.origin,
                    config=alarm_rules_config,
                )
                print("Alarm rule provisioning committed")
            else:
                app = create_user_app(auth, user_config)
                if args.web_dir is not None:
                    mount_web(app, args.web_dir)
                asyncio.run(_serve(app, args))
            return 0
        _ready(engine)
        service = IngestionService(engine)
        scope = {"source_id": args.source_id, "stream_id": args.stream_id}
        if args.command == "configure-source":
            service.configure_source(
                **scope,
                gateway_id=args.gateway_id,
                line_id=args.line_id,
                capture_generation=args.capture_generation,
                now=datetime.now(UTC),
            )
            print("Source registration committed")
        elif args.command == "register-execution":
            service.register_execution(
                **scope,
                execution_id=args.execution_id,
                work_order_id=args.work_order_id,
                recipe_version=args.recipe_version,
                counter_epoch=args.counter_epoch,
                parameters=Parameters(
                    temperature_c=args.temperature_c,
                    speed_m_per_min=args.speed_m_per_min,
                    **(
                        {}
                        if args.air_pressure_bar is None
                        else {"air_pressure_bar": args.air_pressure_bar}
                    ),
                ),
                target_quantity=args.target_quantity,
                now=datetime.now(UTC),
            )
            print("Execution registration committed")
        elif args.command == "status":
            print(
                json.dumps(
                    {
                        "source": service.get_source_status(**scope),
                        "progress": service.get_progress(**scope).model_dump(mode="json"),
                    },
                    sort_keys=True,
                )
            )
        else:
            _verify_binding(engine, args)
            trend_clock_policy = None
            if args.trend_clock_policy == "same-host":
                from apps.api.capture_clock import SameHostClockPolicy

                trend_clock_policy = SameHostClockPolicy(
                    gateway_id=args.gateway_id,
                    source_id=args.source_id,
                    stream_id=args.stream_id,
                    capture_generation=args.capture_generation,
                    valid_from_api_utc=datetime.now(UTC),
                    valid_until_api_utc=expiry,
                )
            consumer = None
            if alarm_config is not None:
                consumer = start_alarm_runtime(engine, alarm_config, args=args)
            service = IngestionService(
                engine,
                alarm_consumer=consumer,
                trend_clock_policy=trend_clock_policy,
                measured_clock_enabled=args.clock_measured_round_trip,
            )
            app = create_internal_app(
                service,
                credential_bindings=[credential],
                config=InternalConfig(allow_insecure_loopback=True),
            )
            asyncio.run(_serve(app, args))
        return 0
    except (
        ValueError,
        OSError,
        SQLAlchemyError,
        SourceBindingError,
        IngestionUnavailable,
        PermissionError,
        AlarmStoreUnavailable,
    ):
        print(
            "API operation failed; verify schema, registration, and protected configuration.",
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
