"""Actual authenticated action JSON plus exact fixed-query receipt recovery."""

from datetime import timedelta

import pytest
from sqlalchemy import text
from test_alarm_http import ORIGIN, QUERY, condition, counts, intent, member, protection
from test_alarm_http import client as client
from test_alarm_http import state as state


@pytest.mark.parametrize("identity", ["a/b", ".", "..", "x/../y", "a%2Fb"])
async def test_authenticated_slash_dot_action_original_receipt_and_timestamp(
    state, client, identity
):
    # Runtime-style safe alarm ID; the actual HTTP caller supplies unusual action ID.
    condition(state)
    body = intent(identity=identity)
    response = await client.post("/api/v1/alarm-actions", json=body, headers=protection(state))
    assert response.status_code == 200, response.text
    original = response.json()
    assert original["intent"]["action_id"] == identity
    before = counts(state)
    state.clock[0] += timedelta(seconds=10)
    replay = await client.post("/api/v1/alarm-actions", json=body, headers=protection(state))
    assert replay.status_code == 200
    assert replay.json() == original | {
        "receipt": original["receipt"] | {"replayed": True, "notify_current": False}
    }
    assert counts(state) == before
    with state.engine.connect() as db:
        idle_before = db.execute(text("SELECT max(last_seen_at) FROM auth_sessions")).scalar_one()
    state.clock[0] += timedelta(minutes=1)
    query = QUERY | {"alarm_id": "alarm-A", "action_id": identity}
    recovered = await client.get("/api/v1/alarm-action-receipts", params=query)
    assert recovered.status_code == 200 and not recovered.history
    assert recovered.json() == original
    with state.engine.connect() as db:
        assert (
            db.execute(text("SELECT max(last_seen_at) FROM auth_sessions")).scalar_one()
            == idle_before
        )
    assert counts(state) == before
    assert (
        await client.get("/api/v1/alarm-action-receipts", params=query | {"source_id": "foreign"})
    ).status_code == 404


@pytest.mark.parametrize("identity", ["a/b", ".", ".."])
async def test_arbitrary_stored_alarm_instance_notes_history_and_no_new_ack_receipt(
    state, client, identity
):
    condition(state, alarm=identity)
    query = QUERY | {"alarm_id": identity}
    detail = await client.get("/api/v1/alarm-instance", params=query)
    assert detail.status_code == 200 and detail.json()["instance"]["alarm_id"] == identity
    ack = intent(identity="ack/one", alarm=identity)
    first = await client.post("/api/v1/alarm-actions", json=ack, headers=protection(state))
    assert first.status_code == 200
    second_ack = ack | {"action_id": ".."}
    repeated = await client.post(
        "/api/v1/alarm-actions", json=second_ack, headers=protection(state)
    )
    assert repeated.status_code == 200 and repeated.json()["action"] is None
    recovered = await client.get(
        "/api/v1/alarm-action-receipts", params=query | {"action_id": ".."}
    )
    assert recovered.json() == repeated.json()
    add = intent(
        identity="note/add", alarm=identity, kind="NOTE_ADD", note_id="note/one", text="observed"
    )
    edit = intent(
        identity="note/edit",
        alarm=identity,
        kind="NOTE_EDIT",
        note_id="note/one",
        text="checked",
        expected_previous_action_id="note/add",
    )
    for body in (add, edit):
        saved = await client.post("/api/v1/alarm-actions", json=body, headers=protection(state))
        assert saved.status_code == 200, saved.text
        record = await client.get(
            "/api/v1/alarm-action-receipts", params=query | {"action_id": body["action_id"]}
        )
        assert record.json() == saved.json()
    page = await client.get("/api/v1/alarm-actions", params=query | {"limit": "1"})
    assert page.status_code == 200 and page.json()["items"][0]["action_id"] == "note/edit"
    next_page = await client.get(
        "/api/v1/alarm-actions",
        params=query | {"limit": "1", "before_action_id": page.json()["next_before_action_id"]},
    )
    assert next_page.status_code == 200 and next_page.json()["items"][0]["action_id"] == "note/add"
    assert counts(state)["actions"] == 3 and counts(state)["outbox"] == 1


@pytest.mark.parametrize(
    "route,ids",
    [
        ("alarm-instance", {"alarm_id": "alarm-A"}),
        ("alarm-actions", {"alarm_id": "alarm-A"}),
        ("alarm-action-receipts", {"alarm_id": "alarm-A", "action_id": "ack"}),
    ],
)
@pytest.mark.parametrize("invalid", ["duplicate", "extra", "missing", "bad"])
async def test_alarm_alias_exact_query_allowlist(state, client, route, ids, invalid):
    query = QUERY | ids
    if invalid == "duplicate":
        query = list(query.items()) + [("alarm_id", "alarm-A")]
    elif invalid == "extra":
        query["extra"] = "x"
    elif invalid == "missing":
        query.pop("alarm_id")
    else:
        query["alarm_id"] = "bad id"
    assert (await client.get("/api/v1/" + route, params=query)).status_code == 422


async def test_alarm_alias_live_authorization_actor_csrf_and_original_conflict(state, client):
    condition(state)
    body = intent(identity="original/ack")
    assert (await client.post("/api/v1/alarm-actions", json=body)).status_code == 403
    assert (
        await client.post(
            "/api/v1/alarm-actions", json=body | {"actor": "foreign"}, headers=protection(state)
        )
    ).status_code == 403
    assert (
        await client.post("/api/v1/alarm-actions?extra=x", json=body, headers=protection(state))
    ).status_code == 422
    assert (
        await client.post("/api/v1/alarm-actions", json=body, headers=protection(state))
    ).status_code == 200
    changed = body | {"kind": "NOTE_ADD", "note_id": "new", "text": "changed"}
    assert (
        await client.post("/api/v1/alarm-actions", json=changed, headers=protection(state))
    ).status_code == 409
    viewer = member(state.auth, "viewer", {"viewer"})
    client.cookies.set("hmi_session", viewer.token)
    assert (
        await client.get("/api/v1/alarm-instance", params=QUERY | {"alarm_id": "alarm-A"})
    ).status_code == 200
    assert (
        await client.post(
            "/api/v1/alarm-actions",
            json=intent(actor="viewer"),
            headers={"Origin": ORIGIN, "X-CSRF-Token": viewer.csrf_token},
        )
    ).status_code == 403
    state.auth.revoke(viewer.token, csrf_token=viewer.csrf_token, origin=ORIGIN)
    assert (
        await client.get("/api/v1/alarm-instance", params=QUERY | {"alarm_id": "alarm-A"})
    ).status_code == 401
