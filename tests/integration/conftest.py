"""Shared disposable PostgreSQL fixtures; never connect to an application database."""

import os
import secrets
import subprocess
import time

import psycopg
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import URL

from apps.api.database import create_database_engine, migration_config

IMAGE = (
    "postgres:17-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826"
)


@pytest.fixture(scope="module")
def postgres_url(tmp_path_factory):
    docker_env = os.environ.copy()
    for key in (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    ):
        docker_env.pop(key, None)

    def docker(*args):
        try:
            result = subprocess.run(
                ["docker", "--host=unix:///var/run/docker.sock", *args],
                env=docker_env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pytest.fail("Local Docker is unavailable; real PostgreSQL integration is required")
        if result.returncode:
            pytest.fail(
                "Local Docker command failed; check daemon access and the pinned PostgreSQL image"
            )
        return result.stdout.strip()

    docker("info", "--format", "{{.ServerVersion}}")
    directory = tmp_path_factory.mktemp("hmi-postgres")
    secret_file = directory / "postgres.env"
    password = secrets.token_urlsafe(32)
    container = None
    try:
        descriptor = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as secret_handle:
            secret_handle.write(f"POSTGRES_PASSWORD={password}\nPOSTGRES_DB=hmi_mes_test\n")
        container = docker(
            "run",
            "--detach",
            "--rm",
            "--pull=never",
            "--publish",
            "127.0.0.1::5432",
            "--env-file",
            str(secret_file),
            IMAGE,
        )
        port = int(docker("port", container, "5432/tcp").rsplit(":", 1)[1])
        deadline = time.monotonic() + 45
        while True:
            try:
                with psycopg.connect(
                    host="127.0.0.1",
                    port=port,
                    user="postgres",
                    password=password,
                    dbname="hmi_mes_test",
                    connect_timeout=2,
                ) as connection:
                    assert (
                        connection.execute("SHOW server_version_num").fetchone()[0].startswith("17")
                    )
                break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    pytest.fail("Disposable authenticated PostgreSQL did not become ready")
                time.sleep(0.25)
        yield URL.create(
            "postgresql+psycopg",
            username="postgres",
            password=password,
            host="127.0.0.1",
            port=port,
            database="hmi_mes_test",
        )
    finally:
        try:
            if container:
                docker("rm", "--force", "--volumes", container)
        finally:
            secret_file.unlink(missing_ok=True)


@pytest.fixture
def engine(postgres_url):
    engine = create_database_engine(postgres_url)
    try:
        yield engine
    finally:
        # This is exclusively the generated container's disposable DB.
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
        engine.dispose()


@pytest.fixture
def migrated(engine):
    with engine.connect() as connection:
        command.upgrade(migration_config(connection), "head")
    return engine
