"""Capacity helper uses only metadata in a real read-only PG transaction."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("db_capacity", ROOT / "scripts/check-db-capacity.py")
capacity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capacity)


def test_capacity_is_readonly_and_reports_metadata_only(migrated):
    statements = []
    read_only = []

    @sa.event.listens_for(migrated, "before_cursor_execute")
    def observe(db, cursor, statement, parameters, context, executemany):
        statements.append(statement)
        if statement.startswith("SELECT"):
            # Use the driver's cursor to avoid recursively invoking this listener.
            with db.connection.cursor() as probe:
                probe.execute("SHOW transaction_read_only")
                read_only.append(probe.fetchone()[0])

    try:
        report = capacity.read_capacity(migrated)
    finally:
        sa.event.remove(migrated, "before_cursor_execute", observe)
    assert report["transaction_read_only"] is True
    assert report["database_bytes"] > 0
    assert read_only and set(read_only) == {"on"}
    assert {row["relation"] for row in report["relations"]} >= {"ingest_batches", "auth_audit"}
    assert all(
        set(row) == {"relation", "total_bytes", "estimated_rows"} for row in report["relations"]
    )
    assert not any(
        statement.lstrip().split()[0] in {"INSERT", "UPDATE", "DELETE", "ALTER", "DROP"}
        for statement in statements
    )


def test_main_redacts_connection_errors(monkeypatch, capsys):
    secret = "credential-sentinel-never-print"

    def unavailable():
        raise RuntimeError(secret)

    monkeypatch.setattr(capacity, "create_database_engine", unavailable)
    assert capacity.main() == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert secret not in captured.err
    assert "Capacity check failed" in captured.err
