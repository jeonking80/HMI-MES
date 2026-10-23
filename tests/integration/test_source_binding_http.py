"""Explicit fixed-scope commissioning; real Neon-init PG/hmi_app, no cloud access."""

import asyncio
import hashlib
import json
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa

from apps.api import serverless
from apps.api.auth import UserAuth
from apps.api.ingestion import IngestionService
from apps.api.internal import ServiceCredential, SourceBindingPolicy, create_internal_app
from tests.integration.test_user_http import ORIGIN, login
from tests.integration.test_vercel_app import DSN
from tests.integration.test_vercel_app import runtime as runtime

PATH = "/internal/v1/source-binding"


def policy(runtime, source="commission-source", **kwargs):
    c = ServiceCredential(
        "commission-key",
        "commission-gateway",
        source,
        "commission-stream",
        hashlib.sha256(runtime.token.encode()).digest(),
        datetime.now(UTC) + timedelta(days=1),
    )
    return replace(SourceBindingPolicy(c, "packaging-line-1", 1), **kwargs)


def client(runtime, p, *, engine=None, credentials=None):
    config = serverless.VercelConfig(ORIGIN, DSN, tuple(credentials or [p.credential]), p)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=serverless.create_vercel_app(config, engine=engine or runtime.engine)
        ),
        base_url=ORIGIN,
    )


def headers(runtime):
    return {"Authorization": "Bearer " + runtime.token}


def rows(runtime, p):
    with runtime.engine.connect() as db:
        params = {"source": p.credential.source_id, "stream": p.credential.stream_id}
        return tuple(
            db.execute(
                sa.text(
                    "SELECT * FROM " + table + " WHERE source_id=:source AND stream_id=:stream"
                ),
                params,
            )
            .mappings()
            .all()
            for table in ["source_streams", "stream_progress"]
        )


@pytest.fixture
def packaged(tmp_path, monkeypatch):
    token = secrets.token_urlsafe(32)
    data = {
        "schema_version": 1,
        "enabled": True,
        "origin": ORIGIN,
        "binding": {
            "credential": {
                "key_id": "public-key",
                "gateway_id": "public-gateway",
                "source_id": "public-source",
                "stream_id": "public-stream",
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "expires_at": "2099-01-01T00:00:00+00:00",
            },
            "line_id": "packaging-line-1",
            "capture_generation": "1",
        },
    }
    file = tmp_path / "cloud_plant.json"
    file.write_text(json.dumps(data))
    monkeypatch.setattr(serverless, "__file__", str(tmp_path / "serverless.py"))
    monkeypatch.setenv("HMI_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setenv("HMI_DATABASE_DSN", DSN)
    monkeypatch.delenv("HMI_GATEWAY_CREDENTIALS_JSON", raising=False)
    return data, file


def test_absent_only_fallback_and_env_override_has_no_binding_authority(packaged, monkeypatch):
    data, file = packaged
    config = serverless.VercelConfig.from_environment()
    assert config.source_binding_policy.credential == config.credentials[0]
    assert config.source_binding_policy.capture_generation == 1
    # A present override must never even load the packaged policy.
    file.unlink()
    monkeypatch.setenv("HMI_GATEWAY_CREDENTIALS_JSON", "[]")
    assert serverless.VercelConfig.from_environment().source_binding_policy is None
    assert serverless.VercelConfig.from_environment().credentials == ()
    c = data["binding"]["credential"] | {
        "token_sha256": hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    }
    monkeypatch.setenv("HMI_GATEWAY_CREDENTIALS_JSON", json.dumps([c]))
    assert serverless.VercelConfig.from_environment().source_binding_policy is None
    for invalid in ["", "{}", "null", "not-json"]:
        monkeypatch.setenv("HMI_GATEWAY_CREDENTIALS_JSON", invalid)
        with pytest.raises(ValueError):
            serverless.VercelConfig.from_environment()


@pytest.mark.parametrize(
    "change",
    [
        "unknown",
        "version",
        "enabled",
        "origin",
        "binding-extra",
        "digest",
        "generation",
        "expiry",
        "duplicate",
        "oversized",
    ],
)
def test_policy_is_strict_and_bounded(packaged, change):
    data, file = packaged
    if change == "unknown":
        data["unknown"] = True
    elif change == "version":
        data["schema_version"] = True
    elif change == "enabled":
        data["enabled"] = 1
    elif change == "origin":
        data["origin"] = "https://other.example.test"
    elif change == "binding-extra":
        data["binding"]["line"] = "another"
    elif change == "digest":
        data["binding"]["credential"]["token_sha256"] = "00 " * 32
    elif change == "generation":
        data["binding"]["capture_generation"] = True
    elif change == "expiry":
        data["binding"]["credential"]["expires_at"] = "2099-01-01"
    file.write_text(json.dumps(data))
    if change == "duplicate":
        file.write_text('{"schema_version":1,"schema_version":1}')
    if change == "oversized":
        file.write_text(" " * 8193)
    with pytest.raises(ValueError):
        serverless.VercelConfig.from_environment()


async def test_explicit_post_concurrent_retry_preserves_flags_frontiers_timestamps(runtime):
    p = policy(runtime)
    before = rows(runtime, p)
    assert before == ([], [])
    async with client(runtime, p) as c:
        assert rows(runtime, p) == before  # no construction mutation
        assert (await c.get("/api/ready")).status_code == 200
        assert (await c.get(PATH, headers=headers(runtime))).status_code == 405
        assert rows(runtime, p) == before
        results = await asyncio.gather(*(c.post(PATH, headers=headers(runtime)) for _ in range(6)))
        assert all(
            r.status_code == 200 and r.json() == {"binding_registered": True} for r in results
        )
        with runtime.admin.begin() as db:
            db.execute(
                sa.text(
                    "UPDATE source_streams SET conflicted=true,reconciliation_required=true "
                    "WHERE source_id='commission-source'"
                )
            )
            db.execute(
                sa.text(
                    "UPDATE stream_progress SET verified_through_seq=4,latest_ingested_seq=9 "
                    "WHERE source_id='commission-source'"
                )
            )
        changed = rows(runtime, p)
        assert (await c.post(PATH, headers=headers(runtime))).status_code == 200
        assert rows(runtime, p) == changed


async def test_no_request_control_of_scope_and_cookie_or_wrong_bearer_denied(runtime):
    p = policy(runtime, "request-rejection-source")
    async with client(runtime, p) as c:
        for body in [b"{}", b" ", b"x", b'{"line_id":"another"}']:
            assert (await c.post(PATH, headers=headers(runtime), content=body)).status_code == 422
        assert (
            await c.post(PATH + "?line_id=another", headers=headers(runtime))
        ).status_code == 422
        assert (await c.post(PATH)).status_code == 401
        assert (
            await c.post(PATH, headers={"Authorization": "Bearer " + secrets.token_urlsafe(32)})
        ).status_code == 401
        password = secrets.token_urlsafe(24)
        UserAuth(runtime.admin, allowed_origins={ORIGIN}).bootstrap_user(
            "commission-person", password, roles={"operator"}
        )
        assert (await login(c, password, login="commission-person")).status_code == 200
        assert (await c.post(PATH)).status_code == 401
        assert rows(runtime, p) == ([], [])


@pytest.mark.parametrize("mode", ["expired", "revoked", "different-digest", "different-key"])
async def test_full_credential_capability_and_expiry_not_key_alone(runtime, mode):
    p = policy(runtime, "denied-" + mode)
    selected = p.credential
    token = runtime.token
    if mode == "expired":
        selected = replace(selected, expires_at=datetime.now(UTC) - timedelta(seconds=1))
        p = replace(p, credential=selected)
    elif mode == "revoked":
        selected = replace(selected, revoked=True)
        p = replace(p, credential=selected)
    elif mode == "different-digest":
        token = secrets.token_urlsafe(32)
        selected = replace(selected, token_digest=hashlib.sha256(token.encode()).digest())
    elif mode == "different-key":
        selected = replace(selected, key_id="another-key")
    app = create_internal_app(IngestionService(runtime.engine), [selected], source_binding_policy=p)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        response = await c.post(PATH, headers={"Authorization": "Bearer " + token})
        assert response.status_code == (401 if mode in ("expired", "revoked") else 403)
        assert rows(runtime, p) == ([], [])


async def test_default_and_env_override_cannot_commission_even_same_key(
    runtime, packaged, monkeypatch
):
    data, file = packaged
    override = data["binding"]["credential"] | {
        "token_sha256": hashlib.sha256(runtime.token.encode()).hexdigest()
    }
    monkeypatch.setenv("HMI_GATEWAY_CREDENTIALS_JSON", json.dumps([override]))
    config = serverless.VercelConfig.from_environment()
    assert config.source_binding_policy is None
    app = serverless.create_vercel_app(config, engine=runtime.engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        assert (await c.post(PATH, headers=headers(runtime))).status_code == 404


async def test_exact_conflict_and_missing_progress_are_409_not_repaired(runtime):
    p = policy(runtime, "conflict-source")
    async with client(runtime, p) as c:
        assert (await c.post(PATH, headers=headers(runtime))).status_code == 200
    before = rows(runtime, p)
    async with client(runtime, replace(p, capture_generation=2)) as c:
        assert (await c.post(PATH, headers=headers(runtime))).status_code == 409
    assert rows(runtime, p) == before
    with runtime.admin.begin() as db:
        db.execute(sa.text("DELETE FROM stream_progress WHERE source_id='conflict-source'"))
    before = rows(runtime, p)
    async with client(runtime, p) as c:
        assert (await c.post(PATH, headers=headers(runtime))).status_code == 409
    assert rows(runtime, p) == before


async def test_deferred_commit_failure_returns_no_success_and_rolls_back_both_rows(runtime):
    p = policy(runtime, "commit-failure-source")
    # A deferred trigger fails at COMMIT after both inserts have succeeded.
    with runtime.admin.begin() as db:
        db.exec_driver_sql("""CREATE FUNCTION public.test_source_binding_commit_failure()
          RETURNS trigger
          LANGUAGE plpgsql AS $$ BEGIN IF NEW.source_id='commit-failure-source' THEN
          RAISE EXCEPTION 'fixture-private-failure'; END IF; RETURN NEW; END $$;
          CREATE CONSTRAINT TRIGGER test_source_binding_commit_failure
          AFTER INSERT ON public.stream_progress DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION public.test_source_binding_commit_failure();""")
    try:
        async with client(runtime, p) as c:
            response = await c.post(PATH, headers=headers(runtime))
            assert response.status_code == 503 and "fixture-private-failure" not in response.text
            assert rows(runtime, p) == ([], [])
    finally:
        with runtime.admin.begin() as db:
            db.exec_driver_sql(
                "DROP TRIGGER test_source_binding_commit_failure ON public.stream_progress; "
                "DROP FUNCTION public.test_source_binding_commit_failure();"
            )


async def test_wrong_database_and_wrong_revision_rejected_before_source_write(runtime, engine):
    p = policy(runtime, "wrong-database-source")
    # Matching schema head in a different DB must still reject the capability.
    with engine.begin() as db:
        db.exec_driver_sql("CREATE TABLE alembic_version(version_num text PRIMARY KEY)")
        db.exec_driver_sql("INSERT INTO alembic_version VALUES ('0014_mixed_alarm_runtime')")
    async with client(runtime, p, engine=engine) as c:
        assert (await c.post(PATH, headers=headers(runtime))).status_code == 503
    with runtime.admin.begin() as db:
        db.execute(sa.text("UPDATE alembic_version SET version_num='0013_work_order_cancel'"))
    try:
        async with client(runtime, p) as c:
            assert (await c.post(PATH, headers=headers(runtime))).status_code == 503
    finally:
        with runtime.admin.begin() as db:
            db.execute(sa.text("UPDATE alembic_version SET version_num='0014_mixed_alarm_runtime'"))
    assert rows(runtime, p) == ([], [])
