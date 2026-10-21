"""Explicit, transactional reference-data bootstrap after an Alembic migration."""

from __future__ import annotations

import hashlib
import json
import sys
from typing import Any

from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, Engine, Table, select, text
from sqlalchemy.exc import SQLAlchemyError

from apps.api.database import (
    SCHEMA_LOCK_ID,
    SUPPORTED_REFERENCE_REVISIONS,
    create_database_engine,
)
from apps.api.models import Asset, Line, Site, TagDefinition
from contracts.tags.validate import load_contract, validate_contract

SITE_ID = "site-local"
LINE_ID = "packaging-line-1"
ASSET_IDS = {group: f"packaging-{group.lower()}" for group in ("Infeed", "Sealer", "Outfeed")}


class BootstrapConflict(ValueError):
    """Existing reference data differs; an explicit reviewed change is required."""


def _insert_or_verify(connection: Connection, table: Table, expected: dict[str, Any]) -> bool:
    existing = (
        connection.execute(select(table).where(table.c.id == expected["id"]))
        .mappings()
        .one_or_none()
    )
    if existing is None:
        connection.execute(table.insert().values(**expected))
        return True
    if any(existing[key] != value for key, value in expected.items()):
        raise BootstrapConflict(
            f"Existing {table.name} record {expected['id']} differs; bootstrap refuses overwrite"
        )
    return False


def _tag_record(contract: dict[str, Any], tag: dict[str, Any]) -> dict[str, Any]:
    payload = {key: value for key, value in contract.items() if key != "tags"}
    payload["tag"] = tag
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "id": tag["logical_name"],
        "line_id": LINE_ID,
        "asset_id": ASSET_IDS.get(tag["group"]),
        "tag_group": tag["group"],
        "name": tag["name"],
        "logical_name": tag["logical_name"],
        "namespace_uri": contract["namespace_uri"],
        "identifier_type": tag["node_id"]["identifier_type"],
        "node_identifier": tag["node_id"]["identifier"],
        "data_type": tag["data_type"],
        "unit": tag["unit"],
        "enum_name": tag["enum"],
        "access": tag["access"],
        "polling_interval_ms": tag["polling_interval_ms"],
        "analog_history": tag["analog_history"],
        "enabled": True,
        "schema_version": contract["schema_version"],
        "contract_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "contract_payload": payload,
    }


def bootstrap_reference_data(
    engine: Engine, contract: dict[str, Any] | None = None
) -> dict[str, int]:
    """Insert missing baseline rows, verify existing content, or roll back everything."""
    contract = load_contract() if contract is None else contract
    validate_contract(contract)
    if engine.dialect.name != "postgresql":
        raise ValueError("Bootstrap requires real PostgreSQL")
    inserted = {"sites": 0, "lines": 0, "assets": 0, "tag_definitions": 0}
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCHEMA_LOCK_ID})
        heads = MigrationContext.configure(connection).get_current_heads()
        if len(heads) != 1 or heads[0] not in SUPPORTED_REFERENCE_REVISIONS:
            raise ValueError("Apply the expected Alembic migration before bootstrap")
        inserted["sites"] += _insert_or_verify(
            connection, Site.__table__, {"id": SITE_ID, "code": "LOCAL", "name": "Local site"}
        )
        inserted["lines"] += _insert_or_verify(
            connection,
            Line.__table__,
            {"id": LINE_ID, "site_id": SITE_ID, "code": "PACKAGING-1", "name": "Packaging line 1"},
        )
        for group, asset_id in ASSET_IDS.items():
            inserted["assets"] += _insert_or_verify(
                connection,
                Asset.__table__,
                {"id": asset_id, "line_id": LINE_ID, "code": group, "name": group},
            )
        for tag in contract["tags"]:
            inserted["tag_definitions"] += _insert_or_verify(
                connection, TagDefinition.__table__, _tag_record(contract, tag)
            )
    return inserted


def main() -> int:
    engine = None
    try:
        engine = create_database_engine()
        inserted = bootstrap_reference_data(engine)
    except (ValueError, SQLAlchemyError):
        # Driver errors/SQL parameters may contain secrets; never print the DSN or exception.
        print(
            "Bootstrap failed: verify HMI_DATABASE_DSN, migration version, and existing "
            "reference contract. Inspect database state before retrying.",
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    print("Reference bootstrap committed; inserted rows: " + json.dumps(inserted, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
