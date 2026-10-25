"""Real PostgreSQL immutable MES definitions, transaction ownership and contention."""

import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest
from alembic import command
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from apps.api.auth import UserAuth
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.database import migration_config
from apps.api.ingestion import IngestionService
from apps.api.work_orders import (
    RecipeDefinition,
    RegistrationConflict,
    RegistrationNotFound,
    WorkOrderDefinition,
    get_recipe_version,
    get_work_order,
    list_registrations,
    register_recipe_version,
    register_work_order,
)
from contracts.models import Parameters
from tests.integration.item_seed import seed_item

AT = datetime(2026, 10, 22, tzinfo=UTC)
ORIGIN = "https://mes.example.test"


def recipe(**changes):
    return RecipeDefinition.model_validate(
        {
            "id": "recipe-v1",
            "recipe_id": "recipe",
            "version": "v1",
            "item_id": "item-A",
            "parameters": {"temperature_c": 150.0, "speed_m_per_min": 6.0},
            **changes,
        }
    )


def order(**changes):
    return WorkOrderDefinition.model_validate(
        {
            "id": "work-A",
            "item_id": "item-A",
            "line_id": LINE_ID,
            "recipe_version_id": "recipe-v1",
            "target_quantity": 100,
            "planned_date": "2026-10-23",
            **changes,
        }
    )


@pytest.fixture
def catalog(migrated, request):
    if getattr(request, "param", None) == "legacy20":
        with migrated.connect() as db:
            command.downgrade(migration_config(db), "0020_line_queue")
    bootstrap_reference_data(migrated)
    UserAuth(migrated, allowed_origins={ORIGIN}).bootstrap_user(
        "manager", secrets.token_urlsafe(32), roles={"production_manager"}
    )
    seed_item(migrated, "item-A", actor="manager", now=AT)
    seed_item(migrated, "wrong-item", actor="manager")
    return migrated


def scalar(engine, sql):
    with engine.connect() as db:
        return db.execute(text(sql)).scalar_one()


def seed_recipe(engine):
    with engine.begin() as db:
        return register_recipe_version(db, recipe(), actor="manager", now=AT)


def test_same_definition_replay_is_immutable_and_does_not_duplicate_audit(catalog):
    with catalog.begin() as db:
        first_recipe = register_recipe_version(db, recipe(), actor="manager", now=AT)
        first_order = register_work_order(db, order(), actor="manager", now=AT)
    assert first_order.status == "DRAFT" and first_order.row_version == 1
    assert first_order.model_dump(mode="json")["row_version"] == "1"
    with catalog.begin() as db:
        assert register_recipe_version(db, recipe(), actor="manager", now=AT) == first_recipe
        assert register_work_order(db, order(), actor="manager", now=AT) == first_order
        assert get_recipe_version(db, "recipe-v1") == first_recipe
        assert get_work_order(db, "work-A") == first_order
    assert scalar(catalog, "SELECT count(*) FROM mes_management_audit") == 2
    assert scalar(catalog, "SELECT count(*) FROM production_executions") == 0
    assert scalar(catalog, "SELECT count(*) FROM command_requests") == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"item_id": "other"},
        {"parameters": {"temperature_c": 151.0, "speed_m_per_min": 6.0}},
        {"id": "other-id"},
        {"version": "other-version"},
    ],
)
def test_recipe_id_or_family_version_conflict_preserves_original(catalog, changes):
    original = seed_recipe(catalog)
    with pytest.raises(RegistrationConflict), catalog.begin() as db:
        register_recipe_version(db, recipe(**changes), actor="manager", now=AT)
    with catalog.begin() as db:
        assert get_recipe_version(db, "recipe-v1") == original
    assert scalar(catalog, "SELECT count(*) FROM recipe_versions") == 1
    assert scalar(catalog, "SELECT count(*) FROM mes_management_audit") == 1


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"target_quantity": 101}, RegistrationConflict),
        ({"planned_date": "2026-10-24"}, RegistrationConflict),
        ({"id": "different", "line_id": "missing"}, RegistrationNotFound),
        ({"id": "different", "recipe_version_id": "missing"}, RegistrationNotFound),
        ({"id": "different", "item_id": "wrong-item"}, RegistrationConflict),
    ],
)
def test_order_conflicts_and_references_never_replace_or_write_partial_audit(
    catalog, changes, error
):
    seed_recipe(catalog)
    with catalog.begin() as db:
        original = register_work_order(db, order(), actor="manager", now=AT)
    with pytest.raises(error), catalog.begin() as db:
        register_work_order(db, order(**changes), actor="manager", now=AT)
    with catalog.begin() as db:
        assert get_work_order(db, "work-A") == original
    assert scalar(catalog, "SELECT count(*) FROM work_orders") == 1
    assert scalar(catalog, "SELECT count(*) FROM mes_management_audit") == 2


def test_caller_owns_commit_and_full_rollback(catalog):
    with catalog.begin() as db:
        register_recipe_version(db, recipe(), actor="manager", now=AT)
        register_work_order(db, order(), actor="manager", now=AT)
        assert scalar(catalog, "SELECT count(*) FROM recipe_versions") == 0
        assert scalar(catalog, "SELECT count(*) FROM work_orders") == 0
        assert scalar(catalog, "SELECT count(*) FROM mes_management_audit") == 0
        db.rollback()
    assert scalar(catalog, "SELECT count(*) FROM mes_management_audit") == 0
    assert scalar(catalog, "SELECT count(*) FROM recipe_versions") == 0


@pytest.mark.parametrize("kind", ["recipe", "work_order"])
def test_audit_failure_rolls_back_its_definition(catalog, kind):
    if kind == "work_order":
        seed_recipe(catalog)

    def fail_audit(_db, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().startswith("INSERT INTO mes_management_audit"):
            raise RuntimeError("Injected audit write failure")

    event.listen(catalog, "before_cursor_execute", fail_audit)
    try:
        with pytest.raises(RuntimeError), catalog.begin() as db:
            if kind == "recipe":
                register_recipe_version(db, recipe(), actor="manager", now=AT)
            else:
                register_work_order(db, order(), actor="manager", now=AT)
    finally:
        event.remove(catalog, "before_cursor_execute", fail_audit)
    assert scalar(catalog, "SELECT count(*) FROM work_orders") == 0
    assert scalar(catalog, "SELECT count(*) FROM recipe_versions") == (kind == "work_order")
    assert scalar(catalog, "SELECT count(*) FROM mes_management_audit") == (kind == "work_order")


@pytest.mark.parametrize("different_ids", [False, True])
def test_concurrent_recipe_pair_registration_serializes_and_audits_once(catalog, different_ids):
    barrier = Barrier(2)

    def write(index):
        barrier.wait(timeout=5)
        try:
            with catalog.begin() as db:
                definition = recipe(id=f"recipe-{index}") if different_ids else recipe()
                return register_recipe_version(db, definition, actor="manager", now=AT).id
        except RegistrationConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, range(2)))
    assert results.count("conflict") == (1 if different_ids else 0)
    assert scalar(catalog, "SELECT count(*) FROM recipe_versions") == 1
    assert scalar(catalog, "SELECT count(*) FROM mes_management_audit") == 1


def test_global_work_order_identity_serializes_conflicting_lines(catalog):
    seed_recipe(catalog)
    with catalog.begin() as db:
        db.execute(text("INSERT INTO lines VALUES ('line-2','site-local','LINE-2','Second line')"))
    barrier = Barrier(2)

    def write(line):
        barrier.wait(timeout=5)
        try:
            with catalog.begin() as db:
                return register_work_order(db, order(line_id=line), actor="manager", now=AT).line_id
        except RegistrationConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, (LINE_ID, "line-2")))
    assert results.count("conflict") == 1
    assert scalar(catalog, "SELECT count(*) FROM work_orders") == 1
    assert scalar(catalog, "SELECT count(*) FROM mes_management_audit") == 2


def test_bounded_keyset_pages_and_filters(catalog):
    with catalog.begin() as db:
        for suffix in ("C", "A", "B"):
            register_recipe_version(
                db, recipe(id=f"r-{suffix}", version=suffix), actor="manager", now=AT
            )
            register_work_order(
                db,
                order(id=f"w-{suffix}", recipe_version_id=f"r-{suffix}"),
                actor="manager",
                now=AT,
            )
        page = list_registrations(db, kind="work_order", limit=2, line_id=LINE_ID)
        assert [item["id"] for item in page["items"]] == ["w-A", "w-B"]
        assert page["next_cursor"] == "w-B"
        tail = list_registrations(db, kind="work_order", after_id="w-B", limit=2)
        assert [item["id"] for item in tail["items"]] == ["w-C"]
        assert tail["next_cursor"] is None
        assert list_registrations(db, kind="recipe", item_id="absent")["items"] == []
        with pytest.raises(ValueError):
            list_registrations(db, kind="recipe", limit=101)


@pytest.mark.parametrize(
    "changes",
    [
        {"target_quantity": True},
        {"target_quantity": "1"},
        {"target_quantity": 1.0},
        {"target_quantity": 0},
        {"target_quantity": 2**32},
        {"planned_date": 0},
        {"planned_date": "2026-02-30"},
        {"planned_date": "2026-10-23T00:00:00Z"},
    ],
)
def test_strict_work_order_input(changes):
    with pytest.raises(ValueError):
        order(**changes)


@pytest.mark.parametrize(
    "temperature,speed", [(float("nan"), 1.0), (float("inf"), 1.0), (-1.0, 1.0), (1.0, 0.0)]
)
def test_recipe_parameters_use_existing_contract(temperature, speed):
    with pytest.raises(ValueError):
        recipe(parameters={"temperature_c": temperature, "speed_m_per_min": speed})


@pytest.mark.parametrize("expression", ["'NaN'::float8", "'Infinity'::float8", "-1"])
def test_database_rejects_nonfinite_or_negative_temperature(catalog, expression):
    seed_recipe(catalog)
    with pytest.raises(IntegrityError), catalog.begin() as db:
        db.execute(text(f"UPDATE recipe_versions SET temperature_c={expression}"))


@pytest.mark.parametrize(
    "changes",
    [
        {"target_quantity": 0},
        {"target_quantity": 2**32},
        {"item_id": "wrong"},
        {"line_id": "absent"},
        {"recipe_version_id": "absent"},
        {"status": "RUNNING"},
        {"row_version": 0},
    ],
)
def test_database_order_checks_and_foreign_keys(catalog, changes):
    seed_recipe(catalog)
    values = {
        **order().model_dump(),
        "created_by": "manager",
        "created_at": AT,
        "status": "DRAFT",
        "row_version": 1,
        **changes,
    }
    with pytest.raises(IntegrityError), catalog.begin() as db:
        db.execute(
            text(
                "INSERT INTO work_orders(id,item_id,line_id,recipe_version_id,target_quantity,"
                "planned_date,status,row_version,created_by,created_at) VALUES "
                "(:id,:item_id,:line_id,:recipe_version_id,:target_quantity,:planned_date,"
                ":status,:row_version,:created_by,:created_at)"
            ),
            values,
        )
    assert scalar(catalog, "SELECT count(*) FROM work_orders") == 0


def test_database_recipe_family_version_unique_constraint(catalog):
    seed_recipe(catalog)
    with pytest.raises(IntegrityError), catalog.begin() as db:
        db.execute(
            text(
                "INSERT INTO recipe_versions SELECT 'another-id',recipe_id,version,item_id,"
                "temperature_c,speed_m_per_min,created_by,created_at FROM recipe_versions"
            )
        )
    assert scalar(catalog, "SELECT count(*) FROM recipe_versions") == 1


@pytest.mark.parametrize("catalog", ["legacy20"], indirect=True)
def test_historical_0008_still_rejects_released_state(catalog):
    seed_recipe(catalog)
    with catalog.connect() as db:
        command.downgrade(migration_config(db), "0008_work_orders")
    with pytest.raises(IntegrityError), catalog.begin() as db:
        db.execute(
            text(
                "INSERT INTO work_orders(id,item_id,line_id,recipe_version_id,target_quantity,"
                "planned_date,status,created_by,created_at) VALUES "
                "('historical','item-A',:line,'recipe-v1',1,'2026-10-23','RELEASED','manager',:at)"
            ),
            {"line": LINE_ID, "at": AT},
        )
    with catalog.connect() as db:
        command.upgrade(migration_config(db), "0009_work_order_binding")


@pytest.mark.parametrize("catalog", ["legacy20"], indirect=True)
def test_migration_roundtrip_preserves_legacy_execution_and_plan(catalog):
    service = IngestionService(catalog)
    service.configure_source(
        source_id="source",
        stream_id="stream",
        gateway_id="gateway",
        line_id=LINE_ID,
        capture_generation=1,
        now=AT,
    )
    service.register_execution(
        execution_id="legacy",
        source_id="source",
        stream_id="stream",
        work_order_id="legacy-work",
        recipe_version="legacy-recipe",
        parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
        counter_epoch="epoch",
        target_quantity=4,
        now=AT,
    )
    seed_recipe(catalog)
    with catalog.connect() as db:
        before = db.execute(text("SELECT * FROM production_executions")).all()
        plan = db.execute(text("SELECT * FROM execution_plans")).all()
        db.rollback()
        command.downgrade(migration_config(db), "0007_execution_plan")
        assert db.execute(text("SELECT to_regclass('work_orders')")).scalar_one() is None
        assert db.execute(text("SELECT * FROM production_executions")).all() == before
        assert db.execute(text("SELECT * FROM execution_plans")).all() == plan
        db.rollback()
        command.upgrade(migration_config(db), "0008_work_orders")
        assert db.execute(text("SELECT count(*) FROM recipe_versions")).scalar_one() == 0
        assert db.execute(text("SELECT count(*) FROM work_orders")).scalar_one() == 0
        assert db.execute(text("SELECT * FROM production_executions")).all() == before
        assert db.execute(text("SELECT * FROM execution_plans")).all() == plan
