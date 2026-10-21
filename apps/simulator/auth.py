"""Internal simulator console authentication; no HTTP routes or default accounts.

The simulator's canonical storage owner supplies its connection and transaction.
Every operation that writes (including authentication's idle-time refresh) requires
an outer transaction. This module never commits, opens files, or provisions users
on normal construction. HTTP transport, rate limiting and cookie handling belong
to the future console adapter.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

ROLES = frozenset({"viewer", "simulator_operator"})
IDLE_TIMEOUT = timedelta(minutes=30)
ABSOLUTE_TIMEOUT = timedelta(hours=8)
LOGIN_TIMEOUT = timedelta(minutes=5)
_LOGIN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,63}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1


class AuthenticationError(PermissionError):
    """Missing, invalid, expired, revoked or disabled credentials (HTTP 401)."""


class AuthorizationError(PermissionError):
    """Valid identity lacks permission or mutation protection (HTTP 403)."""


@dataclass(frozen=True)
class Principal:
    login: str
    role: str
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


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


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


class SimulatorAuth:
    """Use only with the simulator storage owner's connection and transaction.

    ``bootstrap_user`` is an explicit local provisioning primitive, never a
    request handler. There is no administrator role or permission bypass.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        allowed_origins: frozenset[str] | set[str],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not allowed_origins or not all(_origin(origin) for origin in allowed_origins):
            raise ValueError("an explicit set of exact HTTP(S) origins is required")
        self._db = connection
        self._origins = frozenset(allowed_origins)
        self._clock = clock
        # Unknown users still pay the configured password derivation cost.
        self._dummy_salt = secrets.token_bytes(16)
        self._dummy_hash = _derive(secrets.token_bytes(32), self._dummy_salt)

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must supply an aware UTC-compatible datetime")
        return now.astimezone(UTC)

    def _one(self, query: str, arguments: tuple = ()) -> sqlite3.Row | None:
        cursor = self._db.cursor()
        cursor.row_factory = sqlite3.Row
        return cursor.execute(query, arguments).fetchone()

    @contextmanager
    def _unit(self) -> Iterator[None]:
        if not self._db.in_transaction:
            raise RuntimeError("authentication writes require the storage owner's transaction")
        self._db.execute("SAVEPOINT simulator_auth_unit")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK TO simulator_auth_unit")
            self._db.execute("RELEASE simulator_auth_unit")
            raise
        else:
            self._db.execute("RELEASE simulator_auth_unit")

    def initialize_schema(self) -> None:
        """Explicit additive migration; leaves simulator PRAGMA user_version alone."""
        with self._unit():
            if self._one("SELECT 1 FROM sqlite_master WHERE name='auth_schema_version'"):
                row = self._one("SELECT version FROM auth_schema_version WHERE singleton=1")
                if row is None or row["version"] != 1:
                    raise ValueError(
                        "unsupported authentication schema; explicit migration required"
                    )
                return
            # Individual statements preserve the owner's transaction (no executescript).
            statements = (
                "CREATE TABLE auth_schema_version (singleton INTEGER PRIMARY KEY "
                "CHECK(singleton=1),version INTEGER NOT NULL CHECK(version=1))",
                "CREATE TABLE console_users (login TEXT PRIMARY KEY NOT NULL,"
                "salt BLOB NOT NULL CHECK(length(salt)=16),"
                "password_hash BLOB NOT NULL CHECK(length(password_hash)=32),"
                "role TEXT NOT NULL CHECK(role IN ('viewer','simulator_operator')),"
                "active INTEGER NOT NULL CHECK(active IN (0,1)),"
                "auth_version INTEGER NOT NULL CHECK(auth_version>=1),"
                "must_change_password INTEGER NOT NULL CHECK(must_change_password IN (0,1)))",
                "CREATE TABLE console_sessions (token_digest BLOB PRIMARY KEY NOT NULL "
                "CHECK(length(token_digest)=32),"
                "login TEXT NOT NULL REFERENCES console_users(login),"
                "csrf_digest BLOB NOT NULL CHECK(length(csrf_digest)=32),"
                "auth_version INTEGER NOT NULL,role TEXT NOT NULL,created_at TEXT NOT NULL,"
                "last_seen_at TEXT NOT NULL,idle_expires_at TEXT NOT NULL,"
                "expires_at TEXT NOT NULL,revoked_at TEXT)",
                "CREATE INDEX console_sessions_login ON console_sessions(login)",
                "CREATE TABLE console_login_challenges (token_digest BLOB PRIMARY KEY NOT NULL "
                "CHECK(length(token_digest)=32),csrf_digest BLOB NOT NULL "
                "CHECK(length(csrf_digest)=32),expires_at TEXT NOT NULL)",
                "CREATE TABLE console_auth_audit (audit_id INTEGER PRIMARY KEY,"
                "actor TEXT NOT NULL,action TEXT NOT NULL,occurred_at TEXT NOT NULL)",
                "INSERT INTO auth_schema_version VALUES (1,1)",
            )
            for statement in statements:
                self._db.execute(statement)

    def _audit(self, login: str, action: str, now: datetime) -> None:
        self._db.execute(
            "INSERT INTO console_auth_audit(actor,action,occurred_at) VALUES (?,?,?)",
            (login, action, _stamp(now)),
        )

    def bootstrap_user(self, login: str, password: str, *, role: str) -> Principal:
        """Explicit local provisioning, with no reset/replacement of existing users."""
        login = _login(login)
        encoded = _password(password)
        if role not in ROLES:
            raise ValueError("unsupported simulator role")
        with self._unit():
            if self._one("SELECT 1 FROM console_users WHERE login=?", (login,)):
                raise ValueError("user already exists; bootstrap cannot reset credentials")
            salt = secrets.token_bytes(16)
            self._db.execute(
                "INSERT INTO console_users VALUES (?,?,?,?,1,1,1)",
                (login, salt, _derive(encoded, salt), role),
            )
            self._audit(login, "user_bootstrapped", self._now())
            return Principal(login, role, True)

    def begin_login(self) -> LoginChallenge:
        """Create a short lived pre-auth CSRF challenge; does not grant access."""
        with self._unit():
            now = self._now()
            self._db.execute(
                "DELETE FROM console_login_challenges WHERE expires_at<=?", (_stamp(now),)
            )
            challenge = LoginChallenge(secrets.token_urlsafe(32), secrets.token_urlsafe(32))
            self._db.execute(
                "INSERT INTO console_login_challenges VALUES (?,?,?)",
                (
                    _digest(challenge.token),
                    _digest(challenge.csrf_token),
                    _stamp(now + LOGIN_TIMEOUT),
                ),
            )
            return challenge

    def _check_mutation(
        self, csrf_digest: bytes, csrf_token: str | None, origin: str | None
    ) -> None:
        if origin not in self._origins:
            raise AuthorizationError("origin is not allowed")
        try:
            valid = secrets.compare_digest(csrf_digest, _digest(csrf_token))
        except AuthenticationError:
            valid = False
        if not valid:
            raise AuthorizationError("invalid CSRF token")

    def login(
        self,
        login: str,
        password: str,
        *,
        challenge_token: str,
        csrf_token: str,
        origin: str,
    ) -> SessionCredentials:
        with self._unit():
            now = self._now()
            challenge = self._one(
                "SELECT * FROM console_login_challenges WHERE token_digest=?",
                (_digest(challenge_token),),
            )
            if challenge is None or challenge["expires_at"] <= _stamp(now):
                raise AuthenticationError("invalid credentials")
            self._check_mutation(challenge["csrf_digest"], csrf_token, origin)
            try:
                normalized, encoded = _login(login), _password(password)
            except ValueError:
                normalized, encoded = "", b"invalid credential input"
            user = self._one("SELECT * FROM console_users WHERE login=?", (normalized,))
            salt = user["salt"] if user else self._dummy_salt
            expected = user["password_hash"] if user else self._dummy_hash
            matched = secrets.compare_digest(_derive(encoded, salt), expected)
            if not matched or user is None or not user["active"]:
                raise AuthenticationError("invalid credentials")
            self._db.execute(
                "DELETE FROM console_login_challenges WHERE token_digest=?",
                (_digest(challenge_token),),
            )
            result = self._issue(user, now)
            self._audit(user["login"], "login_succeeded", now)
            return result

    def _issue(self, user: sqlite3.Row, now: datetime) -> SessionCredentials:
        token, csrf_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        expires = now + ABSOLUTE_TIMEOUT
        self._db.execute(
            "INSERT INTO console_sessions VALUES (?,?,?,?,?,?,?,?,?,NULL)",
            (
                _digest(token),
                user["login"],
                _digest(csrf_token),
                user["auth_version"],
                user["role"],
                _stamp(now),
                _stamp(now),
                _stamp(now + IDLE_TIMEOUT),
                _stamp(expires),
            ),
        )
        return SessionCredentials(
            token,
            csrf_token,
            Principal(user["login"], user["role"], bool(user["must_change_password"])),
            expires,
        )

    def _session(self, token: str, now: datetime) -> sqlite3.Row:
        row = self._one(
            "SELECT s.*,u.active,u.must_change_password,u.auth_version AS current_version,"
            "u.role AS current_role FROM console_sessions s JOIN console_users u USING(login) "
            "WHERE token_digest=?",
            (_digest(token),),
        )
        at = _stamp(now)
        if (
            row is None
            or not row["active"]
            or row["revoked_at"] is not None
            or row["auth_version"] != row["current_version"]
            or row["role"] != row["current_role"]
            or not row["created_at"]
            <= row["last_seen_at"]
            <= at
            < min(row["expires_at"], row["idle_expires_at"])
        ):
            raise AuthenticationError("invalid credentials")
        return row

    @staticmethod
    def _principal(row: sqlite3.Row) -> Principal:
        return Principal(row["login"], row["current_role"], bool(row["must_change_password"]))

    def authenticate(self, token: str) -> Principal:
        """Validate live user/session and refresh the idle timeout within absolute expiry."""
        with self._unit():
            now = self._now()
            row = self._session(token, now)
            self._db.execute(
                "UPDATE console_sessions SET last_seen_at=?,idle_expires_at=? WHERE token_digest=?",
                (_stamp(now), min(_stamp(now + IDLE_TIMEOUT), row["expires_at"]), _digest(token)),
            )
            return self._principal(row)

    def authorize_mutation(self, token: str, *, csrf_token: str, origin: str) -> Principal:
        """Operator-only console changes; PLC still decides physical permissions."""
        with self._unit():
            row = self._session(token, self._now())
            self._check_mutation(row["csrf_digest"], csrf_token, origin)
            if row["current_role"] != "simulator_operator" or row["must_change_password"]:
                raise AuthorizationError("simulator operator with changed password required")
            return self.authenticate(token)

    def revoke(self, token: str, *, csrf_token: str, origin: str) -> None:
        """Authenticated logout; viewer and initial-password sessions can log out too."""
        with self._unit():
            now = self._now()
            row = self._session(token, now)
            self._check_mutation(row["csrf_digest"], csrf_token, origin)
            self._db.execute(
                "UPDATE console_sessions SET revoked_at=? WHERE token_digest=?",
                (_stamp(now), _digest(token)),
            )
            self._audit(row["login"], "session_revoked", now)

    def change_password(
        self,
        token: str,
        new_password: str,
        *,
        csrf_token: str,
        origin: str,
    ) -> SessionCredentials:
        """Change own password, invalidate all old sessions, issue a rotated session."""
        encoded = _password(new_password)
        with self._unit():
            now = self._now()
            row = self._session(token, now)
            self._check_mutation(row["csrf_digest"], csrf_token, origin)
            user = self._one("SELECT * FROM console_users WHERE login=?", (row["login"],))
            if secrets.compare_digest(_derive(encoded, user["salt"]), user["password_hash"]):
                raise ValueError("new password must differ from the current password")
            salt = secrets.token_bytes(16)
            self._db.execute(
                "UPDATE console_users SET salt=?,password_hash=?,auth_version=auth_version+1,"
                "must_change_password=0 WHERE login=?",
                (salt, _derive(encoded, salt), row["login"]),
            )
            self._db.execute(
                "UPDATE console_sessions SET revoked_at=? WHERE login=? AND revoked_at IS NULL",
                (_stamp(now), row["login"]),
            )
            user = self._one("SELECT * FROM console_users WHERE login=?", (row["login"],))
            result = self._issue(user, now)
            self._audit(row["login"], "password_changed", now)
            return result
