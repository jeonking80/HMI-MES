"""Atomic composition boundaries for process and future command/controller components."""

from datetime import UTC, datetime

import pytest

from apps.simulator.process import fresh_execution, step
from apps.simulator.storage import SimulatorStorage, StorageCorruption

AT = datetime(2026, 10, 21, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    with SimulatorStorage(tmp_path / "state.sqlite3") as database:
        database.acknowledge_recovery()
        with database.transaction() as connection:
            connection.execute("CREATE TABLE companion (value TEXT NOT NULL)")
        yield database


def test_outer_failure_rolls_back_registered_execution_and_companion(store):
    initial = fresh_execution(None, "a")
    with pytest.raises(RuntimeError):
        with store.transaction() as connection:
            store.register_execution(initial)
            connection.execute("INSERT INTO companion VALUES ('accepted')")
            raise RuntimeError("before commit")
    assert store.load_execution() is None
    assert store.connection.execute("SELECT * FROM companion").fetchall() == []


def test_process_result_and_companion_share_commit_and_delayed_hook(store, monkeypatch):
    initial = fresh_execution(None, "a")
    store.register_execution(initial)
    result = step(initial, run=True, admit=True)
    observed = []
    monkeypatch.setattr(
        store, "_after_transition_commit", lambda: observed.append(store.connection.in_transaction)
    )
    with store.transaction() as connection:
        store.apply_transition(initial, result, occurred_at=AT)
        connection.execute("INSERT INTO companion VALUES ('success')")
        assert observed == []
    assert observed == [False]
    assert store.load_execution() == result.state
    assert len(store.connection.execute("SELECT * FROM companion").fetchall()) == 1


def test_rolled_back_nested_unit_discards_callbacks_only_for_that_unit(store, monkeypatch):
    initial = fresh_execution(None, "a")
    store.register_execution(initial)
    result = step(initial, run=True, admit=True)
    observed = []
    monkeypatch.setattr(store, "_after_transition_commit", lambda: observed.append("committed"))
    with store.transaction() as connection:
        connection.execute("INSERT INTO companion VALUES ('keep')")
        with pytest.raises(ValueError):
            with store.transaction():
                store.apply_transition(initial, result, occurred_at=AT)
                raise ValueError("reject companion")
        assert store.load_execution() == initial
    assert observed == []
    assert len(store.connection.execute("SELECT * FROM companion").fetchall()) == 1


def test_caught_corruption_poison_rolls_back_outer_and_persists_gate(store):
    with pytest.raises(StorageCorruption):
        with store.transaction() as connection:
            connection.execute("INSERT INTO companion VALUES ('must rollback')")
            with pytest.raises(StorageCorruption):
                with store.transaction():
                    raise StorageCorruption("invalid source data")
    assert store.recovery_required
    assert store.connection.execute("SELECT * FROM companion").fetchall() == []


def test_nested_recovery_is_provisional_until_outer_commit(store):
    store.connection.execute("UPDATE simulator_identity SET recovery_required=1")
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.acknowledge_recovery()
            assert not store.recovery_required
            raise RuntimeError("controller audit failed")
    assert store.recovery_required
    with store.transaction() as connection:
        store.acknowledge_recovery()
        connection.execute("INSERT INTO companion VALUES ('recovery audit')")
    assert not store.recovery_required
    assert len(store.connection.execute("SELECT * FROM companion").fetchall()) == 1


def test_after_commit_hook_failure_does_not_rollback_durable_state(store, monkeypatch):
    initial = fresh_execution(None, "a")
    store.register_execution(initial)
    result = step(initial, run=True, admit=True)

    def fail_after_commit():
        raise RuntimeError("response lost")

    monkeypatch.setattr(store, "_after_transition_commit", fail_after_commit)
    with pytest.raises(RuntimeError):
        with store.transaction() as connection:
            store.apply_transition(initial, result, occurred_at=AT)
            connection.execute("INSERT INTO companion VALUES ('durable')")
    assert store.load_execution() == result.state
    assert len(store.connection.execute("SELECT * FROM companion").fetchall()) == 1
