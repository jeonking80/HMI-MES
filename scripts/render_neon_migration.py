"""Render reviewed 0014->0017, 0017->0018 and 0018->0019 and 0019->0020 / 0020->0021 HTTP batches.

The Neon HTTP /sql batch endpoint runs a statement list in one transaction. This
renders the exact migration modules (no hand-written DDL) with the same guards the
online environment applies: schema advisory lock, migrator role and exact start
revision. Output is reviewed and replayed against a disposable PostgreSQL before
any operator applies it.
"""

import argparse
import io
import json
import sys
from pathlib import Path

from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.api.database import SCHEMA_LOCK_ID  # noqa: E402

START, REVISIONS = (
    "0014_mixed_alarm_runtime",
    (
        "0015_analog_history",
        "0016_downtime_reasons",
        "0017_guest_viewer",
    ),
)


def statements():
    config = Config(str(ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    rendered = []
    for revision in REVISIONS:
        buffer = io.StringIO()
        context = MigrationContext.configure(
            dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
        )
        with Operations.context(context):
            script.get_revision(revision).module.upgrade()
        rendered += [s.strip() for s in buffer.getvalue().split(";\n\n") if s.strip()]
    return (
        ["SET LOCAL ROLE hmi_migrator", f"SELECT pg_advisory_xact_lock({SCHEMA_LOCK_ID})"]
        + [
            "DO $$ BEGIN IF (SELECT version_num FROM alembic_version) <> "
            f"'{START}' THEN RAISE EXCEPTION 'unexpected schema revision'; END IF; END $$"
        ]
        + [s.rstrip(";") for s in rendered]
        + [f"UPDATE alembic_version SET version_num='{REVISIONS[-1]}' WHERE version_num='{START}'"]
    )


def grant_statements():
    """Split the reviewed grant file into batch statements; the batch is its transaction."""
    return _grant_statements(ROOT / "deploy/neon-kuls-grants.sql")


def _grant_statements(path):
    text = path.read_text()
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("--")]
    result, current, dollar = [], [], False
    for line in lines:
        current.append(line)
        dollar ^= line.count("$$") % 2 == 1
        if not dollar and line.rstrip().endswith(";"):
            statement = "\n".join(current).strip().rstrip(";").strip()
            current = []
            if statement.upper() not in {"BEGIN", "COMMIT"}:
                result.append(statement)
    if "".join(current).strip() or dollar:
        raise ValueError("unterminated statement in grant file")
    return result


def clock_statements():
    """Exact reviewed 0017 -> 0018 migration, for one Neon HTTP transaction."""
    config = Config(str(ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
    )
    with Operations.context(context):
        script.get_revision("0018_clock_evidence").module.upgrade()
    rendered = [s.strip().rstrip(";") for s in buffer.getvalue().split(";\n\n") if s.strip()]
    return [
        f"SELECT pg_catalog.pg_advisory_xact_lock({SCHEMA_LOCK_ID})",
        "SET LOCAL search_path = pg_catalog, public, pg_temp",
        "SET LOCAL ROLE hmi_migrator",
        "DO $$ BEGIN IF current_database() <> 'hmi_mes_demo' "
        "THEN RAISE EXCEPTION 'unexpected database'; END IF; "
        "IF (SELECT count(*) FROM public.alembic_version) <> 1 "
        "OR NOT EXISTS (SELECT 1 FROM public.alembic_version "
        "WHERE version_num='0017_guest_viewer') "
        "THEN RAISE EXCEPTION 'unexpected schema revision or database'; END IF; END $$",
        # PostgreSQL implicitly searches pg_catalog first when omitted. Explicit
        # pg_temp last prevents temporary shadowing, while unqualified Alembic
        # CREATE statements target public rather than the pg_catalog schema.
        "SET LOCAL search_path = public, pg_temp",
        *rendered,
        "SET LOCAL search_path = pg_catalog, public, pg_temp",
        "UPDATE public.alembic_version SET version_num='0018_clock_evidence' "
        "WHERE version_num='0017_guest_viewer'",
    ]


def clock_grant_statements():
    """Reviewed schema18 grants; BEGIN/COMMIT belong to the HTTP batch."""
    return _grant_statements(ROOT / "deploy/neon-clock-grants.sql")


def registration_statements():
    """Exact reviewed 0018 -> 0019 migration, for one Neon HTTP transaction."""
    config = Config(str(ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
    )
    with Operations.context(context):
        script.get_revision("0019_registration_ids").module.upgrade()
    rendered = [s.strip().rstrip(";") for s in buffer.getvalue().split(";\n\n") if s.strip()]
    return [
        f"SELECT pg_catalog.pg_advisory_xact_lock({SCHEMA_LOCK_ID})",
        "SET LOCAL search_path = pg_catalog, public, pg_temp",
        "DO $$ BEGIN IF current_database() <> 'hmi_mes_demo' "
        "THEN RAISE EXCEPTION 'unexpected database'; END IF; "
        "IF (SELECT count(*) FROM public.alembic_version) <> 1 "
        "OR NOT EXISTS (SELECT 1 FROM public.alembic_version "
        "WHERE version_num='0018_clock_evidence') "
        "THEN RAISE EXCEPTION 'unexpected schema revision or database'; END IF; "
        "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='hmi_migrator') "
        "OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='hmi_app') "
        "THEN RAISE EXCEPTION 'dedicated roles required'; END IF; "
        "IF (SELECT nspowner FROM pg_namespace WHERE nspname='public') "
        "IS DISTINCT FROM 'hmi_migrator'::regrole::oid "
        "OR NOT EXISTS (SELECT 1 FROM pg_class "
        "WHERE oid=to_regclass('public.alembic_version') "
        "AND relowner='hmi_migrator'::regrole::oid AND relkind='r') "
        "THEN RAISE EXCEPTION 'dedicated migrator ownership required'; END IF; END $$",
        "SET LOCAL ROLE hmi_migrator",
        "SET LOCAL search_path = public, pg_temp",
        *rendered,
        "SET LOCAL search_path = pg_catalog, public, pg_temp",
        "UPDATE public.alembic_version SET version_num='0019_registration_ids' "
        "WHERE version_num='0018_clock_evidence'",
    ]


def registration_grant_statements():
    """Reviewed schema19 grants; BEGIN/COMMIT belong to the HTTP batch."""
    return _grant_statements(ROOT / "deploy/neon-registration-grants.sql")


def queue_statements():
    """Exact reviewed 0019 -> 0020 migration, for one Neon HTTP transaction."""
    config = Config(str(ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
    )
    with Operations.context(context):
        script.get_revision("0020_line_queue").module.upgrade()
    rendered = [s.strip().rstrip(";") for s in buffer.getvalue().split(";\n\n") if s.strip()]
    return [
        f"SELECT pg_catalog.pg_advisory_xact_lock({SCHEMA_LOCK_ID})",
        "SET LOCAL search_path = pg_catalog, public, pg_temp",
        "DO $$ BEGIN IF current_database() <> 'hmi_mes_demo' "
        "THEN RAISE EXCEPTION 'unexpected database'; END IF; "
        "IF (SELECT count(*) FROM public.alembic_version) <> 1 "
        "OR NOT EXISTS (SELECT 1 FROM public.alembic_version "
        "WHERE version_num='0019_registration_ids') "
        "THEN RAISE EXCEPTION 'unexpected schema revision or database'; END IF; "
        "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='hmi_migrator') "
        "OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='hmi_app') "
        "THEN RAISE EXCEPTION 'dedicated roles required'; END IF; "
        "IF (SELECT nspowner FROM pg_namespace WHERE nspname='public') "
        "IS DISTINCT FROM 'hmi_migrator'::regrole::oid "
        "OR NOT EXISTS (SELECT 1 FROM pg_class "
        "WHERE oid=to_regclass('public.alembic_version') "
        "AND relowner='hmi_migrator'::regrole::oid AND relkind='r') "
        "THEN RAISE EXCEPTION 'dedicated migrator ownership required'; END IF; END $$",
        "SET LOCAL ROLE hmi_migrator",
        "SET LOCAL search_path = public, pg_temp",
        *rendered,
        "SET LOCAL search_path = pg_catalog, public, pg_temp",
        "UPDATE public.alembic_version SET version_num='0020_line_queue' "
        "WHERE version_num='0019_registration_ids'",
    ]


def queue_grant_statements():
    """Reviewed schema20 grants; BEGIN/COMMIT belong to the HTTP batch."""
    return _grant_statements(ROOT / "deploy/neon-queue-grants.sql")


def item_statements():
    """Exact reviewed 0020 -> 0021 migration, for one Neon HTTP transaction."""
    config = Config(str(ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
    )
    with Operations.context(context):
        script.get_revision("0021_item_master").module.upgrade()
    rendered = [s.strip().rstrip(";") for s in buffer.getvalue().split(";\n\n") if s.strip()]
    return [
        f"SELECT pg_catalog.pg_advisory_xact_lock({SCHEMA_LOCK_ID})",
        "SET LOCAL search_path = pg_catalog, public, pg_temp",
        "DO $$ BEGIN IF current_database() <> 'hmi_mes_demo' "
        "THEN RAISE EXCEPTION 'unexpected database'; END IF; "
        "IF (SELECT count(*) FROM public.alembic_version) <> 1 "
        "OR NOT EXISTS (SELECT 1 FROM public.alembic_version "
        "WHERE version_num='0020_line_queue') "
        "THEN RAISE EXCEPTION 'unexpected schema revision or database'; END IF; "
        "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='hmi_migrator') "
        "OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='hmi_app') "
        "THEN RAISE EXCEPTION 'dedicated roles required'; END IF; "
        "IF (SELECT nspowner FROM pg_namespace WHERE nspname='public') "
        "IS DISTINCT FROM 'hmi_migrator'::regrole::oid "
        "OR NOT EXISTS (SELECT 1 FROM pg_class "
        "WHERE oid=to_regclass('public.alembic_version') "
        "AND relowner='hmi_migrator'::regrole::oid AND relkind='r') "
        "THEN RAISE EXCEPTION 'dedicated migrator ownership required'; END IF; END $$",
        "SET LOCAL ROLE hmi_migrator",
        "SET LOCAL search_path = public, pg_temp",
        *rendered,
        "SET LOCAL search_path = pg_catalog, public, pg_temp",
        "UPDATE public.alembic_version SET version_num='0021_item_master' "
        "WHERE version_num='0020_line_queue'",
    ]


def item_grant_statements():
    """Reviewed schema21 grants; BEGIN/COMMIT belong to the HTTP batch."""
    return _grant_statements(ROOT / "deploy/neon-item-grants.sql")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target",
        nargs="?",
        default="upgrade",
        choices=(
            "upgrade",
            "grants",
            "clock-upgrade",
            "clock-grants",
            "registration-upgrade",
            "registration-grants",
            "queue-upgrade",
            "queue-grants",
            "item-upgrade",
            "item-grants",
        ),
    )
    target = parser.parse_args(argv).target
    renderer = {
        "upgrade": statements,
        "grants": grant_statements,
        "clock-upgrade": clock_statements,
        "clock-grants": clock_grant_statements,
        "registration-upgrade": registration_statements,
        "registration-grants": registration_grant_statements,
        "queue-upgrade": queue_statements,
        "queue-grants": queue_grant_statements,
        "item-upgrade": item_statements,
        "item-grants": item_grant_statements,
    }[target]
    json.dump(renderer(), sys.stdout, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
