"""Field integrity across real storage-v3/runtime-v2 database upgrades."""

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from apps.simulator import field_channels
from apps.simulator.controller import SimulatorController
from apps.simulator.plc import RecoveryChecks
from apps.simulator.storage import SimulatorStorage, StorageCorruption, encode_uint64

AT = datetime(2026, 10, 25, tzinfo=UTC)
CHECKS = RecoveryChecks(
    outputs_stopped=True,
    positions_verified=True,
    storage_reconciled=True,
    commands_reconciled=True,
    operator_confirmed=True,
)

# Runtime-v2 DDL before CX-35: there is genuinely no parameter-history table
# or either of its triggers. Merely changing today's version row would not
# exercise the production upgrade path.
RUNTIME_V2_DDL = (
    "CREATE TABLE runtime_schema_version (singleton INTEGER PRIMARY KEY "
    "CHECK(singleton=1), version INTEGER NOT NULL CHECK(version>=1))",
    "CREATE TABLE runtime_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "snapshot_json TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE runtime_runs (execution_id TEXT PRIMARY KEY NOT NULL "
    "REFERENCES executions(execution_id),summary_json TEXT NOT NULL)",
    "CREATE TABLE runtime_settings (execution_id TEXT NOT NULL REFERENCES "
    "executions(execution_id),recipe_version TEXT NOT NULL,parameters_json TEXT NOT NULL,"
    "effective_tick TEXT NOT NULL,effective_seq TEXT NOT NULL,recorded_at TEXT NOT NULL,"
    "PRIMARY KEY(execution_id,recipe_version))",
    "CREATE TABLE runtime_audit (audit_id INTEGER PRIMARY KEY,occurred_at TEXT NOT NULL,"
    "cause TEXT NOT NULL,execution_id TEXT,control_version TEXT NOT NULL,"
    "details_json TEXT NOT NULL)",
    "CREATE TABLE runtime_command_authority (command_id TEXT PRIMARY KEY NOT NULL "
    "REFERENCES command_journal(command_id),authority TEXT NOT NULL "
    "CHECK(authority IN ('LOCAL','REMOTE')))",
    "CREATE TRIGGER runtime_final_immutable BEFORE UPDATE ON runtime_runs "
    "WHEN json_extract(OLD.summary_json,'$.final')=1 "
    "BEGIN SELECT RAISE(ABORT,'Final summary is immutable'); END",
    "CREATE TRIGGER runtime_final_delete_immutable BEFORE DELETE ON runtime_runs "
    "WHEN json_extract(OLD.summary_json,'$.final')=1 "
    "BEGIN SELECT RAISE(ABORT,'Final summary is immutable'); END",
)


class StorageV3(SimulatorStorage):
    @staticmethod
    def _events_table_sql(name):
        return SimulatorStorage._events_table_sql(name).replace(
            "WHEN 'PARAMETERS_CHANGED' THEN execution_id IS NOT NULL AND item_id IS NULL", ""
        )

    def _migrate_schema_v3(self):
        """Stop at the historical storage-v3 schema, including its old CHECKs."""


class ControllerV2(SimulatorController):
    def _initialize_schema(self):
        for statement in RUNTIME_V2_DDL:
            self.storage.connection.execute(statement)
        self.storage.connection.execute("INSERT INTO runtime_schema_version VALUES(1,2)")


def rows(db, table):
    return [tuple(row) for row in db.execute(f'SELECT * FROM "{table}" ORDER BY 1')]


def field_ddl(db):
    return [
        tuple(row)
        for row in db.execute(
            "SELECT name,type,tbl_name,sql FROM sqlite_master "
            "WHERE name GLOB 'field_channel_*' OR tbl_name GLOB 'field_channel_*' ORDER BY name"
        )
    ]


@pytest.fixture
def legacy_copy(tmp_path):
    source = tmp_path / "historical-source.sqlite"
    target = tmp_path / "migration-copy.sqlite"
    with StorageV3(source) as store:
        controller = ControllerV2(store, now=AT)
        controller.recover(now=AT, operator_id="historical-operator", checks=CHECKS)
        # Field-v3 DDL did not change in CX-35. Seed an existing component,
        # including a durable configuration change, before either owner upgrades.
        for statement in field_channels._DDL.values():
            store.connection.execute(statement)
        store.connection.execute("INSERT INTO field_channel_schema VALUES(1,3)")
        for group in field_channels.GROUPS:
            state = field_channels.FieldFaultState(group_id=group)
            after = state.model_copy(update={"revision": 1, "mode": "BAD"})
            selected = after if group == "Sealer" else state
            store.connection.execute(
                "INSERT INTO field_channel_faults VALUES(?,?,?,3)",
                (group, encode_uint64(selected.revision), selected.model_dump_json()),
            )
            if group == "Sealer":
                store.connection.execute(
                    "INSERT INTO field_channel_audit VALUES(1,?,?,?,?,?,?,3,3)",
                    (
                        group,
                        encode_uint64(1),
                        "historical-operator",
                        AT.isoformat(),
                        state.model_dump_json(),
                        after.model_dump_json(),
                    ),
                )
        db = store.connection
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert rows(db, "runtime_schema_version") == [(1, 2)]
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE 'runtime_parameter%'"
        ).fetchall()
        for table in ("source_events", "archived_event_keys"):
            assert (
                "PARAMETERS_CHANGED"
                not in db.execute(
                    "SELECT sql FROM sqlite_master WHERE name=?", (table,)
                ).fetchone()[0]
            )
        historical = {
            name: rows(db, name)
            for name in (
                "source_events",
                "event_retention",
                "runtime_state",
                "runtime_audit",
                "field_channel_schema",
                "field_channel_faults",
                "field_channel_audit",
            )
        }
        assert all(historical.values())
        owned_ddl = field_ddl(db)
        with sqlite3.connect(target) as copy:
            db.backup(copy)
    return source, target, historical, owned_ddl


def test_real_storage3_runtime2_copy_upgrades_and_keeps_existing_field_history(legacy_copy):
    source, target, historical, owned_ddl = legacy_copy
    with SimulatorStorage(target, utc_clock=lambda: AT, monotonic_clock=lambda: 0.0) as store:
        db = store.connection
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert {name: rows(db, name) for name in historical} == historical
        SimulatorController(store, now=AT)
        assert rows(db, "runtime_schema_version") == [(1, 3)]
        assert (
            db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'runtime_parameter%'"
            ).fetchone()[0]
            == 3
        )
        component = field_channels.FieldChannels(store)
        sealer = next(state for state in component.states() if state.group_id == "Sealer")
        assert sealer.mode == "BAD"
        assert field_ddl(db) == owned_ddl
        for name in ("field_channel_schema", "field_channel_faults", "field_channel_audit"):
            assert rows(db, name) == historical[name]
        # Restart deliberately emits one RECOVERY_REQUIRED state event. Every
        # historical event/age remains byte-for-byte intact beside that append.
        old_ids = {row[0] for row in historical["source_events"]}
        events = rows(db, "source_events")
        assert [row for row in events if row[0] in old_ids] == historical["source_events"]
        added = [row for row in events if row[0] not in old_ids]
        assert len(added) == 1
        event_id, source_id, stream_id, seq, execution_id, item_id, body = added[0]
        before = json.loads(historical["runtime_state"][0][1])
        next_seq = int(before["last_event_seq"]) + 1
        next_version = int(before["control_version"]) + 1
        assert (source_id, stream_id) == (store.identity.source_id, store.identity.stream_id)
        assert seq == encode_uint64(next_seq)
        assert execution_id is None and item_id is None
        assert json.loads(body) == {
            "schema_version": "1.0",
            "event_id": event_id,
            "source_id": source_id,
            "stream_id": stream_id,
            "seq": str(next_seq),
            "equipment_id": "Line",
            "execution_id": None,
            "kind": "STATE_CHANGED",
            "occurred_at": AT.isoformat().replace("+00:00", "Z"),
            "payload": {
                "previous_state": before["state"],
                "state": "RECOVERY_REQUIRED",
                "control_version": str(next_version),
            },
        }
        assert rows(db, "event_retention") == sorted(
            [*historical["event_retention"], (event_id, AT.isoformat())]
        )
        audit = rows(db, "runtime_audit")
        assert audit[:-1] == historical["runtime_audit"]
        assert audit[-1][0] == historical["runtime_audit"][-1][0] + 1
        assert audit[-1][1:5] == (
            AT.isoformat(),
            "PROCESS_RESTART",
            None,
            encode_uint64(next_version),
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with SimulatorStorage(target) as store:
        SimulatorController(store, now=AT)
        assert len(field_channels.FieldChannels(store).states()) == 3
    # Migration touched only the SQLite backup in tmp_path, never its source.
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert rows(db, "runtime_schema_version") == [(1, 2)]
        assert {name: rows(db, name) for name in historical} == historical


def test_fresh_and_migrated_v4_accept_identical_field_integrity_contract(tmp_path, legacy_copy):
    _, target, _, _ = legacy_copy
    accepted = []
    for path in (target, tmp_path / "fresh.sqlite"):
        with SimulatorStorage(path) as store:
            SimulatorController(store, now=AT)
            component = field_channels.FieldChannels(store)
            component._check_environment()
            accepted.append((field_ddl(store.connection), component._layout()))
            assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 4
            assert rows(store.connection, "runtime_schema_version") == [(1, 3)]
    assert accepted[0] == accepted[1]


@pytest.mark.parametrize("kind", ["trigger", "view", "virtual", "known_trigger", "field_ddl"])
def test_v4_rejects_unknown_objects_and_tampered_known_ddl(tmp_path, kind):
    with SimulatorStorage(tmp_path / "untrusted.sqlite") as store:
        SimulatorController(store, now=AT)
        field_channels.FieldChannels(store)
        db = store.connection
        if kind == "trigger":
            db.execute(
                "CREATE TRIGGER unrelated AFTER UPDATE ON simulator_identity BEGIN SELECT 1; END"
            )
        elif kind == "view":
            db.execute("CREATE VIEW unrelated AS SELECT * FROM source_events")
        elif kind == "virtual":
            db.execute("CREATE VIRTUAL TABLE unrelated USING fts5(body)")
        elif kind == "known_trigger":
            db.execute("DROP TRIGGER runtime_parameters_no_update")
            db.execute(
                "CREATE TRIGGER runtime_parameters_no_update BEFORE UPDATE "
                "ON runtime_parameter_changes BEGIN SELECT 1; END"
            )
        else:
            db.execute("ALTER TABLE field_channel_faults ADD COLUMN unexpected TEXT")
        error = (
            StorageCorruption if kind == "field_ddl" else field_channels.FieldEnvironmentUnavailable
        )
        with pytest.raises(error):
            field_channels.FieldChannels(store)
