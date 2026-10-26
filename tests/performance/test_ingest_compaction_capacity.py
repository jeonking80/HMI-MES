"""Interleaved synthetic 24-hour storage measurement on disposable PostgreSQL.

A test-only copy of the installed maintenance function graph uses a shared clock
offset. The database clock, product functions, guards and application paths are
unchanged. Each minute loads 60 validated captures, runs bounded maintenance, and
periodic ordinary VACUUM permits reuse without first allocating a full-day heap.
Real alarm consumption is measured separately with zero and one configured rule.
This measures raw/clock/alarm storage, not PLC collection or trend projections.
"""

import hashlib
import json
import os
import time
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from apps.api.alarm_consumer import AlarmConsumer, AlarmConsumerConfig
from apps.api.alarm_lifecycle import AlarmRule, AlarmScope
from apps.api.alarm_qualification import PressureRuleConfig
from apps.api.alarm_store import install_rule
from apps.api.auth import Principal, UserAuth
from apps.api.bootstrap import LINE_ID
from apps.api.capture_clock import record_capture_clock
from apps.api.ingest_retention import IngestRetentionExpired
from apps.api.ingestion import ingest_batches
from contracts.transport import (
    _TAGS,
    BatchBody,
    GatewayBatch,
    batch_digest,
    canonical_batch_body,
    gateway_batch_digest,
)
from tests.integration.conftest import engine as engine
from tests.integration.conftest import migrated as migrated
from tests.integration.conftest import postgres_url as postgres_url
from tests.integration.test_ingest_retention import SCOPE
from tests.integration.test_ingest_retention import observation_service as observation_service

CAPTURES = 86_400
RETENTION_HOURS = 3
CAPTURES_PER_MINUTE = 60
VACUUM_EVERY_MINUTES = 60
CHECKPOINT_MINUTES = {180, 360, 720, 1080, 1440}
FUNCTIONS = (
    "compact_ingest_receipts(text,text[],timestamptz)",
    "prune_ingest_batches_hours(integer,integer,integer)",
)


def _snapshot(engine):
    """Include every public table, especially all real alarm persistence rows."""
    tables = {}
    with engine.connect() as db:
        names = (
            db.execute(
                sa.text(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
                )
            )
            .scalars()
            .all()
        )
        quote = db.dialect.identifier_preparer.quote
        for name in names:
            columns = (
                db.execute(
                    sa.text("""SELECT attname FROM pg_attribute
                    WHERE attrelid=CAST(:name AS regclass)
                    AND attnum>0 AND NOT attisdropped ORDER BY attnum"""),
                    {"name": "public." + name},
                )
                .scalars()
                .all()
            )
            column_sizes = "+".join(f"coalesce(pg_column_size(t.{quote(c)}),0)" for c in columns)
            # Identifiers come from this disposable database's catalog and are quoted.
            row = (
                db.execute(
                    sa.text(
                        f"SELECT count(*) AS rows,coalesce(sum({column_sizes}),0) "
                        f"AS live_column_bytes FROM public.{quote(name)} t"
                    )
                )
                .mappings()
                .one()
            )
            tables[name] = {
                "rows": row["rows"],
                "live_column_bytes": int(row["live_column_bytes"]),
                **dict(
                    db.execute(
                        sa.text("""SELECT pg_table_size(CAST(:name AS regclass)) AS table_bytes,
                        pg_indexes_size(CAST(:name AS regclass)) AS index_bytes,
                        pg_total_relation_size(CAST(:name AS regclass)) AS total_bytes"""),
                        {"name": "public." + name},
                    )
                    .mappings()
                    .one()
                ),
            }
        totals = (
            db.execute(
                sa.text("""SELECT
            (SELECT count(*) FROM ingest_receipts WHERE expired_at IS NOT NULL) AS expired_receipts,
            (SELECT coalesce(sum(deleted_count),0) FROM ingest_retention_ranges)
                AS covered_captures,
            (SELECT coalesce(sum(cardinality(uuid_ids)),0) FROM ingest_retired_batch_segments)
                AS retired_uuid_ids,
            (SELECT coalesce(sum(cardinality(text_ids)),0) FROM ingest_retired_batch_segments)
                AS retired_text_ids,
            (SELECT coalesce(max(cardinality(uuid_ids)+cardinality(text_ids)),0)
                FROM ingest_retired_batch_segments) AS maximum_segment_ids,
            (SELECT count(*) FROM alarm_capture_decisions d JOIN ingest_receipts r
                ON r.gateway_id=d.gateway_id AND r.batch_id=d.batch_id
                AND r.content_sha256=d.content_sha256) AS exact_alarm_receipt_fk_rows,
            pg_database_size(current_database()) AS database_bytes""")
            )
            .mappings()
            .one()
        )
    return {
        "tables": tables,
        **{key: int(value) for key, value in totals.items()},
        "all_public_relation_bytes": sum(t["total_bytes"] for t in tables.values()),
        "all_public_live_column_bytes": sum(t["live_column_bytes"] for t in tables.values()),
    }


def _clock_clones(engine):
    """Copy installed SQL; only names, child call and clock source are replaced."""
    mapping = {
        "public.compact_ingest_receipts(": "public.capacity_compact_ingest_receipts(",
        "public.prune_ingest_batches_hours(": "public.capacity_prune_ingest_batches_hours(",
        "clock_timestamp()": "public.capacity_clock_timestamp()",
    }
    graph = []
    with engine.begin() as db:
        db.execute(
            sa.text("""CREATE FUNCTION public.capacity_clock_timestamp()
            RETURNS timestamptz LANGUAGE sql VOLATILE AS $$
            SELECT clock_timestamp()+make_interval(secs=>
                current_setting('hmi_capacity.clock_offset')::double precision) $$""")
        )
        for signature in FUNCTIONS:
            original = db.execute(
                sa.text("SELECT pg_get_functiondef(CAST(:name AS regprocedure))"),
                {"name": "public." + signature},
            ).scalar_one()
            cloned = original
            for before, after in mapping.items():
                cloned = cloned.replace(before, after)
            assert cloned != original and "public.capacity_clock_timestamp()" in cloned
            db.execute(sa.text(cloned))
            graph.append(
                {
                    "installed_signature": signature,
                    "installed_sha256": hashlib.sha256(original.encode()).hexdigest(),
                    "clone_sha256": hashlib.sha256(cloned.encode()).hexdigest(),
                    "installed_sql": original,
                    "cloned_sql": cloned,
                }
            )
    return {"replacements": mapping, "functions": graph}


def _maintenance(engine, simulated_now):
    with engine.begin() as db:
        # Only the clones read this test GUC; product guards never consult it.
        # One offset covers parent and child, preserving their real 500ms budget.
        # Stay just beyond the real one-minute throttle despite SQL scheduling
        # jitter between rounds; do not reset or bypass durable throttle state.
        db.execute(
            sa.text("""SELECT set_config('hmi_capacity.clock_offset',
            extract(epoch FROM (greatest(CAST(:at AS timestamptz),
                (SELECT last_attempt_at+interval '1 minute 1 millisecond'
                 FROM ingest_retention_state WHERE singleton))-clock_timestamp()))::text,true)"""),
            {"at": simulated_now},
        )
        db.execute(sa.text("SET LOCAL statement_timeout='1500ms'"))
        db.execute(sa.text("SET LOCAL lock_timeout='100ms'"))
        outcome = db.execute(
            sa.text("SELECT public.capacity_prune_ingest_batches_hours(:hours,200,400)"),
            {"hours": RETENTION_HOURS},
        ).scalar_one()
        outcome["maintenance_tick"] = (
            db.execute(
                sa.text("SELECT last_attempt_at FROM ingest_retention_state WHERE singleton")
            )
            .scalar_one()
            .isoformat()
        )
        return outcome


def _vacuum(engine):
    # Ordinary VACUUM permits reuse, without FULL/table rewrites or shrinking files.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as db:
        db.execute(sa.text("VACUUM (ANALYZE)"))


def _consumer(service, at):
    scope = AlarmScope(
        line_id=LINE_ID, source_id="simulator-1", stream_id="stream-1", asset_id="Sealer"
    )
    rule = PressureRuleConfig(
        rule=AlarmRule(rule_id="capacity-pressure", version="v1", severity="HIGH")
    )
    UserAuth(service.engine, allowed_origins={"https://capacity.test"}).bootstrap_user(
        "capacity-admin", "Capacity-test-only-password-123!", roles={"equipment_admin"}
    )
    with service.engine.begin() as db:
        install_rule(
            db,
            scope,
            rule,
            input_id="capacity-rule-install",
            principal=Principal("capacity-admin", frozenset({"equipment_admin"}), False),
            at=at,
        )
    consumer = AlarmConsumer(
        AlarmConsumerConfig(
            scope=scope,
            gateway_id="gateway-1",
            capture_generation=1,
            rules=(rule,),
            clock_policy=None,
        ),
        consumer_epoch="capacity-api-epoch",
    )
    with service.engine.begin() as db:
        consumer.start(db, now=at)
    return consumer


def _growth(checkpoints):
    earlier = next(p for p in checkpoints if p["simulated_minute"] == 720)
    final = checkpoints[-1]
    result = {
        "basis": "12h to 24h after warmup; extrapolation, not a capacity guarantee",
        "includes": "all public tables including alarm decisions/receipts and retired IDs",
        "limit_bytes": 500_000_000,
    }
    for kind, key in (
        ("live_columns", "all_public_live_column_bytes"),
        ("relation_files", "all_public_relation_bytes"),
        ("database_files", "database_bytes"),
    ):
        daily = max(0, final[key] - earlier[key]) * 2
        result[kind] = {
            "observed_12h_delta_bytes": final[key] - earlier[key],
            "estimated_growth_bytes_per_day": daily,
            "estimated_additional_days_until_0_5gb": (
                0.0
                if final[key] >= 500_000_000
                else (500_000_000 - final[key]) / daily
                if daily
                else None
            ),
        }
    return result


@pytest.mark.parametrize("alarm_channels", [0, 1], ids=["no-alarm-channel", "one-alarm-channel"])
def test_86400_capture_three_hour_compaction_capacity(
    observation_service, tmp_path, record_property, alarm_channels
):
    service = observation_service
    engine = service.engine
    started = time.monotonic()
    with engine.connect() as db:
        assert db.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0024_ingest_retired_segments"
        )
        final_database_now = db.execute(sa.text("SELECT clock_timestamp()")).scalar_one()
    first_at = final_database_now - timedelta(seconds=CAPTURES)
    consumer = _consumer(service, first_at) if alarm_channels else None
    service.alarm_consumer = consumer
    tags = list(tuple(_TAGS.items())[:27])
    if "Sealer.AirPressure" not in {name for name, _ in tags}:
        tags[-1] = ("Sealer.AirPressure", _TAGS["Sealer.AirPressure"])
    sample_prefix = uuid4().int & ~((1 << 32) - 1)

    def body_for(seq):
        at = first_at + timedelta(seconds=seq - 1)
        return BatchBody.model_validate(
            {
                "schema_version": "1.0",
                "batch_id": str(uuid4()),
                "gateway_id": "gateway-1",
                "source_id": "simulator-1",
                "stream_id": "stream-1",
                "capture_generation": 1,
                "capture_seq": seq,
                "captured_at": at,
                "samples": [
                    {
                        "sample_id": str(UUID(int=sample_prefix | (seq << 8) | index)),
                        "tag_id": name,
                        "source_timestamp": None,
                        "server_timestamp": None,
                        "acquired_at": at,
                        "status_code": 0x80000000,
                        "reading": {"data_type": tag["data_type"], "value": None},
                    }
                    for index, (name, tag) in enumerate(tags)
                ],
            }
        )

    first = body_for(1)
    ack_template = service.ingest(
        GatewayBatch(body=first, content_sha256=batch_digest(first)), **SCOPE, now=first_at
    ).model_dump(mode="json")
    with engine.connect() as db:
        source = dict(
            db.execute(
                sa.text(
                    "SELECT * FROM source_streams WHERE source_id='simulator-1' "
                    "AND stream_id='stream-1'"
                )
            )
            .mappings()
            .one()
        )
    graph = _clock_clones(engine)
    requested_path = os.environ.get("HMI_CAPACITY_REPORT_PATH")
    base = Path(requested_path) if requested_path else tmp_path / "ingest-compaction-capacity.json"
    report_path = base.with_name(base.stem + f"-channels-{alarm_channels}" + base.suffix)
    report = {
        "measurement": "interleaved_synthetic_raw_clock_and_real_alarm_storage",
        "captures": CAPTURES,
        "alarm_channels": alarm_channels,
        "samples_per_capture": len(tags),
        "retention_hours": RETENTION_HOURS,
        "captures_per_simulated_minute": CAPTURES_PER_MINUTE,
        "sample_quality": "actual contract tags; Bad/null; real UNKNOWN pressure decisions",
        "scope_limit": (
            "bulk raw inserts plus real capture clock and alarm consumers; no trends/PLC"
        ),
        "db_clock_unchanged": True,
        "final_anchor_database_now": final_database_now.isoformat(),
        "maintenance_clock_graph": graph,
        "vacuum_every_simulated_minutes": VACUUM_EVERY_MINUTES,
        "vacuum_mode": "ordinary VACUUM ANALYZE; no FULL",
        "live_bytes_definition": "sum pg_column_size(each column); excludes tuple/index overhead",
        "maintenance": [],
        "checkpoints": [],
        "outcome": "running",
    }

    def save():
        report["elapsed_seconds"] = time.monotonic() - started
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    record_property("capacity_report_path", str(report_path))
    payload_bytes = len(canonical_batch_body(first).encode())
    report["before_load"] = _snapshot(engine)
    save()
    try:
        for minute in range(1, CAPTURES // CAPTURES_PER_MINUTE + 1):
            values = []
            for seq in range(max(2, (minute - 1) * 60 + 1), minute * 60 + 1):
                body = body_for(seq)
                canonical = canonical_batch_body(body)
                digest = hashlib.sha256(canonical.encode()).hexdigest()
                payload_bytes += len(canonical.encode())
                row_ack = dict(
                    ack_template,
                    batch_id=body.batch_id,
                    content_sha256=digest,
                    committed_at=body.captured_at.isoformat().replace("+00:00", "Z"),
                )
                values.append(
                    (
                        body,
                        digest,
                        {
                            "gateway_id": body.gateway_id,
                            "batch_id": body.batch_id,
                            "source_id": body.source_id,
                            "stream_id": body.stream_id,
                            "capture_generation": body.capture_generation,
                            "capture_seq": seq,
                            "content_sha256": digest,
                            "body": json.loads(canonical),
                            "ack": row_ack,
                            "stored_at": body.captured_at,
                        },
                    )
                )
            with engine.begin() as db:
                db.execute(ingest_batches.insert(), [row for _, _, row in values])
                for body, digest, _ in values:
                    record_capture_clock(
                        db,
                        body,
                        source=source,
                        now=body.captured_at,
                        policy=None,
                        batch_digest=digest,
                        measured=None,
                        input_evidence_sha256=gateway_batch_digest(
                            GatewayBatch(body=body, content_sha256=digest)
                        ),
                    )
                    if consumer is not None:
                        consumer.consume(db, body, source=source, now=body.captured_at)
            simulated_now = first_at + timedelta(minutes=minute)
            outcome = _maintenance(engine, simulated_now)
            assert outcome["status"] in {"PRUNED", "EMPTY"}, outcome
            report["maintenance"].append({"simulated_minute": minute, **outcome})
            if minute % VACUUM_EVERY_MINUTES == 0:
                if minute in CHECKPOINT_MINUTES:
                    report["before_vacuum"] = _snapshot(engine)
                _vacuum(engine)
            if minute in CHECKPOINT_MINUTES:
                report["checkpoints"].append({"simulated_minute": minute, **_snapshot(engine)})
                save()
        report["payload_average_bytes"] = payload_bytes / CAPTURES
        after = report["checkpoints"][-1]
        report["growth_estimate"] = _growth(report["checkpoints"])
        raw = after["tables"]["ingest_batches"]["rows"]
        deleted = sum(m["deleted_count"] for m in report["maintenance"])
        assert 6_000 <= report["payload_average_bytes"] <= 7_500
        assert raw + deleted == CAPTURES
        assert 10_740 <= raw <= 10_860, after
        assert after["covered_captures"] == deleted
        assert after["exact_alarm_receipt_fk_rows"] == alarm_channels * CAPTURES
        assert after["tables"]["alarm_capture_decisions"]["rows"] == alarm_channels * CAPTURES
        assert after["tables"]["ingest_receipts"]["rows"] == (CAPTURES if alarm_channels else raw)
        assert after["retired_uuid_ids"] == (0 if alarm_channels else deleted)
        assert after["retired_text_ids"] == 0
        assert after["maximum_segment_ids"] <= 256
        assert after["expired_receipts"] == (deleted if alarm_channels else 0)
        with pytest.raises(IngestRetentionExpired):
            service.ingest(
                GatewayBatch(body=first, content_sha256=batch_digest(first)),
                **SCOPE,
                now=final_database_now,
            )
        report["outcome"] = "passed"
    except BaseException as error:
        report["outcome"] = "failed"
        report["failure_type"] = type(error).__name__
        raise
    finally:
        save()
