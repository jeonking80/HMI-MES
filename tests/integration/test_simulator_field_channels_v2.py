"""Frozen V2 SQLite regression prerequisites; live V3 has separate migration tests."""

from datetime import UTC, datetime, timedelta

import pytest
from asyncua import ua

from apps.simulator.controller import SimulatorController
from apps.simulator.plc import PLCState
from apps.simulator.storage import SimulatorStorage, StorageCorruption
from apps.simulator.tags import project_tags
from tests.fixtures import field_channels_v1 as v1
from tests.fixtures import field_channels_v2 as v2

NOW = datetime(2026, 10, 21, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    with SimulatorStorage(tmp_path / "stage.sqlite") as db:
        SimulatorController(db, now=NOW)
        yield db


def frame(c, status=ua.StatusCodes.Good, seconds=0, hb=2**53 + 1):
    at = NOW + timedelta(seconds=seconds)
    values = project_tags(PLCState(), None, counter_epoch="epoch", heartbeat=hb)
    return c.prepare(values, acquired_at=at, published_at=at, status_code=status)


def publish(c, **kwargs):
    fields, frames = frame(c, **kwargs)
    c.published(frames)
    return fields, frames


def change(c, mode, group="Sealer"):
    module = v1 if type(c) is v1.FieldChannels else v2
    state = next(s for s in c.states() if s.group_id == group)
    return c.set_fault(
        group,
        module.FieldFaultInput(mode=mode, expected_revision=state.revision),
        actor="operator",
        at=NOW,
    )


def evidence(db):
    return tuple(
        [tuple(r) for r in db.connection.execute(q)]
        for q in (
            "SELECT group_id,revision,CAST(state_json AS BLOB) FROM field_channel_faults "
            "ORDER BY group_id",
            "SELECT audit_id,group_id,revision,actor,changed_at,CAST(before_json AS "
            "BLOB),CAST(after_json AS BLOB) FROM field_channel_audit ORDER BY audit_id",
        )
    )


def schema(db):
    return [
        tuple(r)
        for r in db.connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE name GLOB 'field_channel_*' ORDER BY name"
        )
    ]


def legacy(db):
    c = v1.FieldChannels(db)
    publish(c)
    change(c, "FROZEN")
    change(c, "BAD", "Infeed")
    change(c, "NORMAL", "Infeed")
    change(c, "UNCERTAIN", "Outfeed")
    return c


def test_real_v1_bytes_and_mixed_payload_history(store):
    legacy(store)
    before = evidence(store)
    c = v2.FieldChannels(store)
    assert evidence(store) == before
    assert tuple(store.connection.execute("SELECT * FROM field_channel_schema").fetchone()) == (
        1,
        2,
    )
    assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 4
    assert store.connection.execute("SELECT version FROM runtime_schema_version").fetchone()[0] == 3
    old = store.connection.execute(
        "SELECT state_json FROM field_channel_faults WHERE group_id='Sealer'"
    ).fetchone()[0]
    change(c, "NORMAL")
    row = store.connection.execute(
        "SELECT before_json,before_payload_version,after_payload_version FROM "
        "field_channel_audit WHERE group_id='Sealer' ORDER BY revision DESC LIMIT 1"
    ).fetchone()
    assert tuple(row) == (old, 1, 2)
    publish(c)
    change(c, "READ_FAILURE")
    assert tuple(
        store.connection.execute(
            "SELECT before_payload_version,after_payload_version FROM field_channel_audit "
            "WHERE group_id='Sealer' ORDER BY revision DESC LIMIT 1"
        ).fetchone()
    ) == (2, 2)
    v2.FieldChannels(store)


@pytest.mark.parametrize(
    "step",
    [
        "state_payload_version",
        "before_payload_version",
        "after_payload_version",
        "metadata",
        "validated",
        "before_commit",
    ],
)
def test_migration_rollback_exact_v1_then_retry(store, step):
    legacy(store)
    before, ddl = evidence(store), schema(store)

    class Broken(v2.FieldChannels):
        def _migration_step(self, stage):
            if stage == step:
                raise RuntimeError("injected")

    with pytest.raises(RuntimeError):
        Broken(store)
    assert evidence(store) == before
    assert schema(store) == ddl
    v2.FieldChannels(store)


@pytest.mark.parametrize(
    "mode,code",
    [
        ("READ_FAILURE", ua.StatusCodes.BadCommunicationError),
        ("LINK_DOWN", ua.StatusCodes.BadNotConnected),
    ],
)
@pytest.mark.parametrize(
    "raw",
    [
        ua.StatusCodes.Good,
        ua.StatusCodes.BadSensorFailure,
        ua.StatusCodes.UncertainSensorNotAccurate,
    ],
)
def test_failure_quality_values_time_restart_and_normal_reentry(store, mode, code, raw):
    c = v2.FieldChannels(store)
    _, old = publish(c, status=raw)
    physical = [tuple(r) for r in store.connection.execute("SELECT * FROM runtime_state")]
    state = change(c, mode)
    assert state.normal_checkpoint == old["Sealer"]
    fields, _ = frame(c, seconds=10, hb=99)
    for source in old["Sealer"].fields:
        output = fields[source.tag_id]
        assert (output.reading, output.source_timestamp) == (
            source.reading,
            source.source_timestamp,
        )
        assert output.status_code == code
    assert all(f.status_code == raw for f in state.normal_checkpoint.fields)
    assert fields["Infeed.LinkHeartbeat"].reading.value == 99
    assert [tuple(r) for r in store.connection.execute("SELECT * FROM runtime_state")] == physical
    restarted = v2.FieldChannels(store)
    assert all(s.pending for s in restarted.describe())
    assert next(s for s in restarted.states() if s.group_id == "Sealer") == state
    change(restarted, "NORMAL")
    with pytest.raises(v2.FieldFaultConflict):
        change(restarted, mode)
    fields, _ = publish(restarted, status=ua.StatusCodes.UncertainSensorNotAccurate, hb=100)
    assert fields["Sealer.LinkHeartbeat"].status_code == ua.StatusCodes.UncertainSensorNotAccurate
    change(restarted, mode)


@pytest.mark.parametrize("mode", ["FROZEN", "READ_FAILURE", "LINK_DOWN"])
def test_actual_current_normal_provenance(store, mode):
    c = v2.FieldChannels(store)
    frame(c)
    with pytest.raises(v2.FieldFaultConflict):
        change(c, mode)
    publish(c)
    change(c, "BAD")
    publish(c)
    with pytest.raises(v2.FieldFaultConflict):
        change(c, mode)
    change(c, "NORMAL")
    with pytest.raises(v2.FieldFaultConflict):
        change(c, mode)
    publish(c)
    change(c, mode)
    with pytest.raises(v2.FieldFaultConflict):
        change(c, "LINK_DOWN" if mode != "LINK_DOWN" else "READ_FAILURE")


def test_outer_rollback_does_not_publish_memory(store):
    c = v2.FieldChannels(store)
    publish(c)
    before = c.describe()
    with pytest.raises(RuntimeError):
        with store.transaction():
            change(c, "LINK_DOWN")
            with pytest.raises(v2.FieldFaultConflict):
                frame(c)
            raise RuntimeError("rollback")
    assert c.describe() == before


def test_live_deleted_component_repeatedly_rejected(store):
    c = v2.FieldChannels(store)
    for table in ("field_channel_audit", "field_channel_faults", "field_channel_schema"):
        store.connection.execute(f"DROP TABLE {table}")
    for _ in range(2):
        with pytest.raises(StorageCorruption):
            c.states()


@pytest.mark.parametrize(
    "damage", ["version", "noop", "nonprefix", "partial", "old_json", "gap", "head"]
)
def test_v1_preflight_rejects_damage_without_migration(store, damage):
    legacy(store)
    db = store.connection
    if damage == "version":
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("UPDATE field_channel_schema SET version=9")
    elif damage == "noop":
        db.execute("DROP TRIGGER field_channel_audit_delete")
        db.execute(
            "CREATE TRIGGER field_channel_audit_delete BEFORE DELETE ON field_channel_audit "
            "BEGIN SELECT 1; END"
        )
    elif damage == "nonprefix":
        db.execute(
            "CREATE TRIGGER unexpected_side_effect AFTER UPDATE ON field_channel_faults BEGIN"
            " SELECT 1; END"
        )
    elif damage == "partial":
        db.execute("DROP TABLE field_channel_audit")
    elif damage in {"old_json", "gap"}:
        db.execute("DROP TRIGGER field_channel_audit_update")
        if damage == "old_json":
            db.execute("UPDATE field_channel_audit SET before_json='{}' WHERE audit_id=1")
        else:
            db.execute(
                "UPDATE field_channel_audit SET revision='00000000000000000009' WHERE audit_id=1"
            )
        db.execute(v1._DDL["field_channel_audit_update"])
    else:
        db.execute(
            "UPDATE field_channel_faults SET revision='00000000000000000009' WHERE "
            "group_id='Sealer'"
        )
    ddl = schema(store)
    with pytest.raises(StorageCorruption):
        v2.FieldChannels(store)
    assert schema(store) == ddl
    if damage != "partial":
        assert "state_payload_version" not in [
            r[1] for r in db.execute("PRAGMA table_info(field_channel_faults)")
        ]


@pytest.mark.parametrize(
    "outer,raw",
    [
        (1, '{"payload_version":2,"group_id":"Sealer","revision":"0","mode":"NORMAL"}'),
        (2, '{"group_id":"Sealer","revision":"0","mode":"NORMAL"}'),
        (9, "{}"),
        (2, "[]"),
        (2, '{"payload_version":2.0,"group_id":"Sealer"}'),
    ],
)
def test_strict_payload_discriminator(outer, raw):
    with pytest.raises(ValueError):
        v2.FieldChannels._parse(outer, raw)


@pytest.mark.parametrize("old_mode", ["FROZEN", "READ_FAILURE", "LINK_DOWN"])
def test_failed_config_commit_and_identical_noop(store, old_mode):
    c = v2.FieldChannels(store)
    publish(c)
    state = change(c, old_mode)
    before = evidence(store)
    assert change(c, old_mode) == state
    assert evidence(store) == before
    c._before_fault_commit = lambda: (_ for _ in ()).throw(RuntimeError("injected"))
    with pytest.raises(RuntimeError):
        change(c, "NORMAL")
    assert evidence(store) == before


def test_failed_full_history_cache_and_no_ddl_orphan(store):
    c = v2.FieldChannels(store)
    change(c, "BAD")
    change(c, "NORMAL")
    db = store.connection
    db.execute("DROP TRIGGER field_channel_audit_update")
    db.execute("UPDATE field_channel_audit SET before_json='{}' WHERE audit_id=1")
    db.execute(v2._DDL["field_channel_audit_update"])
    for _ in range(2):
        with pytest.raises(StorageCorruption):
            c.states()
    with pytest.raises(StorageCorruption):
        frame(c)


def test_unrelated_ddl_and_rollback_do_not_break_live_cache(store):
    c = v2.FieldChannels(store)
    before = c.states()
    with store.transaction():
        store.connection.execute("CREATE TABLE other_component(value TEXT)")
    assert c.states() == before
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.connection.execute("CREATE TABLE rolled_back_component(value TEXT)")
            assert c.states() == before
            raise RuntimeError("rollback")
    assert c.states() == before


def test_outer_migration_rollback_cannot_be_silently_repeated_by_live_object(store):
    legacy(store)
    before, ddl = evidence(store), schema(store)
    with pytest.raises(RuntimeError):
        with store.transaction():
            c = v2.FieldChannels(store)
            with pytest.raises(v2.FieldFaultConflict):
                frame(c)
            raise RuntimeError("outer rollback")
    assert schema(store) == ddl
    assert evidence(store) == before
    for _ in range(2):
        with pytest.raises(StorageCorruption):
            c.states()
    v2.FieldChannels(store)


def test_frozen_v1_parser_rejects_new_modes_and_keys():
    raw = '{"group_id":"Sealer","revision":"0","mode":"LINK_DOWN"}'
    with pytest.raises(ValueError):
        v2.FieldChannels._parse(1, raw)
    raw = '{"group_id":"Sealer","normal_checkpoint":null}'
    with pytest.raises(ValueError):
        v2.FieldChannels._parse(1, raw)


def test_v1_semantic_lineage_and_noncanonical_json_bytes_are_preserved(store):
    import json

    legacy(store)
    db = store.connection
    db.execute("DROP TRIGGER field_channel_audit_update")
    for audit_id, raw in db.execute(
        "SELECT audit_id,before_json FROM field_channel_audit"
    ).fetchall():
        changed = json.dumps(json.loads(raw), indent=3, sort_keys=True)
        db.execute(
            "UPDATE field_channel_audit SET before_json=? WHERE audit_id=?", (changed, audit_id)
        )
    db.execute(v1._DDL["field_channel_audit_update"])
    before = evidence(store)
    v2.FieldChannels(store)
    assert evidence(store) == before


def test_mixed_payload_downgrade_and_orphan_future_are_rejected(store):
    import json

    legacy(store)
    c = v2.FieldChannels(store)
    change(c, "NORMAL")
    db = store.connection
    raw = db.execute(
        "SELECT state_json FROM field_channel_faults WHERE group_id='Sealer'"
    ).fetchone()[0]
    before = c.states()
    normal = next(s for s in before if s.group_id == "Sealer")
    # No DDL change: latest audit bounded lookup must detect this future orphan.
    after = normal.model_copy(update={"revision": normal.revision + 1, "mode": "BAD"})
    db.execute(
        "INSERT INTO "
        "field_channel_audit(group_id,revision,actor,changed_at,before_json,after_json,before_payload_version,after_payload_version)"
        " VALUES(?,?,?,?,?,?,2,2)",
        (
            "Sealer",
            str(after.revision).zfill(20),
            "operator",
            NOW.isoformat(),
            raw,
            after.model_dump_json(),
        ),
    )
    with pytest.raises(StorageCorruption):
        c.states()
    with pytest.raises(StorageCorruption):
        v2.FieldChannels(store)
    legacy_after = json.loads(after.model_dump_json())
    legacy_after.pop("payload_version")
    legacy_after.pop("normal_checkpoint")
    row = (
        "Sealer",
        str(after.revision).zfill(20),
        "operator",
        NOW.isoformat(),
        raw,
        json.dumps(legacy_after),
        2,
        1,
    )
    with pytest.raises(ValueError):
        c._audit_states(row)


def test_actual_sqlite_commit_denial_restores_v1(store):
    import sqlite3

    legacy(store)
    before, ddl = evidence(store), schema(store)

    def authorizer(action, first, second, database, trigger):
        if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    store.connection.set_authorizer(authorizer)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            v2.FieldChannels(store)
    finally:
        store.connection.set_authorizer(None)
    assert schema(store) == ddl
    assert evidence(store) == before
    v2.FieldChannels(store)


def test_mixed_original_quality_uint64max_survives_actual_close_reopen(tmp_path):
    path = tmp_path / "restart.sqlite"
    with SimulatorStorage(path) as db:
        SimulatorController(db, now=NOW)
        c = v2.FieldChannels(db)
        _, frames = frame(c, hb=2**64 - 1)
        original = frames["Sealer"]
        codes = (
            ua.StatusCodes.Good,
            ua.StatusCodes.UncertainSensorNotAccurate,
            ua.StatusCodes.BadSensorFailure,
        )
        fields = tuple(
            field.model_copy(update={"status_code": codes[i % 3]})
            for i, field in enumerate(original.fields)
        )
        frames["Sealer"] = original.model_copy(update={"fields": fields})
        c.published(frames)
        state = change(c, "LINK_DOWN")
        assert state.normal_checkpoint.fields == fields
    with SimulatorStorage(path) as db:
        c = v2.FieldChannels(db)
        restored = next(s for s in c.states() if s.group_id == "Sealer")
        assert restored == state
        assert all(s.pending for s in c.describe())
        output, _ = frame(c, seconds=10, hb=0)
        assert output["Sealer.LinkHeartbeat"].reading.value == 2**64 - 1
        assert all(output[f.tag_id].status_code == ua.StatusCodes.BadNotConnected for f in fields)
        assert db.recovery_required
