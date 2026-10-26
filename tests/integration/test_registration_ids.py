"""Automatic registration identities and receipts on real disposable PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from alembic import command
from sqlalchemy import event

from apps.api.database import migration_config
from apps.api.registration_ids import (
    recover_registration_receipt,
    register_recipe_request,
    register_work_order_request,
)
from apps.api.registration_requests import RecipeRegistrationRequest, WorkOrderRegistrationRequest
from apps.api.work_orders import (
    RegistrationConflict,
    WorkOrdersUnavailable,
    register_recipe_version,
    register_work_order,
)
from tests.integration.test_work_orders import AT, order, recipe, scalar, seed_recipe
from tests.integration.test_work_orders import catalog as catalog


def automatic_recipe(request_id="recipe-request", **changes):
    value = recipe().model_dump(mode="json", exclude={"id", "version"})
    return RecipeRegistrationRequest.model_validate({**value, "request_id": request_id, **changes})


def automatic_order(request_id="order-request", **changes):
    value = order().model_dump(mode="json", exclude={"id"})
    return WorkOrderRegistrationRequest.model_validate(
        {**value, "request_id": request_id, **changes}
    )


def test_recipe_next_numeric_version_and_receipt_replay(catalog):
    with catalog.begin() as db:
        for number in (2, 9):
            register_recipe_request(
                db,
                RecipeRegistrationRequest.model_validate(
                    recipe(id=f"recipe-v{number}", version=f"v{number}").model_dump()
                ),
                actor="manager",
                now=AT,
            )
        first = register_recipe_request(db, automatic_recipe(), actor="manager", now=AT)
    assert (first.id, first.version) == ("recipe-v10", "v10")
    with catalog.begin() as db:
        replay = register_recipe_request(
            db, automatic_recipe(), actor="manager", now=AT + timedelta(days=2)
        )
        assert replay == first
        assert recover_registration_receipt(
            db, kind="recipe", request_id="recipe-request", actor="manager"
        ) == first.model_dump(mode="json")
    assert scalar(catalog, "SELECT count(*) FROM registration_receipts") == 1
    assert scalar(catalog, "SELECT count(*) FROM mes_management_audit") == 3
    for changes, actor in (({"item_id": "different"}, "manager"), ({}, "other-manager")):
        with pytest.raises(RegistrationConflict), catalog.begin() as db:
            register_recipe_request(db, automatic_recipe(**changes), actor=actor, now=AT)


def test_order_namespace_uses_planned_date_and_global_daily_sequence(catalog):
    seed_recipe(catalog)
    with catalog.begin() as db:
        first = register_work_order_request(db, automatic_order(), actor="manager", now=AT)
        second = register_work_order_request(
            db, automatic_order("second"), actor="manager", now=AT + timedelta(days=10)
        )
        tomorrow = register_work_order_request(
            db, automatic_order("tomorrow", planned_date="2026-10-24"), actor="manager", now=AT
        )
    assert [first.id, second.id, tomorrow.id] == [
        "WO-20261023-001",
        "WO-20261023-002",
        "WO-20261024-001",
    ]
    with catalog.begin() as db:
        assert (
            register_work_order_request(
                db, automatic_order(), actor="manager", now=AT + timedelta(days=2)
            )
            == first
        )
    assert scalar(catalog, "SELECT count(*) FROM work_orders") == 3
    assert scalar(catalog, "SELECT count(*) FROM registration_receipts") == 3


@pytest.mark.parametrize("kind", ["recipe", "work_order"])
@pytest.mark.parametrize("failure", ["rollback", "audit", "receipt"])
def test_counter_definition_audit_receipt_rollback_together(catalog, kind, failure):
    if kind == "work_order":
        seed_recipe(catalog)
    request = automatic_recipe() if kind == "recipe" else automatic_order()
    register = register_recipe_request if kind == "recipe" else register_work_order_request
    baseline = int(kind == "work_order")

    def inject(_db, _cursor, statement, _parameters, _context, _many):
        table = "mes_management_audit" if failure == "audit" else "registration_receipts"
        if statement.lstrip().startswith("INSERT INTO " + table):
            raise RuntimeError("Injected registration transaction failure")

    if failure != "rollback":
        event.listen(catalog, "before_cursor_execute", inject)
    try:
        if failure == "rollback":
            with catalog.begin() as db:
                register(db, request, actor="manager", now=AT)
                db.rollback()
        else:
            with pytest.raises(RuntimeError), catalog.begin() as db:
                register(db, request, actor="manager", now=AT)
    finally:
        if failure != "rollback":
            event.remove(catalog, "before_cursor_execute", inject)
    for table in ("registration_id_counters", "registration_receipts", "work_orders"):
        assert scalar(catalog, "SELECT count(*) FROM " + table) == 0
    assert scalar(catalog, "SELECT count(*) FROM recipe_versions") == baseline
    assert scalar(catalog, "SELECT count(*) FROM mes_management_audit") == baseline
    with catalog.begin() as db:
        result = register(db, request, actor="manager", now=AT)
    assert result.id == ("recipe-v1" if kind == "recipe" else "WO-20261023-001")


@pytest.mark.parametrize("kind", ["recipe", "work_order"])
def test_concurrent_automatic_requests_allocate_distinct_ids(catalog, kind):
    if kind == "work_order":
        seed_recipe(catalog)
    barrier = Barrier(2)

    def write(index):
        request = automatic_recipe(str(index)) if kind == "recipe" else automatic_order(str(index))
        register = register_recipe_request if kind == "recipe" else register_work_order_request
        barrier.wait(timeout=10)
        with catalog.begin() as db:
            return register(db, request, actor="manager", now=AT).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, range(2)))
    expected = (
        {"recipe-v1", "recipe-v2"} if kind == "recipe" else {"WO-20261023-001", "WO-20261023-002"}
    )
    assert set(results) == expected
    assert scalar(catalog, "SELECT count(*) FROM registration_receipts") == 2


@pytest.mark.parametrize("kind", ["recipe", "work_order"])
def test_cooperating_manual_and_automatic_writers_never_overwrite(catalog, kind):
    if kind == "work_order":
        seed_recipe(catalog)
    barrier = Barrier(2)

    def write(manual):
        register = register_recipe_request if kind == "recipe" else register_work_order_request
        request_model = (
            RecipeRegistrationRequest if kind == "recipe" else WorkOrderRegistrationRequest
        )
        definition = recipe() if kind == "recipe" else order(id="WO-20261023-001")
        request = (
            request_model.model_validate(definition.model_dump())
            if manual
            else (automatic_recipe() if kind == "recipe" else automatic_order())
        )
        barrier.wait(timeout=10)
        try:
            with catalog.begin() as db:
                if manual:
                    writer = register_recipe_version if kind == "recipe" else register_work_order
                    return writer(db, definition, actor="manager", now=AT).id
                return register(db, request, actor="manager", now=AT).id
        except RegistrationConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        manual_result, automatic_result = list(pool.map(write, [True, False]))
    first = "recipe-v1" if kind == "recipe" else "WO-20261023-001"
    second = "recipe-v2" if kind == "recipe" else "WO-20261023-002"
    assert automatic_result in {first, second}
    assert manual_result in {first, "conflict"}
    assert scalar(catalog, "SELECT count(*) FROM registration_receipts") == 1


@pytest.mark.parametrize("kind", ["recipe", "work_order"])
def test_concurrent_same_request_returns_one_original_response(catalog, kind):
    if kind == "work_order":
        seed_recipe(catalog)
    barrier = Barrier(2)
    request = automatic_recipe() if kind == "recipe" else automatic_order()
    register = register_recipe_request if kind == "recipe" else register_work_order_request

    def write(_index):
        barrier.wait(timeout=10)
        with catalog.begin() as db:
            return register(db, request, actor="manager", now=AT)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(write, range(2)))
    assert first == second
    assert scalar(catalog, "SELECT count(*) FROM registration_receipts") == 1
    assert scalar(catalog, "SELECT count(*) FROM mes_management_audit") == (
        1 if kind == "recipe" else 2
    )


@pytest.mark.parametrize("catalog", ["legacy20"], indirect=True)
def test_head18_manual_registration_stays_compatible_and_auto_requires_head19(catalog):
    with catalog.connect() as db:
        command.downgrade(migration_config(db), "0018_clock_evidence")
    with catalog.begin() as db:
        manual_recipe = register_recipe_request(
            db,
            RecipeRegistrationRequest.model_validate(recipe().model_dump()),
            actor="manager",
            now=AT,
        )
        manual_order = register_work_order_request(
            db,
            WorkOrderRegistrationRequest.model_validate(order().model_dump()),
            actor="manager",
            now=AT,
        )
    assert manual_recipe.id == "recipe-v1" and manual_order.id == "work-A"
    with pytest.raises(WorkOrdersUnavailable), catalog.begin() as db:
        register_recipe_request(db, automatic_recipe(), actor="manager", now=AT)
    assert scalar(catalog, "SELECT count(*) FROM mes_management_audit") == 2
