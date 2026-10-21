"""Passive full screen envelopes from real PostgreSQL and browser-session ASGI."""

import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import event, text

import apps.api.user_http as user_http
from apps.api.auth import UserAuth
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.ingestion import IngestionService
from apps.api.screen_frames import APP_SCREEN_ADAPTER, APP_SCREEN_MAX_BYTES, ScreenDataFrame
from apps.api.user_http import SESSION_COOKIE, UserHTTPConfig, create_user_app
from contracts.models import UINT64_MAX
from contracts.tags.validate import load_contract
from contracts.transport import BatchBody, GatewayBatch, batch_digest

ORIGIN = "https://screen.example.test"
SCOPE = {"line_id": LINE_ID, "source_id": "source/Ａ", "stream_id": "stream%2FＡ"}


def account(auth, login, roles, *, changed=True):
    password = secrets.token_urlsafe(24)
    auth.bootstrap_user(login, password, roles=roles)
    challenge = auth.begin_login()
    session = auth.login(
        login,
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=ORIGIN,
    )
    return (
        auth.change_password(
            session.token, secrets.token_urlsafe(24), csrf_token=session.csrf_token, origin=ORIGIN
        )
        if changed
        else session
    )


def capture(state, *, seq=1, total=UINT64_MAX, worst=False):
    tags = load_contract()
    values = {"Line.GoodTotal": total, "Sealer.AirPressure": None, "Sealer.TemperaturePV": 99.5}
    samples = []
    for index, tag in enumerate(tags["tags"]):
        name, kind = tag["logical_name"], tag["data_type"]
        default = {
            "Boolean": False,
            "String": "",
            "Double": 0.0,
            "Int32": 0,
            "UInt32": 0,
            "UInt64": 0,
        }[kind]
        if tag["enum"]:
            default = int(next(iter(tags["enums"][tag["enum"]])))
        samples.append(
            {
                "sample_id": str(index) + "🙂" * (128 - len(str(index)))
                if worst
                else f"sample-{seq}-{name}",
                "tag_id": name,
                "source_timestamp": state.at - timedelta(days=1),
                "server_timestamp": None,
                "acquired_at": state.at,
                "status_code": 0x80000000
                if name == "Sealer.AirPressure"
                else 0x40000000
                if name == "Sealer.TemperaturePV"
                else 0,
                "reading": {"data_type": kind, "value": values.get(name, default)},
            }
        )
    body = BatchBody(
        schema_version="1.1",
        batch_id="🙂" * 128 if worst else f"batch-{seq}",
        gateway_id="gateway",
        source_id=SCOPE["source_id"],
        stream_id=SCOPE["stream_id"],
        capture_generation=1,
        capture_seq=seq,
        captured_at=state.at,
        samples=samples,
        communication={
            "state": "GOOD",
            "connected": True,
            "read_age_seconds": 0.0,
            "heartbeat_ages_seconds": {
                key: 0.0
                for key in (
                    "Line.Heartbeat",
                    "Infeed.LinkHeartbeat",
                    "Sealer.LinkHeartbeat",
                    "Outfeed.LinkHeartbeat",
                )
            },
        },
    )
    return GatewayBatch(body=body, content_sha256=batch_digest(body))


def ingest(state, batch):
    return state.service.ingest(
        batch,
        authenticated_gateway_id="gateway",
        authenticated_source_id=SCOPE["source_id"],
        authenticated_stream_id=SCOPE["stream_id"],
        now=state.at,
    )


@pytest.fixture
def state(migrated):
    bootstrap_reference_data(migrated)
    at = datetime.now(UTC)
    clock = [at]
    auth = UserAuth(migrated, allowed_origins={ORIGIN}, clock=lambda: clock[0])
    session = account(auth, "operator", {"operator"})
    service = IngestionService(migrated)
    service.configure_source(**SCOPE, gateway_id="gateway", capture_generation=1, now=at)
    return SimpleNamespace(
        engine=migrated,
        auth=auth,
        session=session,
        service=service,
        at=at,
        clock=clock,
        app=create_user_app(auth, UserHTTPConfig(origin=ORIGIN)),
    )


@pytest.fixture
async def client(state):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=state.app), base_url=ORIGIN
    ) as value:
        value.cookies.set(SESSION_COOKIE, state.session.token)
        yield value


async def test_complete_envelope_keeps_original_samples_quality_and_uint64(client, state):
    batch = capture(state)
    ingest(state, batch)
    response = await client.get("/api/v1/screen", params=SCOPE)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("application/json")
    assert "set-cookie" not in response.headers
    assert len(response.content) <= APP_SCREEN_MAX_BYTES
    frame = APP_SCREEN_ADAPTER.validate_json(response.content)
    assert isinstance(frame, ScreenDataFrame)
    assert frame.scope.model_dump() == SCOPE
    assert set(response.json()) == {
        "schema_version",
        "scope",
        "screen",
        "health",
        "evidence",
        "missing_tag_ids",
    }
    assert frame.screen.kind == "SNAPSHOT" and frame.screen.revision == 0
    expected = {sample.tag_id: sample for sample in batch.body.samples}
    assert {sample.tag_id: sample for sample in frame.screen.view.samples} == expected
    assert all(item.batch_id == "batch-1" and item.capture_seq == 1 for item in frame.evidence)
    assert all(item.captured_at == item.stored_at == state.at for item in frame.evidence)
    assert {sample.tag_id for sample in frame.screen.view.samples} == {
        item.tag_id for item in frame.evidence
    }
    assert frame.missing_tag_ids == ()
    raw = {sample["tag_id"]: sample for sample in response.json()["screen"]["view"]["samples"]}
    assert raw["Line.GoodTotal"]["reading"]["value"] == str(UINT64_MAX)
    assert raw["Sealer.AirPressure"]["reading"]["value"] is None
    assert raw["Sealer.AirPressure"]["status_code"] == 0x80000000
    assert raw["Sealer.TemperaturePV"]["status_code"] == 0x40000000
    again = await client.get("/api/v1/screen", params=SCOPE)
    assert again.json()["screen"]["kind"] == "SNAPSHOT"
    assert again.json()["screen"]["sync_id"] != frame.screen.sync_id
    legacy = await client.get("/api/v1/snapshot", params=SCOPE)
    assert legacy.status_code == 200 and "source_id" in legacy.json()
    assert "scope" not in legacy.json()


async def test_never_observed_and_maximum_multibyte_evidence_remain_bounded(client, state):
    empty = await client.get("/api/v1/screen", params=SCOPE)
    assert empty.status_code == 200
    assert empty.json()["health"]["state"] == "NEVER_OBSERVED"
    assert empty.json()["screen"]["view"]["samples"] == []
    assert len(empty.json()["missing_tag_ids"]) == 48
    ingest(state, capture(state, worst=True))
    full = await client.get("/api/v1/screen", params=SCOPE)
    assert full.status_code == 200 and len(full.content) <= APP_SCREEN_MAX_BYTES
    assert len(APP_SCREEN_ADAPTER.validate_json(full.content).evidence) == 48


async def test_scope_query_is_exact_once_validated_and_foreign_scope_is404(client, state):
    for query in (
        {},
        {**SCOPE, "extra": "x"},
        {**SCOPE, "csrf_token": "not-in-query"},
        list(SCOPE.items()) + [("line_id", LINE_ID)],
        {**SCOPE, "source_id": ""},
        {**SCOPE, "source_id": "has space"},
    ):
        response = await client.get("/api/v1/screen", params=query)
        assert response.status_code == 422
    for changed in ({"line_id": "foreign"}, {"source_id": "foreign"}, {"stream_id": "foreign"}):
        response = await client.get("/api/v1/screen", params=SCOPE | changed)
        assert response.status_code == 404
    bad_host = await client.get("/api/v1/screen", params=SCOPE, headers={"Host": "foreign.test"})
    assert bad_host.status_code == 400


async def test_real_read_roles_and_initial_password_gate(client, state):
    for role in ("viewer", "operator", "production_manager", "equipment_admin"):
        session = account(state.auth, "reader-" + role, {role})
        client.cookies.set(SESSION_COOKIE, session.token)
        response = await client.get("/api/v1/screen", params=SCOPE)
        assert response.status_code == 200
    initial = account(state.auth, "initial", {"operator"}, changed=False)
    client.cookies.set(SESSION_COOKIE, initial.token)
    assert (await client.get("/api/v1/screen", params=SCOPE)).status_code == 403


@pytest.mark.parametrize("change", ["logout", "roles", "inactive", "password", "idle", "absolute"])
async def test_every_poll_checks_live_session_without_idle_renewal(client, state, change):
    assert (await client.get("/api/v1/screen", params=SCOPE)).status_code == 200
    session = state.session
    if change == "logout":
        state.auth.revoke(session.token, csrf_token=session.csrf_token, origin=ORIGIN)
    elif change in {"roles", "inactive"}:
        admin = account(state.auth, "admin", {"equipment_admin"})
        if change == "roles":
            state.auth.set_user_roles(
                admin.token, "operator", {"viewer"}, csrf_token=admin.csrf_token, origin=ORIGIN
            )
        else:
            state.auth.set_user_active(
                admin.token, "operator", False, csrf_token=admin.csrf_token, origin=ORIGIN
            )
    elif change == "password":
        state.auth.change_password(
            session.token, secrets.token_urlsafe(24), csrf_token=session.csrf_token, origin=ORIGIN
        )
    elif change == "idle":
        state.clock[0] += timedelta(minutes=29)
        assert (await client.get("/api/v1/screen", params=SCOPE)).status_code == 200
        state.clock[0] += timedelta(minutes=2)
    else:
        state.clock[0] += timedelta(hours=8)
    assert (await client.get("/api/v1/screen", params=SCOPE)).status_code == 401


async def test_cookie_required_and_poll_is_read_only_including_auth(client, state):
    statements = []

    def capture_sql(_db, _cursor, statement, _params, _context, _many):
        statements.append(statement.lstrip().split()[0].upper())

    with state.engine.connect() as db:
        before = db.execute(
            text(
                "SELECT token_digest,last_seen_at,idle_expires_at FROM auth_sessions "
                "ORDER BY token_digest"
            )
        ).all()
    event.listen(state.engine, "before_cursor_execute", capture_sql)
    try:
        for _ in range(2):
            response = await client.get("/api/v1/screen", params=SCOPE)
            assert response.status_code == 200 and "set-cookie" not in response.headers
        client.cookies.clear()
        assert (await client.get("/api/v1/screen", params=SCOPE)).status_code == 401
        response = await client.get(
            "/api/v1/screen",
            params=SCOPE,
            headers={
                "Authorization": "Bearer service-token",
                "Cookie": "simulator_session=foreign",
            },
        )
        assert response.status_code == 401
        assert not {"INSERT", "UPDATE", "DELETE"} & set(statements)
    finally:
        event.remove(state.engine, "before_cursor_execute", capture_sql)
    with state.engine.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT token_digest,last_seen_at,idle_expires_at FROM auth_sessions "
                    "ORDER BY token_digest"
                )
            ).all()
            == before
        )


async def test_line_share_lock_keeps_screen_and_evidence_one_real_capture(
    client, state, monkeypatch
):
    ingest(state, capture(state))
    next_batch = capture(state, seq=2, total=7)
    attempted = Event()
    original = user_http.read_snapshot
    future = None
    with ThreadPoolExecutor(max_workers=1) as pool:

        def ingest_next():
            attempted.set()
            return ingest(state, next_batch)

        def interleaved(*args, **kwargs):
            nonlocal future
            result = original(*args, **kwargs)
            if future is None:
                future = pool.submit(ingest_next)
                assert attempted.wait(2)
                deadline = time.monotonic() + 2
                blocked = False
                # Statistics are cached per transaction; refresh each observation.
                with state.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as db:
                    while time.monotonic() < deadline:
                        blocked = db.execute(
                            text("""SELECT EXISTS(SELECT 1 FROM pg_stat_activity
                            WHERE wait_event_type='Lock' AND query LIKE '%lines%'
                            AND pid<>pg_backend_pid())""")
                        ).scalar_one()
                        if blocked:
                            break
                        time.sleep(0.01)
                assert blocked and not future.done()
            return result

        monkeypatch.setattr(user_http, "read_snapshot", interleaved)
        first = await client.get("/api/v1/screen", params=SCOPE)
        assert first.status_code == 200
        assert {item["batch_id"] for item in first.json()["evidence"]} == {"batch-1"}
        future.result(timeout=3)
        second = await client.get("/api/v1/screen", params=SCOPE)
        assert {item["batch_id"] for item in second.json()["evidence"]} == {"batch-2"}
        total = next(
            sample["reading"]["value"]
            for sample in second.json()["screen"]["view"]["samples"]
            if sample["tag_id"] == "Line.GoodTotal"
        )
        assert total == "7"
