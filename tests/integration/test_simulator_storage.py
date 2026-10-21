"""Actual SQLite transactions and abrupt child-process termination; partial T09 evidence."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from apps.simulator import storage as storage_module
from apps.simulator.process import (
    ProcessConfig,
    ProcessState,
    StepResult,
    fresh_execution,
    scrap,
    step,
)
from apps.simulator.storage import (
    RecoveryRequired,
    SimulatorStorage,
    StorageCorruption,
    decode_uint64,
    encode_uint64,
)
from contracts.models import UINT64_MAX, EventPage, EventReadError, ReadEventsRequest

AT = datetime(2026, 10, 20, 12, tzinfo=UTC)
FAST = ProcessConfig(stage_lengths_mm=(1, 1, 1))
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def store(tmp_path):
    with SimulatorStorage(tmp_path / "simulator.sqlite3", source_id="test-source") as database:
        database.acknowledge_recovery()
        yield database


def read(database, after=0, limit=1000, **overrides):
    arguments = {
        "source_id": database.identity.source_id,
        "stream_id": database.identity.stream_id,
        "after_seq": after,
        "limit": limit,
    }
    arguments.update(overrides)
    return database.read_events(ReadEventsRequest(**arguments))


def advance(database, state, *, admit=True):
    result = step(state, run=True, admit=admit)
    events = database.apply_transition(state, result, occurred_at=AT)
    assert database.load_execution() == result.state
    return result.state, events


def filled_pipeline(database):
    state = fresh_execution(None, "execution-A", config=FAST)
    database.register_execution(state)
    for _ in range(3):
        state, events = advance(database, state)
        assert not events
    assert state.input_quantity == state.wip_quantity == 3
    return state


def test_new_and_reopened_store_are_gated_and_never_tick_automatically(tmp_path):
    path = tmp_path / "state.sqlite3"
    initial = ProcessState("execution-A", config=FAST)
    with SimulatorStorage(path) as database:
        identity = database.identity
        assert database.recovery_required
        with pytest.raises(RecoveryRequired):
            database.register_execution(initial)
        database.acknowledge_recovery()
        database.register_execution(initial)
        state, _ = advance(database, initial)
    with SimulatorStorage(path) as database:
        assert database.identity == identity
        assert database.recovery_required
        assert database.load_execution() == state
        with pytest.raises(RecoveryRequired):
            read(database)
        result = step(state, run=True, admit=True)
        with pytest.raises(RecoveryRequired):
            database.apply_transition(state, result, occurred_at=AT)
        assert database.load_execution() == state
        database.acknowledge_recovery()
        assert database.load_execution() == state
        assert not database.recovery_required
        assert database.apply_transition(state, result, occurred_at=AT) == ()


def test_sqlite_settings_single_owner_and_identity_preservation(store):
    assert store._db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert store._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store._db.execute("PRAGMA synchronous").fetchone()[0] == 2
    with pytest.raises(BlockingIOError):
        SimulatorStorage(store.path)
    identity = store.identity
    store.close()
    with pytest.raises(ValueError, match="identity"):
        SimulatorStorage(store.path, source_id="replacement")
    with SimulatorStorage(store.path) as reopened:
        assert reopened.identity == identity


def test_good_reject_scrap_events_and_paged_reads_do_not_double_count(store):
    state = filled_pipeline(store)
    for _ in range(3):
        state, events = advance(store, state)
        assert len(events) == 1
    result = scrap(
        state, tuple(item.item_id for item in state.slots if item), reason="Damaged test WIP"
    )
    generated = store.apply_transition(state, result, occurred_at=AT)
    assert len(generated) == 3
    assert all(event.kind == "WIP_SCRAP" for event in generated)
    assert all(event.payload.reason == "Damaged test WIP" for event in generated)
    assert all(event.payload.counter_epoch == store.identity.counter_epoch for event in generated)
    state = result.state
    assert (
        state.input_quantity,
        state.good_quantity,
        state.reject_quantity,
        state.scrap_quantity,
    ) == (6, 2, 1, 3)
    first = read(store, limit=2)
    assert isinstance(first, EventPage)
    assert first.has_more
    assert first.page_last_seq == 2
    assert first.retained_from_seq == 1
    assert first.retained_through_seq == 6
    second = read(store, first.page_last_seq, limit=2)
    third = read(store, second.page_last_seq, limit=2)
    all_events = (*first.events, *second.events, *third.events)
    assert [event.seq for event in all_events] == list(range(1, 7))
    assert [event.kind for event in all_events[:3]] == [
        "PRODUCTION_GOOD",
        "PRODUCTION_GOOD",
        "PRODUCTION_REJECT",
    ]
    assert len({event.event_id for event in all_events}) == 6
    assert all(UUID(event.event_id) for event in all_events)
    assert all(event.execution_id == "execution-A" for event in all_events)
    assert all(event.payload.quantity == 1 for event in all_events)
    assert '"quantity":"1"' in first.model_dump_json()
    assert read(store, limit=2) == first
    assert store.load_execution() == state
    assert not read(store, 6).has_more
    store.acknowledge_recovery()


def test_execution_switch_keeps_original_events_and_snapshots(store):
    state = filled_pipeline(store)
    with pytest.raises(ValueError, match="WIP"):
        store.register_execution(ProcessState("execution-B", config=FAST))
    while state.wip_quantity:
        state, _ = advance(store, state, admit=False)
    previous_snapshot = state
    next_state = fresh_execution(state, "execution-B", config=FAST)
    store.register_execution(next_state)
    for _ in range(4):
        next_state, _ = advance(store, next_state)
    events = read(store).events
    assert [event.execution_id for event in events] == [
        "execution-A",
        "execution-A",
        "execution-A",
        "execution-B",
    ]
    assert [event.seq for event in events] == [1, 2, 3, 4]
    assert store.load_execution("execution-A") == previous_snapshot
    assert store.load_execution() == next_state
    store.acknowledge_recovery()


@pytest.mark.parametrize("invalid", ["missing", "wrong-outcome", "mixed-execution", "duplicate"])
def test_inconsistent_deltas_and_snapshots_rollback(store, invalid):
    state = filled_pipeline(store)
    result = step(state, run=True, admit=True)
    delta = result.deltas[0]
    deltas = {
        "missing": (),
        "wrong-outcome": (replace(delta, outcome="REJECT"),),
        "mixed-execution": (replace(delta, execution_id="execution-B"),),
        "duplicate": (delta, delta),
    }[invalid]
    with pytest.raises(ValueError):
        store.apply_transition(state, StepResult(result.state, deltas), occurred_at=AT)
    assert store.load_execution() == state
    assert read(store).retained_through_seq == 0
    assert read(store).events == ()


def test_stale_replayed_transition_and_event_id_collision_cannot_double_apply(store, monkeypatch):
    previous = filled_pipeline(store)
    first_result = step(previous, run=True, admit=True)
    event = store.apply_transition(previous, first_result, occurred_at=AT)[0]
    with pytest.raises(ValueError, match="Stale"):
        store.apply_transition(previous, first_result, occurred_at=AT)
    next_result = step(first_result.state, run=True, admit=True)
    monkeypatch.setattr(storage_module, "uuid4", lambda: event.event_id)
    with pytest.raises(sqlite3.IntegrityError):
        store.apply_transition(first_result.state, next_result, occurred_at=AT)
    assert store.load_execution() == first_result.state
    assert read(store).events == (event,)


def test_reopen_preserves_sequence_and_recovers_before_continuing(store):
    state = filled_pipeline(store)
    state, generated = advance(store, state)
    identity = store.identity
    path = store.path
    store.close()
    with SimulatorStorage(path) as reopened:
        assert reopened.identity == identity
        assert reopened.load_execution() == state
        reopened.acknowledge_recovery()
        assert read(reopened).events == generated
        state, next_events = advance(reopened, state)
        assert next_events[0].seq == 2
        assert next_events[0].event_id != generated[0].event_id


@pytest.mark.parametrize("phase", ["before", "after"])
def test_abrupt_child_exit_at_commit_is_all_or_nothing(tmp_path, phase):
    path = tmp_path / "crash.sqlite3"
    with SimulatorStorage(path) as database:
        database.acknowledge_recovery()
        previous = filled_pipeline(database)
        identity = database.identity
    expected = step(previous, run=True, admit=True)
    script = r"""
import os
import sys
from datetime import UTC, datetime
from apps.simulator.process import step
from apps.simulator.storage import SimulatorStorage

class CrashStorage(SimulatorStorage):
    def _before_transition_commit(self):
        if sys.argv[2] == "before":
            os._exit(71)
    def _after_transition_commit(self):
        if sys.argv[2] == "after":
            os._exit(72)

with CrashStorage(sys.argv[1]) as database:
    database.acknowledge_recovery()
    previous = database.load_execution()
    result = step(previous, run=True, admit=True)
    database.apply_transition(previous, result, occurred_at=datetime(2026,10,20,tzinfo=UTC))
raise SystemExit(99)
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(path), phase],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == {"before": 71, "after": 72}[phase], child.stderr
    with SimulatorStorage(path) as reopened:
        assert reopened.identity == identity
        assert reopened.recovery_required
        expected_state = previous if phase == "before" else expected.state
        assert reopened.load_execution() == expected_state
        with pytest.raises(RecoveryRequired):
            read(reopened)
        reopened.acknowledge_recovery()
        events = read(reopened).events
        assert len(events) == (0 if phase == "before" else 1)
        if events:
            assert events[0].seq == 1
            assert events[0].execution_id == previous.execution_id
            assert events[0].kind == "PRODUCTION_GOOD"
        row = reopened._db.execute("SELECT * FROM executions").fetchone()
        for field in ("input_quantity", "good_quantity", "reject_quantity", "scrap_quantity"):
            assert decode_uint64(row[field]) == getattr(expected_state, field)
        assert reopened._db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert reopened._db.execute("PRAGMA foreign_key_check").fetchall() == []
        reopened.acknowledge_recovery()
        assert reopened.load_execution() == expected_state


def test_recovery_refuses_corrupted_projection_and_event_metadata(store):
    state = filled_pipeline(store)
    state, _ = advance(store, state)
    store._db.execute("UPDATE executions SET good_quantity=?", (encode_uint64(7),))
    with pytest.raises(StorageCorruption, match="projections"):
        store.acknowledge_recovery()
    assert store.recovery_required
    store._db.execute("UPDATE executions SET good_quantity=?", (encode_uint64(1),))
    event_row = store._db.execute("SELECT event_id,body FROM source_events").fetchone()
    event_body = json.loads(event_row["body"])
    event_body["payload"]["quantity"] = "2"
    store._db.execute("UPDATE source_events SET body=?", (json.dumps(event_body),))
    with pytest.raises(StorageCorruption, match="quantity"):
        store.acknowledge_recovery()
    assert store.recovery_required
    with pytest.raises(RecoveryRequired):
        store.apply_transition(state, step(state, run=True, admit=True), occurred_at=AT)
    with pytest.raises(RecoveryRequired):
        read(store)


@pytest.mark.parametrize("mutation", ["quantity", "event-id", "kind"])
def test_corrupt_event_body_cannot_be_replayed_before_or_after_failed_recovery(store, mutation):
    state = filled_pipeline(store)
    advance(store, state)
    row = store._db.execute("SELECT body FROM source_events").fetchone()
    body = json.loads(row[0])
    if mutation == "quantity":
        body["payload"]["quantity"] = "2"
    elif mutation == "event-id":
        body["event_id"] = "corrupt-event-id"
    else:
        body["kind"] = "PRODUCTION_REJECT"
    path = store.path
    store.close()
    with sqlite3.connect(path) as writer:
        writer.execute("UPDATE source_events SET body=?", (json.dumps(body),))
    with SimulatorStorage(path) as reopened:
        with pytest.raises(RecoveryRequired):
            read(reopened)
        with pytest.raises(StorageCorruption):
            reopened.acknowledge_recovery()
        assert reopened.recovery_required
        with pytest.raises(RecoveryRequired):
            read(reopened)


@pytest.mark.parametrize("mutation", ["quantity", "event-id"])
def test_active_read_compares_event_body_with_row_and_latches_recovery(store, mutation):
    state = filled_pipeline(store)
    advance(store, state)
    body = json.loads(store._db.execute("SELECT body FROM source_events").fetchone()[0])
    if mutation == "quantity":
        body["payload"]["quantity"] = "2"
    else:
        body["event_id"] = "corrupt-event-id"
    store._db.execute("UPDATE source_events SET body=?", (json.dumps(body),))
    with pytest.raises(StorageCorruption):
        read(store)
    assert store.recovery_required
    with pytest.raises(RecoveryRequired):
        read(store)


@pytest.mark.parametrize("active_id", ["old-B", None])
def test_recovery_refuses_inactive_wip_and_blocks_replacement(store, active_id):
    old = ProcessState("old-B", config=FAST)
    store.register_execution(old)
    active = fresh_execution(old, "run-A", config=FAST)
    store.register_execution(active)
    active, _ = advance(store, active)
    path = store.path
    store.close()
    with sqlite3.connect(path) as writer:
        writer.execute("UPDATE simulator_identity SET active_execution_id=?", (active_id,))
    with SimulatorStorage(path) as reopened:
        with pytest.raises(StorageCorruption):
            reopened.acknowledge_recovery()
        assert reopened.load_execution("run-A") == active
        with pytest.raises(RecoveryRequired):
            reopened.register_execution(ProcessState("run-C", config=FAST))
        assert reopened.load_execution("run-C") is None


@pytest.mark.parametrize("operation", ["register", "apply"])
def test_inactive_wip_is_also_checked_before_each_write(store, operation):
    old = ProcessState("old-B", config=FAST)
    store.register_execution(old)
    active = fresh_execution(old, "run-A", config=FAST)
    store.register_execution(active)
    active, _ = advance(store, active)
    store._db.execute("UPDATE simulator_identity SET active_execution_id='old-B'")
    with pytest.raises(StorageCorruption, match="Inactive"):
        if operation == "register":
            store.register_execution(ProcessState("run-C", config=FAST))
        else:
            store.apply_transition(old, step(old, run=True, admit=True), occurred_at=AT)
    assert store.recovery_required
    assert store.load_execution("run-A") == active
    assert store.load_execution("run-C") is None


def test_symlink_alias_uses_same_owner_lock_without_resetting_recovery(store, tmp_path):
    alias = tmp_path / "alias.sqlite3"
    alias.symlink_to(store.path)
    with pytest.raises(BlockingIOError):
        SimulatorStorage(alias)
    assert not store.recovery_required


def test_hardlink_aliases_are_explicitly_unsupported(store, tmp_path):
    alias = tmp_path / "hardlink.sqlite3"
    os.link(store.path, alias)
    try:
        with pytest.raises(ValueError, match="Hardlinked"):
            SimulatorStorage(alias)
        with pytest.raises(ValueError, match="Hardlinked"):
            SimulatorStorage(store.path)
        assert not store.recovery_required
    finally:
        alias.unlink()


def test_empty_page_cursor_ahead_stream_mismatch_and_explicit_retention_gap(store):
    empty = read(store)
    assert (empty.retained_from_seq, empty.retained_through_seq, empty.page_last_seq) == (1, 0, 0)
    assert empty.events == ()
    assert read(store, 1).code == "CURSOR_AHEAD"
    mismatch = read(store, stream_id="wrong-stream")
    assert mismatch.code == "STREAM_MISMATCH"
    assert mismatch.stream_id == store.identity.stream_id
    assert read(store, source_id="wrong-source").code == "STREAM_MISMATCH"
    # Synthetic retained high-water fixture; no retention scheduler is implemented here.
    store._db.execute(
        "UPDATE simulator_identity SET retained_from_seq=?,last_seq=?",
        (encode_uint64(10), encode_uint64(9)),
    )
    gap = read(store, 3)
    assert isinstance(gap, EventReadError)
    assert (gap.code, gap.missing_from_seq, gap.missing_through_seq) == ("RETENTION_GAP", 4, 9)
    assert read(store, 9).events == ()


@pytest.mark.parametrize("value", [0, 2**53 + 1, 2**63, UINT64_MAX])
def test_exact_uint64_encoding_and_sqlite_text_storage(store, value):
    encoded = encode_uint64(value)
    assert decode_uint64(encoded) == value
    assert len(encoded) == 20
    store._db.execute("UPDATE simulator_identity SET last_seq=?", (encoded,))
    row = store._db.execute("SELECT last_seq,typeof(last_seq) FROM simulator_identity").fetchone()
    assert (row[0], row[1]) == (encoded, "text")


@pytest.mark.parametrize("value", [-1, UINT64_MAX + 1, 1.5, True, "1"])
def test_uint64_encoder_never_coerces_invalid_values(value):
    with pytest.raises(ValueError):
        encode_uint64(value)


@pytest.mark.parametrize(
    "value", ["-0000000000000000001", "18446744073709551616", "1", "0" * 19 + "a"]
)
def test_sqlite_rejects_invalid_uint64_projections(store, value):
    with pytest.raises(sqlite3.IntegrityError):
        store._db.execute("UPDATE simulator_identity SET last_seq=?", (value,))


def test_last_uint64_sequence_commits_then_overflow_rolls_back(store):
    state = filled_pipeline(store)
    # Explicit artificial high-water fixture exercises representation, not 2**64 production events.
    store._db.execute(
        "UPDATE simulator_identity SET retained_from_seq=?,last_seq=?",
        (encode_uint64(UINT64_MAX), encode_uint64(UINT64_MAX - 1)),
    )
    state, events = advance(store, state)
    assert events[0].seq == UINT64_MAX
    page = read(store, UINT64_MAX - 1)
    assert page.events == events
    assert page.page_last_seq == UINT64_MAX
    assert not page.has_more
    assert read(store, UINT64_MAX).events == ()
    result = step(state, run=True, admit=True)
    with pytest.raises(ValueError, match="exhausted"):
        store.apply_transition(state, result, occurred_at=AT)
    assert store.load_execution() == state
    assert read(store, UINT64_MAX - 1) == page
    path = store.path
    store.close()
    with SimulatorStorage(path) as reopened:
        # This representation fixture fabricates a deleted prefix without 2**64
        # archived identities. V3 must reject it as recovery evidence.
        with pytest.raises(StorageCorruption):
            reopened.acknowledge_recovery()
        assert reopened.recovery_required


def test_full_uint64_snapshot_projection_roundtrip_is_lossless(store):
    # Synthetic counter-limit fixture. It is deliberately not an event-complete recovered execution.
    maximum = ProcessState(
        "limit",
        input_quantity=UINT64_MAX,
        good_quantity=UINT64_MAX,
        decision_cursor=UINT64_MAX,
        tick=UINT64_MAX,
    )
    store._save_state(maximum, insert=True)
    store._db.execute("UPDATE simulator_identity SET active_execution_id='limit'")
    assert store.load_execution("limit") == maximum
    assert store._db.execute(
        "SELECT good_quantity FROM executions WHERE execution_id='limit'"
    ).fetchone()[0] == str(UINT64_MAX)
    with pytest.raises(StorageCorruption, match="counters"):
        store.acknowledge_recovery()
    assert store.recovery_required
