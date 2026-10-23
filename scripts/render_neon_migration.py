"""Render the reviewed 0014->0017 Alembic upgrade as one SQL statement list.

The Neon HTTP /sql batch endpoint runs a statement list in one transaction. This
renders the exact migration modules (no hand-written DDL) with the same guards the
online environment applies: schema advisory lock, migrator role and exact start
revision. Output is reviewed and replayed against a disposable PostgreSQL before
any operator applies it.
"""

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
    text = (ROOT / "deploy/neon-kuls-grants.sql").read_text()
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


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "upgrade"
    rendered = statements() if target == "upgrade" else grant_statements()
    json.dump(rendered, sys.stdout, ensure_ascii=False, indent=1)
