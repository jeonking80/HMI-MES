"""Real PostgreSQL immutable item registration and authenticated browser boundaries."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from apps.api.auth import UserAuth
from apps.api.bootstrap import bootstrap_reference_data
from apps.api.items import ItemDefinition, get_item, register_item
from apps.api.user_http import UserHTTPConfig, create_user_app
from apps.api.work_orders import RegistrationConflict
from tests.integration.test_work_orders import AT
from tests.integration.test_work_orders_http import ORDER, ORIGIN, RECIPE, account, protection

ITEM = {"id": "item-A", "name": "Registered item", "unit": "EA", "enabled": True}


@pytest.fixture
def state(migrated):
    bootstrap_reference_data(migrated)
    auth = UserAuth(migrated, allowed_origins={ORIGIN}, clock=lambda: AT)
    session = account(auth, "production_manager")
    return SimpleNamespace(
        engine=migrated,
        auth=auth,
        session=session,
        app=create_user_app(auth, UserHTTPConfig(origin=ORIGIN)),
    )


@pytest.fixture
async def client(state):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=state.app), base_url=ORIGIN
    ) as client:
        client.cookies.set("hmi_session", state.session.token)
        yield client


def counts(state):
    with state.engine.connect() as db:
        return tuple(
            db.execute(text("SELECT count(*) FROM " + table)).scalar_one()
            for table in ("item_master", "item_audit", "recipe_versions", "work_orders")
        )


async def test_create_replay_conflict_and_original_audit(state, client):
    first = await client.post("/api/v1/items", json=ITEM, headers=protection(state.session))
    assert first.status_code == 200, first.text
    assert first.json() == {
        **ITEM,
        "origin": "USER",
        "created_by": "production_manager",
        "created_at": "2026-10-22T00:00:00Z",
    }
    second = await client.post("/api/v1/items", json=ITEM, headers=protection(state.session))
    assert second.status_code == 200 and second.json() == first.json()
    detail = await client.get("/api/v1/items/item-A")
    assert detail.json() == first.json() and detail.headers["cache-control"] == "no-store"
    for change in ({"name": "Changed"}, {"unit": "kg"}, {"enabled": False}):
        response = await client.post(
            "/api/v1/items", json={**ITEM, **change}, headers=protection(state.session)
        )
        assert response.status_code == 409
    assert counts(state) == (1, 1, 0, 0)
    with state.engine.connect() as db:
        audit = db.execute(text("SELECT actor,action,evidence FROM item_audit")).one()
        assert tuple(audit) == ("production_manager", "CREATE", ITEM)
        assert db.execute(text("SELECT count(*) FROM command_requests")).scalar_one() == 0
        assert db.execute(text("SELECT count(*) FROM production_executions")).scalar_one() == 0
    assert (await client.get("/api/v1/items/missing")).status_code == 404


@pytest.mark.parametrize("role", ["viewer", "operator", "equipment_admin", "production_manager"])
async def test_role_permissions_and_no_edit_routes(state, client, role):
    session = state.session if role == "production_manager" else account(state.auth, role)
    client.cookies.set("hmi_session", session.token)
    assert (await client.get("/api/v1/items")).status_code == 200
    response = await client.post("/api/v1/items", json=ITEM, headers=protection(session))
    assert response.status_code == (200 if role == "production_manager" else 403)
    for method in ("put", "patch", "delete"):
        assert (await client.request(method, "/api/v1/items/item-A", json=ITEM)).status_code == 405
    assert counts(state)[:2] == ((1, 1) if role == "production_manager" else (0, 0))


async def test_session_initial_password_csrf_and_origin_are_required(state, client):
    client.cookies.clear()
    assert (await client.get("/api/v1/items")).status_code == 401
    client.cookies.set("hmi_session", state.session.token)
    for headers in (
        {},
        {"Origin": ORIGIN},
        {"Origin": ORIGIN, "X-CSRF-Token": "wrong"},
        {"Origin": "https://other.test", "X-CSRF-Token": state.session.csrf_token},
    ):
        assert (await client.post("/api/v1/items", json=ITEM, headers=headers)).status_code == 403
    initial = account(state.auth, "production_manager", login="initial", changed=False)
    client.cookies.set("hmi_session", initial.token)
    assert (
        await client.post("/api/v1/items", json=ITEM, headers=protection(initial))
    ).status_code == 403
    assert counts(state) == (0, 0, 0, 0)


@pytest.mark.parametrize(
    "change",
    [
        {"name": " "},
        {"name": 12},
        {"name": "x" * 129},
        {"unit": ""},
        {"unit": "x" * 33},
        {"enabled": "true"},
        {"enabled": 1},
        {"id": ""},
        {"created_by": "forged"},
        {"origin": "MIGRATION"},
    ],
)
async def test_strict_item_body_never_writes(state, client, change):
    response = await client.post(
        "/api/v1/items", json={**ITEM, **change}, headers=protection(state.session)
    )
    assert response.status_code == 422
    assert counts(state) == (0, 0, 0, 0)


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=101",
        "limit=01",
        "limit=1&limit=2",
        "enabled=1",
        "enabled=True",
        "enabled=false&enabled=true",
        "after_id=",
        "unknown=x",
    ],
)
async def test_strict_query_rejection(client, query):
    assert (await client.get("/api/v1/items?" + query)).status_code == 422


async def test_duplicate_json_and_query_and_paging(state, client):
    headers = {**protection(state.session), "Content-Type": "application/json"}
    response = await client.post(
        "/api/v1/items",
        content='{"id":"a","id":"b","name":"A","unit":"EA"}',
        headers=headers,
    )
    assert response.status_code == 422
    assert (
        await client.post(
            "/api/v1/items?unexpected=x", json=ITEM, headers=protection(state.session)
        )
    ).status_code == 422
    for identity, enabled in (("C", True), ("A", True), ("B", False)):
        response = await client.post(
            "/api/v1/items",
            json={**ITEM, "id": identity, "enabled": enabled},
            headers=protection(state.session),
        )
        assert response.status_code == 200
    page = (await client.get("/api/v1/items?limit=2")).json()
    assert [item["id"] for item in page["items"]] == ["A", "B"]
    assert page["next_cursor"] == "B"
    tail = (await client.get("/api/v1/items?limit=2&after_id=B")).json()
    assert [item["id"] for item in tail["items"]] == ["C"]
    assert tail["next_cursor"] is None
    disabled = (await client.get("/api/v1/items?enabled=false")).json()
    assert [item["id"] for item in disabled["items"]] == ["B"]
    assert (await client.get("/api/v1/items/A?unknown=x")).status_code == 422


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("enabled,status", [(None, 404), (False, 409), (True, 200)])
async def test_registered_enabled_item_required_for_manual_and_automatic_writers(
    state, client, automatic, enabled, status
):
    if enabled is not None:
        response = await client.post(
            "/api/v1/items", json={**ITEM, "enabled": enabled}, headers=protection(state.session)
        )
        assert response.status_code == 200
        replay = await client.post(
            "/api/v1/items", json={**ITEM, "enabled": enabled}, headers=protection(state.session)
        )
        assert replay.status_code == 200 and replay.json() == response.json()
    recipe = dict(RECIPE)
    order = dict(ORDER)
    if automatic:
        recipe = {key: value for key, value in recipe.items() if key not in {"id", "version"}}
        recipe["request_id"] = "new-recipe"
        order.pop("id")
        order["request_id"] = "new-order"
    first = await client.post(
        "/api/v1/recipe-versions", json=recipe, headers=protection(state.session)
    )
    assert first.status_code == status, first.text
    second = await client.post("/api/v1/work-orders", json=order, headers=protection(state.session))
    assert second.status_code == status, second.text
    assert counts(state)[2:] == ((1, 1) if enabled else (0, 0))
    with state.engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM registration_receipts")).scalar_one() == (
            2 if automatic and enabled else 0
        )
        assert db.execute(text("SELECT count(*) FROM registration_id_counters")).scalar_one() == (
            2 if automatic and enabled else 0
        )


def test_caller_rollback_and_audit_failure_are_atomic(state):
    definition = ItemDefinition(**ITEM)
    with state.engine.begin() as db:
        register_item(db, definition, actor="production_manager", now=AT)
        assert counts(state) == (0, 0, 0, 0)
        db.rollback()
    assert counts(state) == (0, 0, 0, 0)

    def fail(_db, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().startswith("INSERT INTO item_audit"):
            raise RuntimeError("Injected item audit failure")

    event.listen(state.engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError), state.engine.begin() as db:
            register_item(db, definition, actor="production_manager", now=AT)
    finally:
        event.remove(state.engine, "before_cursor_execute", fail)
    assert counts(state) == (0, 0, 0, 0)


def test_concurrent_same_identity_creates_once_and_conflict_preserves_original(state):
    barrier = Barrier(2)

    def write(_index):
        barrier.wait(timeout=5)
        with state.engine.begin() as db:
            return register_item(db, ItemDefinition(**ITEM), actor="production_manager", now=AT)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, replay = list(pool.map(write, range(2)))
    assert first == replay and counts(state) == (1, 1, 0, 0)
    with state.engine.begin() as db:
        assert (
            register_item(
                db, ItemDefinition(**ITEM), actor="production_manager", now=AT + timedelta(days=1)
            )
            == first
        )
        assert get_item(db, ITEM["id"]) == first
    with pytest.raises(RegistrationConflict), state.engine.begin() as db:
        register_item(
            db, ItemDefinition(**{**ITEM, "unit": "kg"}), actor="production_manager", now=AT
        )


@pytest.mark.parametrize("table", ["recipe_versions", "work_orders"])
def test_database_rejects_unknown_item_without_domain_writer(state, table):
    with pytest.raises(IntegrityError) as caught, state.engine.begin() as db:
        if table == "recipe_versions":
            db.execute(
                text(
                    "INSERT INTO recipe_versions VALUES "
                    "('r','r','v1','unknown',150,6,'production_manager',:at)"
                ),
                {"at": AT},
            )
        else:
            db.execute(
                text(
                    "INSERT INTO work_orders(id,item_id,line_id,recipe_version_id,target_quantity,"
                    "planned_date,status,row_version,created_by,created_at) VALUES "
                    "('w','unknown',:line,'unknown-recipe',1,'2026-10-23','DRAFT',1,"
                    "'production_manager',:at)"
                ),
                {"at": AT, "line": ORDER["line_id"]},
            )
    assert caught.value.orig.sqlstate == "23503"
    allowed = {f"fk_{table}_item_master"}
    if table == "work_orders":
        allowed.add("work_orders_recipe_version_id_item_id_fkey")
    assert caught.value.orig.diag.constraint_name in allowed
    with state.engine.connect() as db:
        constraint = db.execute(
            text(
                "SELECT c.confrelid='item_master'::regclass,c.confdeltype,"
                "ARRAY(SELECT a.attname::text FROM pg_attribute a "
                "WHERE a.attrelid=c.conrelid AND a.attnum=ANY(c.conkey)),"
                "ARRAY(SELECT a.attname::text FROM pg_attribute a "
                "WHERE a.attrelid=c.confrelid AND a.attnum=ANY(c.confkey)) "
                "FROM pg_constraint c WHERE c.conrelid=to_regclass(:table) "
                "AND c.conname=:name AND c.contype='f'"
            ),
            {"table": table, "name": f"fk_{table}_item_master"},
        ).one()
        assert tuple(constraint) == (True, "r", ["item_id"], ["id"])
    assert counts(state) == (0, 0, 0, 0)


@pytest.mark.parametrize("table", ["item_master", "item_audit"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_database_owner_also_cannot_rewrite_item_evidence(state, table, operation):
    with state.engine.begin() as db:
        register_item(db, ItemDefinition(**ITEM), actor="production_manager", now=AT)
    assignment = "name=name" if table == "item_master" else "actor=actor"
    statements = {
        "UPDATE": f"UPDATE {table} SET {assignment}",
        "DELETE": f"DELETE FROM {table}",
        "TRUNCATE": f"TRUNCATE {table} CASCADE",
    }
    with pytest.raises(DBAPIError), state.engine.begin() as db:
        db.execute(text(statements[operation]))
    assert counts(state) == (1, 1, 0, 0)
