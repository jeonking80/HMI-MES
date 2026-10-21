"""Explicit App A account provisioning through a real separate process and PostgreSQL."""

import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from apps.api.auth import UserAuth

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "http://127.0.0.1:8002"


def provision(engine, path, *extra):
    env = os.environ.copy()
    env["HMI_DATABASE_DSN"] = engine.url.render_as_string(hide_password=False)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "apps.api",
            "bootstrap-user",
            "--login",
            "Operator.A",
            "--role",
            "operator",
            "--origin",
            ORIGIN,
            "--password-file",
            str(path),
            *extra,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def secret_file(tmp_path):
    path = tmp_path / "initial-password"
    password = secrets.token_urlsafe(32)
    path.write_text(password + "\n")
    path.chmod(0o600)
    return path, password


def test_subprocess_provisioning_never_resets_existing_account(migrated, tmp_path):
    path, password = secret_file(tmp_path)
    with migrated.connect() as db:
        assert db.execute(text("SELECT count(*) FROM users")).scalar_one() == 0
    result = provision(migrated, path, "--role", "viewer")
    assert result.returncode == 0, result.stderr
    assert password not in result.stdout + result.stderr
    auth = UserAuth(migrated, allowed_origins={ORIGIN})
    challenge = auth.begin_login()
    session = auth.login(
        "operator.a",
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=ORIGIN,
    )
    assert session.principal.must_change_password
    assert session.principal.roles == frozenset({"operator", "viewer"})
    with migrated.connect() as db:
        before = db.execute(text("SELECT * FROM users")).one()
    replacement = secrets.token_urlsafe(32)
    path.write_text(replacement)
    result = provision(migrated, path, "--role", "equipment_admin")
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert replacement not in result.stdout + result.stderr
    with migrated.connect() as db:
        assert db.execute(text("SELECT * FROM users")).one() == before
        assert set(db.execute(text("SELECT role FROM user_roles")).scalars()) == {
            "operator",
            "viewer",
        }
        assert (
            db.execute(
                text("SELECT count(*) FROM auth_audit WHERE action='user_bootstrapped'")
            ).scalar_one()
            == 1
        )


@pytest.mark.parametrize("kind", ["public", "symlink", "oversized", "fifo"])
def test_unsafe_password_files_refused_without_creating_user(migrated, tmp_path, kind):
    path, password = secret_file(tmp_path)
    if kind == "public":
        path.chmod(0o644)
    elif kind == "symlink":
        link = tmp_path / "link"
        link.symlink_to(path)
        path = link
    elif kind == "oversized":
        path.write_text("x" * 1027)
    else:
        path.unlink()
        os.mkfifo(path, 0o600)
    result = provision(migrated, path)
    assert result.returncode == 1
    assert password not in result.stdout + result.stderr
    with migrated.connect() as db:
        assert db.execute(text("SELECT count(*) FROM users")).scalar_one() == 0


def test_bootstrap_does_not_migrate_or_create_default_accounts(engine, tmp_path):
    path, password = secret_file(tmp_path)
    result = provision(engine, path)
    assert result.returncode == 1
    assert password not in result.stdout + result.stderr
    with engine.connect() as db:
        assert db.execute(text("SELECT to_regclass('users')")).scalar_one() is None
        assert db.execute(text("SELECT to_regclass('alembic_version')")).scalar_one() is None


def test_bootstrap_requires_explicit_roles_and_configuration():
    result = subprocess.run(
        [sys.executable, "-m", "apps.api", "bootstrap-user"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2
    for name in ("--login", "--role", "--origin", "--password-file"):
        assert name in result.stderr
