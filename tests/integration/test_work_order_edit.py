"""Unbound work orders are edited in place with audited before/after values."""
# ruff: noqa: F811 - pytest fixtures are shared from the cancellation suite

import pytest

from apps.api.work_order_edit import EditIntent, edit_work_order
from apps.api.work_orders import RegistrationConflict
from tests.integration.test_work_order_cancel import (  # noqa: F401 - shared fixtures
    bind,
    client,
    headers,
    release,
    rows,
    state,
)

BODY = {
    "edit_id": "edit-1",
    "expected_row_version": "2",
    "recipe_version_id": "recipe-v1",
    "target_quantity": 1000,
    "planned_date": "2026-10-29",
}
EDIT = "/api/v1/work-order-actions/edit?work_order_id=work-A"


async def test_edit_released_order_in_place_and_replay(state, client):
    release(state)
    response = await client.post(EDIT, headers=headers(state), json=BODY)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["row_version"] == "3"
    assert receipt["before"]["target_quantity"] == 4 and receipt["after"]["target_quantity"] == 1000

    again = await client.post(EDIT, headers=headers(state), json=BODY)
    assert again.status_code == 200 and again.json() == receipt
    orders = {row["id"]: row for row in rows(state, "work_orders")}
    assert set(orders) == {"work-A", "work-B"}
    assert orders["work-A"]["status"] == "RELEASED"
    assert int(orders["work-A"]["target_quantity"]) == 1000
    assert int(orders["work-A"]["row_version"]) == 3

    # A load prepared from the old version (2) is refused.
    with pytest.raises(RegistrationConflict):
        bind(state)

    history = await client.get("/api/v1/work-order-history?work_order_id=work-A")
    events = history.json()["events"]
    assert [event["action"] for event in events] == [
        "REGISTER_WORK_ORDER",
        "RELEASE_WORK_ORDER",
        "EDIT_WORK_ORDER",
    ]
    changes = {change["field"]: change for change in events[-1]["changes"]}
    assert changes["target_quantity"]["before"] == 4
    assert changes["target_quantity"]["after"] == 1000
    assert changes["planned_date"]["after"] == "2026-10-29"

    overview = await client.get(
        "/api/v1/work-order-overview?line_id=" + orders["work-A"]["line_id"]
    )
    item = next(item for item in overview.json()["items"] if item["id"] == "work-A")
    assert item["phase"] == "WAITING" and item["target_quantity"] == 1000


async def test_edit_draft_keeps_draft(state, client):
    response = await client.post(
        EDIT, headers=headers(state), json={**BODY, "expected_row_version": "1"}
    )
    assert response.status_code == 200, response.text
    orders = {row["id"]: row for row in rows(state, "work_orders")}
    assert orders["work-A"]["status"] == "DRAFT" and int(orders["work-A"]["row_version"]) == 2


async def test_bound_order_cannot_be_edited_and_nothing_is_written(state, client):
    release(state)
    bind(state)
    before = rows(state, "work_orders")
    response = await client.post(
        EDIT, headers=headers(state), json={**BODY, "expected_row_version": "2"}
    )
    assert response.status_code == 409
    assert rows(state, "work_orders") == before


def test_stale_version_reused_id_and_no_change_are_refused(state):
    release(state)

    def edit(body):
        with state.engine.begin() as db:
            return edit_work_order(
                db,
                "work-A",
                EditIntent.model_validate(body),
                principal=state.session.principal,
                clock=lambda: rows(state, "work_orders")[0]["created_at"],
            )

    for body in (
        {**BODY, "expected_row_version": "1"},
        {**BODY, "target_quantity": 4, "planned_date": "2026-10-28"},
    ):
        with pytest.raises(RegistrationConflict):
            edit(body)
    edit(BODY)
    with pytest.raises(RegistrationConflict):
        edit({**BODY, "target_quantity": 7})


async def test_edit_requires_login(state, client):
    client.cookies.clear()
    response = await client.post(EDIT, headers=headers(state), json=BODY)
    assert response.status_code in {401, 403}
