#!/usr/bin/env python3
"""Read-only database capacity snapshot using the existing protected DSN env.

Never prints a DSN, host, user, credentials, business rows or connection errors.
Only pg_catalog size metadata and approximate row counts are inspected.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlalchemy as sa  # noqa: E402

from apps.api.database import create_database_engine  # noqa: E402


def read_capacity(engine):
    with engine.connect() as db, db.begin():
        db.exec_driver_sql("SET TRANSACTION READ ONLY")
        db.exec_driver_sql("SET LOCAL statement_timeout='5s'")
        db.exec_driver_sql("SET LOCAL lock_timeout='1s'")
        if db.exec_driver_sql("SHOW transaction_read_only").scalar_one() != "on":
            raise RuntimeError("Read-only transaction required")
        size = db.execute(
            sa.text("SELECT pg_catalog.pg_database_size(current_database())")
        ).scalar_one()
        rows = (
            db.execute(
                sa.text("""SELECT c.relname AS relation,
                pg_catalog.pg_total_relation_size(c.oid) AS total_bytes,
                c.reltuples AS estimated_rows
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind IN ('r','m','p')
            ORDER BY total_bytes DESC,c.relname""")
            )
            .mappings()
            .all()
        )
        return {
            "observed_at": datetime.now(UTC).isoformat(),
            "transaction_read_only": True,
            "database_bytes": size,
            "relations": [
                {
                    "relation": row["relation"],
                    "total_bytes": row["total_bytes"],
                    "estimated_rows": (
                        max(0, round(row["estimated_rows"])) if row["estimated_rows"] >= 0 else None
                    ),
                }
                for row in rows
            ],
            "note": "Row estimates may be stale or unknown; database size includes other overhead.",
        }


def main():
    engine = None
    try:
        engine = create_database_engine()
        print(json.dumps(read_capacity(engine), ensure_ascii=False, indent=2))
        return 0
    except Exception:
        # Driver failures may contain connection credentials. Do not reflect them.
        print(
            "Capacity check failed; verify the protected DSN and read permissions.", file=sys.stderr
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
