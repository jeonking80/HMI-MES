"""Offline artifacts + actual disposable PG Neon initializer, never production."""

import hashlib
import json
import secrets
import stat
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import psycopg
import pytest
import sqlalchemy as sa

from apps.api.ingestion import IngestionService
from apps.api.serverless import VercelConfig, create_vercel_app
from deploy import generate_gateway_setup as generator
from tests.integration.test_ingestion import EXAMPLE, batch
from tests.integration.test_vercel_app import DSN, ORIGIN
from tests.integration.test_vercel_app import runtime as runtime


@pytest.fixture
def setup():
    return generator.GatewaySetup(
        "setup-source",
        "setup-stream",
        "setup-gateway",
        "packaging-line-1",
        1,
        "setup-key",
        datetime(2099, 1, 1, tzinfo=UTC),
    )


def execute(runtime, statement):
    raw = runtime.admin.raw_connection()
    try:
        raw.cursor().execute(statement, prepare=False)
        raw.commit()
    except BaseException:
        raw.rollback()
        raise
    finally:
        raw.close()


def rows(runtime, setup):
    with runtime.engine.connect() as db:
        params = {"source": setup.source_id, "stream": setup.stream_id}
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


def test_offline_private_artifacts_no_overwrite_and_token_not_disclosed(tmp_path, setup, capsys):
    output = tmp_path / "artifacts"
    generator.generate(setup, output)
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert {p.name for p in output.iterdir()} == {
        "token.secret",
        "prepare-source.sql",
        "vercel-gateway-credentials.json",
    }
    for p in output.iterdir():
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    token = (output / "token.secret").read_text().strip()
    credentials = json.loads((output / "vercel-gateway-credentials.json").read_text())
    assert credentials == [
        {
            "key_id": setup.key_id,
            "gateway_id": setup.gateway_id,
            "source_id": setup.source_id,
            "stream_id": setup.stream_id,
            "expires_at": setup.expires_at.isoformat(),
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        }
    ]
    assert (
        token
        not in (output / "prepare-source.sql").read_text()
        + (output / "vercel-gateway-credentials.json").read_text()
    )
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    with pytest.raises(FileExistsError):
        generator.generate(setup, output)
    assert before == {p.name: p.read_bytes() for p in output.iterdir()}
    assert token not in "".join(capsys.readouterr())


def test_write_failure_removes_only_new_partial_artifacts(tmp_path, setup, monkeypatch):
    output = tmp_path / "incomplete"
    original = generator.os.fsync

    def fail(fd):
        raise OSError("fixture-secret-error")

    monkeypatch.setattr(generator.os, "fsync", fail)
    with pytest.raises(OSError):
        generator.generate(setup, output)
    assert not output.exists()
    monkeypatch.setattr(generator.os, "fsync", original)
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "token.secret").write_text("preserve")
    link = tmp_path / "link"
    link.symlink_to(existing, target_is_directory=True)
    with pytest.raises(FileExistsError):
        generator.generate(setup, link)
    assert (existing / "token.secret").read_text() == "preserve"


@pytest.mark.parametrize(
    "change",
    [
        {"source_id": "bad whitespace"},
        {"capture_generation": True},
        {"capture_generation": -1},
        {"capture_generation": 2**64},
        {"expires_at": datetime(2099, 1, 1)},
        {"expires_at": datetime(2000, 1, 1, tzinfo=UTC)},
    ],
)
def test_invalid_explicit_identity_generation_expiry_rejected(setup, change):
    with pytest.raises(ValueError):
        replace(setup, **change)


def test_cli_is_offline_and_sanitizes_failures(tmp_path, capsys, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Network forbidden")

    monkeypatch.setattr(psycopg, "connect", no_network)
    args = [
        "--source-id",
        "cli-source",
        "--stream-id",
        "cli-stream",
        "--gateway-id",
        "cli-gateway",
        "--line-id",
        "packaging-line-1",
        "--capture-generation",
        "1",
        "--key-id",
        "cli-key",
        "--expires-at",
        "2099-01-01T00:00:00Z",
        "--output-directory",
        str(tmp_path / "cli"),
    ]
    assert generator.main(args) == 0
    token = (tmp_path / "cli/token.secret").read_text().strip()
    assert generator.main(args) == 1
    output = capsys.readouterr()
    assert token not in output.out + output.err and "Traceback" not in output.err


def test_actual_source_sql_exact_retry_preserves_all_state_and_runtime_privileges(runtime, setup):
    statement = generator.source_sql(setup)
    execute(runtime, statement)
    first = rows(runtime, setup)
    assert len(first[0]) == len(first[1]) == 1 and first[1][0]["verified_through_seq"] == 0
    with runtime.admin.begin() as db:
        db.execute(
            sa.text(
                "UPDATE source_streams SET conflicted=true,reconciliation_required=true "
                "WHERE source_id='setup-source'"
            )
        )
        db.execute(
            sa.text(
                "UPDATE stream_progress SET verified_through_seq=7,latest_ingested_seq=9 "
                "WHERE source_id='setup-source'"
            )
        )
    updated = rows(runtime, setup)
    execute(runtime, statement)
    assert rows(runtime, setup) == updated
    with pytest.raises(psycopg.Error):
        execute(runtime, generator.source_sql(replace(setup, gateway_id="changed-gateway")))
    assert rows(runtime, setup) == updated
    # Existing runtime privilege grants are sufficient for the canonical function.
    IngestionService(runtime.engine).configure_source(
        source_id="runtime-source",
        stream_id="runtime-stream",
        gateway_id="runtime-gateway",
        line_id=setup.line_id,
        capture_generation=1,
        now=datetime.now(UTC),
    )
    with runtime.engine.connect() as db:
        assert (
            db.execute(
                sa.text("SELECT count(*) FROM source_streams WHERE source_id='runtime-source'")
            ).scalar_one()
            == 1
        )


def test_sql_atomic_second_insert_failure_and_wrong_line_reject(runtime, setup):
    failing = replace(setup, source_id="rollback-source", stream_id="rollback-stream")
    execute(
        runtime,
        """CREATE FUNCTION public.test_setup_reject_progress() RETURNS trigger LANGUAGE plpgsql AS
      $$ BEGIN IF NEW.source_id='rollback-source' THEN RAISE EXCEPTION 'fixture';
      END IF; RETURN NEW; END $$;
      CREATE TRIGGER test_setup_reject_progress BEFORE INSERT ON public.stream_progress
      FOR EACH ROW EXECUTE FUNCTION public.test_setup_reject_progress();""",
    )
    try:
        with pytest.raises(psycopg.Error):
            execute(runtime, generator.source_sql(failing))
        assert rows(runtime, failing) == ([], [])
    finally:
        execute(
            runtime,
            "DROP TRIGGER test_setup_reject_progress ON public.stream_progress; "
            "DROP FUNCTION public.test_setup_reject_progress();",
        )
    wrong = replace(setup, source_id="wrong-line-source", line_id="missing-line")
    with pytest.raises(psycopg.Error):
        execute(runtime, generator.source_sql(wrong))
    assert rows(runtime, wrong) == ([], [])


def test_sql_safe_literals_and_uint64_max(runtime, setup):
    unusual = replace(
        setup,
        source_id="source';SELECT(1);--",
        stream_id="stream$$'\\name",
        capture_generation=2**64 - 1,
    )
    execute(runtime, generator.source_sql(unusual))
    records = rows(runtime, unusual)
    assert records[0][0]["source_id"] == unusual.source_id
    assert records[0][0]["capture_generation"] == 2**64 - 1
    with runtime.engine.connect() as db:
        assert db.execute(sa.text("SELECT count(*) FROM tag_definitions")).scalar_one() == 48


async def test_generated_env_and_token_authenticate_exact_scope_on_actual_runtime(
    runtime, setup, tmp_path, monkeypatch
):
    binding = replace(
        setup, source_id="auth-source", stream_id="auth-stream", gateway_id="auth-gateway"
    )
    output = tmp_path / "auth"
    generator.generate(binding, output)
    execute(runtime, (output / "prepare-source.sql").read_text())
    token = (output / "token.secret").read_text().strip()
    monkeypatch.setenv("HMI_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setenv("HMI_DATABASE_DSN", DSN)
    monkeypatch.setenv(
        "HMI_GATEWAY_CREDENTIALS_JSON", (output / "vercel-gateway-credentials.json").read_text()
    )
    config = VercelConfig.from_environment()
    app = create_vercel_app(config, engine=runtime.engine)
    samples = json.loads(EXAMPLE.read_text())["body"]["samples"]
    samples[0]["sample_id"] = "generated-source-sample"
    value = batch(
        samples=samples,
        source_id=binding.source_id,
        stream_id=binding.stream_id,
        gateway_id=binding.gateway_id,
        batch_id="generated-auth-batch",
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        wrong = await client.post(
            "/internal/v1/batches",
            headers={"Authorization": "Bearer " + secrets.token_urlsafe(32)},
            json=value.model_dump(mode="json"),
        )
        assert wrong.status_code == 401
        headers = {"Authorization": "Bearer " + token}
        response = await client.post(
            "/internal/v1/batches", headers=headers, json=value.model_dump(mode="json")
        )
        assert response.status_code == 200, response.text
        again = await client.post(
            "/internal/v1/batches", headers=headers, json=value.model_dump(mode="json")
        )
        assert again.status_code == 200 and again.json() == response.json()
        assert response.json()["durability"] == "COMMITTED"


def test_sql_missing_existing_progress_and_wrong_head_fail_without_repair(runtime, setup):
    binding = replace(setup, source_id="missing-progress-source")
    execute(runtime, generator.source_sql(binding))
    with runtime.admin.begin() as db:
        db.execute(sa.text("DELETE FROM stream_progress WHERE source_id='missing-progress-source'"))
    before = rows(runtime, binding)
    with pytest.raises(psycopg.Error):
        execute(runtime, generator.source_sql(binding))
    assert rows(runtime, binding) == before
    fresh = replace(setup, source_id="unsupported-head-source")
    with runtime.admin.begin() as db:
        db.execute(sa.text("UPDATE alembic_version SET version_num='unsupported-setup-head'"))
    try:
        with pytest.raises(psycopg.Error):
            execute(runtime, generator.source_sql(fresh))
        assert rows(runtime, fresh) == ([], [])
    finally:
        with runtime.admin.begin() as db:
            db.execute(sa.text("UPDATE alembic_version SET version_num='0014_mixed_alarm_runtime'"))


def test_sql_refuses_other_database_before_role_or_source_changes(runtime, setup, engine):
    raw = engine.raw_connection()
    try:
        with pytest.raises(psycopg.Error):
            raw.cursor().execute(generator.source_sql(setup), prepare=False)
        raw.rollback()
        assert raw.cursor().execute("SELECT current_user").fetchone()[0] == "postgres"
    finally:
        raw.close()
