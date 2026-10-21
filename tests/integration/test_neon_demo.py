"""Private account SQL exercised by a real limited owner and runtime DB login."""

import secrets
import stat

import psycopg
import pytest
from psycopg import sql

from apps.api.auth import AuthorizationError, UserAuth
from apps.api.database import create_database_engine
from deploy.generate_neon_demo import ACCOUNTS, render_demo, write_private
from deploy.generate_neon_init import connect, disposable_postgres, render

ORIGIN = "https://neon-demo.example.test"


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    with disposable_postgres(tmp_path_factory.mktemp("neon-demo-pg")) as postgres:
        installer = render(postgres)
        passwords = {name: secrets.token_urlsafe(32) for name in ACCOUNTS}
        seed = render_demo(postgres, passwords, installer)
        owner_password = secrets.token_urlsafe(32)
        with connect(postgres.url, autocommit=True) as db:
            db.execute("DROP DATABASE hmi_mes_demo WITH (FORCE)")
            db.execute("DROP ROLE hmi_app")
            db.execute("DROP ROLE hmi_migrator")
            db.execute(
                sql.SQL("CREATE ROLE demo_owner LOGIN CREATEROLE PASSWORD {}").format(
                    sql.Literal(owner_password)
                )
            )
        yield postgres, installer, seed, passwords, owner_password


@pytest.fixture
def state(cluster):
    postgres, installer, seed, passwords, owner_password = cluster
    with connect(postgres.url, autocommit=True) as db:
        db.execute("CREATE DATABASE hmi_mes_demo OWNER demo_owner")
    owner_url = postgres.url.set(
        username="demo_owner", password=owner_password, database="hmi_mes_demo"
    )
    with connect(owner_url) as db:
        db.execute(installer)
    try:
        yield owner_url, seed, passwords, postgres
    finally:
        with connect(postgres.url, autocommit=True) as db:
            db.execute("DROP DATABASE hmi_mes_demo WITH (FORCE)")
            db.execute("DROP ROLE hmi_app")
            db.execute("DROP ROLE hmi_migrator")


def snapshot(url):
    with connect(url) as db:
        return tuple(
            db.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
            for table in ("users", "user_roles", "auth_audit")
        )


def test_limited_owner_seed_runtime_login_first_change_permissions_and_no_business(state):
    owner, seed, passwords, _ = state
    with connect(owner) as db:
        assert db.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
        ).fetchone() == (False,)
        db.execute(seed)
    assert all(value not in seed for value in passwords.values())
    assert '"audit_id"' not in seed
    with connect(owner) as db:
        assert db.execute("SELECT count(*) FROM users").fetchone() == (2,)
        assert db.execute("SELECT count(*) FROM auth_audit").fetchone() == (2,)
        for table in ("source_streams", "work_orders", "command_requests", "production_events"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        password = secrets.token_urlsafe(32)
        db.execute(sql.SQL("ALTER ROLE hmi_app LOGIN PASSWORD {}").format(sql.Literal(password)))
    engine = create_database_engine(owner.set(username="hmi_app", password=password))
    try:
        auth = UserAuth(engine, allowed_origins={ORIGIN})
        for name, expected_roles in ACCOUNTS.items():
            challenge = auth.begin_login()
            session = auth.login(
                name,
                passwords[name],
                challenge_token=challenge.token,
                csrf_token=challenge.csrf_token,
                origin=ORIGIN,
            )
            assert session.principal.must_change_password
            assert session.principal.roles == expected_roles
            with pytest.raises(AuthorizationError):
                with auth.authorized_transaction(session.token, permission="read"):
                    pass
            session = auth.change_password(
                session.token,
                secrets.token_urlsafe(32),
                csrf_token=session.csrf_token,
                origin=ORIGIN,
            )
            with auth.authorized_transaction(session.token, permission="read"):
                pass
            if name == "viewer":
                with pytest.raises(AuthorizationError):
                    with auth.authorized_transaction(
                        session.token,
                        permission="operate",
                        csrf_token=session.csrf_token,
                        origin=ORIGIN,
                    ):
                        pass
            else:
                with auth.authorized_transaction(
                    session.token,
                    permission="operate",
                    csrf_token=session.csrf_token,
                    origin=ORIGIN,
                ):
                    pass
        with connect(owner) as db:
            assert db.execute(
                "SELECT count(DISTINCT audit_id)=count(*) FROM auth_audit"
            ).fetchone() == (True,)
    finally:
        engine.dispose()


def test_repeat_and_any_existing_account_refuse_without_reset(state):
    owner, seed, _, _ = state
    with connect(owner) as db:
        db.execute(seed)
    before = snapshot(owner)
    with connect(owner) as db:
        with pytest.raises(psycopg.errors.RaiseException, match="never reset"):
            db.execute(seed)
        db.rollback()
    assert snapshot(owner) == before


def test_existing_unrelated_user_is_preserved(state):
    owner, seed, _, _ = state
    engine = create_database_engine(owner)
    try:
        UserAuth(engine, allowed_origins={ORIGIN}).bootstrap_user(
            "original", secrets.token_urlsafe(32), roles={"viewer"}
        )
    finally:
        engine.dispose()
    before = snapshot(owner)
    with connect(owner) as db:
        with pytest.raises(psycopg.errors.RaiseException, match="never reset"):
            db.execute(seed)
        db.rollback()
    assert snapshot(owner) == before


def test_wrong_database_and_revision_refuse(state):
    owner, seed, _, postgres = state
    with connect(postgres.url) as db:
        with pytest.raises(psycopg.errors.RaiseException, match="dedicated"):
            db.execute(seed)
        db.rollback()
    with connect(owner) as db:
        db.execute("SET LOCAL ROLE hmi_migrator")
        db.execute("UPDATE alembic_version SET version_num='wrong'")
    with connect(owner) as db:
        with pytest.raises(psycopg.errors.RaiseException, match="canonical14"):
            db.execute(seed)
        db.rollback()
        assert db.execute("SELECT count(*) FROM users").fetchone() == (0,)


def test_private_outputs_exclusive_permissions_and_cleanup(tmp_path, capsys):
    directory = tmp_path / "private"
    write_private(directory, lambda: ("private hash", "private password"))
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name in ("seed.sql", "LOGIN.md"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    called = []
    with pytest.raises(FileExistsError):
        write_private(directory, lambda: called.append(True))
    assert not called
    assert (directory / "LOGIN.md").read_text() == "private password"
    second = tmp_path / "partial"
    second.mkdir()
    (second / "LOGIN.md").write_text("original")
    with pytest.raises(FileExistsError):
        write_private(second, lambda: called.append(True))
    assert not (second / "seed.sql").exists()
    assert (second / "LOGIN.md").read_text() == "original"
    assert capsys.readouterr().out == ""
