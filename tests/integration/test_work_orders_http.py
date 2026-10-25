"""Real UserAuth and PostgreSQL behind the mounted browser registration adapter."""

import secrets
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError

from apps.api.auth import UserAuth
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.user_http import UserHTTPConfig, create_user_app

ORIGIN = "https://mes.example.test"
RECIPE = {
    "id": "recipe-v1",
    "recipe_id": "recipe",
    "version": "v1",
    "item_id": "item-A",
    "parameters": {"temperature_c": 150.0, "speed_m_per_min": 6.0},
}
ORDER = {
    "id": "work-A",
    "item_id": "item-A",
    "line_id": LINE_ID,
    "recipe_version_id": "recipe-v1",
    "target_quantity": 100,
    "planned_date": "2026-10-23",
}


def account(auth, role, *, login=None, changed=True):
    login = login or role
    password = secrets.token_urlsafe(24)
    auth.bootstrap_user(login, password, roles={role})
    challenge = auth.begin_login()
    session = auth.login(
        login,
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=ORIGIN,
    )
    if changed:
        session = auth.change_password(
            session.token, secrets.token_urlsafe(24), csrf_token=session.csrf_token, origin=ORIGIN
        )
    return session


def protection(session):
    return {"Origin": ORIGIN, "X-CSRF-Token": session.csrf_token}


@pytest.fixture
def state(migrated):
    bootstrap_reference_data(migrated)
    auth = UserAuth(migrated, allowed_origins={ORIGIN})
    session = account(auth, "production_manager")
    # Factory owns mounting; registering routes twice would hide composition regressions.
    app = create_user_app(auth, UserHTTPConfig(origin=ORIGIN))
    return SimpleNamespace(auth=auth, session=session, app=app, engine=migrated)


@pytest.fixture
async def client(state):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=state.app), base_url=ORIGIN
    ) as client:
        client.cookies.set("hmi_session", state.session.token)
        yield client


def count(state, table):
    assert table in {
        "recipe_versions",
        "work_orders",
        "mes_management_audit",
        "production_executions",
        "command_requests",
    }
    with state.engine.connect() as db:
        return db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


async def create_pair(state, client):
    recipe = await client.post(
        "/api/v1/recipe-versions", json=RECIPE, headers=protection(state.session)
    )
    assert recipe.status_code == 200
    order = await client.post("/api/v1/work-orders", json=ORDER, headers=protection(state.session))
    assert order.status_code == 200
    return recipe, order


async def test_registration_reads_and_replay_remain_draft_with_authenticated_audit(state, client):
    recipe, order = await create_pair(state, client)
    assert recipe.json()["created_by"] == "production_manager"
    assert order.json()["status"] == "DRAFT"
    assert order.json()["row_version"] == "1"
    for path, definition, response in (
        ("recipe-versions", RECIPE, recipe),
        ("work-orders", ORDER, order),
    ):
        replay = await client.post(
            "/api/v1/" + path, json=definition, headers=protection(state.session)
        )
        assert replay.status_code == 200 and replay.json() == response.json()
        detail = await client.get("/api/v1/" + path + "/" + definition["id"])
        assert detail.status_code == 200 and detail.json() == response.json()
        page = await client.get("/api/v1/" + path, params={"limit": 1, "item_id": "item-A"})
        assert page.status_code == 200
        assert page.json() == {"items": [response.json()], "next_cursor": None}
    assert count(state, "mes_management_audit") == 2
    assert count(state, "production_executions") == count(state, "command_requests") == 0
    with state.engine.connect() as db:
        audit = (
            db.execute(text("SELECT actor,action,evidence FROM mes_management_audit ORDER BY id"))
            .mappings()
            .all()
        )
        assert [row["action"] for row in audit] == ["REGISTER_RECIPE", "REGISTER_WORK_ORDER"]
        assert all(row["actor"] == "production_manager" for row in audit)
        assert audit[0]["evidence"] == RECIPE and audit[1]["evidence"] == ORDER


@pytest.mark.parametrize("role", ["viewer", "operator", "equipment_admin", "production_manager"])
async def test_permission_matrix_reads_for_all_writes_only_production_manager(state, client, role):
    await create_pair(state, client)
    if role == "production_manager":
        session = state.session
    else:
        session = account(state.auth, role)
    client.cookies.set("hmi_session", session.token)
    for path, definition in (("recipe-versions", RECIPE), ("work-orders", ORDER)):
        assert (await client.get("/api/v1/" + path)).status_code == 200
        response = await client.post(
            "/api/v1/" + path, json=definition, headers=protection(session)
        )
        assert response.status_code == (200 if role == "production_manager" else 403)
    assert count(state, "mes_management_audit") == 2


async def test_auth_initial_password_and_mutation_protection_are_required(state, client):
    path = "/api/v1/recipe-versions"
    client.cookies.clear()
    assert (await client.get(path)).status_code == 401
    assert (
        await client.post(path, json=RECIPE, headers=protection(state.session))
    ).status_code == 401
    initial = account(state.auth, "production_manager", login="initial", changed=False)
    client.cookies.set("hmi_session", initial.token)
    assert (await client.get(path)).status_code == 403
    assert (await client.post(path, json=RECIPE, headers=protection(initial))).status_code == 403
    client.cookies.set("hmi_session", state.session.token)
    for headers in (
        {},
        {"Origin": ORIGIN},
        {"Origin": "https://other.test", "X-CSRF-Token": state.session.csrf_token},
        {"Origin": ORIGIN, "X-CSRF-Token": "invalid"},
        [("Origin", ORIGIN), ("Origin", ORIGIN), ("X-CSRF-Token", state.session.csrf_token)],
    ):
        assert (await client.post(path, json=RECIPE, headers=headers)).status_code == 403
    assert count(state, "recipe_versions") == count(state, "mes_management_audit") == 0


async def test_conflicts_references_and_immutable_surface(state, client):
    await create_pair(state, client)
    recipe = {**RECIPE, "parameters": {"temperature_c": 151.0, "speed_m_per_min": 6.0}}
    recipe_conflict = await client.post(
        "/api/v1/recipe-versions", json=recipe, headers=protection(state.session)
    )
    assert recipe_conflict.status_code == 409
    assert recipe_conflict.json()["reason_code"] == "ID_CONTENT_CONFLICT"
    order_conflict = await client.post(
        "/api/v1/work-orders",
        json={**ORDER, "target_quantity": 101},
        headers=protection(state.session),
    )
    assert order_conflict.status_code == 409
    assert order_conflict.json()["reason_code"] == "ID_CONTENT_CONFLICT"
    for changes, status in (
        ({"line_id": "absent"}, 404),
        ({"recipe_version_id": "absent"}, 404),
        ({"item_id": "wrong"}, 409),
    ):
        response = await client.post(
            "/api/v1/work-orders",
            json={**ORDER, "id": "other", **changes},
            headers=protection(state.session),
        )
        assert response.status_code == status
    for path in ("/api/v1/work-orders/work-A", "/api/v1/recipe-versions/recipe-v1"):
        for method in ("PUT", "PATCH", "DELETE"):
            assert (
                await client.request(method, path, headers=protection(state.session))
            ).status_code == 405
    # C1 exists, but malformed requests cannot mutate the registered draft.
    assert (
        await client.post(
            "/api/v1/work-orders/work-A/cancel", headers=protection(state.session), json={}
        )
    ).status_code == 422
    assert (await client.get("/api/v1/work-orders/absent")).status_code == 404
    assert count(state, "work_orders") == 1 and count(state, "mes_management_audit") == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"target_quantity": True},
        {"target_quantity": "1"},
        {"target_quantity": 0},
        {"target_quantity": 2**32},
        {"planned_date": "2026-02-30"},
        {"planned_date": 0},
        {"created_by": "forged-secret"},
        {"status": "RELEASED"},
        {"row_version": "2"},
    ],
)
async def test_strict_invalid_order_payloads_do_not_echo_or_write(state, client, changes):
    response = await client.post(
        "/api/v1/work-orders", json={**ORDER, **changes}, headers=protection(state.session)
    )
    assert response.status_code == 422
    assert "forged-secret" not in response.text
    assert count(state, "work_orders") == count(state, "mes_management_audit") == 0


async def test_body_type_bound_and_query_bounds(state, client):
    headers = {**protection(state.session), "Content-Type": "text/plain"}
    assert (
        await client.post("/api/v1/recipe-versions", content="{}", headers=headers)
    ).status_code == 415
    headers["Content-Type"] = "application/json"
    assert (
        await client.post("/api/v1/recipe-versions", content=b"x" * 16385, headers=headers)
    ).status_code == 413
    for query in (
        "limit=0",
        "limit=101",
        "limit=bad",
        "limit=1&limit=2",
        "unknown=x",
        "after_id=%20",
    ):
        assert (await client.get("/api/v1/work-orders?" + query)).status_code == 422
    assert (await client.get("/api/v1/recipe-versions?line_id=x")).status_code == 422


async def test_http_audit_failure_returns_sanitized_503_and_rolls_back(state, client):
    def fail_audit(_db, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().startswith("INSERT INTO mes_management_audit"):
            raise SQLAlchemyError("sensitive-injected-driver-detail")

    event.listen(state.engine, "before_cursor_execute", fail_audit)
    try:
        response = await client.post(
            "/api/v1/recipe-versions", json=RECIPE, headers=protection(state.session)
        )
    finally:
        event.remove(state.engine, "before_cursor_execute", fail_audit)
    assert response.status_code == 503
    assert "sensitive-injected-driver-detail" not in response.text
    assert count(state, "recipe_versions") == count(state, "mes_management_audit") == 0
