"""Actual PostgreSQL immutable execution plan and same-transaction registration checks."""

from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.database import migration_config
from apps.api.ingestion import IngestConflict, IngestionService
from contracts.models import Parameters

AT = datetime(2026, 10, 21, tzinfo=UTC)


def configured(engine):
    bootstrap_reference_data(engine)
    service = IngestionService(engine)
    service.configure_source(
        source_id="source",
        stream_id="stream",
        gateway_id="gateway",
        line_id=LINE_ID,
        capture_generation=1,
        now=AT,
    )
    return service


@pytest.fixture
def service(migrated):
    return configured(migrated)


def register(service, **changes):
    values = dict(
        execution_id="execution",
        source_id="source",
        stream_id="stream",
        work_order_id="work",
        recipe_version="recipe-v1",
        counter_epoch="epoch",
        parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
        now=AT,
    )
    values.update(changes)
    service.register_execution(**values)


def rows(engine, table):
    assert table in {"production_executions", "execution_totals", "execution_plans"}
    with engine.connect() as db:
        return db.execute(text(f"SELECT * FROM {table} ORDER BY execution_id")).all()


def test_registration_replay_preserves_plan_original_fields_and_totals(service):
    register(service, target_quantity=100)
    with service.engine.begin() as db:
        db.execute(text("UPDATE execution_totals SET good_quantity=7,reject_quantity=3"))
    before = {
        table: rows(service.engine, table)
        for table in ("production_executions", "execution_totals", "execution_plans")
    }
    register(service, target_quantity=100)
    # The optional legacy path may verify old fields but cannot modify an existing plan.
    register(service)
    for change in ({"target_quantity": 101}, {"target_quantity": 100, "recipe_version": "other"}):
        with pytest.raises(IngestConflict):
            register(service, **change)
    for table, original in before.items():
        assert rows(service.engine, table) == original


def test_plan_write_failure_rolls_back_new_execution_and_totals(service):
    class InjectedPlanWriteFailure(RuntimeError):
        pass

    def fail_plan(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().startswith("INSERT INTO execution_plans"):
            raise InjectedPlanWriteFailure

    event.listen(service.engine, "before_cursor_execute", fail_plan)
    try:
        with pytest.raises(InjectedPlanWriteFailure):
            register(service, target_quantity=100)
    finally:
        event.remove(service.engine, "before_cursor_execute", fail_plan)
    for table in ("production_executions", "execution_totals", "execution_plans"):
        assert rows(service.engine, table) == []


def test_prior_unknown_target_refuses_automatic_enrichment(service):
    register(service)
    before = rows(service.engine, "production_executions")
    with pytest.raises(IngestConflict):
        register(service, target_quantity=100)
    assert rows(service.engine, "execution_plans") == []
    assert rows(service.engine, "production_executions") == before
    register(service)


@pytest.mark.parametrize("quantity", [0, -1, 2**32, True, False, 1.0, "1"])
def test_target_quantity_rejects_non_strict_or_out_of_range_before_writes(service, quantity):
    with pytest.raises(ValueError):
        register(service, target_quantity=quantity)
    for table in ("production_executions", "execution_totals", "execution_plans"):
        assert rows(service.engine, table) == []


@pytest.mark.parametrize("quantity", [1, 2**32 - 1])
def test_target_quantity_accepts_uint32_boundaries(service, quantity):
    register(service, target_quantity=quantity)
    assert rows(service.engine, "execution_plans") == [("execution", quantity)]


def test_migration_preserves_prior_rows_and_downgrades_only_plan(engine):
    with engine.connect() as db:
        command.upgrade(migration_config(db), "0006_settlement")
    service = configured(engine)
    register(service)
    with engine.begin() as db:
        db.execute(text("UPDATE execution_totals SET good_quantity=11"))
    executions, totals = rows(engine, "production_executions"), rows(engine, "execution_totals")
    with engine.connect() as db:
        command.upgrade(migration_config(db), "0007_execution_plan")
    assert rows(engine, "execution_plans") == []
    assert rows(engine, "production_executions") == executions
    assert rows(engine, "execution_totals") == totals
    with pytest.raises(IngestConflict):
        register(service, target_quantity=100)
    register(service, execution_id="new-execution", target_quantity=100)
    executions, totals = rows(engine, "production_executions"), rows(engine, "execution_totals")
    with engine.connect() as db:
        command.downgrade(migration_config(db), "0006_settlement")
        assert db.execute(text("SELECT to_regclass('execution_plans')")).scalar_one() is None
    assert rows(engine, "production_executions") == executions
    assert rows(engine, "execution_totals") == totals
    with engine.connect() as db:
        command.upgrade(migration_config(db), "0007_execution_plan")
    assert rows(engine, "execution_plans") == []


@pytest.mark.parametrize(
    "quantity,execution_id", [(0, "execution"), (2**32, "execution"), (1, "absent")]
)
def test_database_checks_quantity_and_original_execution_fk(service, quantity, execution_id):
    register(service)
    with pytest.raises(IntegrityError), service.engine.begin() as db:
        db.execute(
            text("INSERT INTO execution_plans VALUES (:execution,:quantity)"),
            {
                "execution": execution_id,
                "quantity": quantity,
            },
        )
    assert rows(service.engine, "execution_plans") == []
