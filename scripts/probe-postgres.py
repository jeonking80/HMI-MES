"""Check local PostgreSQL prerequisites in a disposable authenticated container.

This does not create or validate application migrations. The generated password,
temporary configuration, database, and container are removed at exit.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

import psycopg

IMAGE = (
    "postgres:17-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826"
)


def main() -> None:
    docker_env = os.environ.copy()
    for name in (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    ):
        docker_env.pop(name, None)
    docker = ["docker", "--host=unix:///var/run/docker.sock"]

    def command(*args: str) -> str:
        return subprocess.run(
            [*docker, *args],
            env=docker_env,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.strip()

    password = secrets.token_urlsafe(32)
    container_id: str | None = None
    with tempfile.TemporaryDirectory(prefix="hmi-mes-db-probe-") as directory:
        env_file = Path(directory) / "postgres.env"
        env_file.write_text(
            f"POSTGRES_PASSWORD={password}\nPOSTGRES_DB=hmi_mes_probe\n",
            encoding="utf-8",
        )
        env_file.chmod(0o600)
        try:
            container_id = command(
                "run",
                "--detach",
                "--rm",
                "--publish",
                "127.0.0.1::5432",
                "--env-file",
                str(env_file),
                IMAGE,
            )
            port = int(command("port", container_id, "5432/tcp").rsplit(":", 1)[1])
            deadline = time.monotonic() + 45
            connection = None
            while time.monotonic() < deadline:
                try:
                    connection = psycopg.connect(
                        host="127.0.0.1",
                        port=port,
                        user="postgres",
                        password=password,
                        dbname="hmi_mes_probe",
                        connect_timeout=2,
                        autocommit=True,
                    )
                    break
                except psycopg.OperationalError:
                    time.sleep(0.25)
            if connection is None:
                raise RuntimeError("Disposable PostgreSQL did not become ready in 45 seconds")

            with connection:
                version = connection.execute("SHOW server_version").fetchone()[0]
                connection.execute(
                    "CREATE TEMP TABLE probe (id text PRIMARY KEY, value numeric(20,0) "
                    "CHECK (value >= 0 AND value <= 18446744073709551615))"
                )
                maximum = 2**64 - 1
                connection.execute("INSERT INTO probe VALUES (%s, %s)", ("max", maximum))
                actual = connection.execute(
                    "SELECT value FROM probe WHERE id = %s", ("max",)
                ).fetchone()[0]
                if actual != maximum:
                    raise RuntimeError("UInt64 round-trip lost precision")
                try:
                    with connection.transaction():
                        connection.execute("INSERT INTO probe VALUES ('rollback', 1)")
                        connection.execute("INSERT INTO probe VALUES ('max', 2)")
                except psycopg.errors.UniqueViolation:
                    pass
                else:
                    raise RuntimeError("Unique constraint did not reject duplicate id")
                if connection.execute(
                    "SELECT count(*) FROM probe WHERE id = 'rollback'"
                ).fetchone()[0]:
                    raise RuntimeError("Transaction rollback did not remove partial insertion")
                for invalid in (-1, 2**64):
                    try:
                        connection.execute("INSERT INTO probe VALUES ('invalid', %s)", (invalid,))
                    except psycopg.errors.CheckViolation:
                        pass
                    else:
                        raise RuntimeError("UInt64 constraint accepted an out-of-range value")
                print(f"PostgreSQL {version}: authenticated TCP connection passed.")
                print("UInt64 round-trip, range constraints, uniqueness and rollback passed.")
                print("Disposable prerequisite probe only; application migrations remain untested.")
        finally:
            if container_id is not None:
                command("rm", "--force", "--volumes", container_id)


if __name__ == "__main__":
    main()
