"""Seven-day prefix retention, independent recovery evidence and real process exits."""

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from apps.simulator.process import ProcessConfig, fresh_execution, step
from apps.simulator.storage import (
    RetentionBlocked,
    SimulatorStorage,
    StateEventInput,
    StorageCorruption,
)
from contracts.models import UINT64_MAX, ReadEventsRequest

AT = datetime(2026, 10, 21, tzinfo=UTC)
WEEK = 7 * 24 * 60 * 60
ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self):
        self.utc = AT
        self.mono = 0.0

    def advance(self, seconds):
        self.utc += timedelta(seconds=seconds)
        self.mono += seconds


def open_store(path, clock, **kwargs):
    return SimulatorStorage(
        path, utc_clock=lambda: clock.utc, monotonic_clock=lambda: clock.mono, **kwargs
    )


def control(store):
    return store.append_control_events(
        (
            StateEventInput(
                occurred_at=AT - timedelta(days=100),
                payload={"previous_state": "STOPPED", "state": "READY", "control_version": 1},
            ),
        )
    )[0]


def read(store, after=0):
    return store.read_events(
        ReadEventsRequest(
            source_id=store.identity.source_id, stream_id=store.identity.stream_id, after_seq=after
        )
    )


def allowed():
    pass


@pytest.fixture
def ready(tmp_path):
    clock = Clock()
    with open_store(tmp_path / "state.sqlite3", clock) as store:
        store.acknowledge_recovery()
        yield store, clock


def test_retention_requires_explicit_guard_and_seven_days_of_trusted_elapsed_time(ready):
    store, clock = ready
    event = control(store)
    with pytest.raises(TypeError):
        store.prune_events()
    clock.advance(WEEK - 0.001)
    with pytest.raises(RetentionBlocked, match="STARTUP"):
        store.prune_events(controller_guard=allowed)
    clock.advance(0.001)
    result = store.prune_events(controller_guard=allowed)
    assert result.pruned_count == result.pruned_through_seq == 1
    assert result.retained_from_seq == 2
    gap = read(store)
    assert (gap.code, gap.missing_from_seq, gap.missing_through_seq) == ("RETENTION_GAP", 1, 1)
    assert read(store, 1).events == ()
    store.acknowledge_recovery()
    assert control(store).seq == event.seq + 1


def test_prefix_limit_keeps_recent_events_and_uses_recording_not_occurrence_time(ready):
    store, clock = ready
    for _ in range(3):
        control(store)
    clock.advance(WEEK)
    recent = control(store)
    result = store.prune_events(controller_guard=allowed, limit=2)
    assert result.pruned_count == 2
    assert [event.seq for event in read(store, 2).events] == [3, recent.seq]
    assert store.prune_events(controller_guard=allowed).pruned_count == 1
    assert read(store, 3).events == (recent,)


def test_controller_guard_is_fresh_inside_same_transaction_and_rolls_back(ready):
    store, clock = ready
    control(store)
    clock.advance(WEEK)
    calls = []

    def blocked():
        assert store.connection.in_transaction
        calls.append(store.transaction_generation)
        raise RetentionBlocked("UNFINISHED_OR_UNRESOLVED")

    with pytest.raises(RetentionBlocked, match="UNFINISHED"):
        store.prune_events(controller_guard=blocked)
    assert len(calls) == 1
    assert len(read(store).events) == 1
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.prune_events(controller_guard=allowed)
            raise RuntimeError("outer rollback")
    assert len(read(store).events) == 1
    store.acknowledge_recovery()


@pytest.mark.parametrize("change", ["forward", "backward", "monotonic"])
def test_clock_disagreement_freezes_retention_until_restart_and_new_startup_hold(ready, change):
    store, clock = ready
    control(store)
    clock.advance(WEEK)
    if change == "monotonic":
        clock.mono = -1.0
    else:
        clock.utc += timedelta(days=30 if change == "forward" else -30)
    with pytest.raises(RetentionBlocked, match="CLOCK"):
        store.prune_events(controller_guard=allowed)
    clock.utc = AT + timedelta(seconds=WEEK)
    clock.mono = WEEK
    with pytest.raises(RetentionBlocked, match="CLOCK"):
        store.prune_events(controller_guard=allowed)
    assert len(read(store).events) == 1
    # A frozen deletion clock must not stop new source history collection.
    assert control(store).seq == 2
    assert len(read(store).events) == 2
    path = store.path
    store.close()
    with open_store(path, clock) as reopened:
        reopened.acknowledge_recovery()
        with pytest.raises(RetentionBlocked, match="STARTUP"):
            reopened.prune_events(controller_guard=allowed)


def test_archived_production_totals_recover_and_item_event_collisions_remain_blocked(
    ready, monkeypatch
):
    store, clock = ready
    state = fresh_execution(None, "execution-A", config=ProcessConfig(stage_lengths_mm=(1, 1, 1)))
    store.register_execution(state)
    events = []
    for tick in range(6):
        result = step(state, run=True, admit=tick < 3)
        events.extend(store.apply_transition(state, result, occurred_at=AT))
        state = result.state
    assert state.wip_quantity == 0 and len(events) == 3
    row = dict(store.connection.execute("SELECT * FROM source_events ORDER BY seq").fetchone())
    clock.advance(WEEK)
    assert store.prune_events(controller_guard=allowed).pruned_count == 3
    store.acknowledge_recovery()
    assert store.load_execution() == state
    monkeypatch.setattr("apps.simulator.storage.uuid4", lambda: UUID(events[0].event_id))
    with pytest.raises(sqlite3.IntegrityError):
        control(store)
    body = json.loads(row["body"])
    body["event_id"] = "another-event"
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "INSERT INTO source_events VALUES (?,?,?,?,?,?,?)",
            (
                "another-event",
                row["source_id"],
                row["stream_id"],
                "00000000000000000004",
                row["execution_id"],
                row["item_id"],
                json.dumps(body),
            ),
        )
    store.acknowledge_recovery()


@pytest.mark.parametrize("target", ["checkpoint", "archive", "recorded_time"])
def test_corrupted_retention_evidence_blocks_recovery(ready, target):
    store, clock = ready
    control(store)
    clock.advance(WEEK)
    if target == "recorded_time":
        store.connection.execute("DROP TRIGGER retention_age_no_update")
        store.connection.execute("UPDATE event_retention SET recorded_at='invalid'")
    else:
        store.prune_events(controller_guard=allowed)
        if target == "checkpoint":
            store.connection.execute(
                "UPDATE retention_checkpoint SET pruned_through_seq='00000000000000000000'"
            )
        else:
            store.connection.execute("DROP TRIGGER archived_keys_no_delete")
            store.connection.execute("DELETE FROM archived_event_keys")
    with pytest.raises(StorageCorruption):
        store.acknowledge_recovery()
    assert store.recovery_required


@pytest.mark.parametrize(
    "seam,expected", [("_before_retention_commit", 0), ("_after_retention_commit", 1)]
)
def test_actual_process_exit_preserves_atomic_prefix_archive_and_boundary(tmp_path, seam, expected):
    path = tmp_path / "crash.sqlite3"
    code = f"""
import os
from datetime import UTC, datetime, timedelta
from apps.simulator.storage import SimulatorStorage, StateEventInput
at = datetime(2026,10,21,tzinfo=UTC)
clock = [at, 0.0]
store = SimulatorStorage({str(path)!r}, utc_clock=lambda:clock[0],monotonic_clock=lambda:clock[1])
store.acknowledge_recovery()
store.append_control_events((StateEventInput(occurred_at=at,payload={{"previous_state":"STOPPED","state":"READY","control_version":1}}),))
clock[0] += timedelta(days=7)
clock[1] += 604800
store.{seam} = lambda: os._exit(73)
store.prune_events(controller_guard=lambda:None)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, check=False, capture_output=True, text=True
    )
    assert result.returncode == 73, result.stderr
    clock = Clock()
    with open_store(path, clock) as store:
        store.acknowledge_recovery()
        assert (
            store.connection.execute("SELECT count(*) FROM archived_event_keys").fetchone()[0]
            == expected
        )
        assert (
            store.connection.execute("SELECT count(*) FROM source_events").fetchone()[0]
            == 1 - expected
        )
        assert store.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not store.connection.execute("PRAGMA foreign_key_check").fetchall()


def legacy_v2(path):
    clock = Clock()
    with open_store(path, clock) as store:
        store.acknowledge_recovery()
        event = control(store)
        store.connection.execute("CREATE TABLE companion (value TEXT)")
        store.connection.execute("INSERT INTO companion VALUES ('preserve')")
        for row in store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND "
            "(tbl_name IN ('source_events','archived_event_keys',"
            "'event_retention','retention_checkpoint'))"
        ).fetchall():
            store.connection.execute(f'DROP TRIGGER "{row[0]}"')
        for table in (
            "event_retention",
            "execution_checkpoints",
            "archived_event_keys",
            "retention_checkpoint",
        ):
            store.connection.execute(f"DROP TABLE {table}")
        store.connection.execute("PRAGMA user_version=2")
    return event


def test_v2_migration_preserves_other_components_and_protects_legacy_rows_for_new_week(tmp_path):
    path = tmp_path / "v2.sqlite3"
    event = legacy_v2(path)
    clock = Clock()
    clock.advance(100 * WEEK)
    with open_store(path, clock) as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert store.connection.execute("SELECT value FROM companion").fetchone()[0] == "preserve"
        store.acknowledge_recovery()
        assert read(store).events == (event,)
        with pytest.raises(RetentionBlocked, match="STARTUP"):
            store.prune_events(controller_guard=allowed)
        clock.advance(WEEK)
        assert store.prune_events(controller_guard=allowed).pruned_count == 1
        store.acknowledge_recovery()


def test_failed_v2_migration_rolls_back_every_schema_and_companion_write(tmp_path):
    path = tmp_path / "v2.sqlite3"
    legacy_v2(path)

    class Interrupted(SimulatorStorage):
        def _before_retention_migration_commit(self):
            raise RuntimeError("migration interrupted")

    with pytest.raises(RuntimeError, match="migration interrupted"):
        Interrupted(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("SELECT value FROM companion").fetchone()[0] == "preserve"
        assert connection.execute("SELECT count(*) FROM source_events").fetchone()[0] == 1
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='retention_checkpoint'"
        ).fetchone()


def test_v2_migration_refuses_missing_historical_prefix_without_fabricating_evidence(tmp_path):
    path = tmp_path / "v2.sqlite3"
    legacy_v2(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM source_events")
        connection.execute("UPDATE simulator_identity SET retained_from_seq='00000000000000000002'")
    with pytest.raises(StorageCorruption, match="prefix"):
        SimulatorStorage(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_checkpoint_uint64_never_uses_signed_sqlite_integer_and_corruption_is_not_accepted(ready):
    store, _ = ready
    store.connection.execute(
        "UPDATE retention_checkpoint SET pruned_through_seq=?", (str(UINT64_MAX),)
    )
    value, storage_type = store.connection.execute(
        "SELECT pruned_through_seq,typeof(pruned_through_seq) FROM retention_checkpoint"
    ).fetchone()
    assert value == str(UINT64_MAX) and storage_type == "text"
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "UPDATE retention_checkpoint SET pruned_through_seq=?", (str(UINT64_MAX + 1),)
        )
    with pytest.raises(StorageCorruption):
        store.acknowledge_recovery()


def test_uint64_maximum_head_is_never_pruned_into_unrepresentable_empty_range():
    # A bounded eligibility check, not a fabricated recoverable 2**64-event stream.
    cutoff = AT + timedelta(days=7)
    assert SimulatorStorage._retention_expired(UINT64_MAX - 1, AT, cutoff)
    assert not SimulatorStorage._retention_expired(UINT64_MAX, AT, cutoff)


def test_archive_checkpoint_dispositions_are_independently_reconciled(ready):
    store, clock = ready
    state = fresh_execution(None, "execution-A", config=ProcessConfig(stage_lengths_mm=(1, 1, 1)))
    store.register_execution(state)
    for tick in range(6):
        result = step(state, run=True, admit=tick < 3)
        store.apply_transition(state, result, occurred_at=AT)
        state = result.state
    clock.advance(WEEK)
    store.prune_events(controller_guard=allowed)
    store.connection.execute("UPDATE execution_checkpoints SET good='00000000000000000001'")
    with pytest.raises(StorageCorruption):
        store.acknowledge_recovery()
    assert store.recovery_required


def test_recovery_detects_archived_live_collision_even_when_insert_trigger_was_bypassed(ready):
    store, clock = ready
    event = control(store)
    clock.advance(WEEK)
    store.prune_events(controller_guard=allowed)
    store.connection.execute("DROP TRIGGER source_events_archive_collision")
    changed = event.model_copy(update={"seq": 2})
    store.connection.execute(
        "INSERT INTO source_events VALUES (?,?,?,?,NULL,NULL,?)",
        (
            event.event_id,
            event.source_id,
            event.stream_id,
            "00000000000000000002",
            changed.model_dump_json(),
        ),
    )
    store.connection.execute("UPDATE simulator_identity SET last_seq='00000000000000000002'")
    with pytest.raises(StorageCorruption):
        store.acknowledge_recovery()
    assert store.recovery_required


@pytest.mark.parametrize("field", ["source_id", "stream_id", "counter_epoch"])
def test_archive_checkpoint_remains_bound_to_original_identity(ready, field):
    store, clock = ready
    control(store)
    clock.advance(WEEK)
    store.prune_events(controller_guard=allowed)
    store.connection.execute(f"UPDATE simulator_identity SET {field}='replacement'")
    with pytest.raises(StorageCorruption):
        store.acknowledge_recovery()
    path = store.path
    store.close()
    with pytest.raises(StorageCorruption, match="another source/stream/epoch"):
        open_store(path, clock)


def test_missing_checkpoint_binding_is_never_recreated_from_current_identity(ready):
    store, clock = ready
    control(store)
    clock.advance(WEEK)
    store.prune_events(controller_guard=allowed)
    store.connection.execute("DELETE FROM retention_checkpoint")
    path = store.path
    store.close()
    with pytest.raises(StorageCorruption, match="Missing immutable"):
        open_store(path, clock)


def test_recorded_age_and_bound_identity_are_immutable_even_after_startup_hold(ready):
    store, clock = ready
    clock.advance(WEEK)
    recent = control(store)
    with pytest.raises(sqlite3.IntegrityError, match="age is immutable"):
        store.connection.execute("UPDATE event_retention SET recorded_at=?", (AT.isoformat(),))
    with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
        store.connection.execute("UPDATE retention_checkpoint SET source_id='replacement'")
    store.acknowledge_recovery()
    assert store.prune_events(controller_guard=allowed).pruned_count == 0
    assert read(store).events == (recent,)


@pytest.mark.parametrize("limit", [0, 1001, True, 1.0])
def test_retention_limit_is_an_exact_bounded_integer(ready, limit):
    store, _ = ready
    with pytest.raises(ValueError, match="limit"):
        store.prune_events(controller_guard=allowed, limit=limit)


def test_startup_hold_skips_history_scan_but_eligible_corruption_still_latches(ready):
    store, clock = ready
    control(store)
    # Simulate damage after recovery. A blocked maintenance attempt cannot delete
    # anything, while its later eligible attempt must revalidate all evidence.
    store.connection.execute(
        "UPDATE source_events SET body=json_set(body,'$.event_id','mismatched-event')"
    )
    statements = []
    store.connection.set_trace_callback(statements.append)
    try:
        with pytest.raises(RetentionBlocked, match="STARTUP"):
            store.prune_events(controller_guard=allowed)
    finally:
        store.connection.set_trace_callback(None)
    assert not any(
        name in statement.lower()
        for statement in statements
        for name in ("source_events", "archived_event_keys", "executions")
    )
    clock.advance(WEEK)
    with pytest.raises(StorageCorruption):
        store.prune_events(controller_guard=allowed)
    assert store.recovery_required
    assert store.connection.execute("SELECT count(*) FROM source_events").fetchone()[0] == 1
    assert store.connection.execute("SELECT count(*) FROM archived_event_keys").fetchone()[0] == 0
