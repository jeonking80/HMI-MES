"""Real PostgreSQL source-wide mixed ownership and original fault capture decisions."""

import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace

import pytest
from alembic import command
from pydantic import ValidationError
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from apps.api.alarm_consumer import (
    AlarmCaptureConflict,
    AlarmConsumerConfig,
    AlarmConsumerUnavailable,
    ConfiguredClockPolicy,
    assess_capture_clock,
)
from apps.api.alarm_coordinator import AlarmCoordinator, MixedAlarmChannel, MixedAlarmConsumerConfig
from apps.api.alarm_lifecycle import AlarmRule, AlarmScope
from apps.api.alarm_qualification import PressureRuleConfig
from apps.api.alarm_rules import PLCFaultRuleConfig
from apps.api.alarm_store import _channel, install_rule, replace_rule
from apps.api.auth import UserAuth
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.database import migration_config
from apps.api.ingestion import IngestConflict, IngestionService, SourceConflicted
from contracts.models import FaultEvent, StateEvent
from contracts.tags.validate import load_contract
from contracts.transport import BatchBody, GatewayBatch, batch_digest

AT = datetime(2026, 10, 30, tzinfo=UTC)
ORIGIN = "https://coordinator.test"
SCOPE = AlarmScope(line_id=LINE_ID, source_id="source", stream_id="stream", asset_id="Sealer")
LINE = SCOPE.model_copy(update={"asset_id": "Line"})
PRESSURE = PressureRuleConfig(rule=AlarmRule(rule_id="pressure", version="p1", severity="HIGH"))
FAULT = PLCFaultRuleConfig(rule=AlarmRule(rule_id="fault", version="f1", severity="HIGH"))
GATEWAY = {
    "authenticated_gateway_id": "gateway",
    "authenticated_source_id": "source",
    "authenticated_stream_id": "stream",
}


def policy():
    return ConfiguredClockPolicy(
        policy_id="same-host",
        policy_version="v1",
        reference_id="kernel",
        method="CONFIGURED_API_REFERENCE",
        guarantee_kind="SAME_HOST_SHARED_KERNEL_UTC",
        guarantee_basis="Explicit test assumption: trusted shared kernel clock, not measured",
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        capture_generation=1,
        clock_epoch="gateway-epoch",
        valid_from_api_utc=AT - timedelta(days=1),
        valid_until_api_utc=AT + timedelta(days=1),
        offset_lower_seconds=-0.01,
        offset_upper_seconds=0.01,
    )


def mixed(*, fault=FAULT, clock=True):
    return MixedAlarmConsumerConfig(
        kind="MIXED_ALARM_RUNTIME",
        line_id=LINE_ID,
        source_id="source",
        stream_id="stream",
        gateway_id="gateway",
        capture_generation=1,
        channels=(
            MixedAlarmChannel(asset_id="Line", config=fault),
            MixedAlarmChannel(asset_id="Sealer", config=PRESSURE),
        ),
        clock_policy=policy() if clock else None,
    )


def legacy():
    return AlarmConsumerConfig(
        scope=SCOPE,
        gateway_id="gateway",
        capture_generation=1,
        rules=(PRESSURE,),
        clock_policy=policy(),
    )


@pytest.fixture
def state(migrated):
    bootstrap_reference_data(migrated)
    auth = UserAuth(migrated, allowed_origins={ORIGIN}, clock=lambda: AT)
    password = secrets.token_urlsafe(24)
    auth.bootstrap_user("admin", password, roles={"equipment_admin"})
    challenge = auth.begin_login()
    session = auth.login(
        "admin",
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=ORIGIN,
    )
    session = auth.change_password(
        session.token, secrets.token_urlsafe(24), csrf_token=session.csrf_token, origin=ORIGIN
    )
    service = IngestionService(migrated)
    service.configure_source(
        source_id="source",
        stream_id="stream",
        gateway_id="gateway",
        line_id=LINE_ID,
        capture_generation=1,
        now=AT - timedelta(hours=2),
    )
    value = SimpleNamespace(engine=migrated, auth=auth, session=session, service=service)
    for scope, config in ((SCOPE, PRESSURE), (LINE, FAULT)):
        put(value, scope, config)
    return value


def put(state, scope, config, *, expected=None, at=AT - timedelta(hours=1)):
    with state.auth.authorized_transaction(
        state.session.token,
        permission="equipment_manage",
        csrf_token=state.session.csrf_token,
        origin=ORIGIN,
    ) as tx:
        IngestionService._advisory(tx.connection, "gateway", "gateway")
        IngestionService._lock_stream(tx.connection, "source", "stream")
        extra = {} if expected is None else {"expected_version": expected}
        action = install_rule if expected is None else replace_rule
        action(
            tx.connection,
            scope,
            config,
            input_id="rule-" + config.rule.version,
            principal=tx.principal,
            at=at,
            **extra,
        )


def start(state, coordinator=None, *, now=AT):
    coordinator = coordinator or AlarmCoordinator(mixed(), consumer_epoch="api-one")
    with state.engine.begin() as db:
        coordinator.start(db, now=now)
    return coordinator


def rows(state, table):
    with state.engine.begin() as db:
        return list(db.execute(text("SELECT * FROM " + table)).mappings())


def capture(
    seq,
    *,
    seconds=None,
    code=0,
    ceiling=0,
    events=(),
    communication="GOOD",
    epoch="gateway-epoch",
    version="1.2",
    monotonic=None,
    span=0.01,
):
    seconds = seq if seconds is None else seconds
    at = AT + timedelta(seconds=seconds)
    overrides = {
        "Line.State": 5 if code else 0,
        "Line.FaultCode": code,
        "Line.EventSeq": ceiling,
        "Line.ControlVersion": 8,
        "Sealer.AirPressure": 5.0,
    }
    samples = []
    for tag in load_contract()["tags"]:
        name, kind = tag["logical_name"], tag["data_type"]
        default = (
            False
            if kind == "Boolean"
            else ""
            if kind == "String"
            else 0.0
            if kind == "Double"
            else 0
        )
        samples.append(
            {
                "sample_id": f"{seq}-{name}",
                "tag_id": name,
                "source_timestamp": at,
                "server_timestamp": at,
                "acquired_at": at,
                "status_code": 0,
                "reading": {"data_type": kind, "value": overrides.get(name, default)},
            }
        )
    extra = (
        {}
        if version == "1.0"
        else {
            "communication": {
                "state": communication,
                "connected": True,
                "read_age_seconds": 3.0 if communication == "DELAYED" else 0.0,
                "heartbeat_ages_seconds": {
                    name: 0.0
                    for name in (
                        "Line.Heartbeat",
                        "Infeed.LinkHeartbeat",
                        "Sealer.LinkHeartbeat",
                        "Outfeed.LinkHeartbeat",
                    )
                },
            }
        }
    )
    if version == "1.2":
        extra["capture_time"] = {
            "clock_epoch": epoch,
            "monotonic_seconds": float(seq if monotonic is None else monotonic),
            "utc_read_span_seconds": span,
        }
    return BatchBody(
        schema_version=version,
        batch_id=f"batch-{seq}",
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        capture_generation=1,
        capture_seq=seq,
        captured_at=at,
        samples=samples,
        events=events,
        **extra,
    )


def ingest(state, coordinator, body, *, now=None):
    service = IngestionService(state.engine, alarm_consumer=coordinator)
    return service.ingest(
        GatewayBatch(body=body, content_sha256=batch_digest(body)),
        **GATEWAY,
        now=now or body.captured_at + timedelta(seconds=0.05),
    )


def transitions(first, *, active, previous="READY", target="FAULT", version=1):
    common = {
        "source_id": "source",
        "stream_id": "stream",
        "equipment_id": "Line",
        "execution_id": None,
        "occurred_at": AT + timedelta(milliseconds=first),
    }
    return (
        StateEvent(
            kind="STATE_CHANGED",
            event_id=f"state-{first}",
            seq=first,
            payload={"previous_state": previous, "state": target, "control_version": version},
            **common,
        ),
        FaultEvent(
            kind="FAULT_CHANGED",
            event_id=f"fault-{first + 1}",
            seq=first + 1,
            payload={"fault_code": 123, "active": active},
            **common,
        ),
    )


def test_start_legacy_and_mixed_use_shared_fence_epoch_and_exact_omission_refusal(state):
    # An admitted original before activation becomes a fence, not a live replay.
    body = capture(5)
    state.service.ingest(
        GatewayBatch(body=body, content_sha256=batch_digest(body)), **GATEWAY, now=body.captured_at
    )
    old = start(state, AlarmCoordinator(legacy(), consumer_epoch="legacy-api"))
    before = rows(state, "alarm_capture_frontiers")
    assert len(before) == 1
    current = start(state)
    frontiers = rows(state, "alarm_capture_frontiers")
    assert len(frontiers) == 2
    assert {r["consumer_epoch"] for r in frontiers} == {current.consumer_epoch}
    assert {int(r["activation_fence_seq"]) for r in frontiers} == {5}
    assert {r["config_sha256"] for r in frontiers} == {current.config_sha256}
    with state.engine.begin() as db:
        db.execute(
            text("UPDATE alarm_capture_frontiers SET requires_restart=true WHERE channel_id=:c"),
            {"c": _channel(LINE, "fault")},
        )
    before = rows(state, "alarm_capture_frontiers"), rows(state, "alarm_receipts")
    with pytest.raises(AlarmConsumerUnavailable):
        start(state, AlarmCoordinator(legacy(), consumer_epoch="omit-line"))
    assert (rows(state, "alarm_capture_frontiers"), rows(state, "alarm_receipts")) == before
    with pytest.raises(AlarmConsumerUnavailable):
        ingest(state, old, capture(6))


@pytest.mark.parametrize("method", ["start", "consume", "invalidate"])
def test_every_new_mutation_refuses_old_schema_before_writing(state, method):
    with state.engine.connect() as db:
        command.downgrade(migration_config(db), "0013_work_order_cancel")
    coordinator = AlarmCoordinator(mixed())
    with pytest.raises(AlarmConsumerUnavailable), state.engine.begin() as db:
        if method == "start":
            coordinator.start(db, now=AT)
        elif method == "consume":
            coordinator.consume(db, capture(1), source={}, now=AT)
        else:
            coordinator.invalidate(db, source={}, reference_id="invalid", reason="SOURCE_CONFLICT")
    assert rows(state, "alarm_capture_frontiers") == []


def test_start_all_channels_roll_back_if_later_channel_write_fails(state):
    def fail(db, cursor, sql, params, context, many):
        if sql.startswith("INSERT INTO alarm_capture_frontiers") and params.get("c") == _channel(
            LINE, "fault"
        ):
            raise SQLAlchemyError("Injected last-channel failure")

    event.listen(state.engine, "before_cursor_execute", fail)
    before = rows(state, "alarm_receipts")
    try:
        with pytest.raises(SQLAlchemyError):
            start(state)
    finally:
        event.remove(state.engine, "before_cursor_execute", fail)
    assert rows(state, "alarm_capture_frontiers") == []
    assert rows(state, "alarm_receipts") == before


def test_supported_mixed_capture_uses_same_transaction_original_and_applied_fault_proof(state):
    coordinator = start(state)
    ingest(state, coordinator, capture(1, code=123, ceiling=2, events=transitions(1, active=True)))
    alarms = rows(state, "alarm_instances")
    assert len(alarms) == 1 and alarms[0]["channel_id"] == _channel(LINE, "fault")
    decisions = rows(state, "alarm_capture_decisions")
    assert len(decisions) == 2
    assert {r["disposition"] for r in decisions} == {"LIVE"}
    assert all(r["batch_id"] == "batch-1" for r in decisions)


def test_exact_stored_decisions_replay_before_ownership_clock_and_source_flags(state):
    coordinator = start(state)
    body = capture(1, code=123, ceiling=2, events=transitions(1, active=True))
    ingest(state, coordinator, body)
    start(state, AlarmCoordinator(mixed(clock=False), consumer_epoch="new-api"))
    with state.engine.begin() as db:
        db.execute(text("UPDATE source_streams SET conflicted=true"))
    before = {
        table: rows(state, table)
        for table in (
            "alarm_capture_frontiers",
            "alarm_channels",
            "alarm_capture_decisions",
            "alarm_receipts",
            "alarm_outbox",
        )
    }
    with state.engine.begin() as db:
        decisions = coordinator.consume(db, body, source={}, now=None)
    assert all(d.replayed and d.result.replayed and not d.result.notify_current for d in decisions)
    assert {table: rows(state, table) for table in before} == before


def test_new_line_channel_cannot_turn_pre_activation_capture_into_live_state(state):
    old = start(state, AlarmCoordinator(legacy(), consumer_epoch="old"))
    body = capture(1, code=123, ceiling=2, events=transitions(1, active=True))
    ingest(state, old, body)
    coordinator = start(state)
    before = next(
        r["qualification"] for r in rows(state, "alarm_channels") if r["asset_id"] == "Line"
    )
    # Normal raw duplicate ACK does not backfill missing channel decisions.
    ingest(state, coordinator, body)
    assert len(rows(state, "alarm_capture_decisions")) == 1
    with state.engine.begin() as db:
        result = coordinator.consume(db, body, source={}, now=AT + timedelta(seconds=1.05))
    assert any(d.replayed for d in result)
    line = next(d for d in result if d.channel_id == _channel(LINE, "fault"))
    assert line.disposition == "HISTORY_ONLY"
    assert (
        next(r["qualification"] for r in rows(state, "alarm_channels") if r["asset_id"] == "Line")
        == before
    )
    assert rows(state, "alarm_instances") == []


def test_unknown_good_verified_event_ceiling_keeps_floor_and_blocks_stale_reactivation(state):
    coordinator = start(state)
    ingest(state, coordinator, capture(1, code=123, ceiling=2, events=transitions(1, active=True)))
    ingest(
        state,
        coordinator,
        capture(
            2,
            ceiling=4,
            events=transitions(3, active=False, previous="FAULT", target="STOPPED", version=2),
        ),
    )
    extra = tuple(
        StateEvent(
            kind="STATE_CHANGED",
            event_id=f"state-{seq}",
            source_id="source",
            stream_id="stream",
            equipment_id="Line",
            execution_id=None,
            seq=seq,
            occurred_at=AT,
            payload={
                "previous_state": "STOPPED" if seq == 5 else "READY",
                "state": "READY" if seq == 5 else "STOPPED",
                "control_version": seq,
            },
        )
        for seq in (5, 6)
    )
    ingest(state, coordinator, capture(3, ceiling=6, events=extra, communication="DELAYED"))
    ingest(state, coordinator, capture(4, code=123, ceiling=4))
    checkpoint = next(
        r["qualification"] for r in rows(state, "alarm_channels") if r["asset_id"] == "Line"
    )
    assert checkpoint["highest_event_ceiling"] == "6" and checkpoint["latched"] == "NORMAL"
    assert checkpoint["last_reason"] == "SOURCE_EVENT_CEILING_REGRESSED"
    assert (
        len(rows(state, "alarm_instances")) == 1
        and rows(state, "alarm_instances")[0]["returned_at"] is not None
    )


def test_config_interval_unknown_continues_intake_without_claiming_governing_version(state):
    replacement = PLCFaultRuleConfig(rule=FAULT.rule.model_copy(update={"version": "f2"}))
    put(state, LINE, replacement, expected="f1", at=AT + timedelta(seconds=1))
    coordinator = start(
        state, AlarmCoordinator(mixed(fault=replacement)), now=AT + timedelta(seconds=1.01)
    )
    ingest(state, coordinator, capture(1, code=123, ceiling=2, events=transitions(1, active=True)))
    line = next(
        r
        for r in rows(state, "alarm_capture_decisions")
        if r["channel_id"] == _channel(LINE, "fault")
    )
    assert (
        line["disposition"] == "QUARANTINED" and line["reason"] == "NO_UNAMBIGUOUS_INSTALLED_CONFIG"
    )
    assert line["rule_version"] is None and line["context"]["rule"]["version"] == "f2"
    checkpoint = next(
        r["qualification"] for r in rows(state, "alarm_channels") if r["asset_id"] == "Line"
    )
    assert checkpoint["highest_event_ceiling"] == "2" and checkpoint["last_condition"] == "UNKNOWN"
    assert rows(state, "alarm_instances") == []


@pytest.mark.parametrize("reason", ["gap", "freshness", "clock-epoch", "no-clock"])
def test_supported_unknown_restrictions_keep_verified_floor_and_never_return_fault(state, reason):
    coordinator = start(state)
    ingest(state, coordinator, capture(1, code=123, ceiling=2, events=transitions(1, active=True)))
    if reason == "no-clock":
        coordinator = start(state, AlarmCoordinator(mixed(clock=False), consumer_epoch="clockless"))
    seq = 3 if reason == "gap" else 2
    body = capture(
        seq,
        ceiling=4,
        epoch="foreign-epoch" if reason == "clock-epoch" else "gateway-epoch",
        events=transitions(3, active=False, previous="FAULT", target="STOPPED", version=2),
    )
    ingest(state, coordinator, body, now=body.captured_at if reason == "freshness" else None)
    checkpoint = next(
        r["qualification"] for r in rows(state, "alarm_channels") if r["asset_id"] == "Line"
    )
    assert checkpoint["highest_event_ceiling"] == "4"
    assert checkpoint["last_condition"] == "UNKNOWN"
    assert rows(state, "alarm_instances")[0]["returned_at"] is None
    if reason in {"gap", "freshness"}:
        expected = "CAPTURE_SEQUENCE_GAP" if reason == "gap" else "CAPTURE_FRESHNESS_UNKNOWN"
        assert checkpoint["last_reason"] == expected
        assert checkpoint["last_observation"]["forced_unknown_reason"] == expected


def test_old_rule_historical_capture_preserves_entire_current_fault_checkpoint(state):
    coordinator = start(state)
    ingest(state, coordinator, capture(1, code=123, ceiling=2, events=transitions(1, active=True)))
    replacement = PLCFaultRuleConfig(rule=FAULT.rule.model_copy(update={"version": "f2"}))
    put(state, LINE, replacement, expected="f1", at=AT + timedelta(seconds=3))
    coordinator = start(
        state,
        AlarmCoordinator(mixed(fault=replacement), consumer_epoch="new-rule"),
        now=AT + timedelta(seconds=3),
    )
    before = next(
        r["qualification"] for r in rows(state, "alarm_channels") if r["asset_id"] == "Line"
    )
    ingest(
        state,
        coordinator,
        capture(2, seconds=0.1, code=123, ceiling=2),
        now=AT + timedelta(seconds=5),
    )
    after = next(
        r["qualification"] for r in rows(state, "alarm_channels") if r["asset_id"] == "Line"
    )
    assert after == before
    decision = next(
        r
        for r in rows(state, "alarm_capture_decisions")
        if r["batch_id"] == "batch-2" and r["channel_id"] == _channel(LINE, "fault")
    )
    assert decision["disposition"] == "HISTORY_ONLY" and decision["rule_version"] == "f1"


@pytest.mark.parametrize("method", ["start", "consume", "invalidate"])
def test_every_entrypoint_locks_source_and_complete_channel_union_before_input(state, method):
    coordinator = AlarmCoordinator(mixed(), consumer_epoch="api-one")
    if method != "start":
        start(state, coordinator)
    statements = []

    def record(db, cursor, sql, parameters, context, many):
        statements.append((sql, parameters))

    event.listen(state.engine, "before_cursor_execute", record)
    try:
        if method == "start":
            start(state, coordinator)
        elif method == "consume":
            ingest(state, coordinator, capture(1))
        else:
            with state.engine.begin() as db:
                coordinator.invalidate(
                    db, source={}, reference_id="source-change", reason="SOURCE_CONFLICT"
                )
    finally:
        event.remove(state.engine, "before_cursor_execute", record)
    channels = {"alarm-channel:" + c for c, _ in coordinator.channels}
    seen = set()
    first_input = None
    first_channel = None
    for index, (_sql, params) in enumerate(statements):
        key = params.get("key") if isinstance(params, dict) else None
        if isinstance(key, str) and key.startswith("alarm-channel:"):
            first_channel = index if first_channel is None else first_channel
            if first_input is not None:
                assert key in seen  # Re-entering already-owned channel is safe.
            seen.add(key)
        if isinstance(key, str) and key.startswith("alarm-input:"):
            first_input = index if first_input is None else first_input
            assert channels <= seen
    assert first_channel is not None and first_input is not None
    assert any(
        "FROM source_streams" in sql and "FOR UPDATE" in sql
        for sql, _ in statements[:first_channel]
    )
    assert seen == channels


def test_fault_last_channel_failure_rolls_back_original_events_progress_and_both_alarms(state):
    coordinator = start(state)
    ingest(state, coordinator, capture(1, code=123, ceiling=2, events=transitions(1, active=True)))
    tables = (
        "alarm_capture_frontiers",
        "alarm_capture_decisions",
        "alarm_channels",
        "alarm_instances",
        "alarm_receipts",
        "alarm_outbox",
        "ingest_batches",
        "event_inbox",
        "stream_progress",
    )
    before = {t: rows(state, t) for t in tables}

    def fail(db, cursor, sql, params, context, many):
        if sql.startswith("INSERT INTO alarm_capture_decisions") and params.get("c") == _channel(
            LINE, "fault"
        ):
            raise SQLAlchemyError("Injected final capture save failure")

    event.listen(state.engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(SQLAlchemyError):
            ingest(
                state,
                coordinator,
                capture(
                    2,
                    ceiling=4,
                    events=transitions(
                        3, active=False, previous="FAULT", target="STOPPED", version=2
                    ),
                ),
            )
    finally:
        event.remove(state.engine, "before_cursor_execute", fail)
    assert {t: rows(state, t) for t in tables} == before


@pytest.mark.parametrize("version", ["1.0", "1.1"])
def test_unsupported_legacy_captures_never_invent_fault_proof(state, version):
    coordinator = start(state)
    ingest(
        state,
        coordinator,
        capture(1, code=123, ceiling=2, events=transitions(1, active=True), version=version),
    )
    assert {r["disposition"] for r in rows(state, "alarm_capture_decisions")} == {"UNSUPPORTED"}
    assert rows(state, "alarm_instances") == []


def test_source_invalidation_breaks_all_channels_even_after_rule_head_change(state):
    coordinator = start(state)
    replacement = PLCFaultRuleConfig(rule=FAULT.rule.model_copy(update={"version": "f2"}))
    put(state, LINE, replacement, expected="f1", at=AT + timedelta(seconds=1))
    with state.engine.begin() as db:
        db.execute(text("UPDATE source_streams SET conflicted=true"))
        coordinator.invalidate(db, source={}, reference_id="conflict", reason="SOURCE_CONFLICT")
    assert all(r["requires_restart"] for r in rows(state, "alarm_capture_frontiers"))
    # Original runtime cannot emit a fresh observation with an obsolete rule head.
    with pytest.raises(AlarmConsumerUnavailable), state.engine.begin() as db:
        coordinator.consume(db, capture(1), source={}, now=AT)


def test_same_capture_identity_with_changed_original_body_is_rejected(state):
    coordinator = start(state)
    original = capture(1)
    ingest(state, coordinator, original)
    raw = original.model_dump(mode="json")
    next(sample for sample in raw["samples"] if sample["tag_id"] == "Sealer.AirPressure")[
        "reading"
    ]["value"] = 4.9
    changed = BatchBody.model_validate(raw)
    with pytest.raises(AlarmCaptureConflict), state.engine.begin() as db:
        coordinator.consume(db, changed, source={}, now=AT)


def scalar(state, sql, params=None):
    with state.engine.connect() as db:
        return db.execute(text(sql), params or {}).scalar_one()


def wait_advisory(state, pid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if scalar(
            state,
            "SELECT COALESCE(wait_event='advisory',false) FROM pg_stat_activity WHERE pid=:p",
            {"p": pid},
        ):
            return
        time.sleep(0.01)
    pytest.fail("Actual schema advisory lock was not awaited")


def test_actual_start_wins_shared_schema_lock_and_used_downgrade_then_refuses(state):
    initial_revision = scalar(state, "SELECT version_num FROM alembic_version")
    coordinator = AlarmCoordinator(mixed(), consumer_epoch="race-api")
    holding, release, identified = Event(), Event(), Event()
    pid = []

    def hold(db, cursor, sql, params, context, many):
        if sql.startswith("INSERT INTO alarm_capture_frontiers") and not holding.is_set():
            holding.set()
            assert release.wait(timeout=10)

    def downgrade():
        with state.engine.connect() as db:
            pid.append(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
            db.rollback()
            identified.set()
            with pytest.raises(DBAPIError):
                command.downgrade(migration_config(db), "0013_work_order_cancel")

    event.listen(state.engine, "before_cursor_execute", hold)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(start, state, coordinator)
            assert holding.wait(timeout=5)
            migration = pool.submit(downgrade)
            assert identified.wait(timeout=5)
            wait_advisory(state, pid[0])
            release.set()
            writer.result(timeout=10)
            migration.result(timeout=10)
    finally:
        release.set()
        event.remove(state.engine, "before_cursor_execute", hold)
    assert scalar(state, "SELECT version_num FROM alembic_version") == initial_revision


def test_empty_downgrade_wins_and_actual_new_start_refuses_after_waiting_schema(state):
    holding, release, identified = Event(), Event(), Event()
    pid = []

    def hold(db, cursor, sql, params, context, many):
        if sql.lstrip().startswith("DROP INDEX") and "ix_inbox_applied_line_fault_seq" in sql:
            holding.set()
            assert release.wait(timeout=10)

    def writer():
        with state.engine.connect() as db:
            pid.append(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
            db.rollback()
            identified.set()
            with pytest.raises(AlarmConsumerUnavailable), db.begin():
                AlarmCoordinator(mixed()).start(db, now=AT)

    def downgrade():
        with state.engine.connect() as db:
            command.downgrade(migration_config(db), "0013_work_order_cancel")

    event.listen(state.engine, "before_cursor_execute", hold)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            migration = pool.submit(downgrade)
            assert holding.wait(timeout=5)
            pending = pool.submit(writer)
            assert identified.wait(timeout=5)
            wait_advisory(state, pid[0])
            release.set()
            migration.result(timeout=10)
            pending.result(timeout=10)
    finally:
        release.set()
        event.remove(state.engine, "before_cursor_execute", hold)
    assert rows(state, "alarm_capture_frontiers") == []


@pytest.mark.parametrize(
    "change", ["wrong-target", "duplicate", "foreign-policy", "trusted-context"]
)
def test_only_bounded_configuration_is_accepted_not_server_processing_proof(change):
    body = mixed().model_dump(mode="json")
    if change == "wrong-target":
        body["channels"][0]["asset_id"] = "Sealer"
    elif change == "duplicate":
        body["channels"].append(body["channels"][0])
    elif change == "foreign-policy":
        body["clock_policy"]["source_id"] = "foreign"
    else:
        body["classification"] = "LIVE"
    with pytest.raises(ValidationError):
        MixedAlarmConsumerConfig.model_validate(body)


@pytest.mark.parametrize("restriction", ["freshness", "gap-freshness", "config-interval"])
def test_line_unknown_retains_actual_clock_evidence_separately_from_restriction(state, restriction):
    config = mixed()
    if restriction == "config-interval":
        replacement = PLCFaultRuleConfig(rule=FAULT.rule.model_copy(update={"version": "f2"}))
        put(state, LINE, replacement, expected="f1", at=AT + timedelta(seconds=1))
        config = mixed(fault=replacement)
    coordinator = start(state, AlarmCoordinator(config))
    body = capture(
        2 if restriction == "gap-freshness" else 1,
        code=123,
        ceiling=2,
        events=transitions(1, active=True),
    )
    at = body.captured_at
    assessment, _, _ = assess_capture_clock(body, config.clock_policy, at)
    assert assessment is not None
    ingest(state, coordinator, body, now=at)
    line = next(r for r in rows(state, "alarm_channels") if r["asset_id"] == "Line")
    checkpoint = line["qualification"]
    assert checkpoint["last_condition"] == "UNKNOWN"
    assert checkpoint["highest_event_ceiling"] == "2"
    assert checkpoint["last_time_quality"] == "KNOWN"
    decision = next(
        r for r in rows(state, "alarm_capture_decisions") if r["channel_id"] == line["channel_id"]
    )
    assert decision["context"]["clock_assessment"] == assessment.model_dump(mode="json")
    assert rows(state, "alarm_instances") == []
    if restriction == "freshness":
        analog = next(
            r
            for r in rows(state, "alarm_capture_decisions")
            if r["channel_id"] == _channel(SCOPE, "pressure")
        )
        assert analog["context"]["clock_assessment"] is None


def changed_pressure(body):
    value = body.model_dump(mode="json")
    next(s for s in value["samples"] if s["tag_id"] == "Sealer.AirPressure")["reading"]["value"] = (
        4.9
    )
    return BatchBody.model_validate(value)


def test_superseded_subset_collision_atomically_preserves_evidence_and_breaks_current_roster(state):
    old = start(state, AlarmCoordinator(legacy(), consumer_epoch="old-subset"))
    original = capture(1)
    ingest(state, old, original)
    current = start(state)
    decisions = rows(state, "alarm_capture_decisions")
    with pytest.raises(IngestConflict):
        ingest(state, old, changed_pressure(original))
    assert rows(state, "source_streams")[0]["conflicted"]
    assert len(rows(state, "ingest_conflicts")) == 1
    frontiers = rows(state, "alarm_capture_frontiers")
    assert len(frontiers) == 2 and all(r["requires_restart"] for r in frontiers)
    assert {r["consumer_epoch"] for r in frontiers} == {current.consumer_epoch}
    assert rows(state, "alarm_capture_decisions") == decisions
    with pytest.raises(SourceConflicted):
        ingest(state, current, capture(2))
    # A stale generic invalidation remains forbidden even after conflict flags.
    with pytest.raises(AlarmConsumerUnavailable), state.engine.begin() as db:
        old.invalidate(db, source={}, reference_id="untrusted", reason="SOURCE_CONFLICT")


def test_collision_safety_requires_persisted_matching_evidence_and_flag(state):
    coordinator = start(state)
    before = rows(state, "alarm_capture_frontiers"), rows(state, "alarm_receipts")
    with pytest.raises(AlarmConsumerUnavailable), state.engine.begin() as db:
        source = IngestionService._lock_stream(db, "source", "stream")
        coordinator.invalidate_authenticated_collision(
            db, source=source, reference_id="missing", conflict_id=1
        )
    assert (rows(state, "alarm_capture_frontiers"), rows(state, "alarm_receipts")) == before
    with pytest.raises(AlarmConsumerUnavailable), state.engine.begin() as db:
        db.execute(text("UPDATE source_streams SET conflicted=true"))
        source = IngestionService._lock_stream(db, "source", "stream")
        coordinator.invalidate_authenticated_collision(
            db, source=source, reference_id="missing", conflict_id=1
        )
    assert not rows(state, "source_streams")[0]["conflicted"]
    assert (rows(state, "alarm_capture_frontiers"), rows(state, "alarm_receipts")) == before


def test_last_collision_break_failure_rolls_back_source_evidence_and_all_channels(
    state, monkeypatch
):
    import apps.api.alarm_coordinator as module

    old = start(state, AlarmCoordinator(legacy(), consumer_epoch="old-subset"))
    original = capture(1)
    ingest(state, old, original)
    start(state)
    tables = (
        "source_streams",
        "ingest_conflicts",
        "alarm_capture_frontiers",
        "alarm_channels",
        "alarm_receipts",
        "alarm_capture_decisions",
        "alarm_instances",
        "alarm_outbox",
    )
    before = {t: rows(state, t) for t in tables}
    action = module.break_continuity
    calls = []

    def fail_last(*args, **kwargs):
        result = action(*args, **kwargs)
        calls.append(result)
        if len(calls) == 2:
            raise RuntimeError("Injected last current channel failure")
        return result

    monkeypatch.setattr(module, "break_continuity", fail_last)
    with pytest.raises(RuntimeError, match="last current channel"):
        ingest(state, old, changed_pressure(original))
    assert len(calls) == 2
    assert {t: rows(state, t) for t in tables} == before
