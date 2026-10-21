"""Durable application invariants against a migrated, disposable PostgreSQL database."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from apps.api.bootstrap import ASSET_IDS, LINE_ID, bootstrap_reference_data
from apps.api.ingestion import (
    IngestConflict,
    IngestionService,
    SourceBindingError,
    SourceConflicted,
)
from contracts.models import UINT64_MAX, Parameters
from contracts.transport import BatchBody, GatewayBatch, batch_digest, validate_ack

AT = datetime(2026, 10, 20, 12, 1, tzinfo=UTC)
EXAMPLE = Path(__file__).resolve().parents[2] / "contracts/examples/transport-batch.json"


def event(seq=1, *, quantity=1, execution_id="run-A", **changes):
    value = json.loads(EXAMPLE.read_text())["body"]["events"][0]
    value.update(
        seq=str(seq),
        event_id=f"event-{seq}",
        equipment_id=ASSET_IDS["Outfeed"],
        execution_id=execution_id,
    )
    value["payload"]["quantity"] = str(quantity)
    value.update(changes)
    return value


def batch(*events, capture_seq=1, batch_id=None, gaps=(), **changes):
    value = json.loads(EXAMPLE.read_text())["body"]
    value.update(
        batch_id=batch_id or f"batch-{capture_seq}",
        capture_seq=str(capture_seq),
        events=list(events),
        samples=[],
        gaps=list(gaps),
    )
    value.update(changes)
    body = BatchBody.model_validate(value)
    return GatewayBatch(body=body, content_sha256=batch_digest(body))


def submit(service, value):
    return service.ingest(
        value,
        authenticated_gateway_id="gateway-1",
        authenticated_source_id="simulator-1",
        authenticated_stream_id="stream-1",
        now=AT,
    )


def register(service, execution_id="run-A"):
    service.register_execution(
        execution_id=execution_id,
        source_id="simulator-1",
        stream_id="stream-1",
        work_order_id=f"work-{execution_id}",
        recipe_version="recipe-1",
        counter_epoch="epoch-1",
        parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
        now=AT,
    )


@pytest.fixture
def service(migrated):
    bootstrap_reference_data(migrated)
    service = IngestionService(migrated)
    service.configure_source(
        source_id="simulator-1",
        stream_id="stream-1",
        gateway_id="gateway-1",
        line_id=LINE_ID,
        capture_generation=1,
        now=AT,
    )
    register(service)
    return service


def scalar(service, statement):
    with service.engine.connect() as connection:
        return connection.execute(sa.text(statement)).scalar_one()


def test_ack_requires_atomic_commit_and_is_visible_on_independent_connection(service, monkeypatch):
    def before_commit(connection):
        assert (
            connection.execute(sa.text("SELECT count(*) FROM production_events")).scalar_one() == 1
        )
        assert scalar(service, "SELECT count(*) FROM ingest_batches") == 0
        assert scalar(service, "SELECT count(*) FROM event_inbox") == 0
        assert scalar(service, "SELECT good_quantity FROM execution_totals") == 0

    monkeypatch.setattr(service, "_before_commit", before_commit)
    value = batch(event(quantity=7))
    ack = submit(service, value)
    validate_ack(value, ack)
    assert ack.durability == "COMMITTED" and ack.events[0].disposition == "APPLIED"
    assert scalar(service, "SELECT ack FROM ingest_batches") == ack.model_dump(mode="json")
    assert scalar(service, "SELECT count(*) FROM production_events") == 1
    assert service.get_totals("run-A")["good_quantity"] == 7
    assert ack.progress.verified_through_seq == 1


@pytest.mark.parametrize("at_commit", [False, True])
def test_transaction_failure_returns_no_ack_and_rolls_back_every_projection(
    service, monkeypatch, at_commit
):
    def fail(*args):
        raise RuntimeError("injected transaction failure")

    if at_commit:
        sa.event.listen(service.engine, "commit", fail)
    else:
        monkeypatch.setattr(service, "_before_commit", fail)
    try:
        with pytest.raises(RuntimeError, match="injected transaction failure"):
            submit(service, batch(event()))
    finally:
        if at_commit:
            sa.event.remove(service.engine, "commit", fail)
        else:
            monkeypatch.undo()
    for table in ("event_inbox", "production_events", "ingest_batches", "event_apply_errors"):
        assert scalar(service, f"SELECT count(*) FROM {table}") == 0
    assert service.get_totals("run-A")["good_quantity"] == 0
    assert service.get_progress("simulator-1", "stream-1").latest_ingested_seq == 0
    assert submit(service, batch(event())).events[0].disposition == "APPLIED"


def test_lost_ack_retry_and_new_batch_event_duplicate_apply_once(service):
    value = batch(event(quantity=9))
    original = submit(service, value)
    assert submit(service, value) == original
    duplicate = submit(service, batch(event(quantity=9), capture_seq=2))
    assert duplicate.events == original.events
    assert scalar(service, "SELECT count(*) FROM ingest_batches") == 2
    assert scalar(service, "SELECT count(*) FROM event_inbox") == 1
    assert scalar(service, "SELECT count(*) FROM production_events") == 1
    assert service.get_totals("run-A")["good_quantity"] == 9


@pytest.mark.parametrize("collision", ["batch", "capture", "event", "sequence"])
def test_identity_collision_commits_evidence_and_hold_without_ack_or_partial_application(
    service, collision
):
    first = batch(event(quantity=2))
    first_ack = submit(service, first)
    if collision == "batch":
        incoming = batch(event(quantity=3))
    elif collision == "capture":
        incoming = batch(event(2), batch_id="other-batch")
    elif collision == "event":
        incoming = batch(event(2, event_id="event-1"), capture_seq=2)
    else:
        incoming = batch(event(event_id="another-event"), event(2), capture_seq=2)
    with pytest.raises(IngestConflict, match="conflict evidence committed"):
        submit(service, incoming)
    assert scalar(service, "SELECT count(*) FROM ingest_conflicts") == 1
    assert scalar(service, "SELECT incoming FROM ingest_conflicts")
    assert scalar(service, "SELECT original FROM ingest_conflicts")
    assert scalar(service, "SELECT count(*) FROM ingest_batches") == 1
    assert scalar(service, "SELECT count(*) FROM event_inbox") == 1
    assert service.get_totals("run-A")["good_quantity"] == 2
    assert service.get_source_status("simulator-1", "stream-1")["control_restricted"]
    assert submit(service, first) == first_ack
    with pytest.raises(SourceConflicted):
        submit(service, batch(event(3), capture_seq=3))
    with pytest.raises(SourceConflicted):
        service.replay_quarantined(source_id="simulator-1", stream_id="stream-1", now=AT)


def test_unknown_original_execution_is_quarantined_then_replayed_without_mutating_receipt(service):
    value = batch(event(execution_id="late-run", quantity=11))
    original = submit(service, value)
    assert original.events[0].reason_code == "UNKNOWN_EXECUTION"
    assert original.progress.verified_through_seq == 0
    assert original.progress.latest_ingested_seq == 1
    assert service.get_totals("run-A")["good_quantity"] == 0
    assert scalar(service, "SELECT count(*) FROM event_apply_errors") == 1
    register(service, "late-run")
    progress = service.replay_quarantined(
        source_id="simulator-1",
        stream_id="stream-1",
        now=AT + timedelta(seconds=1),
    )
    assert progress.verified_through_seq == 1
    assert service.get_totals("late-run")["good_quantity"] == 11
    assert service.get_totals("run-A")["good_quantity"] == 0
    assert submit(service, value) == original
    assert scalar(service, "SELECT ack FROM ingest_batches") == original.model_dump(mode="json")
    service.replay_quarantined(source_id="simulator-1", stream_id="stream-1", now=AT)
    assert service.get_totals("late-run")["good_quantity"] == 11
    assert scalar(service, "SELECT count(*) FROM production_events") == 1
    assert scalar(service, "SELECT count(*) FROM event_apply_errors") == 1


def test_out_of_order_events_advance_only_after_hole_filled(service):
    first = submit(service, batch(event(3), capture_seq=1))
    assert first.progress.verified_through_seq == 0
    assert [(g.from_seq, g.through_seq) for g in first.progress.unresolved_gaps] == [(1, 2)]
    assert service.get_source_status("simulator-1", "stream-1")["control_restricted"]
    middle = submit(service, batch(event(1), capture_seq=2))
    assert middle.progress.verified_through_seq == 1
    final = submit(service, batch(event(2), capture_seq=3))
    assert final.progress.verified_through_seq == 3
    assert not final.progress.unresolved_gaps
    assert not service.get_source_status("simulator-1", "stream-1")["control_restricted"]
    assert service.get_totals("run-A")["good_quantity"] == 3


def test_irrecoverable_gap_never_becomes_settled_even_if_later_events_arrive(service):
    gap = dict(
        from_seq="1",
        through_seq="2",
        irrecoverable=True,
        detected_at=AT,
        reason_code="SOURCE_RETENTION_EXPIRED",
        reason="Original source events expired",
        affected_execution_ids=["run-A"],
    )
    first = submit(service, batch(event(3), gaps=[gap]))
    assert first.progress.verified_through_seq == 0
    assert first.progress.unresolved_gaps[0].irrecoverable
    filled = submit(service, batch(event(1), event(2), capture_seq=2))
    assert filled.progress.verified_through_seq == 3
    status = service.get_source_status("simulator-1", "stream-1")
    assert status["reconciliation_required"] and status["control_restricted"]
    assert scalar(service, "SELECT ack FROM ingest_batches WHERE batch_id='batch-1'")[
        "recorded_gaps"
    ]


def test_uint64_max_stays_exact_and_overflow_is_quarantined(service):
    first = submit(service, batch(event(quantity=UINT64_MAX), capture_seq=UINT64_MAX))
    assert first.events[0].disposition == "APPLIED"
    assert service.get_totals("run-A")["good_quantity"] == UINT64_MAX
    overflow = submit(service, batch(event(2), capture_seq=2))
    assert overflow.events[0].reason_code == "QUANTITY_OVERFLOW"
    assert overflow.progress.verified_through_seq == 1
    assert service.get_totals("run-A")["good_quantity"] == UINT64_MAX
    assert scalar(service, "SELECT count(*) FROM production_events") == 1
    stored = scalar(
        service, "SELECT body FROM ingest_batches WHERE capture_seq=18446744073709551615"
    )
    assert stored["capture_seq"] == str(UINT64_MAX)
    assert stored["events"][0]["payload"]["quantity"] == str(UINT64_MAX)


def test_max_source_sequence_records_huge_gap_without_rounding_or_iteration(service):
    ack = submit(service, batch(event(UINT64_MAX)))
    assert ack.progress.latest_ingested_seq == UINT64_MAX
    assert ack.progress.verified_through_seq == 0
    assert [(g.from_seq, g.through_seq) for g in ack.progress.unresolved_gaps] == [
        (1, UINT64_MAX - 1)
    ]
    assert scalar(service, "SELECT seq FROM event_inbox") == UINT64_MAX


def test_concurrent_identical_retries_return_one_immutable_ack(service):
    value = batch(event(quantity=13))
    with ThreadPoolExecutor(max_workers=2) as workers:
        acks = list(workers.map(lambda _: submit(service, value), range(2)))
    assert acks[0] == acks[1]
    assert scalar(service, "SELECT count(*) FROM ingest_batches") == 1
    assert scalar(service, "SELECT count(*) FROM production_events") == 1
    assert service.get_totals("run-A")["good_quantity"] == 13


def test_authenticated_scope_and_registered_capture_are_both_required(service):
    with pytest.raises(SourceBindingError):
        service.ingest(
            batch(event()),
            authenticated_gateway_id="other-gateway",
            authenticated_source_id="simulator-1",
            authenticated_stream_id="stream-1",
            now=AT,
        )
    with pytest.raises(SourceBindingError):
        submit(service, batch(event(), capture_generation="2"))
    assert scalar(service, "SELECT count(*) FROM ingest_batches") == 0


@pytest.mark.parametrize("equipment_id", ["Line", "Infeed", "Sealer", "Outfeed", LINE_ID])
def test_simulator_logical_equipment_resolves_inside_configured_line(service, equipment_id):
    value = batch(event(equipment_id=equipment_id))
    assert submit(service, value).events[0].disposition == "APPLIED"
    stored = scalar(service, "SELECT body FROM event_inbox")
    assert stored["equipment_id"] == equipment_id


@pytest.mark.parametrize("equipment_id", ["unknown-asset", "foreign-outfeed"])
def test_unknown_or_other_line_equipment_is_quarantined_without_counting(service, equipment_id):
    from apps.api.models import Asset, Line

    with service.engine.begin() as connection:
        connection.execute(
            Line.__table__.insert().values(
                id="foreign-line",
                site_id="site-local",
                code="FOREIGN",
                name="Foreign line",
            )
        )
        connection.execute(
            Asset.__table__.insert().values(
                id="foreign-outfeed",
                line_id="foreign-line",
                code="Outfeed",
                name="Foreign outfeed",
            )
        )
    ack = submit(service, batch(event(equipment_id=equipment_id)))
    assert ack.events[0].reason_code == "EQUIPMENT_SOURCE_MISMATCH"
    assert ack.progress.verified_through_seq == 0
    assert service.get_totals("run-A")["good_quantity"] == 0
    assert scalar(service, "SELECT count(*) FROM production_events") == 0


def test_migration_preserves_reference_data_and_round_trips_ingestion_schema(engine):
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    from apps.api.database import migration_config
    from apps.api.ingestion import INGESTION_METADATA
    from apps.api.models import Base

    def baseline_snapshot():
        result = {}
        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            for name in sorted(Base.metadata.tables):
                result[name] = {
                    "rows": [
                        dict(row)
                        for row in connection.execute(
                            sa.text(f'SELECT * FROM "{name}" ORDER BY id')
                        ).mappings()
                    ],
                    "columns": inspector.get_columns(name),
                    "primary_key": inspector.get_pk_constraint(name),
                    "foreign_keys": inspector.get_foreign_keys(name),
                    "unique": inspector.get_unique_constraints(name),
                    "checks": inspector.get_check_constraints(name),
                    "indexes": inspector.get_indexes(name),
                }
        return json.dumps(result, sort_keys=True, default=str)

    with engine.connect() as connection:
        command.upgrade(migration_config(connection), "0001_reference_data")
    bootstrap_reference_data(engine)
    before = baseline_snapshot()
    with engine.connect() as connection:
        command.upgrade(migration_config(connection), "0002_ingestion")
    assert baseline_snapshot() == before
    with engine.connect() as connection:
        differences = compare_metadata(
            MigrationContext.configure(connection),
            [Base.metadata, INGESTION_METADATA],
        )
        assert differences == []
    with engine.connect() as connection:
        command.downgrade(migration_config(connection), "0001_reference_data")
    assert baseline_snapshot() == before
    assert set(sa.inspect(engine).get_table_names()) == set(Base.metadata.tables) | {
        "alembic_version"
    }


def test_foreign_execution_and_cross_gateway_event_identity_cannot_count(service):
    service.configure_source(
        source_id="other-source",
        stream_id="other-stream",
        gateway_id="other-gateway",
        line_id=LINE_ID,
        capture_generation=1,
        now=AT,
    )
    foreign_event = event(source_id="other-source", stream_id="other-stream")
    value = batch(
        foreign_event,
        source_id="other-source",
        stream_id="other-stream",
        gateway_id="other-gateway",
    )
    ack = service.ingest(
        value,
        authenticated_gateway_id="other-gateway",
        authenticated_source_id="other-source",
        authenticated_stream_id="other-stream",
        now=AT,
    )
    assert ack.events[0].reason_code == "EXECUTION_STREAM_MISMATCH"
    assert service.get_totals("run-A")["good_quantity"] == 0
    with pytest.raises(IngestConflict, match="EVENT_IDENTITY_CONFLICT"):
        submit(service, batch(event()))
    assert service.get_source_status("simulator-1", "stream-1")["conflicted"]
    assert scalar(service, "SELECT count(*) FROM production_events") == 0
    assert scalar(service, "SELECT count(*) FROM ingest_batches") == 1
