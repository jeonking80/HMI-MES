"""Mixed source history, schema migration, and atomic PLC storage composition."""

import sqlite3
from contextlib import contextmanager
from dataclasses import fields, replace
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from apps.simulator.process import ProcessConfig, ProcessState, fresh_execution, step
from apps.simulator.storage import (
    FaultEventInput,
    RecoveryRequired,
    SimulatorStorage,
    StateEventInput,
    StorageCorruption,
    encode_uint64,
)
from contracts.models import ReadEventsRequest

AT = datetime(2026, 10, 20, 12, tzinfo=UTC)
FAST = ProcessConfig(stage_lengths_mm=(1, 1, 1))


@pytest.fixture
def store(tmp_path):
    with SimulatorStorage(tmp_path / "state.sqlite3") as database:
        database.acknowledge_recovery()
        yield database


def state_event(execution_id=None, **overrides):
    arguments = {
        "occurred_at": AT,
        "execution_id": execution_id,
        "payload": {"previous_state": "STOPPED", "state": "READY", "control_version": 1},
    }
    arguments.update(overrides)
    return StateEventInput(**arguments)


def fault_event(execution_id=None):
    return FaultEventInput(
        occurred_at=AT,
        execution_id=execution_id,
        payload={"fault_code": 101, "active": True},
    )


def read(store, after=0, limit=1000):
    return store.read_events(
        ReadEventsRequest(
            source_id=store.identity.source_id,
            stream_id=store.identity.stream_id,
            after_seq=after,
            limit=limit,
        )
    )


def before_production(store):
    state = fresh_execution(None, "execution-A", config=FAST)
    store.register_execution(state)
    for _ in range(3):
        result = step(state, run=True, admit=True)
        assert store.apply_transition(state, result, occurred_at=AT) == ()
        state = result.state
    return state


def dump_rows(connection):
    return {
        table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
        for table in ("simulator_identity", "executions", "source_events", "controller_journal")
    }


def legacy_file(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with SimulatorStorage(path, source_id="source-A") as database:
        database.acknowledge_recovery()
        state = before_production(database)
        result = step(state, run=True, admit=False)
        database.apply_transition(state, result, occurred_at=AT)
        # Recreate the actual v1 non-null production-only event table, retaining its data.
        database.connection.executescript(
            """
            BEGIN IMMEDIATE;
            DROP TRIGGER source_events_archive_collision;
            DROP TRIGGER source_events_recorded;
            DROP TRIGGER archived_keys_match_live;
            DROP TRIGGER archived_keys_no_update;
            DROP TRIGGER archived_keys_no_delete;
            DROP TRIGGER retention_identity_no_update;
            DROP TRIGGER retention_age_no_update;
            DROP TABLE event_retention;
            DROP TABLE execution_checkpoints;
            DROP TABLE archived_event_keys;
            DROP TABLE retention_checkpoint;
            CREATE TABLE source_events_v1 (
                event_id TEXT PRIMARY KEY NOT NULL,
                source_id TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                seq TEXT NOT NULL COLLATE BINARY CHECK(typeof(seq)='text'
                    AND length(seq)=20 AND seq NOT GLOB '*[^0-9]*'
                    AND seq<='18446744073709551615'),
                execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE RESTRICT,
                item_id TEXT NOT NULL COLLATE BINARY CHECK(typeof(item_id)='text'
                    AND length(item_id)=20 AND item_id NOT GLOB '*[^0-9]*'
                    AND item_id<='18446744073709551615'),
                body TEXT NOT NULL,
                CHECK(seq>'00000000000000000000'),
                CHECK(item_id>'00000000000000000000'),
                UNIQUE(source_id,stream_id,seq), UNIQUE(execution_id,item_id),
                FOREIGN KEY(source_id,stream_id)
                    REFERENCES simulator_identity(source_id,stream_id) ON DELETE RESTRICT
            );
            INSERT INTO source_events_v1 SELECT * FROM source_events;
            DROP TABLE source_events;
            ALTER TABLE source_events_v1 RENAME TO source_events;
            CREATE INDEX ix_events_execution ON source_events(execution_id,seq);
            CREATE TABLE controller_journal (command_id TEXT PRIMARY KEY, result TEXT NOT NULL);
            INSERT INTO controller_journal VALUES ('command-A','SUCCEEDED');
            PRAGMA user_version=1;
            COMMIT;
            """
        )
        original = dump_rows(database.connection)
        identity = database.identity
    return path, original, identity


def test_v1_migration_preserves_all_data_and_next_global_sequence(tmp_path):
    path, original, identity = legacy_file(tmp_path)
    with SimulatorStorage(path) as database:
        assert database.connection.execute("PRAGMA user_version").fetchone()[0] == 4
        expected = {
            **original,
            "simulator_identity": [(*original["simulator_identity"][0][:-1], 1)],
        }
        assert dump_rows(database.connection) == expected
        assert database.identity == identity
        assert not database.connection.execute("PRAGMA foreign_key_check").fetchall()
        database.acknowledge_recovery()
        events = database.append_control_events((state_event(), fault_event("execution-A")))
        assert [event.seq for event in events] == [2, 3]
        assert [event.kind for event in read(database).events] == [
            "PRODUCTION_GOOD",
            "STATE_CHANGED",
            "FAULT_CHANGED",
        ]
        database.acknowledge_recovery()


def test_failed_migration_rolls_back_schema_version_and_every_table(tmp_path):
    path, original, _ = legacy_file(tmp_path)
    with sqlite3.connect(path) as connection:
        schema_before = connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()

    class InterruptedMigration(SimulatorStorage):
        @contextmanager
        def transaction(self):
            with super().transaction() as connection:
                yield connection
                raise RuntimeError("migration interrupted after table replacement")

    with pytest.raises(RuntimeError, match="migration interrupted"):
        InterruptedMigration(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert dump_rows(connection) == original
        assert (
            connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
            == schema_before
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with SimulatorStorage(path) as database:
        database.acknowledge_recovery()
        assert read(database).events[0].seq == 1


def test_failed_retention_stage_rolls_back_entire_v1_to_v3_chain(tmp_path):
    path, original, _ = legacy_file(tmp_path)

    class InterruptedRetention(SimulatorStorage):
        def _before_retention_migration_commit(self):
            raise RuntimeError("retention migration interrupted")

    with pytest.raises(RuntimeError, match="retention migration interrupted"):
        InterruptedRetention(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert dump_rows(connection) == original
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='retention_checkpoint'"
        ).fetchone()


def test_mixed_events_page_and_recover_without_affecting_production_totals(store):
    initial = store.append_control_events((state_event(), fault_event()))
    assert all(event.execution_id is None for event in initial)
    state = before_production(store)
    result = step(state, run=True, admit=False)
    production = store.apply_transition(state, result, occurred_at=AT)
    controls = store.append_control_events((state_event(state.execution_id), fault_event()))
    all_events = (*initial, *production, *controls)
    assert [event.seq for event in all_events] == [1, 2, 3, 4, 5]
    assert len({event.event_id for event in all_events}) == 5
    for event in all_events:
        UUID(event.event_id)
        assert (event.source_id, event.stream_id) == (
            store.identity.source_id,
            store.identity.stream_id,
        )
    pages = [read(store, cursor, limit=2) for cursor in (0, 2, 4)]
    assert tuple(event for page in pages for event in page.events) == all_events
    assert [page.has_more for page in pages] == [True, True, False]
    assert store.load_execution() == result.state
    store.acknowledge_recovery()
    path = store.path
    store.close()
    with SimulatorStorage(path) as reopened:
        reopened.acknowledge_recovery()
        assert read(reopened).events == all_events
        assert reopened.load_execution() == result.state


@pytest.mark.parametrize("abort", [True, False])
def test_process_parameters_control_events_and_command_journal_share_commit(store, abort):
    previous = before_production(store)
    store.connection.execute(
        "CREATE TABLE controller_journal (command_id TEXT PRIMARY KEY, result TEXT)"
    )
    original = dump_rows(store.connection)
    try:
        with store.transaction() as connection:
            changed = store.update_process_parameters(
                previous, temperature_c=180.0, speed_m_per_min=12.0
            )
            result = step(changed, run=True, admit=False)
            production = store.apply_transition(changed, result, occurred_at=AT)
            controls = store.append_control_events(
                (state_event(previous.execution_id), fault_event())
            )
            assert [event.seq for event in (*production, *controls)] == [1, 2, 3]
            connection.execute("INSERT INTO controller_journal VALUES ('command-A','SUCCEEDED')")
            if abort:
                raise RuntimeError("abort unit")
    except RuntimeError:
        assert abort
    if abort:
        assert dump_rows(store.connection) == original
        assert read(store).events == ()
    else:
        assert store.load_execution() == result.state
        assert len(read(store).events) == 3
        assert (
            store.connection.execute("SELECT result FROM controller_journal").fetchone()[0]
            == "SUCCEEDED"
        )
        store.acknowledge_recovery()


def test_parameter_update_preserves_every_process_field_and_only_selected_config(store):
    initial = before_production(store)
    changed = store.update_process_parameters(initial, temperature_c=170.0, speed_m_per_min=7.5)
    assert changed.config == replace(initial.config, temperature_sp_c=170.0, speed_m_per_min=7.5)
    for field in fields(ProcessState):
        if field.name != "config":
            assert getattr(changed, field.name) == getattr(initial, field.name)
    assert store.load_execution() == changed
    assert read(store).events == ()
    with pytest.raises(ValueError, match="Stale"):
        store.update_process_parameters(initial, temperature_c=180.0, speed_m_per_min=8.0)
    with pytest.raises(ValueError):
        store.update_process_parameters(changed, temperature_c=float("nan"), speed_m_per_min=8.0)
    assert store.load_execution() == changed
    path = store.path
    store.close()
    with SimulatorStorage(path) as reopened:
        with pytest.raises(RecoveryRequired):
            reopened.update_process_parameters(changed, temperature_c=180.0, speed_m_per_min=8.0)
        reopened.acknowledge_recovery()
        assert reopened.load_execution() == changed


@pytest.mark.parametrize(
    "field,value", [("source_id", "forged"), ("event_id", "forged"), ("seq", 8)]
)
def test_descriptors_reject_caller_allocated_identity(field, value):
    with pytest.raises(ValidationError):
        state_event(**{field: value})


@pytest.mark.parametrize(
    "descriptor",
    [
        lambda: state_event("missing-execution"),
        lambda: state_event().model_copy(update={"equipment_id": "invalid equipment"}),
        lambda: fault_event().model_copy(update={"payload": {"fault_code": 0, "active": True}}),
    ],
)
def test_invalid_control_descriptors_roll_back_prior_events_in_batch(store, descriptor):
    with pytest.raises(ValueError):
        store.append_control_events((state_event(), descriptor()))
    assert read(store).events == ()
    assert store._bounds() == (1, 0)


def test_typed_control_history_can_be_recorded_while_recovery_stays_gated(store):
    with pytest.raises(ValueError, match="typed"):
        store.append_control_events((state_event().model_dump(),))
    assert store.append_control_events(()) == ()
    path = store.path
    store.close()
    with SimulatorStorage(path) as reopened:
        generated = reopened.append_control_events((state_event(), fault_event()))
        assert [event.seq for event in generated] == [1, 2]
        assert reopened.recovery_required
        with pytest.raises(RecoveryRequired):
            read(reopened)
        reopened.acknowledge_recovery()
        assert read(reopened).events == generated


def test_schema_requires_items_for_production_and_forbids_items_for_controls(store):
    state = before_production(store)
    result = step(state, run=True, admit=False)
    store.apply_transition(state, result, occurred_at=AT)
    store.append_control_events((state_event(),))
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "UPDATE source_events SET item_id=NULL WHERE seq=?", (encode_uint64(1),)
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "UPDATE source_events SET item_id=? WHERE seq=?", (encode_uint64(2), encode_uint64(2))
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "UPDATE source_events SET execution_id=NULL WHERE seq=?", (encode_uint64(1),)
        )


def test_caught_malformed_snapshot_poisons_outer_unit_and_latches_recovery(store):
    state = before_production(store)
    store.connection.execute(
        "CREATE TABLE controller_journal (command_id TEXT PRIMARY KEY, result TEXT)"
    )
    store.connection.execute("UPDATE executions SET snapshot='invalid-json'")
    with pytest.raises(StorageCorruption):
        with store.transaction() as connection:
            with pytest.raises(StorageCorruption, match="persisted execution"):
                store.append_control_events((state_event(),))
            connection.execute("INSERT INTO controller_journal VALUES ('command-A','SUCCEEDED')")
    assert store.recovery_required
    assert not store.connection.execute("SELECT * FROM controller_journal").fetchall()
    assert store._bounds() == (1, 0)
    store.connection.execute("UPDATE executions SET snapshot=?", (state.to_json(),))
    store.acknowledge_recovery()


def test_control_event_id_collision_rolls_back_whole_batch(store, monkeypatch):
    from apps.simulator import storage as storage_module

    monkeypatch.setattr(storage_module, "uuid4", lambda: "same-event-id")
    with pytest.raises(sqlite3.IntegrityError):
        store.append_control_events((state_event(), fault_event()))
    assert store._bounds() == (1, 0)
    assert read(store).events == ()


def test_control_sequence_exhaustion_never_wraps(store):
    from contracts.models import UINT64_MAX

    store.connection.execute(
        "UPDATE simulator_identity SET last_seq=?", (encode_uint64(UINT64_MAX),)
    )
    with pytest.raises(ValueError, match="exhausted"):
        store.append_control_events((state_event(),))
    assert store._bounds() == (1, UINT64_MAX)
    assert not store.connection.execute("SELECT * FROM source_events").fetchall()


def test_control_payload_corruption_blocks_recovery(store):
    import json

    event = store.append_control_events((fault_event(),))[0]
    body = event.model_dump(mode="json")
    body["payload"]["active"] = "true"
    store.connection.execute("UPDATE source_events SET body=?", (json.dumps(body),))
    with pytest.raises(StorageCorruption, match="persisted source event"):
        store.acknowledge_recovery()
    assert store.recovery_required
    with pytest.raises(RecoveryRequired):
        read(store)


def test_public_corruption_marker_latches_without_transaction_and_poisons_composition(store):
    store.mark_corrupt()
    assert store.recovery_required
    store.acknowledge_recovery()
    store.connection.execute(
        "CREATE TABLE controller_journal (command_id TEXT PRIMARY KEY, result TEXT)"
    )
    with pytest.raises(StorageCorruption):
        with store.transaction() as connection:
            store.mark_corrupt()
            connection.execute("INSERT INTO controller_journal VALUES ('command-A','SUCCEEDED')")
    assert store.recovery_required
    assert not store.connection.execute("SELECT * FROM controller_journal").fetchall()


def test_caught_malformed_snapshot_load_also_poisons_composition(store):
    state = before_production(store)
    store.connection.execute("UPDATE executions SET snapshot='invalid-json'")
    with pytest.raises(StorageCorruption):
        with store.transaction():
            with pytest.raises(StorageCorruption):
                store.load_execution()
            # Even repairing it in this unit cannot hide the observed persisted corruption.
            store.connection.execute("UPDATE executions SET snapshot=?", (state.to_json(),))
    assert store.recovery_required
    assert (
        store.connection.execute("SELECT snapshot FROM executions").fetchone()[0] == "invalid-json"
    )


def test_transaction_generation_changes_for_each_outer_begin_including_rollback(store):
    before = store.transaction_generation
    with store.transaction():
        first = store.transaction_generation
        assert first == before + 1
        with store.transaction():
            assert store.transaction_generation == first
        with pytest.raises(RuntimeError):
            with store.transaction():
                assert store.transaction_generation == first
                raise RuntimeError("nested rollback")
        assert store.transaction_generation == first
    with pytest.raises(RuntimeError):
        with store.transaction():
            assert store.transaction_generation == first + 1
            raise RuntimeError("outer rollback")
    with store.transaction():
        assert store.transaction_generation == first + 2
