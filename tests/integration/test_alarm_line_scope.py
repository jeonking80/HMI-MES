"""Real PostgreSQL logical Line fault persistence; mixed runtime is not wired here."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_alarm_http import member

from apps.api.alarm_capture import AlarmProcessingContext, CaptureIdentity, ClockAssessment
from apps.api.alarm_lifecycle import (
    AlarmConflict,
    AlarmEvidence,
    AlarmRule,
    AlarmScope,
    QualifiedCondition,
)
from apps.api.alarm_qualification import PressureRuleConfig
from apps.api.alarm_rules import PLCFaultRuleConfig
from apps.api.alarm_store import (
    apply_condition,
    apply_fault_capture,
    break_continuity,
    install_rule,
    read_instance,
    replace_rule,
)
from apps.api.auth import Principal, UserAuth
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.database import migration_config
from apps.api.ingestion import IngestionService
from apps.api.user_http import UserHTTPConfig, create_user_app
from contracts.models import FaultEvent, StateEvent
from contracts.tags.validate import load_contract
from contracts.transport import BatchBody, GatewayBatch, TagSample, batch_digest

AT = datetime(2026, 10, 24, tzinfo=UTC)
ORIGIN = "https://alarm-http.test"  # Existing authenticated fixture helper's exact origin.
SCOPE = AlarmScope(line_id=LINE_ID, source_id="source", stream_id="stream", asset_id="Line")
CONFIG = PLCFaultRuleConfig(rule=AlarmRule(rule_id="plc-fault", version="v1", severity="HIGH"))
PRINCIPAL = Principal("operator", frozenset({"operator", "equipment_admin"}), False)


def active_events(code=123, seq=1):
    state = StateEvent(
        kind="STATE_CHANGED",
        event_id=f"state-{seq}",
        source_id="source",
        stream_id="stream",
        seq=seq,
        equipment_id="Line",
        execution_id=None,
        occurred_at=AT + timedelta(milliseconds=seq),
        payload={"previous_state": "READY", "state": "FAULT", "control_version": 1},
    )
    event = FaultEvent(
        kind="FAULT_CHANGED",
        event_id=f"fault-{seq + 1}",
        source_id="source",
        stream_id="stream",
        seq=seq + 1,
        equipment_id="Line",
        execution_id=None,
        occurred_at=state.occurred_at,
        payload={"fault_code": code, "active": True},
    )
    return state, event


def reset_events(code=123, seq=3):
    state = StateEvent(
        kind="STATE_CHANGED",
        event_id=f"state-{seq}",
        source_id="source",
        stream_id="stream",
        seq=seq,
        equipment_id="Line",
        execution_id=None,
        occurred_at=AT + timedelta(milliseconds=seq),
        payload={"previous_state": "FAULT", "state": "STOPPED", "control_version": 2},
    )
    event = FaultEvent(
        kind="FAULT_CHANGED",
        event_id=f"fault-{seq + 1}",
        source_id="source",
        stream_id="stream",
        seq=seq + 1,
        equipment_id="Line",
        execution_id=None,
        occurred_at=state.occurred_at,
        payload={"fault_code": code, "active": False},
    )
    return state, event


@pytest.fixture
def state(migrated):
    bootstrap_reference_data(migrated)
    clock = [AT + timedelta(seconds=10)]
    auth = UserAuth(migrated, allowed_origins={ORIGIN}, clock=lambda: clock[0])
    session = member(auth)
    service = IngestionService(migrated)
    service.configure_source(
        source_id="source",
        stream_id="stream",
        gateway_id="gateway",
        line_id=LINE_ID,
        capture_generation=1,
        now=AT,
    )
    with auth.authorized_transaction(
        session.token, permission="equipment_manage", csrf_token=session.csrf_token, origin=ORIGIN
    ) as tx:
        install_rule(
            tx.connection, SCOPE, CONFIG, input_id="install-fault", principal=tx.principal, at=AT
        )
    return SimpleNamespace(
        engine=migrated, auth=auth, session=session, service=service, clock=clock
    )


def capture(
    state,
    seq,
    *,
    code=123,
    line_state=5,
    ceiling=2,
    events=(),
    config=CONFIG,
    communication="GOOD",
    historical=False,
    position=None,
):
    values = {
        "Line.State": line_state,
        "Line.FaultCode": code,
        "Line.EventSeq": ceiling,
        "Line.ControlVersion": 2,
    }
    for group in ("Infeed", "Sealer", "Outfeed"):
        values[group + ".FaultCode"] = code
        values[group + ".State"] = line_state
    samples = []
    dictionary = load_contract()
    for tag in dictionary["tags"]:
        kind = tag["data_type"]
        value = {
            "Boolean": False,
            "Int32": 0,
            "UInt32": 0,
            "UInt64": 0,
            "Double": 0.0,
            "String": "",
        }[kind]
        if tag["enum"]:
            value = int(next(iter(dictionary["enums"][tag["enum"]])))
        samples.append(
            TagSample(
                sample_id=f"{tag['logical_name']}-{seq}",
                tag_id=tag["logical_name"],
                source_timestamp=AT,
                server_timestamp=None,
                acquired_at=AT + timedelta(seconds=seq),
                status_code=0,
                reading={"data_type": kind, "value": values.get(tag["logical_name"], value)},
            )
        )
    ages = 0.0 if communication == "GOOD" else 5.0
    body = BatchBody(
        schema_version="1.2",
        batch_id=f"batch-{seq}",
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        capture_generation=1,
        capture_seq=seq,
        captured_at=AT + timedelta(seconds=seq),
        samples=tuple(samples),
        events=events,
        communication={
            "state": communication,
            "connected": True,
            "read_age_seconds": ages,
            "heartbeat_ages_seconds": {
                name: ages
                for name in (
                    "Line.Heartbeat",
                    "Infeed.LinkHeartbeat",
                    "Sealer.LinkHeartbeat",
                    "Outfeed.LinkHeartbeat",
                )
            },
        },
        capture_time={
            "clock_epoch": "epoch",
            "monotonic_seconds": 100.0 + seq,
            "utc_read_span_seconds": 0.001,
        },
    )
    digest = batch_digest(body)
    state.service.ingest(
        GatewayBatch(body=body, content_sha256=digest),
        authenticated_gateway_id="gateway",
        authenticated_source_id="source",
        authenticated_stream_id="stream",
        now=body.captured_at,
    )
    context = AlarmProcessingContext(
        capture=CaptureIdentity.from_batch(body),
        batch_sha256=digest,
        scope=SCOPE,
        rule=config.rule,
        position=100 + seq if position is None else position,
        classification="HISTORICAL" if historical else "LIVE",
        clock_assessment=ClockAssessment(
            method="CONFIGURED_API_REFERENCE",
            reference_id="fixture-reference",
            gateway_id="gateway",
            clock_epoch="epoch",
            batch_sha256=digest,
            offset_lower_seconds=0.0,
            offset_upper_seconds=0.0,
        ),
    )
    return body, context


def apply(state, original, alarm="alarm-A"):
    with state.engine.begin() as db:
        return apply_fault_capture(db, *original, new_alarm_id=alarm)


def instance(state, alarm="alarm-A"):
    with state.engine.begin() as db:
        return read_instance(db, SCOPE, "plc-fault", alarm)


def counts(state):
    with state.engine.connect() as db:
        return {
            name: db.execute(text("SELECT count(*) FROM alarm_" + name)).scalar_one()
            for name in ("instances", "actions", "receipts", "outbox")
        }


def test_logical_line_no_fake_asset_and_durable_split_reset_pair_only_returns(state):
    original = capture(state, 1, events=active_events())
    raised = apply(state, original)
    assert raised.notify_current and instance(state).status == "ACTIVE_UNACKNOWLEDGED"
    with state.engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM assets WHERE code='Line'")).scalar_one() == 0
        assert (
            db.execute(text("SELECT physical_asset_code FROM alarm_channels")).scalar_one() is None
        )
        raw = db.execute(
            text("SELECT intent FROM alarm_receipts WHERE input_id=:id"), {"id": raised.input_id}
        ).scalar_one()
        assert raw["observation"]["verified_through_event_seq"] == "2"
    apply(state, capture(state, 2, code=0, line_state=0))
    assert instance(state).condition_closed_at is None
    pair = reset_events()
    apply(state, capture(state, 3, code=0, line_state=0, ceiling=4, events=(pair[0],)))
    assert instance(state).condition_closed_at is None
    returned = apply(state, capture(state, 4, code=0, line_state=0, ceiling=4, events=(pair[1],)))
    assert returned.changed_alarm_id == "alarm-A"
    assert instance(state).status == "RETURNED_UNACKNOWLEDGED"
    assert {pair[0].event_id, pair[1].event_id} <= set(instance(state).return_evidence.references)
    assert counts(state)["outbox"] == 1


def test_estop_priority_fault_first_occurrence_and_communication_unknown_keep_active(state):
    apply(state, capture(state, 1, line_state=6, events=active_events()))
    assert instance(state).status == "ACTIVE_UNACKNOWLEDGED"
    apply(
        state,
        capture(
            state, 2, code=0, line_state=0, ceiling=4, events=reset_events(), communication="ALARM"
        ),
    )
    assert instance(state).condition_closed_at is None
    apply(state, capture(state, 3, code=0, line_state=0, ceiling=4))
    assert instance(state).closure_reason == "NORMAL_RETURN"


def test_old_receipt_replays_after_source_conflict_generation_change_and_new_input_stays_gated(
    state,
):
    original = capture(state, 1, events=active_events())
    apply(state, original)
    unprocessed = capture(state, 2)
    before = counts(state)
    with state.engine.begin() as db:
        db.execute(
            text(
                "UPDATE source_streams SET conflicted=true,reconciliation_required=true,"
                "capture_generation=2"
            )
        )
    assert apply(state, original).replayed and counts(state) == before
    with pytest.raises(AlarmConflict):
        apply(state, unprocessed)
    changed = (original[0], original[1].model_copy(update={"position": 900}))
    with pytest.raises(AlarmConflict):
        apply(state, changed)
    assert counts(state) == before


def test_unknown_restart_gap_and_historical_receipts_do_not_close_or_duplicate(state):
    original = capture(state, 1, events=active_events())
    apply(state, original)
    with state.engine.begin() as db:
        break_continuity(
            db,
            SCOPE,
            "plc-fault",
            input_id="restart",
            position=102,
            reason="RESTART",
            evidence_reference="epoch2",
        )
    assert instance(state).condition_closed_at is None
    history = capture(
        state, 2, code=0, line_state=0, ceiling=4, events=reset_events(), position=101
    )
    result = apply(state, history)
    assert result.disposition == "HISTORY_ONLY" and instance(state).condition_closed_at is None
    assert apply(state, history).replayed
    apply(state, capture(state, 3, code=0, line_state=0, ceiling=4))
    assert instance(state).closure_reason == "NORMAL_RETURN" and counts(state)["outbox"] == 1


def test_changed_code_new_recurrence_and_immutable_rule_installation(state):
    apply(state, capture(state, 1, events=active_events()))
    inactive = FaultEvent(
        kind="FAULT_CHANGED",
        event_id="code-swap-old",
        source_id="source",
        stream_id="stream",
        seq=3,
        equipment_id="Line",
        execution_id=None,
        occurred_at=AT + timedelta(milliseconds=3),
        payload={"fault_code": 123, "active": False},
    )
    active = inactive.model_copy(
        update={
            "event_id": "code-swap-new",
            "seq": 4,
            "payload": inactive.payload.model_copy(update={"fault_code": 456, "active": True}),
        }
    )
    apply(state, capture(state, 2, code=456, ceiling=4, events=(inactive, active)))
    assert counts(state)["instances"] == 1
    apply(state, capture(state, 3, code=0, line_state=0, ceiling=6, events=reset_events(456, 5)))
    assert instance(state).closure_reason == "NORMAL_RETURN"
    apply(state, capture(state, 4, code=123, ceiling=8, events=active_events(seq=7)), "alarm-B")
    assert counts(state)["instances"] == 2 and counts(state)["outbox"] == 2
    assert instance(state, "alarm-B").status == "ACTIVE_UNACKNOWLEDGED"
    newer = CONFIG.model_copy(update={"rule": CONFIG.rule.model_copy(update={"version": "v2"})})
    with state.engine.begin() as db:
        replace_rule(
            db,
            SCOPE,
            newer,
            input_id="v2",
            expected_version="v1",
            principal=PRINCIPAL,
            at=AT + timedelta(seconds=5),
        )
    assert instance(state, "alarm-B").closure_reason == "CONFIG_CHANGED"


async def test_http_line_coverage_ack_notes_and_target_isolation(state):
    apply(state, capture(state, 1, events=active_events()))
    app = create_user_app(state.auth, UserHTTPConfig(origin=ORIGIN))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        client.cookies.set("hmi_session", state.session.token)
        query = SCOPE.model_dump() | {"rule_id": "plc-fault"}
        result = await client.get("/api/v1/alarms", params=query)
        assert result.status_code == 200 and result.json()["coverage"]["implemented_kinds"] == [
            "PLC_FAULT"
        ]
        assert result.json()["coverage"]["installed_channels"][0]["kind"] == "PLC_FAULT"
        intent = dict(
            action_id="ack",
            scope=SCOPE.model_dump(),
            rule_id="plc-fault",
            alarm_id="alarm-A",
            actor="operator",
            kind="ACKNOWLEDGE",
        )
        headers = {"Origin": ORIGIN, "X-CSRF-Token": state.session.csrf_token}
        assert (
            await client.post("/api/v1/alarms/alarm-A/actions", json=intent, headers=headers)
        ).status_code == 200
        assert instance(state).condition_closed_at is None
        note = intent | {
            "kind": "NOTE_ADD",
            "action_id": "note-action",
            "note_id": "note",
            "text": "fault still present",
        }
        assert (
            await client.post("/api/v1/alarms/alarm-A/actions", json=note, headers=headers)
        ).status_code == 200
        assert (
            await client.get("/api/v1/alarms/alarm-A", params=query | {"asset_id": "Sealer"})
        ).status_code == 404


def test_target_config_guards_and_digital_cannot_bypass_reset_via_qualified_condition(state):
    with pytest.raises(ValueError):
        with state.engine.begin() as db:
            install_rule(
                db,
                SCOPE.model_copy(update={"asset_id": "Sealer"}),
                CONFIG,
                input_id="wrong-asset",
                principal=PRINCIPAL,
                at=AT,
            )
    with pytest.raises(ValueError):
        with state.engine.begin() as db:
            install_rule(
                db,
                SCOPE,
                PressureRuleConfig(rule=CONFIG.rule),
                input_id="wrong-kind",
                principal=PRINCIPAL,
                at=AT,
            )
    event = QualifiedCondition(
        event_id="unproved-normal",
        scope=SCOPE,
        rule_version="v1",
        position=900,
        condition="NORMAL",
        evidence=AlarmEvidence(observed_at=AT, references=("fake",)),
    )
    with pytest.raises(AlarmConflict):
        with state.engine.begin() as db:
            apply_condition(db, "plc-fault", event)


def test_used_line_extension_refuses_downgrade_and_asset_override(state):
    with state.engine.connect() as db:
        before = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    with pytest.raises(DBAPIError):
        with state.engine.connect() as db:
            command.downgrade(migration_config(db), "0011_alarm_capture_context")
    with state.engine.connect() as db:
        assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == before
    with pytest.raises(DBAPIError):
        with state.engine.begin() as db:
            db.execute(text("UPDATE alarm_channels SET physical_asset_code=NULL"))
    with pytest.raises(IntegrityError):
        with state.engine.begin() as db:
            db.execute(text("UPDATE alarm_channels SET asset_id='Outfeed'"))


def test_repeated_stale_source_event_ceilings_cannot_recreate_returned_fault(state):
    apply(state, capture(state, 1, events=active_events()))
    apply(state, capture(state, 2, code=0, line_state=0, ceiling=4, events=reset_events()))
    assert instance(state).closure_reason == "NORMAL_RETURN"
    before = counts(state)
    for seq in (3, 4):
        result = apply(state, capture(state, seq, code=123, ceiling=2), "must-not-exist")
        assert result.disposition == "UNKNOWN_EVIDENCE"
        with state.engine.connect() as db:
            checkpoint = db.execute(text("SELECT qualification FROM alarm_channels")).scalar_one()
            assert checkpoint["highest_event_ceiling"] == "4"
            assert checkpoint["latched"] == "NORMAL"
    assert counts(state)["instances"] == before["instances"]
    assert counts(state)["outbox"] == before["outbox"]
    with state.engine.begin() as db:
        break_continuity(
            db,
            SCOPE,
            "plc-fault",
            input_id="restart-after-stale",
            position=105,
            reason="RESTART",
            evidence_reference="new-runtime-epoch",
        )
    result = apply(
        state, capture(state, 5, code=123, ceiling=2, position=106), "still-must-not-exist"
    )
    assert result.disposition == "UNKNOWN_EVIDENCE"
    assert counts(state)["outbox"] == before["outbox"]


def test_wrong_eventseq_wiretype_is_rejected_before_database_comparison(state):
    from contracts.transport import StringValue

    original = capture(state, 1, events=active_events())
    altered_samples = tuple(
        sample.model_copy(update={"reading": StringValue(data_type="String", value="2")})
        if sample.tag_id == "Line.EventSeq"
        else sample
        for sample in original[0].samples
    )
    forged = original[0].model_copy(update={"samples": altered_samples})
    before = counts(state)
    with pytest.raises(ValueError):
        apply(state, (forged, original[1]))
    assert counts(state) == before


def test_rule_version_change_preserves_source_event_floor_while_closing_old_condition(state):
    apply(state, capture(state, 1, events=active_events()))
    apply(state, capture(state, 2, code=0, line_state=0, ceiling=4, events=reset_events()))
    newer = CONFIG.model_copy(update={"rule": CONFIG.rule.model_copy(update={"version": "v2"})})
    with state.engine.begin() as db:
        replace_rule(
            db,
            SCOPE,
            newer,
            input_id="install-v2",
            expected_version="v1",
            principal=PRINCIPAL,
            at=AT + timedelta(seconds=2),
        )
    before = counts(state)
    result = apply(state, capture(state, 3, code=123, ceiling=2, config=newer), "not-a-new-fault")
    assert result.disposition == "UNKNOWN_EVIDENCE"
    assert counts(state)["instances"] == before["instances"]
    assert counts(state)["outbox"] == before["outbox"]
    with state.engine.connect() as db:
        checkpoint = db.execute(text("SELECT qualification FROM alarm_channels")).scalar_one()
        assert checkpoint["config"]["rule"]["version"] == "v2"
        assert checkpoint["highest_event_ceiling"] == "4"


@pytest.mark.parametrize(
    "reason", ["CAPTURE_SEQUENCE_GAP", "CAPTURE_FRESHNESS_UNKNOWN", "CONFIG_INTERVAL_UNKNOWN"]
)
def test_forced_unknown_durable_floor_reset_suppression_replay_and_history(state, reason):
    apply(state, capture(state, 1, events=active_events()))
    original = capture(state, 2, code=0, line_state=0, ceiling=4, events=reset_events())
    with state.engine.begin() as db:
        result = apply_fault_capture(
            db, *original, new_alarm_id="alarm-A", forced_unknown_reason=reason
        )
        checkpoint = db.execute(text("SELECT qualification FROM alarm_channels")).scalar_one()
        intent = db.execute(
            text("SELECT intent FROM alarm_receipts WHERE input_id=:id"), {"id": result.input_id}
        ).scalar_one()
    assert checkpoint["highest_event_ceiling"] == "4"
    assert checkpoint["latched"] == "ACTIVE"
    assert checkpoint["last_known_at"] == (AT + timedelta(seconds=1)).isoformat().replace(
        "+00:00", "Z"
    )
    assert intent["input"]["forced_unknown_reason"] == reason
    assert (
        intent["observation"]["events"] and intent["observation"]["forced_unknown_reason"] == reason
    )
    assert instance(state).condition_closed_at is None
    before = counts(state)
    with state.engine.begin() as db:
        assert apply_fault_capture(
            db, *original, new_alarm_id="alarm-A", forced_unknown_reason=reason
        ).replayed
    assert counts(state) == before
    with pytest.raises(AlarmConflict), state.engine.begin() as db:
        apply_fault_capture(db, *original, new_alarm_id="alarm-A")
    history = capture(state, 3, ceiling=4, historical=True)
    with state.engine.begin() as db:
        apply_fault_capture(db, *history, forced_unknown_reason=reason)
        assert (
            db.execute(text("SELECT qualification FROM alarm_channels")).scalar_one() == checkpoint
        )
    for seq in (4, 5):
        apply(state, capture(state, seq, ceiling=2))
    assert counts(state)["outbox"] == 1
    assert instance(state).condition_closed_at is None


def test_forced_first_fault_no_occurrence_rollback_and_old_receipt_input_compatibility(state):
    original = capture(state, 1, events=active_events())
    with pytest.raises(RuntimeError), state.engine.begin() as db:
        apply_fault_capture(db, *original, forced_unknown_reason="CAPTURE_SEQUENCE_GAP")
        raise RuntimeError("caller rollback")
    assert counts(state)["instances"] == 0
    with state.engine.connect() as db:
        assert (
            db.execute(
                text("SELECT qualification->>'highest_event_ceiling' FROM alarm_channels")
            ).scalar_one()
            is None
        )
    with state.engine.begin() as db:
        unknown = apply_fault_capture(db, *original, forced_unknown_reason="CAPTURE_SEQUENCE_GAP")
    assert unknown.created_alarm_id is None and counts(state)["outbox"] == 0
    normal = capture(state, 2)
    raised = apply(state, normal)
    with state.engine.connect() as db:
        intent = db.execute(
            text("SELECT intent FROM alarm_receipts WHERE input_id=:id"), {"id": raised.input_id}
        ).scalar_one()
        assert "forced_unknown_reason" not in intent["input"]
    assert apply(state, normal).replayed


def test_config_interval_unknown_requires_locked_current_owner_not_historical_version(state):
    newer = CONFIG.model_copy(update={"rule": CONFIG.rule.model_copy(update={"version": "v2"})})
    with state.engine.begin() as db:
        replace_rule(
            db,
            SCOPE,
            newer,
            input_id="replace-for-ambiguity",
            expected_version="v1",
            principal=PRINCIPAL,
            at=AT + timedelta(seconds=1),
        )
    old_owner = capture(state, 1, events=active_events())
    before = counts(state)
    with pytest.raises(AlarmConflict, match="locked current"), state.engine.begin() as db:
        apply_fault_capture(db, *old_owner, forced_unknown_reason="CONFIG_INTERVAL_UNKNOWN")
    assert counts(state) == before
    current_owner = (old_owner[0], old_owner[1].model_copy(update={"rule": newer.rule}))
    with state.engine.begin() as db:
        result = apply_fault_capture(
            db, *current_owner, forced_unknown_reason="CONFIG_INTERVAL_UNKNOWN"
        )
    assert result.disposition == "UNKNOWN_EVIDENCE" and counts(state)["instances"] == 0
    with state.engine.begin() as db:
        db.execute(text("UPDATE source_streams SET conflicted=true,reconciliation_required=true"))
    with state.engine.begin() as db:
        assert apply_fault_capture(
            db, *current_owner, forced_unknown_reason="CONFIG_INTERVAL_UNKNOWN"
        ).replayed
    assert counts(state)["instances"] == 0


@pytest.mark.parametrize("explicit_history", [True, False])
@pytest.mark.parametrize("reason", [None, "CAPTURE_SEQUENCE_GAP"])
def test_pre_seam_checkpoint_history_preserves_exact_raw_json_and_cursor(
    state, explicit_history, reason
):
    apply(state, capture(state, 1, events=active_events()))
    with state.engine.begin() as db:
        # Reproduce the actual stored pre-seam format, not a newly serialized model.
        db.execute(
            text(
                "UPDATE alarm_channels SET qualification=qualification #- "
                "'{last_observation,forced_unknown_reason}'"
            )
        )
        before = db.execute(
            text("SELECT qualification::text,last_position,last_observed_at FROM alarm_channels")
        ).one()
        assert "forced_unknown_reason" not in before[0]
    original = capture(
        state,
        2,
        code=0,
        line_state=0,
        ceiling=4,
        events=reset_events(),
        historical=explicit_history,
        position=None if explicit_history else 50,
    )
    with state.engine.begin() as db:
        result = apply_fault_capture(db, *original, forced_unknown_reason=reason)
        after = db.execute(
            text("SELECT qualification::text,last_position,last_observed_at FROM alarm_channels")
        ).one()
    assert result.disposition == "HISTORY_ONLY"
    assert after == before
    assert instance(state).condition_closed_at is None and counts(state)["outbox"] == 1
