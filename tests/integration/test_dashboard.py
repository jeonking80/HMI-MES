"""Dashboard reads the real durable report, snapshot and alarm evidence."""

import json
from datetime import timedelta

import httpx
from sqlalchemy import text

from apps.api.auth import UserAuth
from apps.api.bootstrap import LINE_ID
from apps.api.dashboard import read_dashboard
from apps.api.reports import read_daily_report
from apps.api.user_http import SESSION_COOKIE, UserHTTPConfig, create_user_app
from contracts.models import Parameters
from contracts.tags.validate import load_contract
from tests.integration import test_ingestion
from tests.integration.test_api_auth import ORIGIN, account
from tests.integration.test_ingestion import AT, batch, event
from tests.integration.test_snapshot import communication

service = test_ingestion.service

SCOPE = {"line_id": LINE_ID, "source_id": "simulator-1", "stream_id": "stream-1"}


def capture(service, *, active="", quantity=None):
    tags = load_contract()
    samples = []
    for index, tag in enumerate(tags["tags"]):
        kind = tag["data_type"]
        value = {
            "Boolean": False,
            "String": "",
            "Double": 0.0,
            "Int32": 0,
            "UInt32": 0,
            "UInt64": 0,
        }[kind]
        if tag["enum"]:
            value = int(next(iter(tags["enums"][tag["enum"]])))
        if tag["logical_name"] == "Line.ActiveExecutionId":
            value = active
        samples.append(
            {
                "sample_id": f"dash-{index}",
                "tag_id": tag["logical_name"],
                "source_timestamp": AT,
                "server_timestamp": None,
                "acquired_at": AT,
                "status_code": 0,
                "reading": {"data_type": kind, "value": value},
            }
        )
    payload = batch(
        *([event(quantity=quantity, occurred_at=AT.isoformat())] if quantity is not None else []),
        samples=samples,
        schema_version="1.1",
        communication=communication(),
        captured_at=AT,
    )
    service.ingest(
        payload,
        authenticated_gateway_id="gateway-1",
        authenticated_source_id=SCOPE["source_id"],
        authenticated_stream_id=SCOPE["stream_id"],
        now=AT,
    )


def read(service, *, now=AT, **scope):
    with service.engine.begin() as db:
        return read_dashboard(db, **(SCOPE | scope), day=AT.date(), now=now)


def test_no_data_live_stale_and_report_conservation(service):
    initial = read(service)
    assert initial["freshness"] == {"last_receipt_at": None, "status": "NO_DATA"}
    assert initial["active_execution"] is None
    assert initial["time"]["unknown_seconds"] == initial["time"]["total_seconds"]
    capture(service, active="run-A", quantity=7)
    result = read(service, now=AT + timedelta(seconds=1))
    assert result["freshness"]["status"] == "LIVE"
    assert result["production"]["good"] == "7"
    assert result["production"]["provisional"] is True
    assert result["active_execution"]["execution_id"] == "run-A"
    assert result["active_execution"]["good"] == "7"
    assert result["oee_partial"]["quality"] == 1.0
    assert result["oee_partial"]["performance"] is None
    with service.engine.begin() as db:
        daily = read_daily_report(
            db, LINE_ID, AT.date(), now=AT + timedelta(seconds=1), scope=SCOPE
        )
    assert result["hourly"] == daily["hourly"]
    assert result["production"]["good"] == daily["totals"]["good"]
    assert all(
        result["time"][k + "_seconds"] == daily["durations_seconds"][k]
        for k in ("running", "stopped", "unknown", "total")
    )
    assert len(json.dumps(result).encode()) < 64 * 1024
    stale = read(service, now=AT + timedelta(seconds=3))
    assert stale["freshness"]["status"] == "STALE"
    assert stale["active_execution"] is None


def test_foreign_active_execution_not_exposed(service):
    service.configure_source(
        source_id="other",
        stream_id="other",
        gateway_id="other",
        line_id=LINE_ID,
        capture_generation=1,
        now=AT,
    )
    service.register_execution(
        execution_id="foreign",
        source_id="other",
        stream_id="other",
        work_order_id="other-work",
        recipe_version="recipe-1",
        counter_epoch="epoch-other",
        parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
        now=AT,
    )
    capture(service, active="foreign")
    assert read(service)["active_execution"] is None


async def test_http_viewer_auth_query_no_idle_refresh(service):
    auth = UserAuth(service.engine, allowed_origins={ORIGIN}, clock=lambda: AT)
    session, _ = account(auth, "viewer", {"viewer"})
    app = create_user_app(auth, UserHTTPConfig(origin=ORIGIN))
    params = SCOPE | {"date": AT.date().isoformat()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        assert (await client.get("/api/v1/dashboard", params=params)).status_code == 401
        client.cookies.set(SESSION_COOKIE, session.token)
        with service.engine.connect() as db:
            before = db.execute(
                text("SELECT idle_expires_at FROM auth_sessions WHERE revoked_at IS NULL")
            ).scalar_one()
        response = await client.get("/api/v1/dashboard", params=params)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        with service.engine.connect() as db:
            assert (
                db.execute(
                    text("SELECT idle_expires_at FROM auth_sessions WHERE revoked_at IS NULL")
                ).scalar_one()
                == before
            )
        assert (
            await client.get("/api/v1/dashboard", params=params | {"extra": "1"})
        ).status_code == 422
        assert (
            await client.get(
                "/api/v1/dashboard", params=list(params.items()) + [("date", "2026-10-23")]
            )
        ).status_code == 422
        assert (
            await client.get("/api/v1/dashboard", params=params | {"date": "20261023"})
        ).status_code == 422
        assert (
            await client.get("/api/v1/dashboard", params=params | {"stream_id": "foreign"})
        ).status_code == 404


def test_dynamic_alarm_severity_lifecycle_and_source_isolation(service):
    from apps.api.alarm_lifecycle import AlarmEvidence, AlarmRule, AlarmScope, QualifiedCondition
    from apps.api.alarm_qualification import PressureRuleConfig
    from apps.api.alarm_store import act_on_alarm, apply_condition, install_rule
    from apps.api.auth import Principal

    auth = UserAuth(service.engine, allowed_origins={ORIGIN}, clock=lambda: AT)
    session, _ = account(auth, "alarm-admin", {"equipment_admin", "operator"})
    principal = Principal(
        session.principal.login, frozenset({"equipment_admin", "operator"}), False
    )
    own_scope = AlarmScope(**SCOPE, asset_id="Sealer")
    other_scope = AlarmScope(**(SCOPE | {"source_id": "other-source"}), asset_id="Sealer")
    service.configure_source(
        line_id=LINE_ID,
        source_id="other-source",
        stream_id=SCOPE["stream_id"],
        gateway_id="other-gateway",
        capture_generation=1,
        now=AT,
    )

    def install_and_raise(db, scope, rule_id, severity):
        install_rule(
            db,
            scope,
            PressureRuleConfig(rule=AlarmRule(rule_id=rule_id, version="v1", severity=severity)),
            input_id="install-" + rule_id,
            principal=principal,
            at=AT,
        )
        apply_condition(
            db,
            rule_id,
            QualifiedCondition(
                event_id="raise-" + rule_id,
                scope=scope,
                rule_version="v1",
                position=1,
                condition="ACTIVE",
                new_alarm_id="alarm-" + rule_id,
                evidence=AlarmEvidence(observed_at=AT, references=("evidence-" + rule_id,)),
            ),
        )

    with service.engine.begin() as db:
        install_and_raise(db, own_scope, "critical", "custom-critical")
        install_and_raise(db, own_scope, "advisory", "custom-advisory")
        install_and_raise(db, own_scope, "returned", "custom-returned")
        install_and_raise(db, other_scope, "foreign", "foreign-only")
        act_on_alarm(
            db,
            own_scope,
            "advisory",
            input_id="ack-advisory",
            alarm_id="alarm-advisory",
            principal=principal,
            at=AT + timedelta(seconds=1),
            kind="ACKNOWLEDGE",
        )
        apply_condition(
            db,
            "returned",
            QualifiedCondition(
                event_id="return-returned",
                scope=own_scope,
                rule_version="v1",
                position=2,
                condition="NORMAL",
                evidence=AlarmEvidence(
                    observed_at=AT + timedelta(seconds=1), references=("normal-returned",)
                ),
            ),
        )
    capture(service, active="run-A", quantity=7)
    service.register_execution(
        execution_id="other-run",
        source_id="other-source",
        stream_id=SCOPE["stream_id"],
        work_order_id="other-work",
        recipe_version="recipe-1",
        counter_epoch="epoch-1",
        parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
        now=AT,
    )
    other_batch = batch(
        event(
            event_id="foreign-production-event",
            quantity=99,
            execution_id="other-run",
            source_id="other-source",
            stream_id=SCOPE["stream_id"],
            occurred_at=AT.isoformat(),
        ),
        source_id="other-source",
        gateway_id="other-gateway",
        captured_at=AT,
    )
    service.ingest(
        other_batch,
        authenticated_gateway_id="other-gateway",
        authenticated_source_id="other-source",
        authenticated_stream_id=SCOPE["stream_id"],
        now=AT,
    )
    own = read(service, now=AT + timedelta(seconds=2))
    foreign = read(service, now=AT + timedelta(seconds=2), source_id="other-source")
    assert own["production"]["good"] == "7"
    assert foreign["production"]["good"] == "99"
    assert own["alarms"] == {
        "active_total": 2,
        "active_by_severity": {"custom-critical": 1, "custom-advisory": 1},
        "unacknowledged": 2,
    }
    assert foreign["alarms"] == {
        "active_total": 1,
        "active_by_severity": {"foreign-only": 1},
        "unacknowledged": 1,
    }
    assert "custom-returned" not in own["alarms"]["active_by_severity"]
    assert len(json.dumps(own, ensure_ascii=False).encode("utf-8")) < 64 * 1024
