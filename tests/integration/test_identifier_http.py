"""Exact Identifier query aliases preserve UTF8 and legacy paths on real PostgreSQL."""

import httpx
import pytest
from pydantic import TypeAdapter
from sqlalchemy import event, text

from apps.api.bootstrap import LINE_ID
from contracts.models import Identifier
from tests.integration.test_work_order_cancel import ORIGIN, SCOPE, account, headers
from tests.integration.test_work_order_cancel import state as state

MATRIX = (
    ("recipe-A", "order-A", "orig-A"),
    ("recipe/A", "order/A", "orig/A"),
    (".", ".", "."),
    ("..", "..", ".."),
    ("recipe%2FA", "order%2FA", "orig%2FA"),
    ("공정-é-Ａ", "작업-é-Ａ", "원래-é-Ａ"),
)


async def register(client, state, recipe, order):
    response = await client.post(
        "/api/v1/recipe-versions",
        json={
            "id": recipe,
            "recipe_id": "family-" + recipe,
            "version": "1",
            "item_id": "item",
            "parameters": {"temperature_c": 150.0, "speed_m_per_min": 6.0},
        },
        headers=headers(state),
    )
    assert response.status_code == 200
    response = await client.post(
        "/api/v1/work-orders",
        json={
            "id": order,
            "item_id": "item",
            "line_id": LINE_ID,
            "recipe_version_id": recipe,
            "target_quantity": 4,
            "planned_date": "2026-10-28",
        },
        headers=headers(state),
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("recipe,order,original", MATRIX)
async def test_exact_query_identity_roundtrip_and_passive_original_records(
    state, recipe, order, original
):
    for identity in (recipe, order, original):
        assert TypeAdapter(Identifier).validate_python(identity) == identity
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=state.app), base_url=ORIGIN
    ) as client:
        client.cookies.set("hmi_session", state.session.token)
        definition = await register(client, state, recipe, order)
        for path, key, identity in (
            ("recipe-version-detail", "recipe_version_id", recipe),
            ("work-order-detail", "work_order_id", order),
        ):
            response = await client.get("/api/v1/" + path, params={key: identity})
            assert response.status_code == 200 and response.json()["id"] == identity
        release = await client.post(
            "/api/v1/work-order-actions/release",
            params={"work_order_id": order},
            json={"release_id": original, "expected_row_version": "1"},
            headers=headers(state),
        )
        assert release.status_code == 200
        binding = await client.post(
            "/api/v1/work-order-actions/bind",
            params={"work_order_id": order},
            json={
                "command_id": original,
                **SCOPE,
                "expected_work_order_version": "2",
                "expected_control_version": "42",
            },
            headers=headers(state),
        )
        assert binding.status_code == 200
        cancel_order = "cancel-" + order
        response = await client.post(
            "/api/v1/work-orders",
            json={
                key: value
                for key, value in definition.items()
                if key not in {"created_by", "created_at", "status", "row_version"}
            }
            | {"id": cancel_order},
            headers=headers(state),
        )
        assert response.status_code == 200
        cancel = await client.post(
            "/api/v1/work-order-actions/cancel",
            params={"work_order_id": cancel_order},
            json={"cancellation_id": original, "expected_row_version": "1"},
            headers=headers(state),
        )
        assert cancel.status_code == 200
        statements = []

        def capture(_db, _cursor, statement, _parameters, _context, _many):
            statements.append(statement.lstrip().split()[0].upper())

        event.listen(state.engine, "before_cursor_execute", capture)
        try:
            for kind, identity_key, target, receipt in (
                ("release", "release_id", order, release.json()),
                ("binding", "command_id", order, binding.json()),
                ("cancellation", "cancellation_id", cancel_order, cancel.json()),
            ):
                result = await client.get(
                    "/api/v1/work-order-receipts/" + kind,
                    params={"work_order_id": target, identity_key: original},
                )
                assert result.status_code == 200
                assert result.headers["cache-control"] == "no-store"
                assert result.json()["receipt"] == receipt
                assert result.json()["intent"][identity_key] == original
                assert result.json()["intent"]["actor"] == "manager"
            result = await client.get(
                "/api/v1/command-record", params={"command_id": original, **SCOPE}
            )
            assert result.status_code == 200
            assert result.json()["request"] == binding.json()["load_request"]
            assert result.json()["result"]["command_id"] == original
            assert not {"UPDATE", "INSERT", "DELETE"} & set(statements)
        finally:
            event.remove(state.engine, "before_cursor_execute", capture)
        # Linked work remains authoritative409 and cannot be cleared by a URL alias.
        response = await client.post(
            "/api/v1/work-order-actions/cancel",
            params={"work_order_id": order},
            json={"cancellation_id": "bound-" + original, "expected_row_version": "3"},
            headers=headers(state),
        )
        assert response.status_code == 409
        assert response.json() == {"detail": "cancellation conflicts with original state or intent"}
        if order == "order-A":
            # Existing clients keep all legacy paths and original receipt spelling.
            assert (await client.get("/api/v1/work-orders/order-A")).status_code == 200
            result = await client.get("/api/v1/work-orders/order-A/releases/orig-A")
            assert result.json()["receipt"] == release.json()
            result = await client.get("/api/v1/commands/orig-A", params=SCOPE)
            assert result.json()["request"] == binding.json()["load_request"]


async def test_alias_query_keys_are_exact_once_and_identifiers_are_not_coerced(state):
    routes = (
        ("GET", "recipe-version-detail", {"recipe_version_id": "recipe-v1"}),
        ("GET", "work-order-detail", {"work_order_id": "work-A"}),
        ("POST", "work-order-actions/release", {"work_order_id": "work-A"}),
        ("POST", "work-order-actions/bind", {"work_order_id": "work-A"}),
        ("POST", "work-order-actions/cancel", {"work_order_id": "work-A"}),
        ("GET", "work-order-receipts/release", {"work_order_id": "work-A", "release_id": "x"}),
        ("GET", "work-order-receipts/binding", {"work_order_id": "work-A", "command_id": "x"}),
        (
            "GET",
            "work-order-receipts/cancellation",
            {"work_order_id": "work-A", "cancellation_id": "x"},
        ),
        ("GET", "command-record", {"command_id": "x", **SCOPE}),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=state.app), base_url=ORIGIN
    ) as client:
        client.cookies.set("hmi_session", state.session.token)
        for method, path, valid in routes:
            first = next(iter(valid))
            for invalid in (
                {},
                {**valid, "extra": "x"},
                list(valid.items()) + [(first, valid[first])],
                {**valid, first: ""},
                {**valid, first: "has space"},
            ):
                result = await client.request(method, "/api/v1/" + path, params=invalid)
                assert result.status_code == 422, (path, invalid)


async def test_alias_auth_csrf_scope_and_original_actor_permissions_remain_enforced(state):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=state.app), base_url=ORIGIN
    ) as client:
        for path, query in (
            ("work-order-detail", {"work_order_id": "work-A"}),
            ("command-record", {"command_id": "missing", **SCOPE}),
        ):
            assert (await client.get("/api/v1/" + path, params=query)).status_code == 401
        client.cookies.set("hmi_session", state.session.token)
        query = {"work_order_id": "work-A"}
        body = {"cancellation_id": "cancel/A", "expected_row_version": "1"}
        for protection in (
            {},
            {"Origin": ORIGIN, "X-CSRF-Token": "invalid"},
            {"Origin": "https://wrong.example.test", "X-CSRF-Token": state.session.csrf_token},
        ):
            response = await client.post(
                "/api/v1/work-order-actions/cancel", params=query, json=body, headers=protection
            )
            assert response.status_code == 403
        for role in ("viewer", "operator", "equipment_admin"):
            other = account(state.auth, role, {role})
            client.cookies.set("hmi_session", other.token)
            protection = {"Origin": ORIGIN, "X-CSRF-Token": other.csrf_token}
            assert (await client.get("/api/v1/work-order-detail", params=query)).status_code == 200
            for action, definition in (
                ("cancel", body),
                ("release", {"release_id": "release/A", "expected_row_version": "1"}),
            ):
                response = await client.post(
                    "/api/v1/work-order-actions/" + action,
                    params=query,
                    json=definition,
                    headers=protection,
                )
                assert response.status_code == 403
        initial = account(state.auth, "initial", {"production_manager"}, initial=True)
        client.cookies.set("hmi_session", initial.token)
        assert (await client.get("/api/v1/work-order-detail", params=query)).status_code == 403
        client.cookies.set("hmi_session", state.session.token)
        response = await client.get(
            "/api/v1/command-record", params={"command_id": "missing", **SCOPE}
        )
        assert response.status_code == 404
        response = await client.get(
            "/api/v1/command-record",
            params={"command_id": "missing", **SCOPE, "stream_id": "other"},
        )
        assert response.status_code == 404
        with state.engine.connect() as db:
            assert (
                db.execute(text("SELECT count(*) FROM work_order_cancellations")).scalar_one() == 0
            )
