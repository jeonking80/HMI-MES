"""Stage storage-only V3 checks; no OPC/publisher/HTTP/UI/browser execution."""

import json
import sqlite3
from datetime import UTC, datetime

import pytest
from asyncua import ua

from apps.simulator import field_channels as v3
from apps.simulator.controller import SimulatorController
from apps.simulator.plc import PLCState, RecoveryChecks
from apps.simulator.storage import SimulatorStorage, StorageCorruption
from apps.simulator.tags import project_tags
from tests.fixtures import field_channels_v1 as v1
from tests.fixtures import field_channels_v2 as v2

NOW = datetime(2026, 10, 21, tzinfo=UTC)
CHECKS = RecoveryChecks(
    outputs_stopped=True,
    positions_verified=True,
    storage_reconciled=True,
    commands_reconciled=True,
    operator_confirmed=True,
)


@pytest.fixture
def store(tmp_path):
    with SimulatorStorage(tmp_path / "v3.sqlite") as db:
        c = SimulatorController(db, now=NOW)
        c.recover(now=NOW, operator_id="operator", checks=CHECKS)
        assert db.connection.execute("SELECT COUNT(*) FROM source_events").fetchone()[0] > 0
        yield db


def publish(component, status=ua.StatusCodes.Good):
    values = project_tags(PLCState(), None, counter_epoch="epoch", heartbeat=2**64 - 1)
    _, frames = component.prepare(values, acquired_at=NOW, published_at=NOW, status_code=status)
    component.published(frames)
    return frames


def change(component, mode, group="Sealer", delay=None):
    module = (
        v1
        if type(component) is v1.FieldChannels
        else v2
        if type(component) is v2.FieldChannels
        else v3
    )
    state = next(s for s in component.states() if s.group_id == group)
    args = {"mode": mode, "expected_revision": state.revision}
    if delay is not None:
        args["delay_ms"] = delay
    return component.set_fault(group, module.FieldFaultInput(**args), actor="operator", at=NOW)


def legacy(store, version, mode="FROZEN"):
    module = v1 if version == 1 else v2
    c = module.FieldChannels(store)
    publish(c)
    change(c, mode)
    change(c, "BAD", group="Infeed")
    change(c, "NORMAL", group="Infeed")
    return c


def evidence(db, layout):
    hv = "1" if layout == 1 else "state_payload_version"
    av = "1,1" if layout == 1 else "before_payload_version,after_payload_version"
    return tuple(
        [tuple(r) for r in db.connection.execute(q)]
        for q in (
            f"SELECT rowid,group_id,revision,CAST(state_json AS BLOB),{hv} "
            "FROM main.field_channel_faults ORDER BY group_id",
            "SELECT audit_id,group_id,revision,actor,changed_at,CAST(before_json AS BLOB),"
            f"CAST(after_json AS BLOB),{av} FROM main.field_channel_audit ORDER BY audit_id",
        )
    )


def ddl(db):
    return [
        tuple(r)
        for r in db.connection.execute(
            "SELECT name,type,tbl_name,sql FROM main.sqlite_master ORDER BY name"
        )
    ]


def test_fresh_v3_canonical_and_explicit_discriminators(store):
    c = v3.FieldChannels(store)
    assert tuple(store.connection.execute("SELECT * FROM field_channel_schema").fetchone()) == (
        1,
        3,
    )
    assert len(c.states()) == 3 and all(s.payload_version == 3 for s in c.states())
    for table, columns in [
        ("field_channel_faults", {"state_payload_version"}),
        ("field_channel_audit", {"before_payload_version", "after_payload_version"}),
    ]:
        assert all(
            r[4] is None
            for r in store.connection.execute(f"PRAGMA main.table_info({table})")
            if r[1] in columns
        )
    assert not list(store.connection.execute("SELECT * FROM temp.sqlite_master"))


@pytest.mark.parametrize(
    "version,mode",
    [(1, m) for m in ("NORMAL", "FROZEN", "BAD", "UNCERTAIN")]
    + [(2, m) for m in ("NORMAL", "FROZEN", "BAD", "UNCERTAIN", "READ_FAILURE", "LINK_DOWN")],
)
def test_direct_migration_exact_bytes_ids_nonfield_and_restart(store, version, mode):
    legacy(store, version, mode)
    before, schema = evidence(store, version), ddl(store)
    events = [tuple(r) for r in store.connection.execute("SELECT * FROM source_events")]
    c = v3.FieldChannels(store)
    assert evidence(store, 3) == before
    assert [tuple(r) for r in store.connection.execute("SELECT * FROM source_events")] == events
    field_names = set(v3._DDL)
    assert [r for r in ddl(store) if r[0] not in field_names and r[2] not in v3._OWNED] == [
        r for r in schema if r[0] not in field_names and r[2] not in v3._OWNED
    ]
    assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 3
    assert store.connection.execute("SELECT version FROM runtime_schema_version").fetchone()[0] == 2
    assert all(s.pending for s in c.describe())
    v3.FieldChannels(store)


STEPS = [
    "staged_heads",
    "staged_audit",
    "staged_verified",
    "dropped_audit",
    "dropped_faults",
    "dropped_schema",
    "created_field_channel_faults",
    "created_field_channel_audit",
    "created_field_channel_schema",
    "created_field_channel_audit_update",
    "created_field_channel_audit_delete",
    "copied_heads",
    "copied_audit",
    "validated",
    "dropped_stage_field_v3_heads_stage",
    "dropped_stage_field_v3_audit_stage",
    "staging_dropped",
    "before_commit",
]


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("step", STEPS)
def test_each_migration_failure_restores_old_schema_and_bytes(store, version, step):
    legacy(store, version)
    original, schema = evidence(store, version), ddl(store)

    class Broken(v3.FieldChannels):
        def _migration_step(self, at):
            if at == step:
                raise RuntimeError("injected")

    with pytest.raises(RuntimeError):
        Broken(store)
    assert ddl(store) == schema
    assert evidence(store, version) == original
    assert not list(store.connection.execute("SELECT * FROM temp.sqlite_master"))
    (v1 if version == 1 else v2).FieldChannels(store)
    v3.FieldChannels(store)


@pytest.mark.parametrize("version", [1, 2])
def test_actual_commit_denial_preserves_authorizer_and_old_schema(store, version):
    legacy(store, version)
    original, schema = evidence(store, version), ddl(store)
    commits = []

    def authorizer(action, first, second, database, trigger):
        if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT":
            commits.append(True)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    store.connection.set_authorizer(authorizer)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            v3.FieldChannels(store)
        assert commits == [True]
    finally:
        store.connection.set_authorizer(None)
    assert ddl(store) == schema and evidence(store, version) == original
    assert not store.connection.in_transaction
    v3.FieldChannels(store)


@pytest.mark.parametrize("version", [1, 2])
def test_outer_rollback_old_objects_and_provisional_v3_fail_closed(store, version):
    old = legacy(store, version)
    before, schema = evidence(store, version), ddl(store)
    with pytest.raises(RuntimeError):
        with store.transaction():
            candidate = v3.FieldChannels(store)
            raise RuntimeError("outer rollback")
    assert ddl(store) == schema and evidence(store, version) == before
    for _ in range(2):
        with pytest.raises(StorageCorruption):
            candidate.states()
    old.states()
    v3.FieldChannels(store)
    with pytest.raises(StorageCorruption):
        old.states()


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize(
    "kind",
    [
        "foreign_restrict",
        "foreign_cascade",
        "view",
        "trigger",
        "prefixed_trigger",
        "missing_storage_trigger",
        "missing_runtime_trigger",
        "temp",
        "temp_identity",
        "attached",
        "virtual",
    ],
)
def test_unsupported_environment_refused_without_latch_or_external_changes(store, version, kind):
    legacy(store, version)
    db = store.connection
    if kind.startswith("foreign"):
        action = "CASCADE" if kind.endswith("cascade") else "RESTRICT"
        db.execute(
            'CREATE TABLE unrelated(ref TEXT REFERENCES "FiElD_ChAnNeL_FaUlTs"(group_id) '
            f"ON DELETE {action})"
        )
        db.execute("INSERT INTO unrelated VALUES('Sealer')")
    elif kind == "view":
        db.execute("CREATE VIEW unrelated AS SELECT * FROM main.field_channel_faults")
    elif kind in {"trigger", "prefixed_trigger"}:
        name = "field_channel_fake" if kind == "prefixed_trigger" else "unrelated_side_effect"
        db.execute(
            f"CREATE TRIGGER {name} AFTER UPDATE ON simulator_identity "
            "BEGIN DELETE FROM source_events; END"
        )
    elif kind == "missing_storage_trigger":
        db.execute("DROP TRIGGER retention_age_no_update")
    elif kind == "missing_runtime_trigger":
        db.execute("DROP TRIGGER runtime_final_immutable")
    elif kind == "temp":
        db.execute("CREATE TEMP TABLE unrelated(value TEXT)")
    elif kind == "temp_identity":
        db.execute(
            "CREATE TEMP TABLE simulator_identity(singleton INTEGER,recovery_required INTEGER)"
        )
        db.execute("INSERT INTO temp.simulator_identity VALUES(1,0)")
    elif kind == "attached":
        db.execute("ATTACH DATABASE ':memory:' AS extra")
    else:
        db.execute("CREATE VIRTUAL TABLE unrelated USING fts5(value)")
    before = evidence(store, version)
    events = [tuple(r) for r in db.execute("SELECT * FROM main.source_events")]
    flag = db.execute("SELECT recovery_required FROM main.simulator_identity").fetchone()[0]
    with pytest.raises(v3.FieldEnvironmentUnavailable):
        v3.FieldChannels(store)
    assert evidence(store, version) == before
    assert [tuple(r) for r in db.execute("SELECT * FROM main.source_events")] == events
    assert db.execute("SELECT recovery_required FROM main.simulator_identity").fetchone()[0] == flag
    if kind == "temp_identity":
        assert (
            db.execute("SELECT recovery_required FROM temp.simulator_identity").fetchone()[0] == 0
        )


@pytest.mark.parametrize("entry", ["states", "prepare", "set_fault"])
@pytest.mark.parametrize("kind", ["temp", "attached"])
def test_live_namespace_guard_ignores_main_cookie_and_nested_unit_does_not_latch(
    store, entry, kind
):
    c = v3.FieldChannels(store)
    publish(c)
    db = store.connection
    cookie = db.execute("PRAGMA main.schema_version").fetchone()[0]
    if kind == "temp":
        db.execute(
            "CREATE TEMP TABLE simulator_identity(singleton INTEGER,recovery_required INTEGER)"
        )
        db.execute("INSERT INTO temp.simulator_identity VALUES(1,0)")
    else:
        db.execute("ATTACH DATABASE ':memory:' AS extra")
    assert db.execute("PRAGMA main.schema_version").fetchone()[0] == cookie
    before = db.execute("SELECT recovery_required FROM main.simulator_identity").fetchone()[0]
    with store.transaction():
        with pytest.raises(v3.FieldEnvironmentUnavailable):
            if entry == "states":
                c.states()
            elif entry == "prepare":
                publish(c)
            else:
                change(c, "BAD")
    assert (
        db.execute("SELECT recovery_required FROM main.simulator_identity").fetchone()[0] == before
    )
    if kind == "temp":
        assert (
            db.execute("SELECT recovery_required FROM temp.simulator_identity").fetchone()[0] == 0
        )


@pytest.mark.parametrize("source", [1, 2, 3])
def test_new_v3_bridge_delay_checkpoint_and_storage_only_fail_closed(store, source):
    if source != 3:
        legacy(store, source, mode="NORMAL")
    c = v3.FieldChannels(store)
    publish(c, status=ua.StatusCodes.UncertainSensorNotAccurate)
    raw_before, version = store.connection.execute(
        "SELECT state_json,state_payload_version FROM field_channel_faults WHERE group_id='Sealer'"
    ).fetchone()
    state = change(c, "DELAY", delay=6000)
    assert (
        state.normal_checkpoint.fields[0].status_code == ua.StatusCodes.UncertainSensorNotAccurate
    )
    audit = store.connection.execute(
        "SELECT before_json,before_payload_version,after_payload_version "
        "FROM field_channel_audit WHERE group_id='Sealer' ORDER BY revision DESC LIMIT 1"
    ).fetchone()
    assert tuple(audit) == (raw_before, version, 3)
    assert change(c, "DELAY", delay=6000) == state
    with pytest.raises(v3.FieldFaultConflict):
        change(c, "DELAY", delay=3000)
    with pytest.raises(v3.FieldFaultConflict):
        publish(c)
    with pytest.raises(v3.FieldFaultConflict):
        change(c, "FROZEN")
    restarted = v3.FieldChannels(store)
    assert all(s.pending for s in restarted.describe())
    assert next(s for s in restarted.states() if s.group_id == "Sealer") == state
    change(restarted, "NORMAL")
    with pytest.raises(v3.FieldFaultConflict):
        change(restarted, "DELAY", delay=6000)
    publish(restarted)
    change(restarted, "DELAY", delay=100)
    v3.FieldChannels(store)


def test_v3_raw_head_change_is_rejected_without_schema_cookie_change(store):
    c = v3.FieldChannels(store)
    change(c, "BAD")
    db = store.connection
    cookie = db.execute("PRAGMA main.schema_version").fetchone()[0]
    raw = db.execute(
        "SELECT state_json FROM field_channel_faults WHERE group_id='Sealer'"
    ).fetchone()[0]
    db.execute(
        "UPDATE field_channel_faults SET state_json=? WHERE group_id='Sealer'",
        (json.dumps(json.loads(raw), indent=2),),
    )
    assert db.execute("PRAGMA main.schema_version").fetchone()[0] == cookie
    for _ in range(2):
        with pytest.raises(StorageCorruption):
            c.states()


def test_environment_rechecked_after_write_lock_before_destructive_work(store, monkeypatch):
    from contextlib import contextmanager

    legacy(store, 2)
    original, schema = evidence(store, 2), ddl(store)
    real = store.transaction
    path = store.path
    seen = []

    @contextmanager
    def raced_transaction():
        # Simulate another connection winning between readonly preflight and BEGIN.
        with sqlite3.connect(path) as other:
            other.execute("CREATE VIEW raced_view AS SELECT * FROM field_channel_faults")
        seen.append(True)
        with real() as connection:
            yield connection

    monkeypatch.setattr(store, "transaction", raced_transaction)
    with pytest.raises(v3.FieldEnvironmentUnavailable):
        v3.FieldChannels(store)
    assert seen == [True] and evidence(store, 2) == original
    assert [r for r in ddl(store) if r[0] != "raced_view"] == schema


def test_real_mixed_legacy_history_noncanonical_bytes_and_head_rowids_preserved(store):
    old = v1.FieldChannels(store)
    publish(old)
    change(old, "BAD", group="Infeed")
    newer = v2.FieldChannels(store)
    change(newer, "NORMAL", group="Infeed")
    publish(newer)
    change(newer, "LINK_DOWN")
    db = store.connection
    db.execute("DROP TRIGGER field_channel_audit_update")
    for ident, raw in db.execute("SELECT audit_id,before_json FROM field_channel_audit").fetchall():
        db.execute(
            "UPDATE field_channel_audit SET before_json=? WHERE audit_id=?",
            (json.dumps(json.loads(raw), indent=2, sort_keys=True), ident),
        )
    db.execute(v2._DDL["field_channel_audit_update"])
    db.execute("UPDATE field_channel_faults SET rowid=55 WHERE group_id='Outfeed'")
    before = evidence(store, 2)
    c = v3.FieldChannels(store)
    assert evidence(store, 3) == before
    change(c, "NORMAL")
    publish(c)
    change(c, "DELAY", delay=6000)
    v3.FieldChannels(store)


@pytest.mark.parametrize("version", [1, 2])
def test_owned_write_transaction_blocks_other_connection_ddl(store, version):
    legacy(store, version)
    attempts = []

    class Concurrent(v3.FieldChannels):
        def _migration_step(self, stage):
            if stage == "staged_heads":
                with sqlite3.connect(store.path, timeout=0) as other:
                    with pytest.raises(sqlite3.OperationalError):
                        other.execute("CREATE VIEW concurrent_view AS SELECT 1")
                attempts.append(True)

    Concurrent(store)
    assert attempts == [True]
    assert not store.connection.execute(
        "SELECT 1 FROM main.sqlite_master WHERE name='concurrent_view'"
    ).fetchone()


@pytest.mark.parametrize("version", [1, 2])
def test_actual_close_reopen_v3_delay_checkpoint_and_recovery(tmp_path, version):
    path = tmp_path / "restart.sqlite"
    with SimulatorStorage(path) as db:
        c = SimulatorController(db, now=NOW)
        c.recover(now=NOW, operator_id="operator", checks=CHECKS)
        legacy(db, version, mode="NORMAL")
        c = v3.FieldChannels(db)
        publish(c, status=ua.StatusCodes.BadSensorFailure)
        state = change(c, "DELAY", delay=6000)
        before = evidence(db, 3)
    with SimulatorStorage(path) as db:
        c = v3.FieldChannels(db)
        assert evidence(db, 3) == before
        assert next(s for s in c.states() if s.group_id == "Sealer") == state
        assert all(s.pending for s in c.describe()) and db.recovery_required
        with pytest.raises(v3.FieldFaultConflict):
            publish(c)


@pytest.mark.parametrize(
    "version,raw",
    [
        (1, '{"group_id":"Sealer","mode":"DELAY","revision":"0"}'),
        (2, '{"payload_version":3,"group_id":"Sealer","mode":"NORMAL","revision":"0"}'),
        (3, '{"payload_version":2,"group_id":"Sealer","mode":"NORMAL","revision":"0"}'),
        (3, '{"payload_version":3.0,"group_id":"Sealer"}'),
        (3, '{"group_id":"Sealer","mode":"NORMAL","revision":"0"}'),
        (4, "{}"),
    ],
)
def test_frozen_payload_versions_cannot_admit_future_mode_or_wrong_marker(version, raw):
    with pytest.raises(ValueError):
        v3.FieldChannels._parse(version, raw)


@pytest.mark.parametrize(
    "damage", ["old_json", "noop_field_trigger", "unknown_field_table", "unknown_version"]
)
def test_valid_namespace_payload_corruption_uses_existing_latch(store, damage):
    legacy(store, 2)
    db = store.connection
    if damage == "old_json":
        db.execute("DROP TRIGGER field_channel_audit_update")
        db.execute("UPDATE field_channel_audit SET before_json='{}' WHERE audit_id=1")
        db.execute(v2._DDL["field_channel_audit_update"])
    elif damage == "noop_field_trigger":
        db.execute("DROP TRIGGER field_channel_audit_delete")
        db.execute(
            "CREATE TRIGGER field_channel_audit_delete BEFORE DELETE ON field_channel_audit "
            "BEGIN SELECT 1; END"
        )
    elif damage == "unknown_field_table":
        db.execute("CREATE TABLE FiElD_ChAnNeL_extra(value TEXT)")
    else:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("UPDATE field_channel_schema SET version=9")
    with pytest.raises(StorageCorruption):
        v3.FieldChannels(store)
    assert db.execute("SELECT recovery_required FROM main.simulator_identity").fetchone()[0] == 1


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("nested", [False, True])
def test_exact_legacy_schema_actual_fk_row_violation_uses_corruption_latch(store, version, nested):
    legacy(store, version)
    db = store.connection
    # Privileged fixture corruption only; production migration never disables FK checks.
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DELETE FROM main.field_channel_faults WHERE group_id='Sealer'")
    db.execute("PRAGMA foreign_keys=ON")
    assert db.execute("PRAGMA main.foreign_key_check").fetchone() is not None
    if nested:
        with pytest.raises(StorageCorruption):
            with store.transaction():
                with pytest.raises(StorageCorruption):
                    v3.FieldChannels(store)
    else:
        with pytest.raises(StorageCorruption):
            v3.FieldChannels(store)
    assert db.execute("SELECT recovery_required FROM main.simulator_identity").fetchone()[0] == 1


def test_namespace_shadow_wins_over_fk_row_corruption_without_latch(store):
    legacy(store, 2)
    db = store.connection
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DELETE FROM main.field_channel_faults WHERE group_id='Sealer'")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("CREATE TEMP TABLE simulator_identity(singleton INTEGER,recovery_required INTEGER)")
    db.execute("INSERT INTO temp.simulator_identity VALUES(1,0)")
    flag = db.execute("SELECT recovery_required FROM main.simulator_identity").fetchone()[0]
    with store.transaction():
        with pytest.raises(v3.FieldEnvironmentUnavailable):
            v3.FieldChannels(store)
    assert db.execute("SELECT recovery_required FROM main.simulator_identity").fetchone()[0] == flag
    assert db.execute("SELECT recovery_required FROM temp.simulator_identity").fetchone()[0] == 0
