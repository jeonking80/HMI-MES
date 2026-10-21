"""Opt-in authenticated ingestion to atomic Sealer analog alarm persistence.

Positive clock tests explicitly assume API and gateway share one trusted kernel
UTC reference with declared ±0.01 s uncertainty. They do not verify remote clocks.
"""

import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from types import SimpleNamespace

import httpx
import pytest
from alembic import command
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from apps.api.alarm_consumer import (
    AlarmCaptureConflict,
    AlarmConsumer,
    AlarmConsumerConfig,
    AlarmConsumerUnavailable,
    ConfiguredClockPolicy,
    assess_capture_clock,
)
from apps.api.alarm_lifecycle import AlarmConflict, AlarmRule, AlarmScope
from apps.api.alarm_qualification import PressureRuleConfig, TemperatureRuleConfig
from apps.api.alarm_store import act_on_alarm, install_rule, read_instances, replace_rule
from apps.api.auth import Principal, UserAuth
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.database import migration_config
from apps.api.ingestion import IngestConflict, IngestionService
from apps.api.internal import InternalConfig, ServiceCredential, create_internal_app
from contracts.tags.validate import load_contract
from contracts.transport import BatchBody, GatewayBatch, TagSample, batch_digest

AT = datetime(2026, 10, 25, tzinfo=UTC)
SCOPE = AlarmScope(line_id=LINE_ID, source_id="source", stream_id="stream", asset_id="Sealer")
ADMIN = Principal("admin", frozenset({"operator", "equipment_admin"}), False)
PRESSURE = PressureRuleConfig(rule=AlarmRule(rule_id="pressure", version="p1", severity="HIGH"))
TEMPERATURE = TemperatureRuleConfig(
    rule=AlarmRule(rule_id="temperature", version="t1", severity="WARNING"), setpoint_c=150.0
)


def policy(**changes):
    return ConfiguredClockPolicy(
        **(
            {
                "policy_id": "shared-host",
                "policy_version": "v1",
                "reference_id": "test-shared-kernel",
                "method": "CONFIGURED_API_REFERENCE",
                "guarantee_kind": "SAME_HOST_SHARED_KERNEL_UTC",
                "guarantee_basis": "Test assumption: shared trusted host clock; ±0.01s bound",
                "gateway_id": "gateway",
                "source_id": "source",
                "stream_id": "stream",
                "capture_generation": 1,
                "clock_epoch": "gateway-epoch",
                "valid_from_api_utc": AT - timedelta(days=1),
                "valid_until_api_utc": AT + timedelta(days=1),
                "offset_lower_seconds": -0.01,
                "offset_upper_seconds": 0.01,
            }
            | changes
        )
    )


def config(*, clock=True, rules=(PRESSURE, TEMPERATURE)):
    return AlarmConsumerConfig(
        scope=SCOPE,
        gateway_id="gateway",
        capture_generation=1,
        rules=rules,
        clock_policy=policy() if clock else None,
    )


def capture(
    seq,
    *,
    seconds=None,
    epoch="gateway-epoch",
    span=0.02,
    values=None,
    version="1.2",
    gaps=(),
    batch_id=None,
    communication_age=0.0,
):
    seconds = seq if seconds is None else seconds
    at = AT + timedelta(seconds=seconds)
    overrides = {
        "Line.State": 2,
        "Line.OperationMode": 1,
        "Line.ActiveExecutionId": "run",
        "Sealer.State": 2,
        "Sealer.RunFeedback": True,
        "Sealer.AirPressure": 3.0,
        "Sealer.TemperaturePV": 165.0,
        "Sealer.TemperatureSP": 150.0,
    } | (values or {})
    samples = []
    for tag in load_contract()["tags"]:
        name, kind = tag["logical_name"], tag["data_type"]
        value = {
            "Boolean": False,
            "Int32": 0,
            "UInt32": 0,
            "UInt64": 0,
            "Double": 0.0,
            "String": "",
        }[kind]
        if tag["enum"]:
            value = int(next(iter(load_contract()["enums"][tag["enum"]])))
        samples.append(
            TagSample(
                sample_id=f"{name}-{seq}",
                tag_id=name,
                acquired_at=at,
                source_timestamp=AT - timedelta(days=20),
                server_timestamp=None,
                status_code=0,
                reading={"data_type": kind, "value": overrides.get(name, value)},
            )
        )
    values = dict(
        schema_version=version,
        batch_id=batch_id or f"batch-{seq}",
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        capture_generation=1,
        capture_seq=seq,
        captured_at=at,
        samples=samples,
        events=(),
        gaps=gaps,
    )
    if version != "1.0":
        values["communication"] = {
            "state": "ALARM"
            if communication_age >= 5.0
            else "DELAYED"
            if communication_age >= 3.0
            else "GOOD",
            "connected": True,
            "read_age_seconds": 0.0,
            "heartbeat_ages_seconds": {
                name: communication_age
                for name in (
                    "Line.Heartbeat",
                    "Infeed.LinkHeartbeat",
                    "Sealer.LinkHeartbeat",
                    "Outfeed.LinkHeartbeat",
                )
            },
        }
    if version == "1.2":
        values["capture_time"] = {
            "clock_epoch": epoch,
            "monotonic_seconds": float(seconds + 1000),
            "utc_read_span_seconds": span,
        }
    body = BatchBody.model_validate(values)
    return GatewayBatch(body=body, content_sha256=batch_digest(body))


def ingest(state, batch, *, now=None, service=None):
    return (service or state.service).ingest(
        batch,
        authenticated_gateway_id="gateway",
        authenticated_source_id="source",
        authenticated_stream_id="stream",
        now=now or batch.body.captured_at + timedelta(seconds=0.5),
    )


def start(engine, consumer, at=AT):
    with engine.begin() as db:
        consumer.start(db, now=at)


def rows(state, table):
    with state.engine.begin() as db:
        return db.execute(text("SELECT * FROM " + table)).mappings().all()


def alarms(state, rule="pressure"):
    with state.engine.begin() as db:
        return read_instances(db, SCOPE, rule)


@pytest.fixture
def state(migrated):
    bootstrap_reference_data(migrated)
    UserAuth(migrated, allowed_origins={"https://alarm.test"}).bootstrap_user(
        "admin", "A-test-password-12345!", roles={"operator", "equipment_admin"}
    )
    service = IngestionService(migrated)
    service.configure_source(
        source_id="source",
        stream_id="stream",
        gateway_id="gateway",
        line_id=LINE_ID,
        capture_generation=1,
        now=AT - timedelta(hours=1),
    )
    with migrated.begin() as db:
        for rule in (PRESSURE, TEMPERATURE):
            install_rule(
                db,
                SCOPE,
                rule,
                input_id="install-" + rule.rule.rule_id,
                principal=ADMIN,
                at=AT - timedelta(minutes=1),
            )
    consumer = AlarmConsumer(config(), consumer_epoch="api-epoch1")
    start(migrated, consumer)
    return SimpleNamespace(
        engine=migrated,
        consumer=consumer,
        service=IngestionService(migrated, alarm_consumer=consumer),
    )


def test_ingestion_same_transaction_analog_occurrence_replay_and_history(state, monkeypatch):
    for seq in range(1, 8):
        ingest(state, capture(seq))
    assert len(alarms(state)) == len(alarms(state, "temperature")) == 1
    assert len(rows(state, "alarm_outbox")) == 2
    before = rows(state, "alarm_capture_frontiers")

    def no_regeneration(*_args, **_kwargs):
        raise AssertionError("A committed retry must not evaluate the clock/config context again")

    monkeypatch.setattr("apps.api.alarm_consumer.assess_capture_clock", no_regeneration)
    original = ingest(state, capture(7))
    assert original.batch_id == "batch-7"
    with state.engine.begin() as db:
        decisions = state.consumer.consume(
            db, capture(7).body, source={}, now=AT + timedelta(days=2)
        )
        assert all(item.replayed and not item.result.notify_current for item in decisions)
    assert rows(state, "alarm_capture_frontiers") == before
    assert len(rows(state, "alarm_outbox")) == 2
    monkeypatch.undo()
    ingest(state, capture(8), now=AT + timedelta(seconds=100))
    assert {
        r["disposition"]
        for r in rows(state, "alarm_capture_decisions")
        if r["batch_id"] == "batch-8"
    } == {"HISTORY_ONLY"}
    assert len(rows(state, "alarm_outbox")) == 2


def test_unknown_time_legacy_and_restart_cannot_bridge_dwell(state):
    ingest(state, capture(1))
    ingest(state, capture(2))
    ingest(state, capture(3, version="1.1"))
    for seq in (4, 5, 6):
        ingest(state, capture(seq))
    assert not alarms(state)
    replacement = AlarmConsumer(config(), consumer_epoch="api-epoch2")
    start(state.engine, replacement, AT + timedelta(seconds=6.5))
    state.service = IngestionService(state.engine, alarm_consumer=replacement)
    for seq in (7, 8, 9):
        ingest(state, capture(seq))
    assert not alarms(state)
    for seq in (10, 11):
        ingest(state, capture(seq))
    assert len(alarms(state)) == 1
    active = alarms(state)[0]
    unknown = AlarmConsumer(config(clock=False), consumer_epoch="api-no-clock")
    start(state.engine, unknown, AT + timedelta(seconds=11.5))
    state.service = IngestionService(state.engine, alarm_consumer=unknown)
    ingest(state, capture(12, values={"Sealer.AirPressure": 5.0}))
    assert alarms(state)[0] == active
    with pytest.raises(AlarmConsumerUnavailable):
        start(state.engine, state.consumer, AT + timedelta(seconds=12.5))


def test_commit_failure_rolls_raw_context_cursor_checkpoint_and_outbox_back(state):
    for seq in range(1, 5):
        ingest(state, capture(seq))
    tables = (
        "ingest_batches",
        "alarm_capture_decisions",
        "alarm_capture_frontiers",
        "alarm_channels",
        "alarm_instances",
        "alarm_receipts",
        "alarm_outbox",
    )
    before = {table: rows(state, table) for table in tables}

    def fail(_connection):
        raise RuntimeError("injected commit failure")

    event.listen(state.engine, "commit", fail)
    try:
        with pytest.raises(RuntimeError, match="injected commit"):
            ingest(state, capture(5))
    finally:
        event.remove(state.engine, "commit", fail)
    assert {table: rows(state, table) for table in tables} == before
    ingest(state, capture(5))
    assert len(alarms(state)) == 1


def test_epoch_takeover_fence_and_older_capture_do_not_rewind(state):
    ingest(state, capture(5))
    newer = AlarmConsumer(config(), consumer_epoch="new-runtime")
    start(state.engine, newer)
    with pytest.raises(AlarmConsumerUnavailable):
        start(state.engine, state.consumer)
    with pytest.raises(AlarmConsumerUnavailable):
        ingest(state, capture(6))
    assert len(rows(state, "ingest_batches")) == 1
    state.service = IngestionService(state.engine, alarm_consumer=newer)
    ingest(state, capture(4))
    assert {
        row["disposition"]
        for row in rows(state, "alarm_capture_decisions")
        if row["batch_id"] == "batch-4"
    } == {"HISTORY_ONLY"}
    with state.engine.begin() as db:
        frontiers = db.execute(text("SELECT * FROM alarm_capture_frontiers")).mappings().all()
        assert all(int(row["highest_capture_seq"]) == 5 for row in frontiers)


def test_collision_invalidation_and_gap_require_explicit_recovery(state):
    ingest(state, capture(1))
    changed = capture(1, values={"Sealer.AirPressure": 2.0})
    with pytest.raises(IngestConflict):
        ingest(state, changed)
    assert all(row["requires_restart"] for row in rows(state, "alarm_capture_frontiers"))
    with state.engine.begin() as db:
        db.execute(text("UPDATE source_streams SET conflicted=false"))
    ingest(state, capture(2))
    assert {
        row["disposition"]
        for row in rows(state, "alarm_capture_decisions")
        if row["batch_id"] == "batch-2"
    } == {"QUARANTINED"}
    assert not alarms(state)
    recovery = AlarmConsumer(config(), consumer_epoch="explicit-recovery")
    start(state.engine, recovery)
    state.service = IngestionService(state.engine, alarm_consumer=recovery)
    gap = {
        "from_seq": 1,
        "through_seq": 2,
        "irrecoverable": True,
        "detected_at": AT + timedelta(seconds=3),
        "reason_code": "SOURCE_RETENTION_GAP",
        "reason": "Explicit original retention gap",
    }
    ingest(state, capture(3, gaps=(gap,)))
    assert all(row["requires_restart"] for row in rows(state, "alarm_capture_frontiers"))
    with pytest.raises(AlarmConsumerUnavailable):
        start(state.engine, AlarmConsumer(config(), consumer_epoch="cannot-hide-gap"))


def test_duplicate_concurrency_and_exact_decision_foreign_keys(state):
    barrier = Barrier(2)

    def run(_):
        barrier.wait(timeout=10)
        return ingest(state, capture(1))

    with ThreadPoolExecutor(2) as pool:
        receipts = list(pool.map(run, range(2)))
    assert receipts[0] == receipts[1]
    assert len(rows(state, "alarm_capture_decisions")) == 2
    with pytest.raises(AlarmCaptureConflict), state.engine.begin() as db:
        state.consumer.consume(
            db,
            capture(1, values={"Sealer.AirPressure": 2.0}).body,
            source={},
            now=AT + timedelta(seconds=2),
        )
    with pytest.raises(DBAPIError), state.engine.begin() as db:
        db.execute(text("UPDATE alarm_capture_decisions SET reason='rewritten'"))
    with pytest.raises(IntegrityError), state.engine.begin() as db:
        db.execute(text("UPDATE alarm_capture_frontiers SET gateway_id='wrong'"))


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"clock_epoch": "other"}, "CLOCK_BINDING_MISMATCH"),
        ({"valid_until_api_utc": AT}, "CLOCK_POLICY_EXPIRED"),
        ({"offset_lower_seconds": -2.0, "offset_upper_seconds": 2.0}, "CLOCK_BOUND_UNKNOWN"),
    ],
)
def test_clock_assumptions_never_inferred_or_silently_extended(change, reason):
    batch = capture(1).body
    assessment, interval, actual = assess_capture_clock(
        batch, policy(**change), AT + timedelta(seconds=1.5)
    )
    assert assessment is interval is None and actual == reason


def test_read_bracket_and_policy_validity_interval():
    batch = capture(1, span=0.2).body
    assessment, interval, _ = assess_capture_clock(batch, policy(), AT + timedelta(seconds=1.5))
    assert assessment.offset_lower_seconds == pytest.approx(-0.21)
    assert assessment.offset_upper_seconds == pytest.approx(0.21)
    assert interval[0] <= AT + timedelta(seconds=0.79)
    assert interval[1] >= AT + timedelta(seconds=1.21)
    assert assess_capture_clock(batch, None, AT)[0] is None


def test_temperature_changed_recipe_setpoint_stays_unknown_preserves_active(state):
    for seq in range(1, 8):
        ingest(state, capture(seq))
    before = alarms(state, "temperature")[0]
    for seq in range(8, 15):
        ingest(
            state,
            capture(seq, values={"Sealer.TemperatureSP": 160.0, "Sealer.TemperaturePV": 160.0}),
        )
    assert alarms(state, "temperature")[0] == before
    with state.engine.begin() as db:
        checkpoint = db.execute(
            text("SELECT qualification FROM alarm_channels WHERE rule_id='temperature'")
        ).scalar_one()
        assert checkpoint["last_reason"] == "SETPOINT_VERSION_MISMATCH"
        assert checkpoint["candidate"] is None


def test_empty_0011_downgrade_and_used_state_refusal(state):
    with pytest.raises(DBAPIError), state.engine.connect() as db:
        command.downgrade(migration_config(db), "0010_alarm_store")


def test_missing_raw_capture_sequence_cannot_bridge_short_monotonic_gap(state):
    for seq in (1, 2, 4, 5, 6):
        ingest(state, capture(seq))
    assert not alarms(state)
    assert {
        row["reason"]
        for row in rows(state, "alarm_capture_decisions")
        if row["batch_id"] == "batch-4"
    } == {"CAPTURE_SEQUENCE_GAP"}


def test_frozen_heartbeat_good_samples_cannot_qualify_or_return(state):
    for seq in (1, 2):
        ingest(state, capture(seq))
    for seq in range(3, 8):
        ingest(state, capture(seq, communication_age=5.0))
    assert not alarms(state) and not alarms(state, "temperature")
    for seq in range(8, 15):
        ingest(state, capture(seq))
    active = alarms(state)[0]
    for seq in range(15, 21):
        ingest(state, capture(seq, communication_age=5.0, values={"Sealer.AirPressure": 5.0}))
    assert alarms(state)[0] == active


def test_stored_capture_retry_survives_changed_rule_but_new_input_requires_runtime_reload(state):
    ingest(state, capture(1))
    newer = PRESSURE.model_copy(update={"rule": PRESSURE.rule.model_copy(update={"version": "p2"})})
    with state.engine.begin() as db:
        replace_rule(
            db,
            SCOPE,
            newer,
            input_id="replace-pressure",
            expected_version="p1",
            principal=ADMIN,
            at=AT + timedelta(seconds=2),
        )
    assert ingest(state, capture(1)).batch_id == "batch-1"
    with pytest.raises(AlarmConsumerUnavailable):
        ingest(state, capture(2))
    assert len(rows(state, "ingest_batches")) == 1
    current = AlarmConsumer(config(rules=(newer, TEMPERATURE)), consumer_epoch="updated-config")
    start(state.engine, current, AT + timedelta(seconds=3))
    old = capture(2, seconds=1.5)
    ingest(
        state,
        old,
        service=IngestionService(state.engine, alarm_consumer=current),
        now=AT + timedelta(seconds=2.1),
    )
    assert any(
        row["rule_version"] == "p1" and row["disposition"] == "HISTORY_ONLY"
        for row in rows(state, "alarm_capture_decisions")
        if row["batch_id"] == "batch-2"
    )


@pytest.mark.asyncio
async def test_authenticated_http_positive_path_and_stale_epoch_safe_503(state):
    token = secrets.token_urlsafe(32)
    from apps.api.internal import _token

    binding = ServiceCredential(
        key_id="test-key",
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        token_digest=_token(token),
        expires_at=AT + timedelta(days=1),
    )
    clock = [AT + timedelta(seconds=1.5)]
    app = create_internal_app(
        state.service, credential_bindings=[binding], config=InternalConfig(clock=lambda: clock[0])
    )
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer " + token, "X-Service-Key-Id": "test-key"}
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as client:
        assert (
            await client.post("/internal/v1/batches", json=capture(1).model_dump(mode="json"))
        ).status_code == 401
        for seq in range(1, 8):
            clock[0] = AT + timedelta(seconds=seq + 0.5)
            response = await client.post(
                "/internal/v1/batches", json=capture(seq).model_dump(mode="json"), headers=headers
            )
            assert response.status_code == 200, response.text
        assert len(alarms(state)) == len(alarms(state, "temperature")) == 1
        current = AlarmConsumer(config(), consumer_epoch="http-takeover")
        start(state.engine, current, AT + timedelta(seconds=8))
        clock[0] = AT + timedelta(seconds=8.5)
        response = await client.post(
            "/internal/v1/batches", json=capture(8).model_dump(mode="json"), headers=headers
        )
        assert response.status_code == 503
        assert len(rows(state, "ingest_batches")) == 7
        assert response.json() == {"detail": "ingestion unavailable"}


def test_empty_0011_migration_replay(migrated):
    with migrated.connect() as db:
        command.downgrade(migration_config(db), "0010_alarm_store")
        command.upgrade(migration_config(db), "0011_alarm_capture_context")
        assert db.execute(text("SELECT count(*) FROM alarm_capture_decisions")).scalar_one() == 0


@pytest.mark.parametrize("bad_communication", [False, True])
def test_delayed_forward_gap_or_bad_communication_preserves_exact_live_checkpoint(
    state, bad_communication
):
    ingest(state, capture(1))
    before = {
        row["rule_id"]: (row["last_position"], row["last_observed_at"], row["qualification"])
        for row in rows(state, "alarm_channels")
    }
    ingest(
        state,
        capture(
            3 if not bad_communication else 2, communication_age=5.0 if bad_communication else 0.0
        ),
        now=AT + timedelta(seconds=10),
    )
    after = {
        row["rule_id"]: (row["last_position"], row["last_observed_at"], row["qualification"])
        for row in rows(state, "alarm_channels")
    }
    assert before == after
    assert len(rows(state, "alarm_capture_decisions")) == 4


def test_equal_configuration_timestamps_quarantine_instead_of_guessing_version(state):
    newer = PRESSURE.model_copy(update={"rule": PRESSURE.rule.model_copy(update={"version": "p2"})})
    with state.engine.begin() as db:
        replace_rule(
            db,
            SCOPE,
            newer,
            input_id="tied-rule-change",
            expected_version="p1",
            principal=ADMIN,
            at=AT - timedelta(minutes=1),
        )
    current = AlarmConsumer(config(rules=(newer, TEMPERATURE)), consumer_epoch="tied-config")
    start(state.engine, current)
    ingest(state, capture(1), service=IngestionService(state.engine, alarm_consumer=current))
    pressure_channel = next(
        row["channel_id"] for row in rows(state, "alarm_channels") if row["rule_id"] == "pressure"
    )
    decision = next(
        row
        for row in rows(state, "alarm_capture_decisions")
        if row["channel_id"] == pressure_channel
    )
    assert decision["disposition"] == "QUARANTINED"
    assert decision["reason"] == "NO_UNAMBIGUOUS_INSTALLED_CONFIG"
    assert not alarms(state)


def test_allocator_exhaustion_rolls_back_new_raw_batch_and_all_channel_decisions(state):
    with state.engine.begin() as db:
        db.execute(
            text("UPDATE alarm_capture_frontiers SET allocator_position=:limit"),
            {"limit": 2**64 - 1},
        )
    before = rows(state, "alarm_capture_frontiers")
    with pytest.raises(AlarmConsumerUnavailable):
        ingest(state, capture(1))
    assert not rows(state, "ingest_batches")
    assert not rows(state, "alarm_capture_decisions")
    assert rows(state, "alarm_capture_frontiers") == before


def test_complete_channel_prelock_rejects_operator_input_collision_without_deadlock(
    state, monkeypatch
):
    """A chosen global input ID must conflict rather than invert input/channel locks."""
    first_channel, _first_config = state.consumer.channels[0]
    _second_channel, second_config = state.consumer.channels[1]
    after_first = Event()
    operator_waiting_for_input = Event()
    shared = {}
    original_save = state.consumer._save

    def save(db, channel, batch, decision):
        original_save(db, channel, batch, decision)
        if channel == first_channel:
            shared["input_id"] = decision.result.input_id
            after_first.set()
            # With complete prelocking the operator waits on channel B first.
            # With the old order it reaches input A and forms a PostgreSQL deadlock.
            operator_waiting_for_input.wait(timeout=1.0)

    def watch(_db, _cursor, _statement, parameters, _context, _many):
        if isinstance(parameters, dict) and parameters.get("key") == "alarm-input:" + shared.get(
            "input_id", ""
        ):
            operator_waiting_for_input.set()

    def submit():
        return ingest(state, capture(1))

    def operator():
        assert after_first.wait(timeout=10)
        try:
            with state.engine.begin() as db:
                db.execute(text("SET LOCAL deadlock_timeout='100ms'"))
                return act_on_alarm(
                    db,
                    SCOPE,
                    second_config.rule.rule_id,
                    input_id=shared["input_id"],
                    alarm_id="missing",
                    principal=ADMIN,
                    at=AT + timedelta(seconds=1),
                    kind="ACKNOWLEDGE",
                )
        except AlarmConflict as error:
            return error

    monkeypatch.setattr(state.consumer, "_save", save)
    event.listen(state.engine, "before_cursor_execute", watch)
    try:
        with ThreadPoolExecutor(2) as pool:
            ingestion = pool.submit(submit)
            action = pool.submit(operator)
            assert ingestion.result(timeout=15).batch_id == "batch-1"
            assert isinstance(action.result(timeout=15), AlarmConflict)
    finally:
        event.remove(state.engine, "before_cursor_execute", watch)
    assert len(rows(state, "alarm_capture_decisions")) == 2
    assert len(rows(state, "ingest_batches")) == 1
