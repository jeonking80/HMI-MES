"""PostgreSQL App A authentication, independent of the simulator's storage.

No HTTP routes, startup provisioning, secret logging or implicit role inheritance.
Business mutations use authorized_transaction to keep authorization and business
writes under the same user/session locks and PostgreSQL transaction.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from apps.api.database import SCHEMA_LOCK_ID

AUTH_REVISION = "0003_user_auth"
ROLES = frozenset({"viewer", "operator", "production_manager", "equipment_admin"})
PERMISSIONS = {
    "read": ROLES,
    "downtime_annotate": frozenset({"operator", "production_manager"}),
    "operate": frozenset({"operator"}),
    "load_job": frozenset({"operator", "production_manager"}),
    "end_run": frozenset({"operator", "production_manager"}),
    "alarm_action": frozenset({"operator"}),
    "production_manage": frozenset({"production_manager"}),
    "parameters_manage": frozenset({"production_manager"}),
    "equipment_manage": frozenset({"equipment_admin"}),
}
IDLE_TIMEOUT = timedelta(minutes=30)
ABSOLUTE_TIMEOUT = timedelta(hours=8)
LOGIN_TIMEOUT = timedelta(minutes=5)
GUEST_LOGIN = "guest-viewer"
_LOGIN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,63}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1


class AuthenticationError(PermissionError):
    """Missing, invalid, expired, revoked or disabled credentials (HTTP 401)."""


class AuthorizationError(PermissionError):
    """Authenticated identity lacks permission or mutation protection (HTTP 403)."""


@dataclass(frozen=True)
class Principal:
    login: str
    roles: frozenset[str]
    must_change_password: bool


@dataclass(frozen=True)
class LoginChallenge:
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)


@dataclass(frozen=True)
class SessionCredentials:
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    principal: Principal
    expires_at: datetime


@dataclass(frozen=True)
class AuthorizedTransaction:
    connection: Connection = field(repr=False)
    principal: Principal


def _login(value: str) -> str:
    if not isinstance(value, str) or not _LOGIN.fullmatch(value):
        raise ValueError("login must have 1..64 ASCII identifier characters")
    return value.casefold()


def _password(value: str) -> bytes:
    try:
        encoded = value.encode("utf-8") if isinstance(value, str) else b""
    except UnicodeEncodeError:
        encoded = b""
    if not 12 <= len(encoded) <= 1024 or b"\x00" in encoded:
        raise ValueError("password must have 12..1024 UTF-8 bytes and no NUL")
    return encoded


def _derive(password: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password,
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
        maxmem=64 * 1024 * 1024,
    )


def _digest(token: str | None) -> bytes:
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise AuthenticationError("invalid credentials")
    return hashlib.sha256(token.encode("ascii")).digest()


def _origin(value: str) -> bool:
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return False
    try:
        parts = urlsplit(value)
        return (
            parts.scheme in {"https", "http"}
            and bool(parts.hostname)
            and (parts.scheme == "https" or parts.hostname in {"localhost", "127.0.0.1", "::1"})
            and parts.username is None
            and parts.password is None
            and parts.path == parts.query == parts.fragment == ""
            and "*" not in value
            and parts.port != 0
        )
    except ValueError:
        return False


def _roles(values: frozenset[str] | set[str]) -> frozenset[str]:
    if not isinstance(values, (set, frozenset)) or not values or not values <= ROLES:
        raise ValueError("explicit nonempty App A roles are required")
    return frozenset(values)


class UserAuth:
    """Owns PostgreSQL transactions. Construction does not create users or schema.

    Local provisioning is deliberately separate from authenticated role/account
    management. Use a secret file at the provisioning adapter, never CLI arguments.
    The engine must suppress bound parameters in SQL logs and exceptions.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        allowed_origins: frozenset[str] | set[str],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        guest_viewer: bool = False,
    ) -> None:
        if engine.dialect.name != "postgresql" or not engine.hide_parameters or engine.echo:
            raise ValueError("a PostgreSQL engine with hidden parameters and no echo is required")
        if not allowed_origins or not all(_origin(origin) for origin in allowed_origins):
            raise ValueError("an explicit set of exact HTTP(S) origins is required")
        if type(guest_viewer) is not bool:
            raise ValueError("guest_viewer must be a boolean")
        self.guest_viewer = guest_viewer
        self.engine = engine
        self._origins = frozenset(allowed_origins)
        self._clock = clock
        self._dummy_salt = secrets.token_bytes(16)
        self._dummy_hash = _derive(secrets.token_bytes(32), self._dummy_salt)

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must supply an aware UTC-compatible datetime")
        return now.astimezone(UTC)

    @staticmethod
    def _one(db: Connection, sql: str, **values):
        return db.execute(sa.text(sql), values).mappings().first()

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        # Migrations take this lock before DDL; auth must follow the same order
        # before touching tables or holding user/session row locks.
        with self.engine.begin() as db:
            db.execute(
                sa.text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": SCHEMA_LOCK_ID}
            )
            yield db

    def _user(self, db: Connection, login: str):
        return self._one(db, "SELECT * FROM users WHERE login=:login FOR UPDATE", login=login)

    @staticmethod
    def _principal(db: Connection, user) -> Principal:
        roles = db.execute(
            sa.text("SELECT role FROM user_roles WHERE login=:login"), {"login": user["login"]}
        ).scalars()
        return Principal(user["login"], frozenset(roles), user["must_change_password"])

    def _audit(self, db: Connection, actor: str, target: str, action: str, now: datetime):
        # Only fixed action names and normalized account identifiers are persisted.
        db.execute(
            sa.text(
                "INSERT INTO auth_audit(actor,target_login,action,occurred_at) "
                "VALUES (:actor,:target,:action,:now)"
            ),
            {"actor": actor, "target": target, "action": action, "now": now},
        )

    @staticmethod
    def _replace_roles(db: Connection, login: str, roles: frozenset[str]):
        db.execute(sa.text("DELETE FROM user_roles WHERE login=:login"), {"login": login})
        db.execute(
            sa.text("INSERT INTO user_roles(login,role) VALUES (:login,:role)"),
            [{"login": login, "role": role} for role in sorted(roles)],
        )

    def bootstrap_user(self, login: str, password: str, *, roles: set[str] | frozenset[str]):
        """Explicit local provisioning; never replaces or resets an existing user."""
        login, encoded, roles = _login(login), _password(password), _roles(roles)
        if login == GUEST_LOGIN:
            raise ValueError("dedicated guest requires explicit guest provisioning")
        salt = secrets.token_bytes(16)
        password_hash = _derive(encoded, salt)
        with self._transaction() as db:
            inserted = db.execute(
                sa.text(
                    "INSERT INTO users VALUES (:login,:salt,:hash,true,1,true) "
                    "ON CONFLICT (login) DO NOTHING RETURNING login"
                ),
                {"login": login, "salt": salt, "hash": password_hash},
            ).scalar_one_or_none()
            if inserted is None:
                raise ValueError("user already exists; bootstrap cannot reset credentials")
            self._replace_roles(db, login, roles)
            self._audit(db, login, login, "user_bootstrapped", self._now())
        return Principal(login, roles, True)

    def begin_login(self) -> LoginChallenge:
        """Short lived pre-authentication CSRF challenge; grants no access."""
        challenge = LoginChallenge(secrets.token_urlsafe(32), secrets.token_urlsafe(32))
        with self._transaction() as db:
            now = self._now()
            db.execute(
                sa.text("DELETE FROM auth_login_challenges WHERE expires_at<=:now"), {"now": now}
            )
            db.execute(
                sa.text("INSERT INTO auth_login_challenges VALUES (:token,:csrf,:expires)"),
                {
                    "token": _digest(challenge.token),
                    "csrf": _digest(challenge.csrf_token),
                    "expires": now + LOGIN_TIMEOUT,
                },
            )
        return challenge

    def _check_mutation(self, csrf_digest: bytes, csrf_token: str | None, origin: str | None):
        if origin not in self._origins:
            raise AuthorizationError("origin is not allowed")
        try:
            valid = secrets.compare_digest(csrf_digest, _digest(csrf_token))
        except AuthenticationError:
            valid = False
        if not valid:
            raise AuthorizationError("invalid CSRF token")

    def login(
        self, login: str, password: str, *, challenge_token: str, csrf_token: str, origin: str
    ) -> SessionCredentials:
        with self._transaction() as db:
            challenge = self._one(
                db,
                "SELECT * FROM auth_login_challenges WHERE token_digest=:token FOR UPDATE",
                token=_digest(challenge_token),
            )
            now = self._now()
            if challenge is None or challenge["expires_at"] <= now:
                raise AuthenticationError("invalid credentials")
            self._check_mutation(challenge["csrf_digest"], csrf_token, origin)
            try:
                normalized, encoded = _login(login), _password(password)
            except ValueError:
                normalized, encoded = "", b"invalid credential input"
            user = self._user(db, normalized)
            salt = user["salt"] if user else self._dummy_salt
            expected = user["password_hash"] if user else self._dummy_hash
            matched = secrets.compare_digest(_derive(encoded, salt), expected)
            if normalized == GUEST_LOGIN or not matched or user is None or not user["active"]:
                raise AuthenticationError("invalid credentials")
            # Recheck expiry after waiting for a concurrent user/password operation.
            now = self._now()
            if challenge["expires_at"] <= now:
                raise AuthenticationError("invalid credentials")
            db.execute(
                sa.text("DELETE FROM auth_login_challenges WHERE token_digest=:token"),
                {"token": _digest(challenge_token)},
            )
            result = self._issue(db, user, now)
            self._audit(db, user["login"], user["login"], "login_succeeded", now)
        return result

    def _issue(self, db: Connection, user, now: datetime) -> SessionCredentials:
        token, csrf_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        expires = now + (
            timedelta(minutes=30) if user["login"] == GUEST_LOGIN else ABSOLUTE_TIMEOUT
        )
        db.execute(
            sa.text(
                "INSERT INTO auth_sessions VALUES "
                "(:token,:login,:csrf,:version,:now,:now,:idle,:expires,NULL)"
            ),
            {
                "token": _digest(token),
                "login": user["login"],
                "csrf": _digest(csrf_token),
                "version": user["auth_version"],
                "now": now,
                "idle": now + IDLE_TIMEOUT,
                "expires": expires,
            },
        )
        return SessionCredentials(token, csrf_token, self._principal(db, user), expires)

    def _session(self, db: Connection, token: str):
        digest = _digest(token)
        identity = self._one(
            db, "SELECT login FROM auth_sessions WHERE token_digest=:token", token=digest
        )
        if identity is None:
            raise AuthenticationError("invalid credentials")
        # Global auth lock order: sorted user logins, then their session rows.
        user = self._user(db, identity["login"])
        row = self._one(
            db, "SELECT * FROM auth_sessions WHERE token_digest=:token FOR UPDATE", token=digest
        )
        now = self._now()
        if (
            user is None
            or row is None
            or not user["active"]
            or row["revoked_at"] is not None
            or row["auth_version"] != user["auth_version"]
            or not row["created_at"]
            <= row["last_seen_at"]
            <= now
            < min(row["expires_at"], row["idle_expires_at"])
        ):
            raise AuthenticationError("invalid credentials")
        if user["login"] == GUEST_LOGIN:
            from apps.api.guest import validate_guest

            if not self.guest_viewer or row["expires_at"] > row["created_at"] + timedelta(
                minutes=30
            ):
                raise AuthenticationError("invalid credentials")
            try:
                validate_guest(self, db, user)
            except ValueError:
                raise AuthenticationError("invalid credentials") from None
        return user, row, now

    @staticmethod
    def _touch(db: Connection, token: str, row, now: datetime):
        db.execute(
            sa.text(
                "UPDATE auth_sessions SET last_seen_at=:now,idle_expires_at=:idle "
                "WHERE token_digest=:token"
            ),
            {
                "now": now,
                "idle": min(now + IDLE_TIMEOUT, row["expires_at"]),
                "token": _digest(token),
            },
        )

    def authenticate(self, token: str) -> Principal:
        """Live session check, suitable for reads/WS rechecks; refreshes idle expiry."""
        with self._transaction() as db:
            user, row, now = self._session(db, token)
            self._touch(db, token, row, now)
            return self._principal(db, user)

    @contextmanager
    def authorized_transaction(
        self,
        token: str,
        *,
        permission: str,
        csrf_token: str | None = None,
        origin: str | None = None,
        require_csrf: bool = False,
        refresh_idle: bool = True,
    ) -> Iterator[AuthorizedTransaction]:
        """Validate permission and hold locks through the caller's business writes.

        Mutations always require CSRF and exact Origin; reads can opt in.
        Passive subscription checks can disable idle refresh. All business
        access requires a changed password. The caller must use the yielded
        connection and must not commit, roll back, or perform external I/O inside.
        Exceptions roll back both idle refresh and business writes.
        """
        if type(require_csrf) is not bool or type(refresh_idle) is not bool:
            raise ValueError("authorization options must be booleans")
        if permission not in PERMISSIONS:
            raise ValueError("unknown permission")
        with self._transaction() as db:
            user, row, now = self._session(db, token)
            principal = self._principal(db, user)
            if principal.must_change_password or not principal.roles & PERMISSIONS[permission]:
                raise AuthorizationError("permission denied")
            if permission != "read" or require_csrf:
                self._check_mutation(row["csrf_digest"], csrf_token, origin)
            if refresh_idle:
                self._touch(db, token, row, now)
            yield AuthorizedTransaction(db, principal)

    @contextmanager
    def authorized_snapshot(
        self, token: str, *, permission: str = "read"
    ) -> Iterator[AuthorizedTransaction]:
        """Authorize briefly, then run a long passive read in its own consistent snapshot.

        Reports and dashboards compute for seconds. Inside ``authorized_transaction`` they
        would hold the user row (stalling every other request of the account, including
        the 1 s screen poll) and any shared row locks (stalling equipment intake).
        A REPEATABLE READ, READ ONLY transaction gives the same consistent view without
        blocking writers; it still follows the schema lock order of migrations.
        """
        with self.authorized_transaction(token, permission=permission, refresh_idle=False) as tx:
            principal = tx.principal
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as db:
            with db.begin():
                db.execute(sa.text("SET TRANSACTION READ ONLY"))
                db.execute(
                    sa.text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": SCHEMA_LOCK_ID}
                )
                yield AuthorizedTransaction(db, principal)

    def revoke(self, token: str, *, csrf_token: str, origin: str) -> None:
        """Authenticated logout, including viewer/initial-password sessions."""
        with self._transaction() as db:
            user, row, now = self._session(db, token)
            self._check_mutation(row["csrf_digest"], csrf_token, origin)
            db.execute(
                sa.text("UPDATE auth_sessions SET revoked_at=:now WHERE token_digest=:token"),
                {"now": now, "token": _digest(token)},
            )
            self._audit(db, user["login"], user["login"], "session_revoked", now)

    def change_password(
        self, token: str, new_password: str, *, csrf_token: str, origin: str
    ) -> SessionCredentials:
        encoded = _password(new_password)
        with self._transaction() as db:
            user, row, now = self._session(db, token)
            self._check_mutation(row["csrf_digest"], csrf_token, origin)
            if user["login"] == GUEST_LOGIN:
                raise AuthorizationError("guest password changes are prohibited")
            if secrets.compare_digest(_derive(encoded, user["salt"]), user["password_hash"]):
                raise ValueError("new password must differ from the current password")
            salt = secrets.token_bytes(16)
            db.execute(
                sa.text(
                    "UPDATE users SET salt=:salt,password_hash=:hash,"
                    "auth_version=auth_version+1,must_change_password=false WHERE login=:login"
                ),
                {"salt": salt, "hash": _derive(encoded, salt), "login": user["login"]},
            )
            self._invalidate(db, user["login"], now)
            result = self._issue(db, self._user(db, user["login"]), now)
            self._audit(db, user["login"], user["login"], "password_changed", now)
        return result

    @staticmethod
    def _invalidate(db: Connection, login: str, now: datetime):
        db.execute(
            sa.text(
                "UPDATE auth_sessions SET revoked_at=:now WHERE login=:login AND revoked_at IS NULL"
            ),
            {"now": now, "login": login},
        )

    def _manage_user(
        self,
        token: str,
        target_login: str,
        *,
        csrf_token: str,
        origin: str,
        roles: frozenset[str] | None = None,
        active: bool | None = None,
    ) -> None:
        target_login = _login(target_login)
        with self._transaction() as db:
            identity = self._one(
                db,
                "SELECT login FROM auth_sessions WHERE token_digest=:token",
                token=_digest(token),
            )
            if identity is None:
                raise AuthenticationError("invalid credentials")
            # Two administrators changing each other must acquire users in one order.
            for login in sorted({identity["login"], target_login}):
                self._user(db, login)
            user, row, now = self._session(db, token)
            principal = self._principal(db, user)
            self._check_mutation(row["csrf_digest"], csrf_token, origin)
            if principal.must_change_password or "equipment_admin" not in principal.roles:
                raise AuthorizationError("permission denied")
            if self._user(db, target_login) is None:
                raise ValueError("target user does not exist")
            if target_login == GUEST_LOGIN and roles is not None:
                raise AuthorizationError("guest roles cannot be changed")
            if roles is not None:
                self._replace_roles(db, target_login, roles)
                action = "roles_changed"
            else:
                db.execute(
                    sa.text("UPDATE users SET active=:active WHERE login=:login"),
                    {"active": active, "login": target_login},
                )
                action = "active_changed"
            db.execute(
                sa.text("UPDATE users SET auth_version=auth_version+1 WHERE login=:login"),
                {"login": target_login},
            )
            self._invalidate(db, target_login, now)
            self._audit(db, user["login"], target_login, action, now)

    def set_user_roles(
        self,
        token: str,
        target_login: str,
        roles: set[str] | frozenset[str],
        *,
        csrf_token: str,
        origin: str,
    ) -> None:
        """Explicit role replacement; revokes every target session, including self."""
        self._manage_user(
            token, target_login, roles=_roles(roles), csrf_token=csrf_token, origin=origin
        )

    def set_user_active(
        self, token: str, target_login: str, active: bool, *, csrf_token: str, origin: str
    ) -> None:
        if type(active) is not bool:
            raise ValueError("active must be a boolean")
        self._manage_user(token, target_login, active=active, csrf_token=csrf_token, origin=origin)
