"""SPC joins real retained ingestion evidence and passive viewer sessions on PostgreSQL."""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import text

from apps.api.alarm_lifecycle import AlarmRule, AlarmScope
from apps.api.alarm_qualification import TemperatureRuleConfig
from apps.api.alarm_store import install_rule
from apps.api.auth import Principal, UserAuth
from apps.api.bootstrap import bootstrap_reference_data
from apps.api.ingestion import IngestionService
from apps.api.spc import OFFSETS
from apps.api.spc_http import SPCQuery, read_spc
from apps.api.user_http import UserHTTPConfig, create_user_app
from contracts.models import Parameters
from contracts.transport import GatewayBatch, TagSample, batch_digest
from tests.integration.test_screen_http import ORIGIN, account
from tests.integration.test_trend_history import AT, SCOPE, TAG, batch, policy, submit


@pytest.fixture
def spc_state(migrated, request):
    bootstrap_reference_data(migrated)
    service = IngestionService(migrated, trend_clock_policy=policy())
    service.configure_source(**SCOPE, gateway_id="gateway-1", capture_generation=1, now=AT)
    service.register_execution(
        execution_id="run-a",
        source_id=SCOPE["source_id"],
        stream_id=SCOPE["stream_id"],
        work_order_id="work-a",
        recipe_version="recipe-a",
        parameters=Parameters(temperature_c=150.0, speed_m_per_min=5.0),
        counter_epoch="counter-a",
        now=AT,
    )
    count = getattr(request, "param", 25)
    for m in range(count):
        for i, seconds in enumerate(OFFSETS):
            seq = m * 5 + i + 1
            at = AT + timedelta(minutes=m, seconds=seconds)
            original = batch(seq, at, 148.0 + i).body
            identity = TagSample(
                sample_id=f"identity-{seq}",
                tag_id="Line.ActiveExecutionId",
                source_timestamp=at,
                server_timestamp=None,
                acquired_at=at,
                status_code=0,
                reading=dict(data_type="String", value="run-a"),
            )
            body = original.model_copy(update={"samples": original.samples + (identity,)})
            submit(
                service,
                GatewayBatch(body=body, content_sha256=batch_digest(body)),
                now=AT + timedelta(minutes=count + 1),
            )
    return service


def query(**changes):
    return SPCQuery(**dict(SCOPE, execution_id="run-a", tag=TAG, **changes))


def test_real_raw_selected_good_execution_and_no_current_rule_guess(spc_state):
    # Installing a current rule after this run cannot retroactively assign a spec.
    auth = UserAuth(spc_state.engine, allowed_origins={ORIGIN})
    account(auth, "spc-admin", {"equipment_admin"})
    config = TemperatureRuleConfig(
        rule=AlarmRule(rule_id="temperature", version="v-later", severity="HIGH"), setpoint_c=150.0
    )
    with spc_state.engine.begin() as db:
        install_rule(
            db,
            AlarmScope(**SCOPE, asset_id="Sealer"),
            config,
            input_id="later-rule",
            principal=Principal("spc-admin", frozenset({"equipment_admin"}), False),
            at=AT + timedelta(hours=1),
        )
    with spc_state.engine.connect() as db:
        result = read_spc(db, query(), now=AT + timedelta(minutes=25))
    assert result["status"] == "OK"
    assert len(result["subgroups"]) == 25
    assert result["limits"]["xbar"]["ucl"] == pytest.approx(152.308)
    assert result["spec"] is None  # No historical assigned rule is available.
    assert result["capability"] is None
    assert result["unit"] == "°C"
    assert result["generated_at"].endswith("Z")
    # A real but different execution must never inherit run-a's analog evidence.
    spc_state.register_execution(
        execution_id="run-b",
        source_id=SCOPE["source_id"],
        stream_id=SCOPE["stream_id"],
        work_order_id="work-b",
        recipe_version="recipe-b",
        parameters=Parameters(temperature_c=150.0, speed_m_per_min=5.0),
        counter_epoch="counter-b",
        now=AT,
    )
    with spc_state.engine.connect() as db:
        other = read_spc(
            db, SPCQuery(**SCOPE, execution_id="run-b", tag=TAG), now=AT + timedelta(minutes=25)
        )
    assert other["subgroups"] == []
    assert other["status"] == "INSUFFICIENT_DATA"
    assert other["excluded"]["reasons"]["MISSING"] == 25


@pytest.mark.asyncio
async def test_viewer_auth_and_strict_scope(spc_state, monkeypatch):
    import apps.api.spc_http as module

    actual = read_spc
    monkeypatch.setattr(
        module, "read_spc", lambda db, q: actual(db, q, now=AT + timedelta(minutes=25))
    )
    auth = UserAuth(spc_state.engine, allowed_origins={ORIGIN})
    viewer = account(auth, "spc-viewer", {"viewer"})
    app = create_user_app(auth, UserHTTPConfig(origin=ORIGIN))
    params = list(SCOPE.items()) + [("execution_id", "run-a"), ("tag", TAG)]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        assert (await client.get("/api/v1/spc/xbar-r", params=params)).status_code == 401
        client.cookies.set("hmi_session", viewer.token)
        response = await client.get("/api/v1/spc/xbar-r", params=params)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["status"] == "OK"
        for extra in [[("unused", "x")], [("tag", TAG)], [("stream_id", "other")]]:
            assert (
                await client.get("/api/v1/spc/xbar-r", params=params + extra)
            ).status_code == 422
        wrong = list(SCOPE.items()) + [("execution_id", "not-present"), ("tag", TAG)]
        assert (await client.get("/api/v1/spc/xbar-r", params=wrong)).status_code == 404


@pytest.mark.parametrize("spc_state", [40], indirect=True)
def test_retention_cannot_replace_first20_with_later25(spc_state):
    now = AT + timedelta(minutes=40)
    with spc_state.engine.connect() as db:
        original = read_spc(db, query(), now=now)
    assert original["status"] == "OK"
    assert original["baseline"]["from"] == "2026-10-23T12:00:00Z"
    with spc_state.engine.begin() as db:
        db.execute(
            text("DELETE FROM analog_history_raw WHERE acquired_at<:cutoff"),
            {"cutoff": AT + timedelta(minutes=15)},
        )
    with spc_state.engine.connect() as db:
        retained = read_spc(db, query(), now=now)
    assert len(retained["subgroups"]) == 25
    assert retained["status"] == "INSUFFICIENT_DATA"
    assert retained["baseline"] is None
    assert retained["limits"] is None
    assert retained["capability"] is None
    assert retained["baseline_limitation"] == "ORIGINAL_PREFIX_RAW_UNAVAILABLE"
    assert all(group["violations"] == [] for group in retained["subgroups"])


def test_samples_after_confirmed_end_are_not_attributed(spc_state, monkeypatch):
    # The PLC keeps the ended execution ID while idle; only samples before the confirmed
    # STOPPING->STOPPED end (the bound reports also use) belong to the run.
    import apps.api.spc_http as module

    monkeypatch.setattr(
        module, "execution_state_bounds", lambda db, execution_id: (AT, AT + timedelta(minutes=10))
    )
    with spc_state.engine.connect() as db:
        result = read_spc(db, query(), now=AT + timedelta(minutes=25))
    assert len(result["subgroups"]) == 10
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["limits"] is None
