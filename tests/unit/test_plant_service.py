"""Persistent hosted credentials must survive restarts without silent rotation."""

import secrets

import pytest

from deploy.plant_service import gateway_secret


def test_service_credential_resume_preserves_original_bytes_and_permissions(tmp_path):
    token = secrets.token_urlsafe(32)
    path = gateway_secret(tmp_path, token)
    before = path.stat()
    assert path.stat().st_mode & 0o777 == 0o600
    assert gateway_secret(tmp_path, token) == path
    assert path.read_text() == token + "\n"
    assert path.stat().st_ino == before.st_ino
    with pytest.raises(ValueError, match="never overwrite"):
        gateway_secret(tmp_path, secrets.token_urlsafe(32))
    assert path.read_text() == token + "\n"


def test_service_never_follows_token_symlink_or_accepts_public_secret(tmp_path):
    token = secrets.token_urlsafe(32)
    outside = tmp_path / "outside"
    outside.write_text(token + "\n")
    outside.chmod(0o600)
    target = tmp_path / "gateway-token"
    target.symlink_to(outside)
    with pytest.raises(OSError):
        gateway_secret(tmp_path, token)
    assert outside.read_text() == token + "\n"
    target.unlink()
    target.write_text(token + "\n")
    target.chmod(0o644)
    with pytest.raises(ValueError, match="Owner-only"):
        gateway_secret(tmp_path, token)


def test_service_rejects_invalid_credentials_without_creating_state(tmp_path):
    with pytest.raises(ValueError):
        gateway_secret(tmp_path, "invalid")
    assert not list(tmp_path.iterdir())
