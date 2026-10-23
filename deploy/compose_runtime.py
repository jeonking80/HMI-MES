"""Local Compose lifecycle. Never selects a public API or an inherited database."""

from __future__ import annotations

import asyncio
import os
import secrets
import signal
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from deploy.plant_runtime import IDENTITY

ROOT = Path("/var/lib/hmi")
PORT = int(os.environ.get("HMI_COMPOSE_PORT", "8002"))
ORIGIN = f"http://127.0.0.1:{PORT}"


def secret(name):
    path = ROOT / name
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(secrets.token_urlsafe(32))
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("Owner-only secret required")
    return path.read_text()


def database():
    from sqlalchemy.engine import URL

    # Ignore every inherited deployment selector, including HMI_DATABASE_DSN.
    os.environ["HMI_DATABASE_DSN"] = URL.create(
        "postgresql+psycopg",
        username="postgres",
        password=secret("postgres-password"),
        host="postgres",
        database="hmi_compose",
    ).render_as_string(hide_password=False)


def initialize():
    from alembic import command
    from sqlalchemy import select, text

    from apps.api.auth import UserAuth
    from apps.api.bootstrap import bootstrap_reference_data
    from apps.api.database import create_database_engine, migration_config
    from apps.api.guest import bootstrap_guest, validate_guest
    from apps.api.ingestion import IngestionService, source_streams
    from deploy.plant_runtime import prepare, validate

    database()
    engine = create_database_engine()
    try:
        with engine.connect() as connection:
            command.upgrade(migration_config(connection), "head")
        print("Compose schema ready", flush=True)
        bootstrap_reference_data(engine)
        with engine.connect() as connection:
            binding = (
                connection.execute(
                    select(source_streams).where(
                        source_streams.c.source_id == IDENTITY["source_id"],
                        source_streams.c.stream_id == IDENTITY["stream_id"],
                    )
                )
                .mappings()
                .one_or_none()
            )
        if binding is None:
            IngestionService(engine).configure_source(
                **IDENTITY, capture_generation=1, now=datetime.now(UTC)
            )
        elif (
            any(binding[k] != v for k, v in IDENTITY.items()) or binding["capture_generation"] != 1
        ):
            raise ValueError("Existing source binding differs")
        auth = UserAuth(engine, allowed_origins={ORIGIN}, guest_viewer=True)
        with engine.connect() as connection:
            user = (
                connection.execute(text("SELECT * FROM users WHERE login='guest-viewer'"))
                .mappings()
                .one_or_none()
            )
            if user is not None:
                validate_guest(auth, connection, user)
        if user is None:
            bootstrap_guest(auth)
        print("Compose identity and guest ready", flush=True)
        runtime = ROOT / "runtime"
        if runtime.exists():
            validate(runtime)
        else:
            # The reusable commissioning initializer has a fixed identity. Local Compose
            # uses that same identity to avoid creating an independent second source.
            prepare(runtime, initialize=True, console_port=8011, opc_port=4850)
    finally:
        engine.dispose()


async def proxy(reader, writer):
    try:
        remote_reader, remote_writer = await asyncio.open_connection("127.0.0.1", PORT)
    except OSError:
        writer.close()
        return

    async def copy(source, target):
        try:
            while data := await source.read(65536):
                target.write(data)
                await target.drain()
        finally:
            target.close()

    await asyncio.gather(copy(reader, remote_writer), copy(remote_reader, writer))


async def api():
    database()
    common = [
        "--source-id",
        IDENTITY["source_id"],
        "--stream-id",
        IDENTITY["stream_id"],
        "--gateway-id",
        IDENTITY["gateway_id"],
        "--capture-generation",
        "1",
    ]
    internal = [
        "serve",
        *common,
        "--key-id",
        "compose-v1",
        "--token-file",
        str(ROOT / "gateway-token"),
        "--expires-at",
        (datetime.now(UTC) + timedelta(days=30)).isoformat(),
    ]
    user = [
        "serve-user",
        "--origin",
        ORIGIN,
        "--port",
        str(PORT),
        "--enable-guest-viewer",
        "--web-dir",
        "/app/web",
    ]
    children = [
        await asyncio.create_subprocess_exec(sys.executable, "-m", "apps.api", *args)
        for args in (internal, user)
    ]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    server = await asyncio.start_server(proxy, "0.0.0.0", 8080)
    tasks = [asyncio.create_task(child.wait()) for child in children]
    tasks.append(asyncio.create_task(stop.wait()))
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        server.close()
        await server.wait_closed()
        for child in children:
            if child.returncode is None:
                child.terminate()
        await asyncio.gather(*(child.wait() for child in children))
        for task in tasks:
            task.cancel()
    if any(child.returncode not in (0, -signal.SIGTERM) for child in children):
        raise RuntimeError("API child exited")


def main():
    from deploy.plant_runtime import commands, validate

    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.umask(0o077)
    mode = sys.argv[1]
    if mode == "secrets":
        secret("postgres-password")
        secret("gateway-token")
    elif mode == "init":
        initialize()
    elif mode == "api":
        asyncio.run(api())
    else:
        selected = commands(
            ROOT / "runtime",
            validate(ROOT / "runtime"),
            ROOT / "gateway-token",
            "http://127.0.0.1:8000",
            allow_local_test=True,
        )[mode]
        os.execv(selected[0], selected)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(
            "Compose operation failed; inspect configuration and service status.", file=sys.stderr
        )
        raise SystemExit(1) from None
