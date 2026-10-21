"""Public read-CSRF/nonrefresh options on real PostgreSQL, independent of WS."""

import secrets
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from apps.api.auth import AuthenticationError, AuthorizationError, UserAuth

ORIGIN = "https://hmi.example.test"


@pytest.fixture
def state(migrated):
    clock = [datetime(2026, 10, 21, tzinfo=UTC)]
    auth = UserAuth(migrated, allowed_origins={ORIGIN}, clock=lambda: clock[0])
    password = secrets.token_urlsafe(24)
    auth.bootstrap_user("viewer", password, roles={"viewer"})
    challenge = auth.begin_login()
    initial = auth.login(
        "viewer",
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=ORIGIN,
    )
    session = auth.change_password(
        initial.token, secrets.token_urlsafe(24), csrf_token=initial.csrf_token, origin=ORIGIN
    )
    return auth, clock, session


def latest(auth):
    with auth.engine.connect() as db:
        return db.execute(sa.text("SELECT max(last_seen_at) FROM auth_sessions")).scalar_one()


def test_read_csrf_opt_in_and_default_read_backward_compatibility(state):
    auth, clock, session = state
    with auth.authorized_transaction(session.token, permission="read"):
        pass
    for csrf, origin in (
        (None, ORIGIN),
        (session.csrf_token, ORIGIN + "/"),
        (secrets.token_urlsafe(32), ORIGIN),
    ):
        with pytest.raises(AuthorizationError):
            with auth.authorized_transaction(
                session.token, permission="read", require_csrf=True, csrf_token=csrf, origin=origin
            ):
                pytest.fail("protected read accepted invalid mutation protection")
    clock[0] += timedelta(minutes=1)
    with auth.authorized_transaction(
        session.token,
        permission="read",
        require_csrf=True,
        csrf_token=session.csrf_token,
        origin=ORIGIN,
    ):
        pass
    assert latest(auth) == clock[0]


def test_passive_checks_never_refresh_idle_and_still_expire(state):
    auth, clock, session = state
    original = latest(auth)
    for minutes in (1, 10, 29):
        clock[0] = original + timedelta(minutes=minutes)
        with auth.authorized_transaction(session.token, permission="read", refresh_idle=False):
            pass
        assert latest(auth) == original
    clock[0] = original + timedelta(minutes=30)
    with pytest.raises(AuthenticationError):
        with auth.authorized_transaction(session.token, permission="read", refresh_idle=False):
            pytest.fail("passive read extended expired session")


def test_nonrefresh_still_checks_revocation_and_initial_password(state):
    auth, _, session = state
    auth.revoke(session.token, csrf_token=session.csrf_token, origin=ORIGIN)
    with pytest.raises(AuthenticationError):
        with auth.authorized_transaction(session.token, permission="read", refresh_idle=False):
            pytest.fail("revoked session passed passive check")
    password = secrets.token_urlsafe(24)
    auth.bootstrap_user("initial", password, roles={"viewer"})
    challenge = auth.begin_login()
    initial = auth.login(
        "initial",
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=ORIGIN,
    )
    with pytest.raises(AuthorizationError):
        with auth.authorized_transaction(initial.token, permission="read", refresh_idle=False):
            pytest.fail("initial password passed passive business read")


def test_require_csrf_false_cannot_disable_mutation_protection(state):
    auth, _, session = state
    # Give an explicit operator account permission so rejection tests CSRF, not roles.
    with auth.engine.begin() as db:
        db.execute(sa.text("INSERT INTO user_roles VALUES ('viewer','operator')"))
    with pytest.raises(AuthorizationError, match="CSRF"):
        with auth.authorized_transaction(
            session.token,
            permission="operate",
            require_csrf=False,
            refresh_idle=False,
            origin=ORIGIN,
        ):
            pytest.fail("mutation disabled CSRF enforcement")


@pytest.mark.parametrize("option,value", [("refresh_idle", 1), ("require_csrf", None)])
def test_option_types_fail_closed(state, option, value):
    auth, _, session = state
    with pytest.raises(ValueError):
        with auth.authorized_transaction(session.token, permission="read", **{option: value}):
            pytest.fail("nonboolean authorization option accepted")
