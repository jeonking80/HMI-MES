"""Work-order revision is cancel-and-replace in one transaction; overview and history read it."""
# ruff: noqa: F811 - pytest fixtures are shared from the cancellation suite

import pytest

from apps.api.work_order_revise import RevisionIntent, revise_work_order
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
    "new_work_order_id": "work-A2",
    "recipe_version_id": "recipe-v1",
    "target_quantity": 9,
    "planned_date": "2026-10-29",
    "expected_row_version": "2",
    "release_id": "release-A2",
}


async def test_revise_released_order_cancels_original_and_releases_replacement(state, client):
    release(state)
    response = await client.post(
        "/api/v1/work-order-actions/revise?work_order_id=work-A", headers=headers(state), json=BODY
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["work_order_id"] == "work-A2"
    assert result["cancellation"]["cancellation_id"] == "REVISE-work-A2"
    assert result["release"]["status"] == "RELEASED"

    # Exact replay returns the same outcome and creates nothing new.
    again = await client.post(
        "/api/v1/work-order-actions/revise?work_order_id=work-A", headers=headers(state), json=BODY
    )
    assert again.status_code == 200 and again.json() == result
    orders = {row["id"]: row for row in rows(state, "work_orders")}
    assert orders["work-A"]["status"] == "CANCELLED"
    assert orders["work-A2"]["status"] == "RELEASED"
    assert int(orders["work-A2"]["target_quantity"]) == 9

    overview = await client.get(
        "/api/v1/work-order-overview?line_id=" + orders["work-A"]["line_id"]
    )
    assert overview.status_code == 200
    items = {item["id"]: item for item in overview.json()["items"]}
    assert items["work-A"]["phase"] == "CANCELLED" and items["work-A"]["revised_to"] == "work-A2"
    assert items["work-A2"]["phase"] == "WAITING" and items["work-A2"]["revised_from"] == "work-A"
    assert items["work-B"]["phase"] == "DRAFT"

    history = await client.get("/api/v1/work-order-history?work_order_id=work-A")
    actions = [event["action"] for event in history.json()["events"]]
    assert actions == ["REGISTER_WORK_ORDER", "RELEASE_WORK_ORDER", "CANCEL_WORK_ORDER"]


async def test_revise_draft_keeps_replacement_as_draft(state, client):
    response = await client.post(
        "/api/v1/work-order-actions/revise?work_order_id=work-A",
        headers=headers(state),
        json={**BODY, "expected_row_version": "1"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["release"] is None
    orders = {row["id"]: row for row in rows(state, "work_orders")}
    assert orders["work-A2"]["status"] == "DRAFT"


async def test_bound_order_cannot_be_revised_and_nothing_is_written(state, client):
    release(state)
    bind(state)
    before = rows(state, "work_orders")
    response = await client.post(
        "/api/v1/work-order-actions/revise?work_order_id=work-A",
        headers=headers(state),
        json={**BODY, "expected_row_version": "3"},
    )
    assert response.status_code == 409
    assert rows(state, "work_orders") == before
    overview = await client.get("/api/v1/work-order-overview?line_id=" + before[0]["line_id"])
    item = next(item for item in overview.json()["items"] if item["id"] == "work-A")
    assert item["phase"] == "RUNNING"
    assert item["execution"]["settlement_status"] == "PENDING"


def test_stale_version_and_same_id_are_refused(state):
    release(state)
    for body in ({**BODY, "expected_row_version": "1"}, {**BODY, "new_work_order_id": "work-A"}):
        with pytest.raises(RegistrationConflict), state.engine.begin() as db:
            revise_work_order(
                db,
                "work-A",
                RevisionIntent.model_validate(body),
                principal=state.session.principal,
                clock=lambda: None,
            )
    assert {row["id"] for row in rows(state, "work_orders")} == {"work-A", "work-B"}


async def test_revision_requires_production_manage(state, client):
    client.cookies.clear()
    response = await client.post(
        "/api/v1/work-order-actions/revise?work_order_id=work-A", headers=headers(state), json=BODY
    )
    assert response.status_code in {401, 403}
