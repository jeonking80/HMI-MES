"""Generate SQL Editor installation from canonical14 in a disposable cached PG17.

No application DSN is accepted. No production/Neon connection is made. Passwords
exist only in the generated temporary container environment file and memory.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import psycopg
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]
REVISION = "0014_mixed_alarm_runtime"
IMAGE = (
    "postgres:17-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826"
)
BASELINE = ("sites", "lines", "assets", "tag_definitions", "roles")
# Explicit canonical14 capabilities; no blanket grants on future tables.
READ_ONLY = {"sites", "assets", "tag_definitions", "roles", "alembic_version"}
APPEND = {
    "auth_audit",
    "command_request_audit",
    "equipment_state_events",
    "event_apply_errors",
    "ingest_batches",
    "ingest_conflicts",
    "production_events",
    "run_summary_conflicts",
    "execution_plans",
    "mes_management_audit",
    "work_order_releases",
    "work_order_execution_links",
    "work_order_cancellations",
    "alarm_rule_versions",
    "alarm_actions",
    "alarm_receipts",
    "alarm_outbox",
    "alarm_capture_decisions",
}
MUTABLE = {
    "users",
    "auth_sessions",
    "source_streams",
    "production_executions",
    "execution_totals",
    "event_inbox",
    "stream_progress",
    "command_requests",
    "run_reconciliations",
    "work_orders",
    "recipe_versions",
    "alarm_channels",
    "alarm_instances",
    "alarm_capture_frontiers",
}
DELETABLE = {"auth_login_challenges", "user_roles", "stream_gaps", "line_execution_reservations"}


def docker(*arguments, input_text=None):
    environment = os.environ.copy()
    for name in (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    ):
        environment.pop(name, None)
    result = subprocess.run(
        ["docker", "--host=unix:///var/run/docker.sock", *arguments],
        env=environment,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError("Disposable PostgreSQL Docker operation failed")
    return result.stdout.strip()


@contextmanager
def disposable_postgres(directory: Path):
    docker("info", "--format", "{{.ServerVersion}}")
    password = secrets.token_urlsafe(32)
    secret_file = directory / "postgres.env"
    container = None
    descriptor = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(f"POSTGRES_PASSWORD={password}\nPOSTGRES_DB=hmi_canonical14\n")
    try:
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
        url = URL.create(
            "postgresql+psycopg",
            username="postgres",
            password=password,
            host="127.0.0.1",
            port=port,
            database="hmi_canonical14",
        )
        deadline = time.monotonic() + 45
        while True:
            try:
                with connect(url) as db:
                    if not db.execute("SHOW server_version_num").fetchone()[0].startswith("17"):
                        raise RuntimeError("Canonical generator requires PostgreSQL17")
                break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Disposable PostgreSQL startup timed out") from None
                time.sleep(0.25)
        yield SimpleNamespace(container=container, url=url)
    finally:
        try:
            if container:
                docker("rm", "--force", "--volumes", container)
        finally:
            secret_file.unlink(missing_ok=True)


def connect(url, *, autocommit=False):
    return psycopg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        dbname=url.database,
        autocommit=autocommit,
    )


def clean_dump(value: str) -> str:
    # pg_dump17's randomized psql guard is not SQL and SQL Editor cannot run it.
    lines = [
        line
        for line in value.splitlines()
        if not line.startswith(("\\restrict ", "\\unrestrict "))
        and line.strip() != "CREATE SCHEMA public;"
    ]
    return "\n".join(lines).strip() + "\n"


def dump(container, *options):
    return clean_dump(
        docker(
            "exec",
            "--user",
            "postgres",
            container,
            "pg_dump",
            "--dbname=hmi_canonical14",
            "--no-owner",
            "--no-privileges",
            *options,
        )
    )


def prepare_canonical(postgres):
    from alembic import command

    from apps.api.bootstrap import bootstrap_reference_data
    from apps.api.database import create_database_engine, migration_config

    engine = create_database_engine(postgres.url)
    try:
        with engine.connect() as db:
            command.upgrade(migration_config(db), REVISION)
        bootstrap_reference_data(engine)
    finally:
        engine.dispose()


def render(postgres) -> str:
    prepare_canonical(postgres)
    schema = dump(postgres.container, "--schema-only", "--schema=public")
    baseline = dump(
        postgres.container,
        "--data-only",
        "--column-inserts",
        *[f"--table=public.{table}" for table in BASELINE],
    )
    with connect(postgres.url) as db:
        tables = {
            row[0]
            for row in db.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        }
        sequences = [
            row[0]
            for row in db.execute(
                "SELECT sequencename FROM pg_sequences WHERE schemaname='public' "
                "ORDER BY sequencename"
            )
        ]
        functions = [
            (row[0], row[1])
            for row in db.execute(
                "SELECT p.proname,pg_get_function_identity_arguments(p.oid) FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' "
                "ORDER BY p.proname"
            )
        ]
    if tables != READ_ONLY | APPEND | MUTABLE | DELETABLE | {"lines"}:
        raise RuntimeError("Canonical table inventory changed; review explicit runtime privileges")
    grants = []
    for names, privileges in (
        (READ_ONLY, "SELECT"),
        (APPEND, "SELECT, INSERT"),
        (MUTABLE, "SELECT, INSERT, UPDATE"),
        (DELETABLE, "SELECT, INSERT, UPDATE, DELETE"),
    ):
        targets = ", ".join('public."' + name + '"' for name in sorted(names))
        grants.append(f"GRANT {privileges} ON TABLE {targets} TO hmi_app;")
    grants.append("GRANT SELECT, UPDATE(id) ON TABLE public.lines TO hmi_app;")
    for name in sequences:
        grants.append(f'GRANT USAGE, SELECT ON SEQUENCE public."{name}" TO hmi_app;')
    for name, arguments in functions:
        grants.append(f'GRANT EXECUTE ON FUNCTION public."{name}"({arguments}) TO hmi_app;')
    header = """-- Generated by .venv/bin/python deploy/generate_neon_init.py. Do not edit.
-- Canonical14 migrations + reference bootstrap; EMPTY hmi_mes_demo only.
-- Paste and run this ENTIRE file in Neon SQL Editor as its CREATEROLE database owner.
-- No password, LOGIN role, app user, source, execution or production fixture is included.
BEGIN;
DO $$ BEGIN
 IF current_database() <> 'hmi_mes_demo' THEN
  RAISE EXCEPTION 'Select the dedicated hmi_mes_demo database';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='public') THEN
  RAISE EXCEPTION 'Existing empty public schema required';
 END IF;
 IF EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='public') OR EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n
   ON n.oid=p.pronamespace WHERE n.nspname='public') OR EXISTS(SELECT 1 FROM pg_type t
   JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='public') THEN
  RAISE EXCEPTION 'Empty public schema required; existing objects are never overwritten';
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname IN ('hmi_migrator','hmi_app')) THEN
  RAISE EXCEPTION 'HMI roles already exist; existing identities are never changed';
 END IF;
END $$;
CREATE ROLE hmi_migrator NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
 NOREPLICATION NOBYPASSRLS;
CREATE ROLE hmi_app NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT hmi_migrator TO CURRENT_USER;
REVOKE CREATE, TEMPORARY ON DATABASE hmi_mes_demo FROM PUBLIC;
GRANT CONNECT, CREATE ON DATABASE hmi_mes_demo TO hmi_migrator;
GRANT CONNECT ON DATABASE hmi_mes_demo TO hmi_app;
ALTER SCHEMA public OWNER TO hmi_migrator;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO hmi_app;
SET LOCAL ROLE hmi_migrator;
"""
    footer = f"""
INSERT INTO public.alembic_version(version_num) VALUES ('{REVISION}');
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, hmi_app;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, hmi_app;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, hmi_app;
{chr(10).join(grants)}
SET search_path = public;
SET row_security = on;
RESET ROLE;
DO $$ BEGIN
 IF (SELECT count(*) FROM public.alembic_version) <> 1
   OR (SELECT version_num FROM public.alembic_version) <> '{REVISION}'
   OR (SELECT count(*) FROM pg_tables WHERE schemaname='public') <> {len(tables)}
   OR EXISTS(SELECT 1 FROM pg_tables WHERE schemaname='public' AND tableowner<>'hmi_migrator')
   OR EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      JOIN pg_roles r ON r.oid=c.relowner WHERE n.nspname='public' AND r.rolname='hmi_app')
   OR pg_has_role('hmi_app','hmi_migrator','MEMBER')
   OR has_schema_privilege('hmi_app','public','CREATE')
   OR has_database_privilege('hmi_app','hmi_mes_demo','TEMP')
   OR has_table_privilege('hmi_app','public.alembic_version','INSERT,UPDATE,DELETE,TRUNCATE')
   OR EXISTS(SELECT 1 FROM pg_roles WHERE rolname IN ('hmi_app','hmi_migrator')
       AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
         OR rolreplication OR rolbypassrls)) THEN
  RAISE EXCEPTION 'Unexpected schema, owner, role or privilege state';
 END IF;
END $$;
COMMIT;
SELECT (SELECT version_num FROM public.alembic_version) AS schema_revision,
 (SELECT count(*) FROM public.sites) AS sites,
 (SELECT count(*) FROM public.lines) AS lines,
 (SELECT count(*) FROM public.assets) AS assets,
 (SELECT count(*) FROM public.tag_definitions) AS tags,
 (SELECT count(*) FROM public.roles) AS roles;
"""
    result = header + schema + baseline + footer
    if re.search(r"^\\", result, flags=re.MULTILINE):
        raise RuntimeError("psql-only command remained in generated SQL")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        with tempfile.TemporaryDirectory(prefix="hmi-neon-generator-") as temporary:
            with disposable_postgres(Path(temporary)) as postgres:
                result = render(postgres)
        target = ROOT / "deploy/neon-init.sql"
        if args.check:
            if target.read_text() != result:
                raise RuntimeError("Generated Neon SQL differs from canonical14")
        else:
            temporary = target.with_suffix(".sql.tmp")
            temporary.write_text(result)
            temporary.replace(target)
    except Exception:
        print(
            "Neon SQL generation failed; verify local Docker, cached PG17 and canonical schema",
            file=sys.stderr,
        )
        return 1
    print("Canonical14 Neon SQL verified" if args.check else "Canonical14 Neon SQL generated")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
