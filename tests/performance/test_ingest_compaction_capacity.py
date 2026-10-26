"""Synthetic 24-hour raw-storage measurement on disposable PostgreSQL only.

Run separately from the integration suite. Bulk rows exercise raw/receipt storage
and bounded maintenance, not PLC collection or production acceptance. The first
capture uses the ingestion service; subsequent validated bodies use its real ACK
shape without running sample projections or business consumers 86,400 times.
"""

import hashlib
import json
import math
import os
import time
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import sqlalchemy as sa

from apps.api.ingest_retention import maybe_prune_ingest
from apps.api.ingestion import ingest_batches
from contracts.transport import (
    _TAGS,
    BatchBody,
    GatewayBatch,
    batch_digest,
    canonical_batch_body,
)
from tests.integration.conftest import engine as engine
from tests.integration.conftest import migrated as migrated
from tests.integration.conftest import postgres_url as postgres_url
from tests.integration.test_ingest_retention import SCOPE
from tests.integration.test_ingest_retention import observation_service as observation_service

CAPTURES = 86_400
RETENTION_HOURS = 3
INSERT_CHUNK = 500
TABLES = (
    "ingest_batches",
    "ingest_receipts",
    "ingest_retention_ranges",
    "ingest_retired_batch_chunks",
)


def _snapshot(engine):
    result = {}
    with engine.connect() as db:
        for table in TABLES:
            # Names come exclusively from this module's fixed table allowlist.
            result[table] = {
                "rows": db.execute(sa.text(f"SELECT count(*) FROM public.{table}")).scalar_one(),
                **dict(
                    db.execute(
                        sa.text("""SELECT pg_table_size(CAST(:name AS regclass)) AS table_bytes,
                            pg_indexes_size(CAST(:name AS regclass)) AS index_bytes,
                            pg_total_relation_size(CAST(:name AS regclass)) AS total_bytes"""),
                        {"name": "public." + table},
                    )
                    .mappings()
                    .one()
                ),
            }
        result["expired_receipts"] = db.execute(
            sa.text("SELECT count(*) FROM ingest_receipts WHERE expired_at IS NOT NULL")
        ).scalar_one()
        result["retired_batch_ids"] = db.execute(
            sa.text(
                "SELECT COALESCE(sum(cardinality(batch_ids)),0) FROM ingest_retired_batch_chunks"
            )
        ).scalar_one()
        result["maximum_chunk_ids"] = db.execute(
            sa.text(
                "SELECT COALESCE(max(cardinality(batch_ids)),0) FROM ingest_retired_batch_chunks"
            )
        ).scalar_one()
        result["covered_captures"] = int(
            db.execute(
                sa.text("SELECT COALESCE(sum(deleted_count),0) FROM ingest_retention_ranges")
            ).scalar_one()
        )
        result["database_bytes"] = db.execute(
            sa.text("SELECT pg_database_size(current_database())")
        ).scalar_one()
    result["selected_relations_total_bytes"] = sum(result[table]["total_bytes"] for table in TABLES)
    return result


def test_86400_capture_three_hour_compaction_capacity(
    observation_service, tmp_path, record_property
):
    service = observation_service
    engine = service.engine
    started = time.monotonic()
    with engine.connect() as db:
        assert db.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0023_ingest_compaction"
        )
        end_at = db.execute(sa.text("SELECT clock_timestamp()")).scalar_one()
    first_at = end_at - timedelta(seconds=CAPTURES - 1)
    tags = tuple(_TAGS.items())[:27]
    assert len(tags) == 27
    sample_prefix = uuid4().int & ~((1 << 32) - 1)

    def body_for(seq):
        captured_at = first_at + timedelta(seconds=seq - 1)
        return BatchBody.model_validate(
            {
                "schema_version": "1.0",
                "batch_id": str(uuid4()),
                "gateway_id": "gateway-1",
                "source_id": "simulator-1",
                "stream_id": "stream-1",
                "capture_generation": 1,
                "capture_seq": seq,
                "captured_at": captured_at,
                "samples": [
                    {
                        # Disjoint low bits give globally unique UUID sample IDs
                        # without retaining 2.3 million IDs in a Python set.
                        "sample_id": str(UUID(int=sample_prefix | (seq << 8) | index)),
                        "tag_id": tag_id,
                        "source_timestamp": None,
                        "server_timestamp": None,
                        "acquired_at": captured_at,
                        "status_code": 0x80000000,
                        "reading": {"data_type": tag["data_type"], "value": None},
                    }
                    for index, (tag_id, tag) in enumerate(tags)
                ],
            }
        )

    first = body_for(1)
    first_batch = GatewayBatch(body=first, content_sha256=batch_digest(first))
    ack = service.ingest(first_batch, **SCOPE, now=first_at)
    ack_template = ack.model_dump(mode="json")
    payload_bytes = len(canonical_batch_body(first).encode("utf-8"))
    pending = []
    for seq in range(2, CAPTURES + 1):
        body = body_for(seq)
        # Validate every exact body and hash its canonical wire representation;
        # neither the first digest nor placeholder IDs are reused across rows.
        canonical = canonical_batch_body(body)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        payload_bytes += len(canonical.encode("utf-8"))
        stored_at = body.captured_at
        row_ack = dict(
            ack_template,
            batch_id=body.batch_id,
            capture_seq=str(body.capture_seq),
            content_sha256=digest,
            committed_at=stored_at.isoformat().replace("+00:00", "Z"),
        )
        pending.append(
            {
                "gateway_id": body.gateway_id,
                "batch_id": body.batch_id,
                "source_id": body.source_id,
                "stream_id": body.stream_id,
                "capture_generation": body.capture_generation,
                "capture_seq": body.capture_seq,
                "content_sha256": digest,
                "body": json.loads(canonical),
                "ack": row_ack,
                "stored_at": stored_at,
            }
        )
        if len(pending) == INSERT_CHUNK or seq == CAPTURES:
            with engine.begin() as db:
                # Receipt and immutable-identity triggers remain enabled.
                db.execute(ingest_batches.insert(), pending)
            pending.clear()

    report_path = Path(
        os.environ.get(
            "HMI_CAPACITY_REPORT_PATH", str(tmp_path / "ingest-compaction-capacity.json")
        )
    )
    report = {
        "measurement": "synthetic_raw_storage_not_plc_or_production_acceptance",
        "captures": CAPTURES,
        "samples_per_capture": len(tags),
        "sample_quality": "Bad, null reading; no fabricated Good values",
        "retention_hours": RETENTION_HOURS,
        "payload_average_bytes": payload_bytes / CAPTURES,
        "fixture_event_fk_references": 0,
        "fk_scope": "event-free baseline; real alarm/business FK receipts remain retained",
        "db_clock_unchanged": True,
        "db_capture_end_at": end_at.isoformat(),
        "load_elapsed_seconds": time.monotonic() - started,
        "before_cleanup": _snapshot(engine),
    }

    def save():
        report["elapsed_seconds"] = time.monotonic() - started
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    save()
    record_property("capacity_report_path", str(report_path))
    assert 6_000 <= report["payload_average_bytes"] <= 7_500
    assert report["before_cleanup"]["ingest_batches"]["rows"] == CAPTURES
    assert report["before_cleanup"]["ingest_receipts"]["rows"] == CAPTURES

    deleted = compacted = attempts = 0
    cleanup_started = time.monotonic()
    while attempts < 10_000 and time.monotonic() - cleanup_started < 1_800:
        # Only the disposable database owner may clear the throttle. Retention
        # timestamps, cursor positions and the actual DB clock are unchanged.
        with engine.begin() as db:
            db.execute(sa.text("UPDATE ingest_retention_state SET last_attempt_at=NULL"))
        outcome = maybe_prune_ingest(
            engine, retention_hours=RETENTION_HOURS, max_batches=1000, max_candidates=2000
        )
        attempts += 1
        assert outcome["status"] in {"PRUNED", "EMPTY"}, outcome
        deleted += outcome["deleted_count"]
        compacted += outcome["compacted_count"]
        with engine.connect() as db:
            tick = db.execute(
                sa.text("SELECT last_attempt_at FROM ingest_retention_state")
            ).scalar_one()
            old_remaining = db.execute(
                sa.text(
                    "SELECT count(*) FROM ingest_batches WHERE stored_at<:tick-interval '3 hours'"
                ),
                {"tick": tick},
            ).scalar_one()
            expired_remaining = db.execute(
                sa.text("SELECT count(*) FROM ingest_receipts WHERE expired_at IS NOT NULL")
            ).scalar_one()
        if old_remaining == 0 and expired_remaining == 0:
            break
    report.update(
        {
            "maintenance_attempts": attempts,
            "deleted_count": deleted,
            "compacted_count": compacted,
            "last_maintenance_tick": tick.isoformat(),
            "old_rows_at_last_tick": old_remaining,
            "cleanup_elapsed_seconds": time.monotonic() - cleanup_started,
            "after_cleanup_before_vacuum": _snapshot(engine),
        }
    )
    save()
    # Ordinary VACUUM reuses space; it does not promise file shrinkage. No FULL.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as db:
        for table in TABLES:
            db.execute(sa.text(f"VACUUM (ANALYZE) public.{table}"))
    report["after_vacuum_analyze"] = _snapshot(engine)
    report["vacuum_mode"] = "ordinary VACUUM ANALYZE; not FULL"
    save()

    after = report["after_cleanup_before_vacuum"]
    raw = after["ingest_batches"]["rows"]
    aging_seconds = (tick - end_at).total_seconds()
    expected_raw = max(0, 10_800 - math.ceil(aging_seconds) + 1)
    report["db_aging_seconds"] = aging_seconds
    report["expected_raw_after_elapsed_seconds"] = expected_raw
    save()
    assert old_remaining == 0
    assert 10_800 - 4_000 <= raw <= 10_800
    assert abs(raw - expected_raw) <= 1
    assert raw + deleted == CAPTURES
    assert after["ingest_receipts"]["rows"] == raw
    assert after["expired_receipts"] == 0
    assert after["covered_captures"] == deleted
    assert after["retired_batch_ids"] == deleted == compacted
    assert 1 <= after["ingest_retention_ranges"]["rows"] <= 4
    assert after["maximum_chunk_ids"] <= 256
    assert after["ingest_retired_batch_chunks"]["rows"] <= math.ceil(CAPTURES / 256)
    for table in TABLES:
        assert report["after_vacuum_analyze"][table]["rows"] == after[table]["rows"]
