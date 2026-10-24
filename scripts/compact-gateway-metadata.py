#!/usr/bin/env python3
"""Explicit, offline, lossless metadata migration for an existing gateway DB.

Only counts, byte sizes and a digest of normalized database semantics are printed.
The gateway's normal exclusive file lock must be available; stop its owner first.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.gateway.buffer import GatewayBuffer  # noqa: E402

_TABLES = (
    "gateway_identity",
    "batch_ledger",
    "outbound_batches",
    "source_progress",
    "missing_ranges",
    "loss_intervals",
    "sqlite_sequence",
)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error can echo sensitive argument values.
        raise ValueError("Invalid command arguments")


def _scalar_bytes(value):
    """Unambiguous, type-preserving framing for ordering and semantic hashing."""
    if value is None:
        return b"n"
    if isinstance(value, bytes):
        kind, raw = b"b", value
    elif isinstance(value, str):
        kind, raw = b"s", value.encode("utf-8")
    elif isinstance(value, int):
        kind, raw = b"i", str(value).encode("ascii")
    elif isinstance(value, float):
        kind, raw = b"f", value.hex().encode("ascii")
    else:
        raise TypeError("Unsupported persisted scalar")
    return kind + str(len(raw)).encode("ascii") + b":" + raw


def _snapshot(connection):
    """Freeze every semantic column and row, including queue/recovery state."""
    tables = []
    for table in _TABLES:
        info = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        if not info:
            raise ValueError("Required gateway table is absent")
        columns = tuple(sorted(row[1] for row in info if row[1] != "metadata_bytes"))
        quoted = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
        frozen_rows = []
        for row in connection.execute(f'SELECT {quoted} FROM "{table}"'):
            values = list(row)
            if table == "batch_ledger":
                index = columns.index("ack")
                if values[index] is not None:
                    values[index] = GatewayBuffer._ack_bytes(values[index])
            elif table == "missing_ranges":
                index = columns.index("body")
                values[index] = GatewayBuffer._loss_bytes(values[index])
            frozen_rows.append(tuple(values))
        rows = tuple(sorted(frozen_rows, key=lambda row: tuple(map(_scalar_bytes, row))))
        tables.append((table, columns, rows))
    return tuple(tables)


def _semantic_digest(snapshot):
    digest = hashlib.sha256()

    def visit(value):
        if isinstance(value, tuple):
            digest.update(b"t" + str(len(value)).encode("ascii") + b":")
            for item in value:
                visit(item)
        else:
            digest.update(_scalar_bytes(value))

    visit(snapshot)
    return digest.hexdigest()


def _existing_database(path):
    """Reject fresh, linked, foreign-owned or permissively readable databases."""
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_nlink != 1
        or before.st_size == 0
    ):
        raise ValueError("Existing private gateway database required")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        current = os.fstat(descriptor)
        if current != before:
            raise ValueError("Database file changed during validation")
    finally:
        os.close(descriptor)
    return before


def main(argv=None):
    try:
        parser = _Parser(description=__doc__)
        parser.add_argument("--database", type=Path, required=True)
        parser.add_argument("--gateway-id", required=True)
        parser.add_argument("--apply", action="store_true", required=True)
        args = parser.parse_args(argv)
        original_file = _existing_database(args.database)
        # mode=ro cannot create a database. Reject missing identity/schema before
        # GatewayBuffer's normal open path can initialize or upgrade anything.
        with closing(
            sqlite3.connect(args.database.absolute().as_uri() + "?mode=ro", uri=True)
        ) as probe:
            probe.execute("BEGIN")
            before = _snapshot(probe)
            identities = probe.execute("SELECT gateway_id FROM gateway_identity").fetchall()
            columns = {row[1] for row in probe.execute("PRAGMA table_info(batch_ledger)")}
            if identities != [(args.gateway_id,)] or "envelope_sha256" not in columns:
                raise ValueError("Existing gateway identity and current ledger required")
            if probe.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("Gateway foreign key violation")
        current_file = _existing_database(args.database)
        if (current_file.st_dev, current_file.st_ino) != (
            original_file.st_dev,
            original_file.st_ino,
        ):
            raise ValueError("Database file changed during validation")
        with GatewayBuffer(
            args.database, gateway_id=args.gateway_id, recover_inflight=False
        ) as buffer:
            if _snapshot(buffer._db) != before:
                raise ValueError("Gateway semantics changed before compaction")
            result = buffer.compact_metadata()
            after = _snapshot(buffer._db)
            if after != before or buffer._db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Gateway semantic verification failed")
            output = {
                "metadata_bytes_before": result["metadata_bytes_before"],
                "metadata_bytes_after": result["metadata_bytes_after"],
                "ack_rows_compacted": result["ack_rows_compacted"],
                "loss_rows_compacted": result["loss_rows_compacted"],
                "row_counts": {table: len(rows) for table, _, rows in after},
                "semantic_bytes_equal": True,
                "semantic_sha256": _semantic_digest(after),
            }
        print(json.dumps(output, sort_keys=True))
        return 0
    except Exception as error:
        # SQLite/validation exceptions can embed raw rows or private paths.
        print(json.dumps({"error_class": type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
