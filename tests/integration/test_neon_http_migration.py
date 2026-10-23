"""The rendered Neon HTTP upgrade equals the reviewed Alembic 0014->0017 upgrade."""

import importlib.util
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from apps.api.database import migration_config
from deploy.generate_neon_init import connect
from tests.integration.test_kuls_runtime_grants import GRANTS, installed, role_engine  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
RENDERED = ROOT / "deploy/neon-http/upgrade-0014-0017.json"

CATALOG = """
SELECT 'rel', c.relname || ':' || c.relkind::text || ':' || pg_get_userbyid(c.relowner)
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='public' AND c.relkind IN ('r','i','S','v')
UNION ALL
SELECT 'col', c.relname || '.' || a.attname || ':' || format_type(a.atttypid, a.atttypmod)
       || ':' || a.attnotnull || ':' || coalesce(pg_get_expr(d.adbin, d.adrelid), '')
  FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
  JOIN pg_namespace n ON n.oid=c.relnamespace
  LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
 WHERE n.nspname='public' AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped
UNION ALL
SELECT 'con', conrelid::regclass::text || '.' || conname || ':' || pg_get_constraintdef(oid)
  FROM pg_constraint WHERE connamespace='public'::regnamespace
UNION ALL
SELECT 'idx', indexdef FROM pg_indexes WHERE schemaname='public'
UNION ALL
SELECT 'trg', pg_get_triggerdef(oid) FROM pg_trigger WHERE NOT tgisinternal
UNION ALL
SELECT 'fn', proname || ':' || pg_get_userbyid(proowner) || ':' || md5(prosrc)
  FROM pg_proc WHERE pronamespace='public'::regnamespace
UNION ALL
SELECT 'acl', relname || ':' || coalesce(relacl::text, '')
  FROM pg_class WHERE relnamespace='public'::regnamespace
UNION ALL
SELECT 'ver', version_num FROM alembic_version
"""


def catalog(url):
    with connect(url) as db:
        return sorted(tuple(row) for row in db.execute(CATALOG).fetchall())


def http_batch(url, statements):
    # Same semantics as the Neon /sql batch: one transaction, statements in order,
    # issued by the owner connection that may SET ROLE hmi_migrator.
    with connect(url) as db, db.transaction():
        for statement in statements:
            db.execute(statement)


def test_rendering_is_reproducible():
    spec = importlib.util.spec_from_file_location(
        "render", ROOT / "scripts/render_neon_migration.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.statements() == json.loads(RENDERED.read_text())


def test_http_upgrade_matches_alembic_and_accepts_grants(installed):  # noqa: F811
    statements = json.loads(RENDERED.read_text())
    migrator = role_engine(installed, "hmi_migrator")
    try:
        http_batch(installed, statements)
        rendered = catalog(installed)
        with connect(installed) as db:
            # The grant file checks head17 and ownership; it must accept this result.
            db.execute(GRANTS)
        with migrator.connect() as db:
            db.commit()
            command.downgrade(migration_config(db), "0014_mixed_alarm_runtime")
        with migrator.connect() as db:
            db.commit()
            command.upgrade(migration_config(db), "0017_guest_viewer")
        assert catalog(installed) == rendered
    finally:
        migrator.dispose()


def test_http_upgrade_refuses_a_database_not_at_0014(installed):  # noqa: F811
    statements = json.loads(RENDERED.read_text())
    http_batch(installed, statements)
    with pytest.raises(Exception, match="unexpected schema revision"):
        http_batch(installed, statements)
    with connect(installed) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0017_guest_viewer",
        )
    assert sa  # imported for parity with sibling runtime tests


def test_http_grant_batch_equals_the_reviewed_grant_file(installed):  # noqa: F811
    spec = importlib.util.spec_from_file_location(
        "render", ROOT / "scripts/render_neon_migration.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    grants = json.loads((ROOT / "deploy/neon-http/grants-0017.json").read_text())
    assert module.grant_statements() == grants
    http_batch(installed, json.loads(RENDERED.read_text()))
    http_batch(installed, grants)
    after_batch = catalog(installed)
    with connect(installed) as db:
        db.execute(GRANTS)  # idempotent rerun of the original file changes nothing
    assert catalog(installed) == after_batch
    assert any(
        kind == "acl" and value.startswith("downtime_reason_history:") and "hmi_app=ar/" in value
        for kind, value in after_batch
    )
