"""Explicit dedicated guest provisioning and PostgreSQL coordinated admission.

No startup account creation. The dedicated login is reserved by UserAuth even
when this feature is disabled. Rate state and admission share its user row lock
across workers and transaction-pooling connections.
"""

import hashlib
import ipaddress
import secrets
from datetime import timedelta

import sqlalchemy as sa

from apps.api.auth import AuthenticationError, AuthorizationError, _derive

GUEST_LOGIN = "guest-viewer"
GUEST_TIMEOUT = timedelta(minutes=30)
GUEST_SESSION_LIMIT = 50
GUEST_IP_LIMIT = 5
GUEST_WINDOW = timedelta(minutes=1)
GUEST_RATE_KEYS = 2048


class GuestThrottled(Exception):
    """A distributed admission bound was reached (HTTP 429)."""


def bootstrap_guest(auth):
    """Explicit one-time local provisioning; never repurpose an existing user."""
    with auth._transaction() as db:
        salt = secrets.token_bytes(16)
        # No usable password is retained or returned; password login is prohibited.
        inserted = db.execute(
            sa.text(
                "INSERT INTO users VALUES (:login,:salt,:hash,true,1,false) "
                "ON CONFLICT (login) DO NOTHING RETURNING login"
            ),
            {"login": GUEST_LOGIN, "salt": salt, "hash": _derive(secrets.token_bytes(32), salt)},
        ).scalar_one_or_none()
        if inserted is None:
            raise ValueError("guest provisioning cannot replace an existing account")
        auth._replace_roles(db, GUEST_LOGIN, frozenset({"viewer"}))
        now = auth._now()
        db.execute(
            sa.text("INSERT INTO guest_viewer_accounts(login,provisioned_at) VALUES (:login,:now)"),
            {"login": GUEST_LOGIN, "now": now},
        )
        auth._audit(db, "GUEST", GUEST_LOGIN, "user_bootstrapped", now)


def validate_guest(auth, db, user):
    marker = db.execute(
        sa.text("SELECT 1 FROM guest_viewer_accounts WHERE login=:login"), {"login": GUEST_LOGIN}
    ).scalar_one_or_none()
    if (
        user is None
        or marker is None
        or not user["active"]
        or user["must_change_password"]
        or auth._principal(db, user).roles != frozenset({"viewer"})
    ):
        raise ValueError("dedicated guest account must be provisioned, active and exactly viewer")


def validate_startup(auth):
    with auth._transaction() as db:
        validate_guest(auth, db, auth._user(db, GUEST_LOGIN))


def issue_guest(auth, *, origin, peer):
    if not auth.guest_viewer:
        raise AuthorizationError("guest access is disabled")
    if origin not in auth._origins:
        raise AuthorizationError("origin is not allowed")
    try:
        peer = ipaddress.ip_address(peer).compressed
    except ValueError:
        # Missing/non-IP ASGI peers share a conservative bucket.
        peer = "unknown"
    digest = hashlib.sha256(peer.encode("ascii")).digest()
    throttled = False
    result = None
    with auth._transaction() as db:
        user = auth._user(db, GUEST_LOGIN)
        try:
            validate_guest(auth, db, user)
        except ValueError:
            raise AuthenticationError("guest access is unavailable") from None
        now = auth._now()
        db.execute(
            sa.text("DELETE FROM guest_rate_windows WHERE window_started_at<=:cutoff"),
            {"cutoff": now - GUEST_WINDOW},
        )
        rate = db.execute(
            sa.text("SELECT attempts FROM guest_rate_windows WHERE peer_digest=:peer"),
            {"peer": digest},
        ).scalar_one_or_none()
        keys = db.execute(sa.text("SELECT count(*) FROM guest_rate_windows")).scalar_one()
        if rate is None and keys >= GUEST_RATE_KEYS:
            throttled = True
        else:
            db.execute(
                sa.text(
                    "INSERT INTO guest_rate_windows VALUES (:peer,:now,1) "
                    "ON CONFLICT (peer_digest) DO UPDATE SET "
                    "attempts=LEAST(guest_rate_windows.attempts+1,:bound)"
                ),
                {"peer": digest, "now": now, "bound": GUEST_IP_LIMIT + 1},
            )
            count = db.execute(
                sa.text(
                    "SELECT count(*) FROM auth_sessions WHERE login=:login "
                    "AND revoked_at IS NULL AND auth_version=:version "
                    "AND expires_at>:now AND idle_expires_at>:now"
                ),
                {"login": GUEST_LOGIN, "version": user["auth_version"], "now": now},
            ).scalar_one()
            throttled = (rate or 0) >= GUEST_IP_LIMIT
            if not throttled:
                # A full pool evicts its oldest guest sessions instead of refusing everyone:
                # one peer filling the pool must not lock every later visitor out.
                if count >= GUEST_SESSION_LIMIT:
                    db.execute(
                        sa.text(
                            "UPDATE auth_sessions SET revoked_at=:now WHERE token_digest IN ("
                            "SELECT token_digest FROM auth_sessions WHERE login=:login "
                            "AND revoked_at IS NULL AND auth_version=:version "
                            "AND expires_at>:now AND idle_expires_at>:now "
                            "ORDER BY created_at, token_digest LIMIT :excess)"
                        ),
                        {
                            "login": GUEST_LOGIN,
                            "version": user["auth_version"],
                            "now": now,
                            "excess": count - GUEST_SESSION_LIMIT + 1,
                        },
                    )
                result = auth._issue(db, user, now)
                auth._audit(db, "GUEST", GUEST_LOGIN, "GUEST", now)
    # Commit rejected attempts too, so a full session pool cannot reset IP limits.
    if throttled:
        raise GuestThrottled
    return result
