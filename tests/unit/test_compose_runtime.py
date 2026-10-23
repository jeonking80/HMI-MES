"""Local lifecycle safeguards, independent from a live Docker daemon."""

import importlib.util
import stat
from pathlib import Path

import pytest

from deploy import compose_runtime


def smoke_module():
    path = Path(__file__).resolve().parents[2] / "scripts/check-compose.py"
    spec = importlib.util.spec_from_file_location("compose_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_secret_restart_preserves_material(tmp_path, monkeypatch):
    monkeypatch.setattr(compose_runtime, "ROOT", tmp_path)
    initial = compose_runtime.secret("gateway-token")
    assert compose_runtime.secret("gateway-token") == initial
    assert stat.S_IMODE((tmp_path / "gateway-token").stat().st_mode) == 0o600


def test_insecure_existing_secret_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(compose_runtime, "ROOT", tmp_path)
    path = tmp_path / "gateway-token"
    path.write_text("existing")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="Owner-only"):
        compose_runtime.secret("gateway-token")
    assert path.read_text() == "existing"


def test_database_ignores_inherited_target(tmp_path, monkeypatch):
    from sqlalchemy.engine import make_url

    monkeypatch.setattr(compose_runtime, "ROOT", tmp_path)
    monkeypatch.setenv("HMI_DATABASE_DSN", "postgresql://other@remote/production")
    compose_runtime.database()
    import os

    url = make_url(os.environ["HMI_DATABASE_DSN"])
    assert url.host == "postgres" and url.database == "hmi_compose"


def test_smoke_rejects_stale_bad_missing_and_duplicate_tags():
    import copy

    good = {
        "screen": {"view": {"samples": [{"tag_id": str(i), "status_code": 0} for i in range(48)]}},
        "evidence": [{"fresh": True} for _ in range(48)],
        "health": {"state": "CAPTURE_FRESH", "heartbeat": "GOOD"},
        "missing_tag_ids": [],
    }
    check = smoke_module().good_snapshot
    assert check(good)
    for mutation in ("stale", "bad", "duplicate", "missing"):
        payload = copy.deepcopy(good)
        if mutation == "stale":
            payload["evidence"][0]["fresh"] = False
        elif mutation == "bad":
            payload["screen"]["view"]["samples"][0]["status_code"] = 0x80000000
        elif mutation == "duplicate":
            payload["screen"]["view"]["samples"][0]["tag_id"] = "1"
        else:
            payload["missing_tag_ids"] = ["one"]
        assert not check(payload)
