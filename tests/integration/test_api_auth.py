"""Real PostgreSQL user/session/authorization and transaction-boundary tests."""

import hashlib
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command

from apps.api.auth import (
    ABSOLUTE_TIMEOUT,
    IDLE_TIMEOUT,
    PERMISSIONS,
    ROLES,
    AuthenticationError,
    AuthorizationError,
    UserAuth,
)
from apps.api.database import migration_config

ORIGIN = "https://hmi.example.test"
AT = datetime(2026, 10, 20, 12, tzinfo=UTC)


@pytest.fixture
def clock():
    return [AT]


@pytest.fixture
def auth(migrated, clock):
    return UserAuth(migrated, allowed_origins={ORIGIN}, clock=lambda: clock[0])


def login(auth, name, password, **changes):
    challenge = auth.begin_login()
    arguments = dict(
        challenge_token=challenge.token, csrf_token=challenge.csrf_token, origin=ORIGIN
    )
    arguments.update(changes)
    return auth.login(name, password, **arguments)


def account(auth, name="person", roles=frozenset({"operator"}), *, changed=True):
    password = secrets.token_urlsafe(24)
    auth.bootstrap_user(name, password, roles=roles)
    session = login(auth, name, password)
    if changed:
        password = secrets.token_urlsafe(24)
        session = auth.change_password(session.token, password, **protection(session))
    return session, password


def protection(session):
    return {"csrf_token": session.csrf_token, "origin": ORIGIN}


def scalar(auth, sql):
    with auth.engine.connect() as db:
        return db.execute(sa.text(sql)).scalar_one()


def test_migration_has_no_accounts_and_roundtrips(engine):
    with engine.connect() as db:
        command.upgrade(migration_config(db), "0003_user_auth")
        assert db.execute(sa.text("SELECT count(*) FROM users")).scalar_one() == 0
        assert set(db.execute(sa.text("SELECT role FROM roles")).scalars()) == ROLES
        db.rollback()
        command.downgrade(migration_config(db), "0002_ingestion")
        assert db.execute(sa.text("SELECT to_regclass('users')")).scalar_one() is None
        db.rollback()
        command.upgrade(migration_config(db), "0003_user_auth")
        assert db.execute(sa.text("SELECT count(*) FROM users")).scalar_one() == 0


def test_bootstrap_normalizes_login_and_never_resets(auth):
    password = secrets.token_urlsafe(24)
    principal = auth.bootstrap_user("Operator.A", password, roles={"operator"})
    assert principal.login == "operator.a" and principal.must_change_password
    with pytest.raises(ValueError, match="cannot reset"):
        auth.bootstrap_user("OPERATOR.A", secrets.token_urlsafe(24), roles={"equipment_admin"})
    assert login(auth, "OPERATOR.A", password).principal.roles == {"operator"}
    assert scalar(auth, "SELECT count(*) FROM auth_audit WHERE action='user_bootstrapped'") == 1


@pytest.mark.parametrize("roles", [set(), {"simulator_operator"}, {"production"}, {"admin"}])
def test_invalid_roles_are_rejected_without_users(auth, roles):
    with pytest.raises(ValueError):
        auth.bootstrap_user("person", secrets.token_urlsafe(24), roles=roles)
    assert scalar(auth, "SELECT count(*) FROM users") == 0


def test_salted_scrypt_and_only_hashed_session_material(auth, caplog):
    password = secrets.token_urlsafe(24)
    for name in ("one", "two"):
        auth.bootstrap_user(name, password, roles={"viewer"})
    with auth.engine.connect() as db:
        users = db.execute(sa.text("SELECT salt,password_hash FROM users ORDER BY login")).all()
    assert users[0].salt != users[1].salt and users[0].password_hash != users[1].password_hash
    assert (
        hashlib.scrypt(
            password.encode(),
            salt=users[0].salt,
            n=2**15,
            r=8,
            p=1,
            dklen=32,
            maxmem=64 * 1024 * 1024,
        )
        == users[0].password_hash
    )
    challenge = auth.begin_login()
    session = auth.login(
        "one",
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=ORIGIN,
    )
    with auth.engine.connect() as db:
        row = db.execute(sa.text("SELECT token_digest,csrf_digest FROM auth_sessions")).one()
        assert row.token_digest == hashlib.sha256(session.token.encode()).digest()
        assert row.csrf_digest == hashlib.sha256(session.csrf_token.encode()).digest()
        assert db.execute(sa.text("SELECT count(*) FROM auth_login_challenges")).scalar_one() == 0
        dump = repr(db.execute(sa.text("SELECT * FROM auth_audit")).all())
    for secret in (
        password,
        session.token,
        session.csrf_token,
        challenge.token,
        challenge.csrf_token,
    ):
        assert secret not in repr(session) + repr(challenge) + dump + caplog.text


def test_initial_password_cannot_access_business_and_rotation_invalidates_all(auth):
    initial, password = account(auth, changed=False)
    other = login(auth, "person", password)
    assert auth.authenticate(initial.token).must_change_password
    for permission in ("read", "operate"):
        with pytest.raises(AuthorizationError):
            with auth.authorized_transaction(
                initial.token, permission=permission, **protection(initial)
            ):
                pytest.fail("initial password gained business access")
    with pytest.raises(ValueError, match="must differ"):
        auth.change_password(initial.token, password, **protection(initial))
    rotated = auth.change_password(initial.token, secrets.token_urlsafe(24), **protection(initial))
    assert not rotated.principal.must_change_password
    assert rotated.token != initial.token and rotated.csrf_token != initial.csrf_token
    for old in (initial, other):
        with pytest.raises(AuthenticationError):
            auth.authenticate(old.token)
    assert auth.authenticate(rotated.token) == rotated.principal


@pytest.mark.parametrize("name,password", [("missing", "invalid-value"), ("???", "short")])
def test_bad_credentials_are_generic(auth, name, password):
    with pytest.raises(AuthenticationError, match="^invalid credentials$"):
        login(auth, name, password)


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "https://hmi.example.test/",
        "https://hmi.example.test.evil",
        "http://hmi.example.test",
    ],
)
def test_exact_origin_rejected_for_login_and_mutation(auth, origin):
    session, password = account(auth)
    with pytest.raises(AuthorizationError, match="origin"):
        login(auth, "person", password, origin=origin)
    with pytest.raises(AuthorizationError, match="origin"):
        with auth.authorized_transaction(
            session.token, permission="operate", csrf_token=session.csrf_token, origin=origin
        ):
            pytest.fail("invalid Origin accepted")


def test_csrf_is_bound_to_challenge_and_session(auth):
    session, password = account(auth)
    other = auth.begin_login()
    with pytest.raises(AuthorizationError, match="CSRF"):
        login(auth, "person", password, csrf_token=other.csrf_token)
    for csrf in (None, "bad", other.csrf_token):
        with pytest.raises(AuthorizationError, match="CSRF"):
            auth.revoke(session.token, csrf_token=csrf, origin=ORIGIN)
    assert auth.authenticate(session.token)


def test_login_challenge_single_use_and_expiration(auth, clock):
    password = secrets.token_urlsafe(24)
    auth.bootstrap_user("person", password, roles={"viewer"})
    challenge = auth.begin_login()
    args = dict(challenge_token=challenge.token, csrf_token=challenge.csrf_token, origin=ORIGIN)
    auth.login("person", password, **args)
    with pytest.raises(AuthenticationError):
        auth.login("person", password, **args)
    challenge = auth.begin_login()
    clock[0] += timedelta(minutes=5)
    with pytest.raises(AuthenticationError):
        auth.login(
            "person",
            password,
            challenge_token=challenge.token,
            csrf_token=challenge.csrf_token,
            origin=ORIGIN,
        )


def test_idle_expiry_and_absolute_cap(auth, clock):
    session, _ = account(auth)
    clock[0] += IDLE_TIMEOUT - timedelta(seconds=1)
    auth.authenticate(session.token)
    assert scalar(auth, "SELECT max(idle_expires_at) FROM auth_sessions") == clock[0] + IDLE_TIMEOUT
    clock[0] += IDLE_TIMEOUT
    with pytest.raises(AuthenticationError):
        auth.authenticate(session.token)
    clock[0] = AT
    session, _ = account(auth, "second")
    for step in range(1, 17):
        clock[0] = AT + timedelta(minutes=29 * step)
        auth.authenticate(session.token)
    assert scalar(auth, "SELECT max(idle_expires_at) FROM auth_sessions") == AT + ABSOLUTE_TIMEOUT
    clock[0] = AT + ABSOLUTE_TIMEOUT
    with pytest.raises(AuthenticationError):
        auth.authenticate(session.token)


def test_clock_reversal_and_malformed_credentials_fail_closed(auth, clock):
    session, _ = account(auth)
    clock[0] -= timedelta(seconds=1)
    with pytest.raises(AuthenticationError):
        auth.authenticate(session.token)
    for token in (None, "", "not-a-token", "x" * 43):
        with pytest.raises(AuthenticationError):
            auth.authenticate(token)


@pytest.mark.parametrize("role", sorted(ROLES))
def test_role_matrix_has_no_implicit_inheritance(auth, role):
    session, _ = account(auth, roles={role})
    for permission, permitted_roles in PERMISSIONS.items():
        if role in permitted_roles:
            with auth.authorized_transaction(
                session.token, permission=permission, **protection(session)
            ) as unit:
                assert unit.principal.roles == {role}
        else:
            with pytest.raises(AuthorizationError):
                with auth.authorized_transaction(
                    session.token, permission=permission, **protection(session)
                ):
                    pytest.fail("unexpected permission")
    with pytest.raises(ValueError, match="unknown permission"):
        with auth.authorized_transaction(session.token, permission="edit_quantities"):
            pytest.fail("undefined permission accepted")


def test_explicit_multiple_roles(auth):
    session, _ = account(auth, roles={"operator", "production_manager"})
    for permission in ("operate", "production_manage"):
        with auth.authorized_transaction(
            session.token, permission=permission, **protection(session)
        ):
            pass
    with pytest.raises(AuthorizationError):
        auth.set_user_roles(session.token, "person", {"equipment_admin"}, **protection(session))


def test_role_and_active_changes_revoke_all_sessions(auth):
    admin, _ = account(auth, "admin", {"equipment_admin"})
    user, password = account(auth)
    second = login(auth, "person", password)
    auth.set_user_roles(admin.token, "person", {"production_manager"}, **protection(admin))
    for session in (user, second):
        with pytest.raises(AuthenticationError):
            auth.authenticate(session.token)
    user = login(auth, "person", password)
    assert user.principal.roles == {"production_manager"}
    auth.set_user_active(admin.token, "person", False, **protection(admin))
    with pytest.raises(AuthenticationError):
        auth.authenticate(user.token)
    with pytest.raises(AuthenticationError):
        login(auth, "person", password)
    auth.set_user_active(admin.token, "person", True, **protection(admin))
    with pytest.raises(AuthenticationError):
        auth.authenticate(user.token)
    assert login(auth, "person", password)
    auth.set_user_roles(admin.token, "admin", {"viewer"}, **protection(admin))
    with pytest.raises(AuthenticationError):
        auth.authenticate(admin.token)


def test_logout_works_for_initial_viewer(auth):
    session, _ = account(auth, roles={"viewer"}, changed=False)
    auth.revoke(session.token, **protection(session))
    with pytest.raises(AuthenticationError):
        auth.authenticate(session.token)


def test_business_writes_and_idle_refresh_commit_or_rollback_together(auth, clock):
    session, _ = account(auth)
    with auth.engine.begin() as db:
        db.execute(sa.text("CREATE TABLE test_business (value INTEGER NOT NULL)"))
    clock[0] += timedelta(minutes=1)
    with pytest.raises(RuntimeError, match="business failure"):
        with auth.authorized_transaction(
            session.token, permission="operate", **protection(session)
        ) as unit:
            unit.connection.execute(sa.text("INSERT INTO test_business VALUES (1)"))
            assert scalar(auth, "SELECT count(*) FROM test_business") == 0
            raise RuntimeError("business failure")
    assert scalar(auth, "SELECT count(*) FROM test_business") == 0
    assert scalar(auth, "SELECT max(last_seen_at) FROM auth_sessions") == AT
    with auth.authorized_transaction(
        session.token, permission="operate", **protection(session)
    ) as unit:
        unit.connection.execute(sa.text("INSERT INTO test_business VALUES (2)"))
    assert scalar(auth, "SELECT value FROM test_business") == 2
    assert scalar(auth, "SELECT max(last_seen_at) FROM auth_sessions") == clock[0]


def test_revocation_cannot_interleave_authorized_business_transaction(auth):
    admin, _ = account(auth, "admin", {"equipment_admin"})
    user, _ = account(auth)
    started = threading.Event()
    released = threading.Event()

    def disable():
        started.set()
        auth.set_user_active(admin.token, "person", False, **protection(admin))
        released.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with auth.authorized_transaction(user.token, permission="operate", **protection(user)):
            future = pool.submit(disable)
            assert started.wait(2)
            # The concurrent transaction has reached the DB row lock, not merely
            # waited for Python scheduling. Poll only lock metadata, never secrets.
            import time

            deadline = time.monotonic() + 3
            while not scalar(
                auth,
                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                "WHERE datname=current_database() AND wait_event_type='Lock' "
                "AND query LIKE 'SELECT * FROM users%')",
            ):
                assert time.monotonic() < deadline
                time.sleep(0.01)
            assert not released.is_set()
        future.result(timeout=3)
    assert released.is_set()
    with pytest.raises(AuthenticationError):
        auth.authenticate(user.token)


def test_schema_migration_precedes_auth_table_locks(auth):
    """A queued auth request must not hold users while waiting for migration."""
    import time

    from apps.api.database import SCHEMA_LOCK_ID

    session, _ = account(auth)

    def access():
        with auth.authorized_transaction(session.token, permission="read") as unit:
            return unit.principal.login

    with ThreadPoolExecutor(max_workers=1) as pool:
        with auth.engine.begin() as migration:
            migration.execute(
                sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCHEMA_LOCK_ID}
            )
            future = pool.submit(access)
            deadline = time.monotonic() + 5
            while not scalar(
                auth,
                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                "WHERE datname=current_database() AND wait_event_type='Lock' "
                "AND query LIKE 'SELECT pg_advisory_xact_lock_shared%')",
            ):
                assert time.monotonic() < deadline
                time.sleep(0.01)
            migration.execute(sa.text("SET LOCAL lock_timeout = '1s'"))
            migration.execute(sa.text("ALTER TABLE users ADD COLUMN migration_probe BOOLEAN"))
            assert not future.done()
        assert future.result(timeout=5) == "person"
