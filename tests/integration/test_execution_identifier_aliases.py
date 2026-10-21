"""Fixed execution query route retains actual externally registered Identifier values."""

import pytest
from sqlalchemy import text
from test_execution_read import AT, ORIGIN, PARAMETERS, SCOPE, account
from test_execution_read import client as client
from test_execution_read import state as state


@pytest.mark.parametrize("identity", ["a/b", ".", "..", "x/../y", "a%2Fb"])
async def test_registered_external_execution_exact_identity_without_redirect(
    state, client, identity
):
    state.service.register_execution(
        execution_id=identity,
        source_id="source",
        stream_id="stream",
        work_order_id="work-A",
        recipe_version="recipe-v1",
        parameters=PARAMETERS,
        counter_epoch="epoch",
        now=AT,
    )
    with state.engine.connect() as db:
        before = db.execute(text("SELECT max(last_seen_at) FROM auth_sessions")).scalar_one()
    response = await client.get(
        "/api/v1/execution-record", params=SCOPE | {"execution_id": identity}
    )
    assert response.status_code == 200 and not response.history
    assert "location" not in response.headers
    assert response.json()["execution"]["execution_id"] == identity
    assert response.json()["binding"] is None and response.json()["load"] is None
    with state.engine.connect() as db:
        assert (
            db.execute(text("SELECT max(last_seen_at) FROM auth_sessions")).scalar_one() == before
        )
    missing_scope = SCOPE | {"execution_id": identity, "source_id": "foreign"}
    assert (await client.get("/api/v1/execution-record", params=missing_scope)).status_code == 404


@pytest.mark.parametrize("change", ["missing", "duplicate", "extra", "bad"])
async def test_execution_alias_exact_query_allowlist(state, client, change):
    query = SCOPE | {"execution_id": state.execution}
    if change == "missing":
        query.pop("execution_id")
    elif change == "duplicate":
        query = list(query.items()) + [("execution_id", state.execution)]
    elif change == "extra":
        query["extra"] = "x"
    else:
        query["execution_id"] = "bad id"
    assert (await client.get("/api/v1/execution-record", params=query)).status_code == 422


@pytest.mark.parametrize("role", ["viewer", "operator", "production_manager", "equipment_admin"])
async def test_execution_alias_same_roles_and_revocation(state, client, role):
    session = account(state.auth, role, {role})
    client.cookies.set("hmi_session", session.token)
    query = SCOPE | {"execution_id": state.execution}
    assert (await client.get("/api/v1/execution-record", params=query)).status_code == 200
    state.auth.revoke(session.token, csrf_token=session.csrf_token, origin=ORIGIN)
    assert (await client.get("/api/v1/execution-record", params=query)).status_code == 401
