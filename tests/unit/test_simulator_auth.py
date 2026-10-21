"""Simulator-local authentication primitives, without publishing control routes."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from apps.simulator.auth import (
    ABSOLUTE_TIMEOUT,
    IDLE_TIMEOUT,
    LOGIN_TIMEOUT,
    AuthenticationError,
    AuthorizationError,
    SimulatorAuth,
)
from apps.simulator.storage import SimulatorStorage

ORIGIN = "https://simulator.example.test"
# Deliberate non-production fixture values; never runtime defaults.
INITIAL = "fixture initial password only"
CHANGED = "fixture changed password only"


@pytest.fixture
def environment(tmp_path):
    connection = sqlite3.connect(tmp_path / "simulator.sqlite3", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA user_version=7")
    clock = [datetime(2026, 10, 20, 12, tzinfo=UTC)]
    auth = SimulatorAuth(connection, allowed_origins={ORIGIN}, clock=lambda: clock[0])
    connection.execute("BEGIN IMMEDIATE")
    auth.initialize_schema()
    try:
        yield auth, connection, clock
    finally:
        connection.rollback()
        connection.close()


def sign_in(auth, *, login="test.operator", password=INITIAL):
    challenge = auth.begin_login()
    return auth.login(
        login,
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=ORIGIN,
    )


def provision(auth, *, role="simulator_operator", change=True):
    auth.bootstrap_user("test.operator", INITIAL, role=role)
    session = sign_in(auth)
    if change:
        return auth.change_password(
            session.token,
            CHANGED,
            csrf_token=session.csrf_token,
            origin=ORIGIN,
        )
    return session


def test_explicit_schema_and_bootstrap_do_not_change_simulator_version(environment):
    auth, db, _ = environment
    auth.initialize_schema()
    assert db.execute("PRAGMA user_version").fetchone()[0] == 7
    assert db.execute("SELECT count(*) FROM console_users").fetchone()[0] == 0
    principal = auth.bootstrap_user("Test.Operator", INITIAL, role="simulator_operator")
    assert principal.login == "test.operator"
    assert principal.must_change_password
    with pytest.raises(ValueError, match="already exists"):
        auth.bootstrap_user("TEST.OPERATOR", CHANGED, role="viewer")
    assert sign_in(auth).principal.role == "simulator_operator"


@pytest.mark.parametrize(
    "login,password,role",
    [
        ("", INITIAL, "viewer"),
        (" leading", INITIAL, "viewer"),
        ("a" * 65, INITIAL, "viewer"),
        ("x", "short", "viewer"),
        ("x", "x" * 1025, "viewer"),
        ("x", "x" * 12 + "\x00", "viewer"),
        ("x", INITIAL, "equipment_admin"),
        ("x", INITIAL, "admin"),
    ],
)
def test_bootstrap_validation_has_no_partial_user(environment, login, password, role):
    auth, db, _ = environment
    with pytest.raises(ValueError):
        auth.bootstrap_user(login, password, role=role)
    assert db.execute("SELECT count(*) FROM console_users").fetchone()[0] == 0


def test_salted_passwords_and_only_token_digests_persist(environment):
    auth, db, _ = environment
    auth.bootstrap_user("one", INITIAL, role="viewer")
    auth.bootstrap_user("two", INITIAL, role="viewer")
    rows = db.execute("SELECT salt,password_hash FROM console_users ORDER BY login").fetchall()
    assert rows[0]["salt"] != rows[1]["salt"]
    assert rows[0]["password_hash"] != rows[1]["password_hash"]
    session = sign_in(auth, login="one")
    stored = db.execute("SELECT * FROM console_sessions").fetchone()
    assert stored["token_digest"] == hashlib.sha256(session.token.encode()).digest()
    assert stored["csrf_digest"] == hashlib.sha256(session.csrf_token.encode()).digest()
    dump = " ".join(db.iterdump())
    assert session.token not in dump and session.csrf_token not in dump and INITIAL not in dump
    assert session.token not in repr(session) and session.csrf_token not in repr(session)


@pytest.mark.parametrize(
    "login,password",
    [
        ("missing", INITIAL),
        ("test.operator", "wrong password"),
        ("test.operator", ""),
        ("invalid login", INITIAL),
    ],
)
def test_invalid_login_has_common_error_and_no_session(environment, login, password):
    auth, db, _ = environment
    auth.bootstrap_user("test.operator", INITIAL, role="viewer")
    with pytest.raises(AuthenticationError, match="^invalid credentials$"):
        sign_in(auth, login=login, password=password)
    assert db.execute("SELECT count(*) FROM console_sessions").fetchone()[0] == 0


def test_initial_password_session_cannot_control_and_rotation_revokes_all(environment):
    auth, _, _ = environment
    first = provision(auth, change=False)
    second = sign_in(auth)
    with pytest.raises(AuthorizationError):
        auth.authorize_mutation(first.token, csrf_token=first.csrf_token, origin=ORIGIN)
    rotated = auth.change_password(
        first.token,
        CHANGED,
        csrf_token=first.csrf_token,
        origin=ORIGIN,
    )
    assert rotated.token != first.token and rotated.csrf_token != first.csrf_token
    for stale in (first, second):
        with pytest.raises(AuthenticationError):
            auth.authenticate(stale.token)
    assert not auth.authorize_mutation(
        rotated.token,
        csrf_token=rotated.csrf_token,
        origin=ORIGIN,
    ).must_change_password
    with pytest.raises(AuthenticationError):
        sign_in(auth)
    assert sign_in(auth, password=CHANGED).principal.login == "test.operator"


def test_viewer_can_read_and_logout_but_cannot_mutate(environment):
    auth, _, _ = environment
    session = provision(auth, role="viewer")
    assert auth.authenticate(session.token).role == "viewer"
    with pytest.raises(AuthorizationError):
        auth.authorize_mutation(session.token, csrf_token=session.csrf_token, origin=ORIGIN)
    auth.revoke(session.token, csrf_token=session.csrf_token, origin=ORIGIN)
    with pytest.raises(AuthenticationError):
        auth.authenticate(session.token)


@pytest.mark.parametrize(
    "csrf,origin",
    [
        (None, ORIGIN),
        ("", ORIGIN),
        ("other", ORIGIN),
        ("correct", None),
        ("correct", "null"),
        ("correct", "https://evil.example.test"),
        ("correct", ORIGIN + ".evil.test"),
        ("correct", ORIGIN + "/"),
    ],
)
def test_mutation_csrf_and_exact_origin(environment, csrf, origin):
    auth, db, _ = environment
    session = provision(auth)
    csrf = session.csrf_token if csrf == "correct" else csrf
    for action in (
        lambda: auth.authorize_mutation(session.token, csrf_token=csrf, origin=origin),
        lambda: auth.revoke(session.token, csrf_token=csrf, origin=origin),
        lambda: auth.change_password(
            session.token,
            "another fixture password",
            csrf_token=csrf,
            origin=origin,
        ),
    ):
        with pytest.raises(AuthorizationError):
            action()
    assert auth.authenticate(session.token).role == "simulator_operator"
    assert db.execute("SELECT count(*) FROM console_users").fetchone()[0] == 1


def test_csrf_token_from_another_session_is_rejected(environment):
    auth, _, _ = environment
    first = provision(auth)
    second = sign_in(auth, password=CHANGED)
    with pytest.raises(AuthorizationError):
        auth.authorize_mutation(first.token, csrf_token=second.csrf_token, origin=ORIGIN)


def test_login_challenge_guards_origin_csrf_expiry_and_success_replay(environment):
    auth, _, clock = environment
    auth.bootstrap_user("test.operator", INITIAL, role="viewer")
    challenge = auth.begin_login()
    arguments = dict(
        challenge_token=challenge.token, csrf_token=challenge.csrf_token, origin=ORIGIN
    )
    with pytest.raises(AuthorizationError):
        auth.login("test.operator", INITIAL, **{**arguments, "origin": "null"})
    with pytest.raises(AuthorizationError):
        auth.login("test.operator", INITIAL, **{**arguments, "csrf_token": "missing"})
    auth.login("test.operator", INITIAL, **arguments)
    with pytest.raises(AuthenticationError):
        auth.login("test.operator", INITIAL, **arguments)
    challenge = auth.begin_login()
    clock[0] += LOGIN_TIMEOUT
    with pytest.raises(AuthenticationError):
        auth.login(
            "test.operator",
            INITIAL,
            challenge_token=challenge.token,
            csrf_token=challenge.csrf_token,
            origin=ORIGIN,
        )


def test_idle_timeout_boundary_and_clock_rollback_fail_closed(environment):
    auth, _, clock = environment
    session = provision(auth)
    clock[0] += IDLE_TIMEOUT - timedelta(microseconds=1)
    assert auth.authenticate(session.token).login == "test.operator"
    clock[0] += IDLE_TIMEOUT
    with pytest.raises(AuthenticationError):
        auth.authenticate(session.token)
    clock[0] -= timedelta(days=1)
    with pytest.raises(AuthenticationError):
        auth.authenticate(session.token)


def test_activity_cannot_extend_absolute_expiry(environment):
    auth, _, clock = environment
    session = provision(auth)
    start = clock[0]
    while clock[0] + timedelta(minutes=20) < start + ABSOLUTE_TIMEOUT:
        clock[0] += timedelta(minutes=20)
        auth.authenticate(session.token)
    clock[0] = session.expires_at
    with pytest.raises(AuthenticationError):
        auth.authenticate(session.token)


@pytest.mark.parametrize("change", ["active=0", "auth_version=auth_version+1", "role='viewer'"])
def test_live_account_state_invalidates_existing_session(environment, change):
    auth, db, _ = environment
    session = provision(auth)
    # Direct SQL models explicit local account maintenance; there is no runtime admin bypass.
    db.execute("UPDATE console_users SET " + change)
    with pytest.raises(AuthenticationError):
        auth.authenticate(session.token)


@pytest.mark.parametrize("token", [None, "", "short", "a" * 44, secrets.token_urlsafe(32)])
def test_invalid_session(environment, token):
    auth, _, _ = environment
    with pytest.raises(AuthenticationError):
        auth.authenticate(token)


def test_caller_transaction_owns_schema_credentials_and_audit_rollback(environment):
    auth, db, _ = environment
    db.rollback()
    with pytest.raises(RuntimeError, match="owner's transaction"):
        auth.initialize_schema()
    db.execute("BEGIN IMMEDIATE")
    auth.initialize_schema()
    provision(auth)
    assert db.in_transaction
    db.rollback()
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='console_users'").fetchone() is None


def test_explicit_schema_rejects_unsupported_version_without_overwrite(environment):
    auth, db, _ = environment
    db.execute("DROP TABLE auth_schema_version")
    db.execute("CREATE TABLE auth_schema_version(singleton INTEGER PRIMARY KEY,version INTEGER)")
    db.execute("INSERT INTO auth_schema_version VALUES (1,99)")
    with pytest.raises(ValueError, match="unsupported authentication schema"):
        auth.initialize_schema()
    assert db.execute("SELECT version FROM auth_schema_version").fetchone()[0] == 99


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "null",
        ORIGIN + "/",
        "https://user:pass@host",
        "https://host:bad",
        "http://public.example.test",
    ],
)
def test_configuration_rejects_wildcard_and_non_origin_allowlist(environment, origin):
    _, db, _ = environment
    with pytest.raises(ValueError):
        SimulatorAuth(db, allowed_origins={origin})


def test_committed_session_survives_auth_reconstruction(environment):
    auth, db, clock = environment
    session = provision(auth)
    db.commit()
    reopened_auth = SimulatorAuth(db, allowed_origins={ORIGIN}, clock=lambda: clock[0])
    db.execute("BEGIN IMMEDIATE")
    assert (
        reopened_auth.authorize_mutation(
            session.token,
            csrf_token=session.csrf_token,
            origin=ORIGIN,
        ).role
        == "simulator_operator"
    )


def test_clock_cannot_move_behind_last_authenticated_activity(environment):
    auth, _, clock = environment
    session = provision(auth)
    clock[0] += timedelta(minutes=10)
    auth.authenticate(session.token)
    clock[0] -= timedelta(seconds=1)
    with pytest.raises(AuthenticationError):
        auth.authenticate(session.token)


def test_auth_uses_canonical_storage_transaction_and_persists_after_reopen(tmp_path):
    path = tmp_path / "canonical-simulator.sqlite3"
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    with SimulatorStorage(path) as store:
        version = store.connection.execute("PRAGMA user_version").fetchone()[0]
        auth = SimulatorAuth(store.connection, allowed_origins={ORIGIN}, clock=lambda: now)
        with store.transaction():
            auth.initialize_schema()
            session = provision(auth)
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == version
    with SimulatorStorage(path) as store:
        assert store.recovery_required
        auth = SimulatorAuth(store.connection, allowed_origins={ORIGIN}, clock=lambda: now)
        with store.transaction():
            auth.initialize_schema()
            assert auth.authenticate(session.token).role == "simulator_operator"
        # Authentication never clears the PLC/process recovery gate.
        assert store.recovery_required
