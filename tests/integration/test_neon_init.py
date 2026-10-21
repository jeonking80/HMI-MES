"""Canonical14 SQL Editor installation as a non-superuser CREATEROLE DB owner."""

import secrets
from pathlib import Path

import psycopg
import pytest
from alembic import command
from psycopg import sql
from sqlalchemy import text

from apps.api.auth import UserAuth
from apps.api.database import create_database_engine, migration_config
from deploy.generate_neon_init import (
    REVISION,
    clean_dump,
    connect,
    disposable_postgres,
    docker,
    render,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    with disposable_postgres(tmp_path_factory.mktemp("neon-init-pg")) as postgres:
        installer = render(postgres)
        password = secrets.token_urlsafe(32)
        with connect(postgres.url, autocommit=True) as db:
            db.execute(
                sql.SQL("CREATE ROLE neon_test_owner LOGIN CREATEROLE PASSWORD {}").format(
                    sql.Literal(password)
                )
            )
        # pg_dump restores can flatten associative CHECK expressions. Compare
        # to an unmodified canonical dump restored once, rather than weaken DDL.
        with connect(postgres.url, autocommit=True) as db:
            db.execute("CREATE DATABASE hmi_expected_dump")
        with connect(postgres.url.set(database="hmi_expected_dump")) as db:
            db.execute(schema(postgres, "hmi_canonical14"))
        yield (
            postgres,
            installer,
            postgres.url.set(
                username="neon_test_owner", password=password, database="hmi_mes_demo"
            ),
        )


@pytest.fixture
def state(cluster):
    postgres, installer, owner_url = cluster
    with connect(postgres.url, autocommit=True) as db:
        db.execute("CREATE DATABASE hmi_mes_demo OWNER neon_test_owner")
    try:
        yield postgres, installer, owner_url
    finally:
        with connect(postgres.url, autocommit=True) as db:
            db.execute("DROP DATABASE hmi_mes_demo WITH (FORCE)")
            db.execute("DROP ROLE IF EXISTS hmi_app")
            db.execute("DROP ROLE IF EXISTS hmi_migrator")


def schema(postgres, database):
    return clean_dump(
        docker(
            "exec",
            "--user",
            "postgres",
            postgres.container,
            "pg_dump",
            "--dbname=" + database,
            "--schema-only",
            "--schema=public",
            "--no-owner",
            "--no-privileges",
        )
    )


def install(state):
    _, installer, owner_url = state
    with connect(owner_url) as db:
        db.execute(installer)


def absent(state):
    _, _, owner_url = state
    with connect(owner_url) as db:
        assert db.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname IN ('hmi_app','hmi_migrator')"
        ).fetchone() == (0,)


def test_checked_in_sql_is_exact_canonical_generation(cluster):
    _, installer, _ = cluster
    assert installer == (ROOT / "deploy/neon-init.sql").read_text()
    assert "\\restrict" not in installer and "\\unrestrict" not in installer
    assert "CREATE SCHEMA public;" not in installer
    assert "PASSWORD" not in installer and "CREATE ROLE hmi_app NOLOGIN" in installer


def test_limited_owner_installs_exact_schema_baseline_and_noop_canonical_upgrade(state):
    postgres, _, owner_url = state
    with connect(owner_url) as db:
        assert db.execute(
            "SELECT rolsuper,rolcreaterole,rolcreatedb FROM pg_roles WHERE rolname=current_user"
        ).fetchone() == (False, True, False)
    install(state)
    before = schema(postgres, "hmi_mes_demo")
    assert before == schema(postgres, "hmi_expected_dump")
    with connect(owner_url) as db:
        assert db.execute("SELECT version_num FROM public.alembic_version").fetchone() == (
            REVISION,
        )
        assert db.execute(
            "SELECT (SELECT count(*) FROM public.sites),(SELECT count(*) FROM public.lines),"
            "(SELECT count(*) FROM public.assets),(SELECT count(*) FROM public.tag_definitions),"
            "(SELECT count(*) FROM public.roles),(SELECT count(*) FROM public.users)"
        ).fetchone() == (1, 1, 3, 48, 4, 0)
        assert db.execute(
            "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname='public'"
        ).fetchall() == [("hmi_migrator",)]
        assert db.execute(
            "SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolbypassrls "
            "FROM pg_roles WHERE rolname='hmi_app'"
        ).fetchone() == (False, False, False, False, False)
    engine = create_database_engine(owner_url)
    try:
        with engine.connect() as db:
            db.execute(text("SET ROLE hmi_migrator"))
            db.commit()
            command.upgrade(migration_config(db), REVISION)
    finally:
        engine.dispose()
    assert schema(postgres, "hmi_mes_demo") == before


def test_repeat_refuses_without_resetting_baseline_or_roles(state):
    postgres, installer, owner_url = state
    install(state)
    before = schema(postgres, "hmi_mes_demo")
    with connect(owner_url) as db:
        with pytest.raises(psycopg.errors.RaiseException):
            db.execute(installer)
        db.rollback()
        assert db.execute("SELECT version_num FROM public.alembic_version").fetchone() == (
            REVISION,
        )
        assert db.execute("SELECT count(*) FROM public.tag_definitions").fetchone() == (48,)
    assert schema(postgres, "hmi_mes_demo") == before


@pytest.mark.parametrize("preexisting", ["table", "function", "type", "role"])
def test_preexisting_guard_preserves_original_objects_and_roles(state, preexisting):
    postgres, installer, owner_url = state
    with connect(owner_url) as db:
        if preexisting == "table":
            db.execute("CREATE TABLE public.existing(value TEXT)")
            db.execute("INSERT INTO public.existing VALUES ('original')")
        elif preexisting == "function":
            db.execute(
                "CREATE FUNCTION public.existing() RETURNS integer LANGUAGE SQL AS 'SELECT 7'"
            )
        elif preexisting == "type":
            db.execute("CREATE TYPE public.existing AS ENUM ('original')")
        else:
            db.execute("CREATE ROLE hmi_migrator NOLOGIN")
    before = schema(postgres, "hmi_mes_demo")
    with connect(owner_url) as db:
        with pytest.raises(psycopg.errors.RaiseException):
            db.execute(installer)
        db.rollback()
        assert db.execute("SELECT count(*) FROM pg_roles WHERE rolname='hmi_app'").fetchone() == (
            0,
        )
        if preexisting == "table":
            assert db.execute("SELECT value FROM public.existing").fetchone() == ("original",)
    assert schema(postgres, "hmi_mes_demo") == before


def test_wrong_database_never_touches_canonical_or_creates_roles(state):
    postgres, installer, _ = state
    before = schema(postgres, "hmi_canonical14")
    with connect(postgres.url) as db:
        with pytest.raises(psycopg.errors.RaiseException, match="dedicated hmi_mes_demo"):
            db.execute(installer)
        db.rollback()
    assert schema(postgres, "hmi_canonical14") == before
    absent(state)


def test_late_failure_rolls_back_role_schema_grants_and_allows_fresh_retry(state):
    _, installer, owner_url = state
    broken = installer.replace(
        "COMMIT;", "DO $$ BEGIN RAISE EXCEPTION 'late failure'; END $$;\nCOMMIT;"
    )
    with connect(owner_url) as db:
        with pytest.raises(psycopg.errors.RaiseException, match="late failure"):
            db.execute(broken)
        db.rollback()
    absent(state)
    install(state)


def test_runtime_role_real_login_workflow_locks_and_ddl_audit_denials(state):
    _, _, owner_url = state
    install(state)
    app_password = secrets.token_urlsafe(32)
    with connect(owner_url) as db:
        db.execute(
            sql.SQL("ALTER ROLE hmi_app LOGIN PASSWORD {}").format(sql.Literal(app_password))
        )
    app_url = owner_url.set(username="hmi_app", password=app_password)
    with connect(app_url) as db:
        assert db.execute(
            "SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls "
            "FROM pg_roles WHERE rolname=current_user"
        ).fetchone() == (False, False, False, False)
        assert db.execute(
            "SELECT pg_has_role(current_user,'hmi_migrator','MEMBER')"
        ).fetchone() == (False,)
        assert db.execute(
            "SELECT has_schema_privilege(current_user,'public','CREATE'),"
            "has_database_privilege(current_user,'hmi_mes_demo','TEMP')"
        ).fetchone() == (False, False)
        assert db.execute("SELECT version_num FROM public.alembic_version").fetchone() == (
            REVISION,
        )
    for statement in (
        "CREATE TABLE public.denied(id int)",
        "CREATE TEMP TABLE denied(id int)",
        "ALTER TABLE public.users ADD COLUMN denied int",
        "UPDATE public.alembic_version SET version_num='bad'",
        "DELETE FROM public.auth_audit",
        "TRUNCATE public.users",
    ):
        with connect(app_url) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute(statement)
    engine = create_database_engine(app_url)
    origin = "https://neon-test.example"
    password = secrets.token_urlsafe(32)
    try:
        auth = UserAuth(engine, allowed_origins={origin})
        auth.bootstrap_user("neon.operator", password, roles={"operator"})
        challenge = auth.begin_login()
        session = auth.login(
            "neon.operator",
            password,
            challenge_token=challenge.token,
            csrf_token=challenge.csrf_token,
            origin=origin,
        )
        session = auth.change_password(
            session.token, secrets.token_urlsafe(32), csrf_token=session.csrf_token, origin=origin
        )
        with auth.authorized_transaction(
            session.token, permission="read", refresh_idle=False
        ) as tx:
            assert (
                tx.connection.execute(text("SELECT id FROM lines FOR SHARE")).scalar_one()
                == "packaging-line-1"
            )
        auth.revoke(session.token, csrf_token=session.csrf_token, origin=origin)
    finally:
        engine.dispose()
