"""Real PostgreSQL alarm repository transactions, identity isolation and contention."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from alembic import command
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from apps.api.alarm_lifecycle import (
    AlarmConflict,
    AlarmEvidence,
    AlarmRule,
    AlarmScope,
    QualifiedCondition,
)
from apps.api.alarm_qualification import PressureRuleConfig, RuleObservation
from apps.api.alarm_store import (
    act_on_alarm,
    apply_condition,
    apply_observation,
    break_continuity,
    install_rule,
    read_actions,
    read_instances,
    replace_rule,
)
from apps.api.auth import AuthorizationError, Principal, UserAuth
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.database import migration_config
from apps.api.ingestion import IngestionService
from contracts.transport import TagSample

AT = datetime(2026, 10, 24, tzinfo=UTC)
SCOPE = AlarmScope(line_id=LINE_ID, source_id="source", stream_id="stream", asset_id="Sealer")
PRINCIPAL = Principal("operator", frozenset({"operator", "equipment_admin"}), False)
CONFIG = PressureRuleConfig(rule=AlarmRule(rule_id="pressure", version="v1", severity="HIGH"))


@pytest.fixture
def state(migrated):
    bootstrap_reference_data(migrated)
    UserAuth(migrated, allowed_origins={"https://alarm.test"}).bootstrap_user(
        "operator", "A-test-password-12345!", roles={"operator", "equipment_admin"}
    )
    IngestionService(migrated).configure_source(
        source_id="source",
        stream_id="stream",
        gateway_id="gateway",
        line_id=LINE_ID,
        capture_generation=1,
        now=AT,
    )
    with migrated.begin() as db:
        install_rule(db, SCOPE, CONFIG, input_id="install", principal=PRINCIPAL, at=AT)
    return migrated


def condition(identity, position, value="ACTIVE", alarm="alarm-A", *, historical=False):
    return QualifiedCondition(
        event_id=identity,
        scope=SCOPE,
        rule_version="v1",
        position=position,
        condition=value,
        new_alarm_id=alarm,
        historical=historical,
        evidence=AlarmEvidence(
            observed_at=AT + timedelta(seconds=position), references=(identity,)
        ),
    )


def apply(engine, value):
    with engine.begin() as db:
        return apply_condition(db, "pressure", value)


def action(engine, identity, alarm="alarm-A", **changes):
    with engine.begin() as db:
        return act_on_alarm(
            db,
            SCOPE,
            "pressure",
            input_id=identity,
            alarm_id=alarm,
            principal=PRINCIPAL,
            at=AT + timedelta(seconds=100),
            kind=changes.pop("kind", "ACKNOWLEDGE"),
            **changes,
        )


def instances(engine):
    with engine.begin() as db:
        return read_instances(db, SCOPE, "pressure")


def counts(engine):
    with engine.begin() as db:
        return {
            name: db.execute(text("SELECT count(*) FROM alarm_" + name)).scalar_one()
            for name in ("instances", "actions", "receipts", "outbox")
        }


def test_replay_conflict_unknown_history_and_recurrence(state):
    original = condition("raise", 1)
    result = apply(state, original)
    assert result.notify_current and result.created_alarm_id == "alarm-A"
    old = counts(state)
    retry = apply(state, original)
    assert retry.replayed and not retry.notify_current and counts(state) == old
    with pytest.raises(AlarmConflict):
        apply(state, original.model_copy(update={"condition": "NORMAL"}))
    apply(state, condition("unknown", 2, "UNKNOWN"))
    assert instances(state)[0].status == "ACTIVE_UNACKNOWLEDGED"
    apply(state, condition("return", 3, "NORMAL"))
    apply(state, condition("recur", 4, alarm="alarm-B"))
    action(state, "ack-old")
    by_id = {item.alarm_id: item for item in instances(state)}
    assert by_id["alarm-A"].status == "RETURNED_ACKNOWLEDGED"
    assert by_id["alarm-B"].status == "ACTIVE_UNACKNOWLEDGED"
    history = apply(state, condition("historical", 0, "NORMAL", historical=True))
    assert history.disposition == "HISTORY_ONLY" and not history.notify_current
    assert len(instances(state)) == 2 and counts(state)["outbox"] == 2
    with state.begin() as db:
        assert db.execute(text("SELECT last_position FROM alarm_channels")).scalar_one() == 4
        assert len(read_instances(db, SCOPE, "pressure", limit=1)) == 1
        assert (
            read_instances(db, SCOPE, "pressure", before_alarm_id="alarm-B")[0].alarm_id
            == "alarm-A"
        )
    apply(state, condition("return-again", 5, "NORMAL"))
    with pytest.raises(IntegrityError):
        apply(state, condition("reuse", 6, alarm="alarm-A"))


def test_notes_revision_config_close_and_permissions(state):
    apply(state, condition("raise", 1))
    action(state, "note-create", kind="NOTE_ADD", note_id="note", text_value="before")
    action(
        state,
        "note-edit",
        kind="NOTE_EDIT",
        note_id="note",
        text_value="after",
        expected_previous_action_id="note-create",
    )
    with pytest.raises(AlarmConflict):
        action(
            state,
            "stale",
            kind="NOTE_EDIT",
            note_id="note",
            text_value="stale",
            expected_previous_action_id="note-create",
        )
    assert action(
        state,
        "note-edit",
        kind="NOTE_EDIT",
        note_id="note",
        text_value="after",
        expected_previous_action_id="note-create",
    ).replayed
    replacement = CONFIG.model_copy(
        update={"rule": CONFIG.rule.model_copy(update={"version": "v2"})}
    )
    with state.begin() as db:
        replace_rule(
            db,
            SCOPE,
            replacement,
            input_id="replace",
            expected_version="v1",
            principal=PRINCIPAL,
            at=AT + timedelta(seconds=110),
        )
    with state.begin() as db:
        assert replace_rule(
            db,
            SCOPE,
            replacement,
            input_id="replace",
            expected_version="v1",
            principal=PRINCIPAL,
            at=AT + timedelta(seconds=110),
        ).replayed
        latest = read_actions(db, SCOPE, "pressure", "alarm-A", limit=1)
        assert latest[0].kind == "RULE_CHANGE_CLOSE"
        page = read_actions(
            db, SCOPE, "pressure", "alarm-A", limit=1, before_action_id=latest[0].action_id
        )
        assert page[0].action_id == "note-edit"
    with state.begin() as db:
        checkpoint = db.execute(text("SELECT qualification FROM alarm_channels")).scalar_one()
        assert (
            apply_condition(
                db,
                "pressure",
                condition(
                    "old-installed-version",
                    0,
                    "NORMAL",
                    historical=True,
                ),
            ).disposition
            == "HISTORY_ONLY"
        )
        assert (
            db.execute(text("SELECT qualification FROM alarm_channels")).scalar_one() == checkpoint
        )
    item = instances(state)[0]
    assert item.closure_reason == "CONFIG_CHANGED" and item.return_evidence is None
    assert item.rule.version == "v1"
    with state.begin() as db:
        evidence = db.execute(text("SELECT payload FROM alarm_actions WHERE action_id='note-edit'"))
        assert evidence.scalar_one()["before"] == "before"
    with pytest.raises(AuthorizationError), state.begin() as db:
        act_on_alarm(
            db,
            SCOPE,
            "pressure",
            input_id="raise",
            alarm_id="alarm-A",
            principal=Principal("operator", frozenset({"viewer"}), False),
            at=AT,
            kind="ACKNOWLEDGE",
        )
    with pytest.raises(DBAPIError), state.begin() as db:
        db.execute(text("UPDATE alarm_rule_versions SET config='{}'::jsonb"))
    with pytest.raises(DBAPIError), state.connect() as db:
        command.downgrade(migration_config(db), "0009_work_order_binding")


def observation(position, *, epoch="epoch", value=3.0):
    at = AT + timedelta(seconds=position)
    return RuleObservation(
        observation_id=f"obs-{position}",
        scope=SCOPE,
        rule_version="v1",
        position=position,
        observed_at=at,
        clock_epoch=epoch,
        monotonic_seconds=float(position),
        time_quality="KNOWN",
        phase="PRODUCTION",
        phase_references=("phase-proof",),
        actual=TagSample(
            sample_id=f"sample-{position}",
            tag_id="Sealer.AirPressure",
            acquired_at=at,
            source_timestamp=at,
            server_timestamp=at,
            status_code=0,
            reading={"data_type": "Double", "value": value},
        ),
    )


def observe(engine, position, **changes):
    with engine.begin() as db:
        return apply_observation(
            db, "pressure", observation(position, **changes), new_alarm_id="qualified-A"
        )


def test_qualification_restart_unsupported_gap_and_replay(state):
    observe(state, 1)
    observe(state, 2)
    observe(state, 3, epoch="restarted")
    assert not instances(state)
    observe(state, 4, epoch="restarted")
    with state.begin() as db:
        break_continuity(
            db,
            SCOPE,
            "pressure",
            input_id="legacy",
            position=5,
            reason="UNSUPPORTED_CAPTURE",
            evidence_reference="batch-legacy",
        )
    observe(state, 6, epoch="restarted")
    observe(state, 7, epoch="restarted")
    assert not instances(state)
    observe(state, 8, epoch="restarted")
    raised = observe(state, 9, epoch="restarted")
    assert raised.created_alarm_id == "qualified-A"
    assert observe(state, 9, epoch="restarted").replayed
    # A later epoch cannot clear the already latched active occurrence.
    observe(state, 10, epoch="again", value=5.0)
    assert instances(state)[0].status == "ACTIVE_UNACKNOWLEDGED"
    observe(state, 13, epoch="again", value=5.0)
    assert instances(state)[0].status == "ACTIVE_UNACKNOWLEDGED"
    observe(state, 14, epoch="again", value=5.0)
    observe(state, 15, epoch="again", value=5.0)
    observe(state, 16, epoch="again", value=5.0)
    assert instances(state)[0].status == "RETURNED_UNACKNOWLEDGED"
    assert counts(state)["outbox"] == 1


def test_forced_commit_failure_rolls_back_everything(state):
    before = counts(state)

    def fail(_connection):
        raise RuntimeError("injected commit failure")

    event.listen(state, "commit", fail)
    try:
        with pytest.raises(RuntimeError, match="injected commit"):
            apply(state, condition("raise", 1))
    finally:
        event.remove(state, "commit", fail)
    assert counts(state) == before
    with state.begin() as db:
        assert db.execute(text("SELECT last_position FROM alarm_channels")).scalar_one() is None
    assert apply(state, condition("raise", 1)).notify_current


def test_concurrent_duplicate_and_note_revision_races(state):
    barrier = Barrier(2)

    def racing_raise():
        barrier.wait(timeout=10)
        return apply(state, condition("raise", 1))

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: racing_raise(), range(2)))
    assert sum(result.replayed for result in results) == 1
    assert counts(state)["instances"] == counts(state)["outbox"] == 1
    action(state, "note", kind="NOTE_ADD", note_id="note-id", text_value="initial")
    barrier = Barrier(2)

    def edit(index):
        barrier.wait(timeout=10)
        try:
            action(
                state,
                f"edit-{index}",
                kind="NOTE_EDIT",
                note_id="note-id",
                text_value=str(index),
                expected_previous_action_id="note",
            )
            return "applied"
        except AlarmConflict:
            return "conflict"

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(edit, range(2))) == ["applied", "conflict"]


def test_scope_foreign_keys_and_exact_identity(state):
    wrong = SCOPE.model_copy(update={"line_id": "other-line"})
    with pytest.raises(IntegrityError), state.begin() as db:
        install_rule(db, wrong, CONFIG, input_id="bad-install", principal=PRINCIPAL, at=AT)
    with pytest.raises(AlarmConflict):
        action(state, "missing", alarm="missing")
    apply(state, condition("raise", 1))
    with pytest.raises(AlarmConflict), state.begin() as db:
        act_on_alarm(
            db,
            SCOPE.model_copy(update={"stream_id": "other"}),
            "pressure",
            input_id="wrong-stream",
            alarm_id="alarm-A",
            principal=PRINCIPAL,
            at=AT + timedelta(seconds=2),
            kind="ACKNOWLEDGE",
        )
    with pytest.raises(AlarmConflict), state.begin() as db:
        install_rule(db, wrong, CONFIG, input_id="install", principal=PRINCIPAL, at=AT)


def test_empty_migration_downgrade_upgrade(migrated):
    with migrated.connect() as db:
        command.downgrade(migration_config(db), "0009_work_order_binding")
        command.upgrade(migration_config(db), "0010_alarm_store")
        assert db.execute(text("SELECT count(*) FROM alarm_channels")).scalar_one() == 0


def test_mixed_qualified_input_does_not_allow_older_observation_dwell(state):
    observe(state, 1)
    apply(
        state,
        condition("later-normal", 2, "NORMAL").model_copy(
            update={
                "evidence": AlarmEvidence(
                    observed_at=AT + timedelta(seconds=100), references=("later-proof",)
                )
            }
        ),
    )
    for position in range(3, 8):
        observe(state, position)
    assert not instances(state)
    with state.begin() as db:
        checkpoint = db.execute(text("SELECT qualification FROM alarm_channels")).scalar_one()
        assert checkpoint["candidate"] is None
        assert checkpoint["latched"] == "NORMAL"
    assert observe(state, 101).disposition == "UNKNOWN_EVIDENCE"
    assert not instances(state)


def test_historical_observation_preserves_exact_qualification_checkpoint(state):
    apply(state, condition("current-normal", 100, "NORMAL"))
    with state.begin() as db:
        original = db.execute(text("SELECT qualification FROM alarm_channels")).scalar_one()
    with state.begin() as db:
        result = apply_observation(db, "pressure", observation(1), new_alarm_id="no-history-alarm")
        assert result.disposition == "HISTORY_ONLY"
        assert db.execute(text("SELECT qualification FROM alarm_channels")).scalar_one() == original
    assert not instances(state)


def test_historical_evidence_requires_installed_rule_version(state):
    with pytest.raises(AlarmConflict):
        apply(
            state,
            condition("uninstalled", 1, historical=True).model_copy(
                update={"rule_version": "never-installed"}
            ),
        )


def test_note_edit_requires_exact_owner_even_when_history_is_omitted(state):
    apply(state, condition("raise-A", 1))
    action(state, "note-A", kind="NOTE_ADD", note_id="note-A", text_value="A owns this")
    apply(state, condition("return-A", 2, "NORMAL"))
    apply(state, condition("raise-B", 3, alarm="alarm-B"))
    apply(state, condition("return-B", 4, "NORMAL"))
    apply(state, condition("raise-C", 5, alarm="alarm-C"))
    with pytest.raises(AlarmConflict):
        action(
            state,
            "wrong-owner",
            alarm="alarm-B",
            kind="NOTE_EDIT",
            note_id="note-A",
            text_value="cannot move",
            expected_previous_action_id="note-A",
        )


def test_rule_replacement_cannot_precede_installed_version_and_original_replay_stays_valid(state):
    replacement = CONFIG.model_copy(
        update={"rule": CONFIG.rule.model_copy(update={"version": "v2"})}
    )
    with pytest.raises(AlarmConflict):
        with state.begin() as db:
            replace_rule(
                db,
                SCOPE,
                replacement,
                input_id="too-early",
                expected_version="v1",
                principal=PRINCIPAL,
                at=AT - timedelta(microseconds=1),
            )
    with state.connect() as db:
        assert db.execute(text("SELECT current_version FROM alarm_channels")).scalar_one() == "v1"
        assert db.execute(text("SELECT count(*) FROM alarm_rule_versions")).scalar_one() == 1
        assert (
            db.execute(
                text("SELECT count(*) FROM alarm_receipts WHERE input_id='too-early'")
            ).scalar_one()
            == 0
        )
    # Equal timestamps retain the existing ambiguity/quarantine policy; this
    # guard does not silently introduce a new strict-increase configuration rule.
    with state.begin() as db:
        result = replace_rule(
            db,
            SCOPE,
            replacement,
            input_id="equal-time",
            expected_version="v1",
            principal=PRINCIPAL,
            at=AT,
        )
    assert result.disposition == "APPLIED"
    third = CONFIG.model_copy(update={"rule": CONFIG.rule.model_copy(update={"version": "v3"})})
    with state.begin() as db:
        replace_rule(
            db,
            SCOPE,
            third,
            input_id="newest",
            expected_version="v2",
            principal=PRINCIPAL,
            at=AT + timedelta(seconds=1),
        )
    with state.begin() as db:
        replay = replace_rule(
            db,
            SCOPE,
            replacement,
            input_id="equal-time",
            expected_version="v1",
            principal=PRINCIPAL,
            at=AT,
        )
    assert replay.replayed
    with state.connect() as db:
        assert db.execute(text("SELECT current_version FROM alarm_channels")).scalar_one() == "v3"
