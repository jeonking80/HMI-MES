"""Real PostgreSQL mixed feature barrier/index and schema-lock serialization.

These tests do not prove the future coordinator mutation gates or public runtime
transition: those belong to the next separately reviewed implementation unit.
"""

import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event

import pytest
from alembic import command
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from apps.api import database
from apps.api.alarm_lifecycle import AlarmRule, AlarmScope
from apps.api.alarm_qualification import PressureRuleConfig, QualificationState
from apps.api.auth import UserAuth
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.database import SCHEMA_LOCK_ID, migration_config
from apps.api.ingestion import IngestionService, IngestionUnavailable
from contracts.transport import BatchBody, GatewayBatch, batch_digest

OLD = "0013_work_order_cancel"
NEW = "0014_mixed_alarm_runtime"
INDEX = "ix_inbox_applied_line_fault_seq"
AT = datetime(2026, 10, 29, tzinfo=UTC)
# Captured deployed0013 admission policy, not derived from future support sets.
DEPLOYED_0013_INGESTION = frozenset(
    {
        "0002_ingestion",
        "0003_user_auth",
        "0004_commands",
        "0005_command_dispatch",
        "0006_settlement",
        "0007_execution_plan",
        "0008_work_orders",
        "0009_work_order_binding",
        "0010_alarm_store",
        "0011_alarm_capture_context",
        "0012_alarm_line_fault",
        OLD,
    }
)

LATEST_FAULT = text(
    "SELECT body,seq FROM event_inbox WHERE source_id=:s AND stream_id=:t "
    "AND kind='FAULT_CHANGED' AND applied_at IS NOT NULL "
    "AND body->>'equipment_id'='Line' "
    "AND seq<=:ceiling ORDER BY seq DESC LIMIT 1"
)
EARLIER_FAULT = text(
    "SELECT body FROM event_inbox WHERE source_id=:s AND stream_id=:t "
    "AND seq<:seq AND kind='FAULT_CHANGED' AND applied_at IS NOT NULL "
    "AND body->>'equipment_id'='Line' ORDER BY seq DESC LIMIT 1"
)
PRECEDING_STATE = text(
    "SELECT body FROM event_inbox WHERE source_id=:s AND stream_id=:t "
    "AND seq=:seq AND kind='STATE_CHANGED' AND applied_at IS NOT NULL "
    "AND body->>'equipment_id'='Line'"
)


def migrate(engine, target, *, downgrade=False):
    with engine.connect() as db:
        action = command.downgrade if downgrade else command.upgrade
        action(migration_config(db), target)


def scalar(engine, sql, parameters=None):
    with engine.connect() as db:
        return db.execute(text(sql), parameters or {}).scalar_one()


@pytest.fixture
def old(engine):
    migrate(engine, OLD)
    bootstrap_reference_data(engine)
    IngestionService(engine).configure_source(
        source_id="source",
        stream_id="stream",
        gateway_id="gateway",
        line_id=LINE_ID,
        capture_generation=1,
        now=AT,
    )
    return engine


def seed_legacy_frontier(db, *, restart=False):
    """Raw frozen schema fixture; does not claim runtime activation/authentication."""
    config = PressureRuleConfig(rule=AlarmRule(rule_id="pressure", version="p1", severity="HIGH"))
    scope = AlarmScope(line_id=LINE_ID, source_id="source", stream_id="stream", asset_id="Sealer")
    db.execute(
        text(
            "INSERT INTO alarm_channels(channel_id,line_id,source_id,stream_id,asset_id,rule_id,"
            "current_version,last_position,last_observed_at,qualification) VALUES "
            "('legacy-channel',:line,'source','stream','Sealer','pressure','p1',NULL,NULL,"
            "CAST(:q AS jsonb))"
        ),
        {"line": LINE_ID, "q": QualificationState(scope=scope, config=config).model_dump_json()},
    )
    db.execute(
        text(
            "INSERT INTO alarm_rule_versions VALUES "
            "('legacy-channel','p1',CAST(:config AS jsonb),'fixture-admin',:at,'fixture-rule')"
        ),
        {"config": config.model_dump_json(), "at": AT},
    )
    db.execute(
        text(
            "INSERT INTO alarm_capture_frontiers VALUES "
            "('legacy-channel','source','stream','gateway',1,0,0,0,'old-api-epoch',:hash,:restart)"
        ),
        {"hash": "a" * 64, "restart": restart},
    )


def fixture_user(engine):
    UserAuth(engine, allowed_origins={"https://fixture.test"}).bootstrap_user(
        "fixture-admin", secrets.token_urlsafe(24), roles={"equipment_admin"}
    )


def test_schema_upgrade_preserves_original_rows_and_empty_roundtrip(old):
    before = scalar(old, "SELECT count(*) FROM source_streams")
    migrate(old, NEW)
    assert scalar(old, "SELECT version_num FROM alembic_version") == NEW
    assert scalar(old, "SELECT count(*) FROM source_streams") == before
    definition = scalar(
        old, "SELECT indexdef FROM pg_indexes WHERE indexname=:name", {"name": INDEX}
    )
    assert (
        "FAULT_CHANGED" in definition
        and "Line" in definition
        and "applied_at IS NOT NULL" in definition
    )
    assert "seq DESC" in definition
    migrate(old, OLD, downgrade=True)
    assert scalar(old, "SELECT version_num FROM alembic_version") == OLD
    assert scalar(old, "SELECT to_regclass(:name)", {"name": INDEX}) is None
    migrate(old, NEW)


@pytest.mark.parametrize("restart,decision", [(False, False), (True, False), (True, True)])
def test_any_legacy_frontier_or_decision_refuses_downgrade_even_without_current_alarm(
    old, restart, decision
):
    fixture_user(old)
    if decision:
        body = BatchBody(
            schema_version="1.0",
            batch_id="original",
            gateway_id="gateway",
            source_id="source",
            stream_id="stream",
            capture_generation=1,
            capture_seq=1,
            captured_at=AT,
            samples=(
                {
                    "sample_id": "pressure",
                    "tag_id": "Sealer.AirPressure",
                    "source_timestamp": AT,
                    "server_timestamp": AT,
                    "acquired_at": AT,
                    "status_code": 0,
                    "reading": {"data_type": "Double", "value": 5.0},
                },
            ),
        )
        IngestionService(old).ingest(
            GatewayBatch(body=body, content_sha256=batch_digest(body)),
            authenticated_gateway_id="gateway",
            authenticated_source_id="source",
            authenticated_stream_id="stream",
            now=AT,
        )
    with old.begin() as db:
        seed_legacy_frontier(db, restart=restart)
        if decision:
            db.execute(
                text(
                    "INSERT INTO alarm_capture_decisions VALUES "
                    "('legacy-channel','gateway','original','source','stream',1,1,:hash,1,"
                    "'old-api-epoch','p1','UNSUPPORTED','FIXTURE',NULL,NULL,'{}'::jsonb)"
                ),
                {"hash": batch_digest(body)},
            )
    migrate(old, NEW)
    assert scalar(old, "SELECT count(*) FROM alarm_instances") == 0
    with pytest.raises(DBAPIError):
        migrate(old, OLD, downgrade=True)
    assert scalar(old, "SELECT version_num FROM alembic_version") == NEW
    assert scalar(old, "SELECT count(*) FROM alarm_capture_frontiers") == 1
    assert scalar(old, "SELECT count(*) FROM alarm_capture_decisions") == int(decision)
    assert scalar(old, "SELECT to_regclass(:name)", {"name": INDEX}) is not None


def test_late_real_ddl_error_rolls_back_index_and_revision_then_retry_succeeds(old):
    def fail_after_index(db, cursor, statement, parameters, context, many):
        if statement.lstrip().startswith("UPDATE alembic_version"):
            db.exec_driver_sql("SELECT 1/0")

    event.listen(old, "before_cursor_execute", fail_after_index)
    try:
        with pytest.raises(DBAPIError):
            migrate(old, NEW)
    finally:
        event.remove(old, "before_cursor_execute", fail_after_index)
    assert scalar(old, "SELECT version_num FROM alembic_version") == OLD
    assert scalar(old, "SELECT to_regclass(:name)", {"name": INDEX}) is None
    migrate(old, NEW)
    assert scalar(old, "SELECT version_num FROM alembic_version") == NEW


def test_actual_ingestion_admission_with_frozen_old_revision_policy_refuses_0014(old, monkeypatch):
    import apps.api.ingestion as ingestion

    migrate(old, NEW)
    monkeypatch.setattr(ingestion, "SUPPORTED_INGESTION_REVISIONS", DEPLOYED_0013_INGESTION)
    with pytest.raises(IngestionUnavailable):
        IngestionService(old).configure_source(
            source_id="forbidden",
            stream_id="forbidden",
            gateway_id="forbidden",
            line_id=LINE_ID,
            capture_generation=1,
            now=AT,
        )
    assert scalar(old, "SELECT count(*) FROM source_streams") == 1
    # Real existing admission mechanism with frozen deployed policy, not an old
    # executable end-to-end test or a proof of the future coordinator14 gate.


def walk(plan):
    yield plan
    for child in plan.get("Plans", ()):
        yield from walk(child)


def explain(db, statement, parameters):
    return db.execute(
        text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement.text), parameters
    ).scalar_one()[0]


def test_exact_sparse_fault_queries_use_partial_index_and_preserve_results(old, tmp_path):
    # Representative synthetic inbox SQL workload, not a claim of production
    # ingestion throughput: 120k applied rows, two old Line faults, one Line state.
    with old.begin() as db:
        db.execute(
            text("""INSERT INTO event_inbox
            (event_id,source_id,stream_id,seq,execution_id,kind,body,content_sha256,
             stored_at,applied_at,reason_code,reason)
            SELECT 'load-'||n,'source','stream',n,NULL,
                CASE WHEN n IN (10,60000) THEN 'FAULT_CHANGED'
                     WHEN n=59999 THEN 'STATE_CHANGED' ELSE 'PRODUCTION_GOOD' END,
                jsonb_build_object('equipment_id',CASE WHEN n IN (10,59999,60000)
                    THEN 'Line' ELSE 'outfeed-1' END,'fixture_seq',n),
                repeat('a',64),:at,:at,NULL,NULL
            FROM generate_series(1,120000) AS n"""),
            {"at": AT},
        )
        db.execute(text("ANALYZE event_inbox"))
        db.execute(
            text(
                "UPDATE stream_progress SET verified_through_seq=120000,"
                "latest_ingested_seq=120000 WHERE source_id='source' AND stream_id='stream'"
            )
        )
        latest_args = {"s": "source", "t": "stream", "ceiling": 120000}
        prior_args = {"s": "source", "t": "stream", "seq": 60000}
        original_latest = db.execute(LATEST_FAULT, latest_args).mappings().one()
        original_prior = db.execute(EARLIER_FAULT, prior_args).scalar_one()
        before = explain(db, LATEST_FAULT, latest_args)
        before_prior = explain(db, EARLIER_FAULT, prior_args)
    migrate(old, NEW)
    with old.begin() as db:
        db.execute(text("ANALYZE event_inbox"))
        after = explain(db, LATEST_FAULT, latest_args)
        prior_plan = explain(db, EARLIER_FAULT, prior_args)
        assert db.execute(LATEST_FAULT, latest_args).mappings().one() == original_latest
        assert db.execute(EARLIER_FAULT, prior_args).scalar_one() == original_prior
        assert (
            db.execute(PRECEDING_STATE, {"s": "source", "t": "stream", "seq": 59999}).scalar_one()[
                "fixture_seq"
            ]
            == 59999
        )
        assert int(original_latest["seq"]) == 60000 and original_prior["fixture_seq"] == 10
        assert INDEX in {node.get("Index Name") for node in walk(after["Plan"])}
        assert INDEX in {node.get("Index Name") for node in walk(prior_plan["Plan"])}
        for measured in (after, prior_plan):
            removed = sum(node.get("Rows Removed by Filter", 0) for node in walk(measured["Plan"]))
            assert removed < 10  # no unrelated production-event prefix scan
        db.execute(
            text("""INSERT INTO event_inbox VALUES
            ('unverified','source','stream',120001,NULL,'FAULT_CHANGED',
             CAST(:body AS jsonb),repeat('b',64),:at,NULL,'WAIT','Unverified fixture')"""),
            {"at": AT, "body": json.dumps({"equipment_id": "Line", "fixture_seq": 120001})},
        )
        assert (
            int(db.execute(LATEST_FAULT, latest_args | {"ceiling": 120001}).mappings().one()["seq"])
            == 60000
        )
        db.execute(
            text(
                "UPDATE event_inbox SET applied_at=:at,reason_code=NULL,reason=NULL "
                "WHERE event_id='unverified'"
            ),
            {"at": AT},
        )
        db.execute(
            text(
                "UPDATE stream_progress SET verified_through_seq=120001,"
                "latest_ingested_seq=120001 WHERE source_id='source' AND stream_id='stream'"
            )
        )
        assert (
            int(db.execute(LATEST_FAULT, latest_args | {"ceiling": 120001}).mappings().one()["seq"])
            == 120001
        )
        assert (
            int(db.execute(LATEST_FAULT, latest_args | {"ceiling": 59999}).mappings().one()["seq"])
            == 10
        )
    # Concrete machine-local evidence is generated only when this future test runs.
    (tmp_path / "line-fault-explain.json").write_text(
        json.dumps(
            {
                "before_latest": before,
                "before_prior": before_prior,
                "after_latest": after,
                "after_prior": prior_plan,
            },
            indent=2,
        )
    )


def wait_until_advisory_wait(engine, pid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if scalar(
            engine,
            "SELECT COALESCE(wait_event='advisory',false) FROM pg_stat_activity WHERE pid=:pid",
            {"pid": pid},
        ):
            return
        time.sleep(0.01)
    pytest.fail("Expected concurrent session to wait on the real schema advisory lock")


def test_shared_schema_writer_wins_then_downgrade_waits_and_refuses_used_frontier(old):
    fixture_user(old)
    migrate(old, NEW)
    acquired = Event()
    downgrade_pid = []

    def downgrade():
        with old.connect() as db:
            # End this metadata transaction before production env.py begins its own.
            downgrade_pid.append(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
            db.rollback()
            acquired.set()
            with pytest.raises(DBAPIError):
                command.downgrade(migration_config(db), OLD)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with old.begin() as writer:
            writer.execute(
                text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": SCHEMA_LOCK_ID}
            )
            seed_legacy_frontier(writer)
            future = pool.submit(downgrade)
            assert acquired.wait(timeout=5)
            wait_until_advisory_wait(old, downgrade_pid[0])
            assert not future.done()
        future.result(timeout=10)
    assert scalar(old, "SELECT version_num FROM alembic_version") == NEW


def test_empty_downgrade_wins_then_schema_reader_observes_old_head(old):
    migrate(old, NEW)
    holding = Event()
    release = Event()
    reader_pid = []

    def stop_before_drop(db, cursor, statement, parameters, context, many):
        if statement.lstrip().startswith("DROP INDEX") and INDEX in statement:
            holding.set()
            assert release.wait(timeout=10)

    def reader():
        with old.begin() as db:
            reader_pid.append(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
            db.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": SCHEMA_LOCK_ID})
            return db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    event.listen(old, "before_cursor_execute", stop_before_drop)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            migration = pool.submit(migrate, old, OLD, downgrade=True)
            assert holding.wait(timeout=5)
            pending = pool.submit(reader)
            deadline = time.monotonic() + 5
            while not reader_pid and time.monotonic() < deadline:
                time.sleep(0.01)
            assert reader_pid
            wait_until_advisory_wait(old, reader_pid[0])
            release.set()
            migration.result(timeout=10)
            assert pending.result(timeout=10) == OLD
    finally:
        release.set()
        event.remove(old, "before_cursor_execute", stop_before_drop)
    # This proves real env.py serialization and observable old head only. Unit B
    # MUST additionally call its actual fresh mutation here and prove rejection.


def test_explicit_mixed_feature_set_rejects_old_heads_without_claiming_coordinator_coverage():
    supported = getattr(database, "SUPPORTED_MIXED_ALARM_REVISIONS", frozenset())
    # Registration, queue and item additions preserve the mixed runtime. Retention
    # moves its immutable decision FK to receipts; compaction keeps referenced
    # receipts; append-only segments also preserve those FK-referenced rows.
    assert supported == {
        NEW,
        "0015_analog_history",
        "0016_downtime_reasons",
        "0017_guest_viewer",
        "0018_clock_evidence",
        "0019_registration_ids",
        "0020_line_queue",
        "0021_item_master",
        "0022_ingest_retention",
        "0023_ingest_compaction",
        "0024_ingest_retired_segments",
    }
    assert OLD not in supported
    # No fake writer/helper mirrors the proposed admission algorithm. The actual
    # start/consume/invalidate gate tests belong to Unit B after it exists.
