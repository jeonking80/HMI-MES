"""Authenticated browser contracts for generated IDs and original-actor recovery."""

import httpx
import pytest
from sqlalchemy import text

from tests.integration.test_work_orders_http import (
    ORDER,
    ORIGIN,
    RECIPE,
    account,
    protection,
)
from tests.integration.test_work_orders_http import client as client
from tests.integration.test_work_orders_http import state as state


def automatic(kind, **changes):
    value = dict(RECIPE if kind == "recipe" else ORDER)
    value.pop("id")
    if kind == "recipe":
        value.pop("version")
    return {**value, "request_id": kind + "-request", **changes}


def route(kind):
    return "/api/v1/" + ("recipe-versions" if kind == "recipe" else "work-orders")


def receipt_route(kind):
    return "/api/v1/registration-receipts/" + kind.replace("_", "-")


async def prepare_order(state, client, kind):
    if kind == "work_order":
        response = await client.post(
            route("recipe"), json=RECIPE, headers=protection(state.session)
        )
        assert response.status_code == 200, response.text


@pytest.mark.parametrize("kind", ["recipe", "work_order"])
@pytest.mark.parametrize("empty", [None, "", "absent"])
async def test_empty_identity_allocates_and_requires_request_id(state, client, kind, empty):
    await prepare_order(state, client, kind)
    body = automatic(kind)
    if empty != "absent":
        body["id"] = empty
        if kind == "recipe":
            body["version"] = empty
    without_request = {key: value for key, value in body.items() if key != "request_id"}
    invalid = await client.post(
        route(kind), json=without_request, headers=protection(state.session)
    )
    assert invalid.status_code == 422, invalid.text
    first = await client.post(route(kind), json=body, headers=protection(state.session))
    assert first.status_code == 200, first.text
    assert first.json()["id"] == ("recipe-v1" if kind == "recipe" else "WO-20261023-001")
    assert "request_id" not in first.json()
    repeat = await client.post(route(kind), json=body, headers=protection(state.session))
    assert repeat.status_code == 200 and repeat.json() == first.json()
    recovered = await client.get(receipt_route(kind), params={"request_id": body["request_id"]})
    assert recovered.status_code == 200 and recovered.json() == first.json()
    assert recovered.headers["cache-control"] == "no-store"
    changed = {**body, "item_id": "another-item"}
    response = await client.post(route(kind), json=changed, headers=protection(state.session))
    assert response.status_code == 409
    with state.engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM registration_receipts")).scalar_one() == 1
        assert db.execute(text("SELECT count(*) FROM production_executions")).scalar_one() == 0
        assert db.execute(text("SELECT count(*) FROM command_requests")).scalar_one() == 0


@pytest.mark.parametrize("kind", ["recipe", "work_order"])
async def test_original_actor_only_receipt_recovery_and_post_conflict(state, client, kind):
    await prepare_order(state, client, kind)
    body = automatic(kind)
    original = await client.post(route(kind), json=body, headers=protection(state.session))
    assert original.status_code == 200, original.text
    other = account(state.auth, "production_manager", login="another-manager")
    client.cookies.set("hmi_session", other.token)
    conflict = await client.post(route(kind), json=body, headers=protection(other))
    assert conflict.status_code == 409
    for request_id in (body["request_id"], "missing"):
        response = await client.get(receipt_route(kind), params={"request_id": request_id})
        assert response.status_code == 404
    # A new authenticated session for the original account can recover after a lost response.
    replacement_password = "replacement-" + state.session.csrf_token
    state.auth.change_password(
        state.session.token,
        replacement_password,
        csrf_token=state.session.csrf_token,
        origin=ORIGIN,
    )
    challenge = state.auth.begin_login()
    replacement = state.auth.login(
        "production_manager",
        replacement_password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=ORIGIN,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=state.app), base_url=ORIGIN
    ) as fresh:
        fresh.cookies.set("hmi_session", replacement.token)
        response = await fresh.get(receipt_route(kind), params={"request_id": body["request_id"]})
        assert response.status_code == 200 and response.json() == original.json()


@pytest.mark.parametrize("kind", ["recipe", "work_order"])
async def test_registration_and_recovery_auth_csrf_and_query_contract(state, client, kind):
    await prepare_order(state, client, kind)
    for headers in ({}, {"Origin": ORIGIN, "X-CSRF-Token": "invalid"}):
        response = await client.post(route(kind), json=automatic(kind), headers=headers)
        assert response.status_code == 403
    for params in (
        {},
        {"request_id": ""},
        {"request_id": "x", "extra": "x"},
        [("request_id", "x"), ("request_id", "x")],
    ):
        response = await client.get(receipt_route(kind), params=params)
        assert response.status_code == 422
    client.cookies.clear()
    response = await client.get(receipt_route(kind), params={"request_id": "missing"})
    assert response.status_code == 401
    response = await client.post(
        route(kind), json=automatic(kind), headers=protection(state.session)
    )
    assert response.status_code == 401
