"""Rendered head17 -> 18 Neon HTTP batches equal actual Alembic and grant SQL."""

import importlib.util
import json
import secrets
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
import sqlalchemy as sa
from alembic import command
from psycopg import sql

from apps.api.database import create_database_engine, migration_config
from apps.api.ingestion import IngestionService
from deploy.generate_neon_init import connect
from tests.integration.test_clock_runtime_grants import head17 as head17
from tests.integration.test_kuls_runtime_grants import AT, SCOPE, batch, denied
from tests.integration.test_kuls_runtime_grants import installed as installed
from tests.integration.test_neon_http_migration import CATALOG, http_batch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "clock_renderer", ROOT / "scripts/render_neon_migration.py"
)
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)
UPGRADE = ROOT / "deploy/neon-http/upgrade-0017-0018.json"
GRANTS = ROOT / "deploy/neon-http/grants-0018.json"


def catalog(url):
    with connect(url) as db:
        rows = db.execute(CATALOG).fetchall()
        # Include full function definitions/security/ACL and column ACLs because
        # schema18 limits probe UPDATE to the three consumption-result columns.
        rows += db.execute("""SELECT 'fullfn', pg_get_functiondef(p.oid)||':'||
            pg_get_userbyid(p.proowner)||':'||coalesce(p.proacl::text,'')
            FROM pg_proc p WHERE p.pronamespace='public'::regnamespace
            UNION ALL SELECT 'columnacl', c.relname||'.'||a.attname||':'||
            coalesce(a.attacl::text,'') FROM pg_attribute a
            JOIN pg_class c ON c.oid=a.attrelid
            WHERE c.relnamespace='public'::regnamespace AND c.relkind='r'
              AND a.attnum>0 AND NOT a.attisdropped""").fetchall()
        return sorted(tuple(row) for row in rows)


def upgrade_alembic(migrator, revision):
    with migrator.connect() as db:
        command.upgrade(migration_config(db), revision)


def test_fixed_artifacts_and_old_outputs_are_reproducible():
    assert renderer.START == "0014_mixed_alarm_runtime"
    assert renderer.REVISIONS == (
        "0015_analog_history",
        "0016_downtime_reasons",
        "0017_guest_viewer",
    )
    assert renderer.statements() == json.loads(
        (ROOT / "deploy/neon-http/upgrade-0014-0017.json").read_text()
    )
    assert renderer.grant_statements() == json.loads(
        (ROOT / "deploy/neon-http/grants-0017.json").read_text()
    )
    assert renderer.clock_statements() == json.loads(UPGRADE.read_text())
    assert renderer.clock_grant_statements() == json.loads(GRANTS.read_text())
    assert all(
        statement.upper() not in {"BEGIN", "COMMIT"}
        for statement in renderer.clock_statements() + renderer.clock_grant_statements()
    )


@pytest.mark.parametrize("target", [None, "upgrade", "grants", "clock-upgrade", "clock-grants"])
def test_explicit_cli_choices_and_compatible_default(target):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/render_neon_migration.py")]
        + ([] if target is None else [target]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    expected = {
        None: renderer.statements,
        "upgrade": renderer.statements,
        "grants": renderer.grant_statements,
        "clock-upgrade": renderer.clock_statements,
        "clock-grants": renderer.clock_grant_statements,
    }[target]()
    assert json.loads(result.stdout) == expected
    artifact = {
        None: "upgrade-0014-0017.json",
        "upgrade": "upgrade-0014-0017.json",
        "grants": "grants-0017.json",
        "clock-upgrade": "upgrade-0017-0018.json",
        "clock-grants": "grants-0018.json",
    }[target]
    assert result.stdout == (ROOT / "deploy/neon-http" / artifact).read_text()


def test_unknown_cli_mode_is_rejected_instead_of_emitting_grants():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/render_neon_migration.py"), "clock-upgarde"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert not result.stdout
    assert "invalid choice" in result.stderr


def test_http_clock_upgrade_catalog_equals_alembic_and_grants_equal_manual_sql(head17):
    url, migrator = head17
    upgrade_alembic(migrator, "0018_clock_evidence")
    canonical = catalog(url)
    with migrator.connect() as db:
        command.downgrade(migration_config(db), "0017_guest_viewer")
    http_batch(url, renderer.clock_statements())
    assert catalog(url) == canonical
    http_batch(url, renderer.clock_grant_statements())
    after_grants = catalog(url)
    with connect(url) as db:
        db.execute((ROOT / "deploy/neon-clock-grants.sql").read_text())
    assert catalog(url) == after_grants
    assert any(
        kind == "columnacl"
        and value.startswith("capture_clock_probes.consumed_at:")
        and "hmi_app=w/" in value
        for kind, value in after_grants
    )
    with pytest.raises(psycopg.errors.RaiseException, match="unexpected schema revision"):
        http_batch(url, renderer.clock_statements())
    assert catalog(url) == after_grants


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE alembic_version SET version_num='0016_downtime_reasons'",
        "DELETE FROM alembic_version",
        "INSERT INTO alembic_version VALUES ('unexpected_extra_head')",
    ],
)
def test_wrong_revision_or_head_count_rolls_back_without_catalog_changes(head17, mutation):
    url, _ = head17
    with connect(url) as db:
        db.execute(mutation)
    before = catalog(url)
    with pytest.raises(psycopg.errors.RaiseException, match="unexpected schema revision"):
        http_batch(url, renderer.clock_statements())
    assert catalog(url) == before


def test_mid_migration_failure_rolls_back_prior_create_statements(head17):
    url, _ = head17
    with connect(url) as db:
        db.execute("SET LOCAL ROLE hmi_migrator")
        db.execute("ALTER TABLE public.analog_history_raw ADD COLUMN canonical_utc timestamptz")
    before = catalog(url)
    with pytest.raises(psycopg.errors.DuplicateColumn):
        http_batch(url, renderer.clock_statements())
    assert catalog(url) == before
    with connect(url) as db:
        assert db.execute("SELECT to_regclass('public.capture_clock_evidence')").fetchone() == (
            None,
        )
        assert db.execute("SELECT to_regclass('public.capture_clock_probes')").fetchone() == (None,)


def test_http_batches_grant_actual_runtime_intake_and_forbid_evidence_changes(head17):
    url, _ = head17
    http_batch(url, renderer.clock_statements())
    http_batch(url, renderer.clock_grant_statements())
    password = secrets.token_urlsafe(32)
    with connect(url) as db:
        db.execute(sql.SQL("ALTER ROLE hmi_app LOGIN PASSWORD {}").format(sql.Literal(password)))
    runtime = create_database_engine(url.set(username="hmi_app", password=password))
    try:
        with runtime.connect() as db:
            assert db.execute(sa.text("SELECT current_user")).scalar_one() == "hmi_app"
        service = IngestionService(runtime)
        service.configure_source(**SCOPE, gateway_id="gateway", capture_generation=1, now=AT)
        value = batch()
        service.ingest(
            value,
            authenticated_gateway_id="gateway",
            authenticated_source_id="source",
            authenticated_stream_id="stream",
            now=AT,
        )
        with runtime.begin() as db:
            assert (
                db.execute(sa.text("SELECT count(*) FROM capture_clock_evidence")).scalar_one() == 1
            )
            db.execute(sa.text("SELECT * FROM capture_clock_probes"))
        for statement in (
            "UPDATE capture_clock_evidence SET reason_code='tampered'",
            "DELETE FROM capture_clock_evidence",
            "TRUNCATE capture_clock_evidence",
            "UPDATE capture_clock_probes SET expires_at=expires_at",
            "CREATE TABLE public.unapproved_runtime_ddl(id int)",
        ):
            denied(runtime, statement)
    finally:
        runtime.dispose()
