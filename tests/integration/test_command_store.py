"""Real SQLite journal boundaries; policy/evidence fixtures are not an operating PLC."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.simulator.command_store import CommandStore
from apps.simulator.process import fresh_execution, step
from apps.simulator.storage import SimulatorStorage, StorageCorruption
from contracts.models import (
    UINT64_MAX,
    CommandRequest,
    CommandResult,
    CommandSubmission,
)

AT = datetime(2026, 10, 20, 12, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def request(**changes):
    values = {
        "command_id": "command-A",
        "target_id": "Line",
        "execution_id": "execution-A",
        "command": {"kind": "START"},
        "requested_by": "fixture-operator",
        "requested_at": AT,
        "expires_at": AT + timedelta(seconds=5),
        "expected_control_version": 3,
    }
    values.update(changes)
    return CommandRequest(**values)


def accept(command, *, at=AT):
    return CommandSubmission(command_id=command.command_id, disposition="ACCEPTED", received_at=at)


def fail(*, at=AT + timedelta(seconds=1)):
    return CommandResult(
        command_id="command-A",
        status="FAILED",
        updated_at=at,
        delivered_at=AT,
        completed_at=at,
        reason_code="FIXTURE_FAILURE",
        reason="A test-only final failure was supplied by the caller",
    )


def initialize(database):
    with database.transaction():
        CommandStore.initialize_schema(database.connection)
    return CommandStore(database)


@pytest.fixture
def stores(tmp_path):
    with SimulatorStorage(tmp_path / "simulator.sqlite3") as database:
        database.acknowledge_recovery()
        yield database, initialize(database)


def test_explicit_additive_schema_is_idempotent_and_preserves_simulator_version(tmp_path):
    with SimulatorStorage(tmp_path / "simulator.sqlite3") as database:
        original_version = database.connection.execute("PRAGMA user_version").fetchone()[0]
        journal = initialize(database)
        assert journal.lookup("missing") is None
        with database.transaction():
            CommandStore.initialize_schema(database.connection)
        assert database.connection.execute("PRAGMA user_version").fetchone()[0] == original_version
        assert (
            database.connection.execute("SELECT * FROM command_schema_version").fetchone()[1] == 1
        )
        with pytest.raises(ValueError, match="caller-owned"):
            CommandStore.initialize_schema(database.connection)


def test_schema_install_rolls_back_with_outer_transaction(tmp_path):
    with SimulatorStorage(tmp_path / "simulator.sqlite3") as database:
        original_version = database.connection.execute("PRAGMA user_version").fetchone()[0]
        with pytest.raises(RuntimeError):
            with database.transaction():
                CommandStore.initialize_schema(database.connection)
                raise RuntimeError("rollback the explicit migration")
        tables = {
            row[0]
            for row in database.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "command_schema_version" not in tables
        assert "command_journal" not in tables
        assert database.connection.execute("PRAGMA user_version").fetchone()[0] == original_version


@pytest.mark.parametrize("version", [0, 2])
def test_unknown_extension_schema_requires_migration(stores, version):
    database, _ = stores
    with database.transaction():
        # Version zero is a malformed fixture: CHECK prevents producing it normally.
        database.connection.execute("PRAGMA ignore_check_constraints=ON")
        database.connection.execute("UPDATE command_schema_version SET version=?", (version,))
        database.connection.execute("PRAGMA ignore_check_constraints=OFF")
        with pytest.raises(ValueError, match="migration"):
            CommandStore.initialize_schema(database.connection)


def test_receipt_and_rejection_require_outer_commit_and_roll_back_together(stores):
    database, journal = stores
    with pytest.raises(ValueError, match="caller-owned"):
        journal.submit(request(), received_at=AT, decide=accept)
    with pytest.raises(RuntimeError):
        with database.transaction():
            outcome = journal.submit(request(), received_at=AT, decide=accept)
            assert outcome.created and outcome.result.status == "ACCEPTED"
            raise RuntimeError("before acknowledgement")
    assert journal.lookup("command-A") is None


def test_double_submit_checks_existing_before_expiry_or_new_version_policy(stores):
    database, journal = stores
    calls = []

    def decision(command):
        calls.append(command.command_id)
        return accept(command)

    with database.transaction():
        first = journal.submit(request(), received_at=AT, decide=decision)
        duplicate = journal.submit(request(), received_at=AT + timedelta(days=1), decide=decision)
    assert calls == ["command-A"]
    assert first.created and not duplicate.created
    assert duplicate.submission == first.submission
    assert duplicate.result == first.result
    assert database.connection.execute("SELECT count(*) FROM command_journal").fetchone()[0] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"target_id": "OtherLine"},
        {"execution_id": "execution-B"},
        {"command": {"kind": "PAUSE"}},
        {"requested_by": "another-user"},
        {"expected_control_version": 4},
        {"expires_at": AT + timedelta(seconds=6)},
        {"requested_at": AT - timedelta(seconds=1)},
    ],
)
def test_same_id_changed_content_conflicts_without_overwriting(stores, changes):
    database, journal = stores
    with database.transaction():
        first = journal.submit(request(), received_at=AT, decide=accept)
        original = journal.lookup("command-A")
        conflict = journal.submit(
            request(**changes), received_at=AT + timedelta(days=1), decide=lambda _: pytest.fail()
        )
    assert conflict.submission.reason_code == "COMMAND_ID_CONFLICT"
    assert conflict.submission.disposition == "REJECTED"
    assert not conflict.created and conflict.result is None
    assert journal.lookup("command-A") == original
    assert journal.lookup("command-A").submission == first.submission


def test_equivalent_offset_and_omitted_args_are_duplicate(stores):
    database, journal = stores
    wire = json.loads(request().model_dump_json())
    wire["requested_at"] = "2026-10-20T21:00:00+09:00"
    wire["expires_at"] = "2026-10-20T21:00:05+09:00"
    wire["command"].pop("args")
    with database.transaction():
        original = journal.submit(request(), received_at=AT, decide=accept)
        duplicate = journal.submit(
            CommandRequest.model_validate_json(json.dumps(wire)),
            received_at=AT + timedelta(seconds=1),
            decide=lambda _: pytest.fail(),
        )
    assert not duplicate.created
    assert duplicate.submission == original.submission


def test_initial_plc_deadline_rejection_is_persisted_delivered_and_never_expired(stores):
    database, journal = stores
    late = AT + timedelta(seconds=5)
    with database.transaction():
        first = journal.submit(request(), received_at=late, decide=lambda _: pytest.fail())
        duplicate = journal.submit(request(), received_at=late + timedelta(days=1), decide=accept)
    assert first.created and not duplicate.created
    assert first.submission.reason_code == "REQUEST_EXPIRED"
    assert first.result.status == "REJECTED"
    assert first.result.delivered_at == first.result.completed_at == late
    assert duplicate.result == first.result


def test_policy_rejection_is_durable_and_not_reconsidered_when_version_changes(stores):
    database, journal = stores

    def stale_version(command):
        return CommandSubmission(
            command_id=command.command_id,
            disposition="REJECTED",
            received_at=AT,
            reason_code="CONTROL_VERSION_MISMATCH",
            reason="Test policy observes a different current version",
        )

    with database.transaction():
        rejected = journal.submit(request(), received_at=AT, decide=stale_version)
        duplicate = journal.submit(request(), received_at=AT, decide=accept)
    assert duplicate.result == rejected.result
    assert duplicate.result.status == "REJECTED"


def test_savepoint_reverts_policy_writes_when_caller_catches_error(stores):
    database, journal = stores
    with database.transaction():
        database.connection.execute("CREATE TABLE fixture_policy(value INTEGER)")

        def invalid_decision(command):
            database.connection.execute("INSERT INTO fixture_policy VALUES (1)")
            return accept(command, at=AT + timedelta(seconds=1))

        with pytest.raises(ValueError, match="receipt timestamp"):
            journal.submit(request(), received_at=AT, decide=invalid_decision)
        assert database.connection.execute("SELECT count(*) FROM fixture_policy").fetchone()[0] == 0
    assert journal.lookup("command-A") is None


def test_reopen_returns_unresolved_acceptance_and_final_result_without_rerun(tmp_path):
    path = tmp_path / "simulator.sqlite3"
    with SimulatorStorage(path) as database:
        journal = initialize(database)
        with database.transaction():
            accepted = journal.submit(request(), received_at=AT, decide=accept)
    with SimulatorStorage(path) as database:
        journal = initialize(database)
        assert database.recovery_required
        assert journal.lookup("command-A").result == accepted.result
        assert journal.pending_records() == (journal.lookup("command-A"),)
        with database.transaction():
            replay = journal.submit(
                request(), received_at=AT + timedelta(days=1), decide=lambda _: pytest.fail()
            )
            assert not replay.created
            assert replay.result.status == "ACCEPTED"
            journal.complete("command-A", fail())
    with SimulatorStorage(path) as database:
        journal = initialize(database)
        assert journal.lookup("command-A").result == fail()
        assert journal.pending_records() == ()
        with database.transaction():
            replay = journal.submit(
                request(), received_at=AT + timedelta(days=2), decide=lambda _: pytest.fail()
            )
        assert not replay.created and replay.result == fail()


def test_final_outcomes_are_immutable_and_timeout_cannot_complete(stores):
    database, journal = stores
    with database.transaction():
        journal.submit(request(), received_at=AT, decide=accept)
        unknown = CommandResult(
            command_id="command-A",
            status="OUTCOME_UNKNOWN",
            updated_at=AT + timedelta(seconds=10),
            delivered_at=AT,
            reason_code="WAIT_TIMEOUT",
            reason="Wait duration alone proves no execution outcome",
        )
        with pytest.raises(ValueError, match="proven"):
            journal.complete("command-A", unknown)
        journal.complete("command-A", fail())
        assert journal.complete("command-A", fail()).result == fail()
        with pytest.raises(ValueError, match="overwritten"):
            journal.complete("command-A", fail(at=AT + timedelta(seconds=2)))
        with pytest.raises(ValueError, match="unknown"):
            journal.complete("missing", fail())


@pytest.mark.parametrize("version", [2**53 + 1, 2**63, UINT64_MAX])
def test_uint64_request_and_caller_completion_are_lossless_after_reopen(tmp_path, version):
    path = tmp_path / "simulator.sqlite3"
    command = request(expected_control_version=version)
    # Contract fixture tests encoding only; it is not a PLC completion assertion.
    evidence_fixture = CommandResult(
        command_id="command-A",
        status="SUCCEEDED",
        updated_at=AT + timedelta(seconds=1),
        delivered_at=AT,
        completed_at=AT + timedelta(seconds=1),
        applied_control_version=version,
        evidence={"state": "RUNNING", "execution_id": "execution-A", "last_event_seq": version},
    )
    with SimulatorStorage(path) as database:
        journal = initialize(database)
        with database.transaction():
            journal.submit(command, received_at=AT, decide=accept)
            journal.complete("command-A", evidence_fixture)
        row = database.connection.execute(
            "SELECT request_json,result_json FROM command_journal"
        ).fetchone()
        assert json.loads(row[0])["expected_control_version"] == str(version)
        assert json.loads(row[1])["applied_control_version"] == str(version)
        assert json.loads(row[1])["evidence"]["last_event_seq"] == str(version)
    with SimulatorStorage(path) as database:
        record = initialize(database).lookup("command-A")
        assert record.request.expected_control_version == version
        assert record.result.applied_control_version == version
        assert record.result.evidence.last_event_seq == version


def test_process_and_completion_rollback_share_existing_connection(stores):
    database, journal = stores
    state = fresh_execution(None, "execution-A")
    database.register_execution(state)
    with database.transaction():
        journal.submit(request(), received_at=AT, decide=accept)
    with pytest.raises(RuntimeError):
        with database.transaction():
            database.apply_transition(state, step(state, run=True, admit=True), occurred_at=AT)
            journal.complete("command-A", fail())
            raise RuntimeError("cancel the composed state/result unit")
    assert database.load_execution() == state
    assert journal.lookup("command-A").result.status == "ACCEPTED"


@pytest.mark.parametrize("boundary", ["before", "after"])
def test_abrupt_exit_preserves_process_and_result_commit_atomically(tmp_path, boundary):
    path = tmp_path / "simulator.sqlite3"
    state = fresh_execution(None, "execution-A")
    with SimulatorStorage(path) as database:
        database.acknowledge_recovery()
        database.register_execution(state)
        journal = initialize(database)
        with database.transaction():
            journal.submit(request(), received_at=AT, decide=accept)
    script = """
import os, sys
from datetime import UTC, datetime, timedelta
from apps.simulator.storage import SimulatorStorage
from apps.simulator.command_store import CommandStore
from apps.simulator.process import step
from contracts.models import CommandResult
at = datetime(2026, 10, 20, 12, tzinfo=UTC)
with SimulatorStorage(sys.argv[1]) as database:
    database.acknowledge_recovery()
    journal = CommandStore(database)
    previous = database.load_execution()
    with database.transaction():
        database.apply_transition(previous, step(previous, run=True, admit=True), occurred_at=at)
        journal.complete('command-A', CommandResult(
            command_id='command-A', status='FAILED', updated_at=at + timedelta(seconds=1),
            delivered_at=at, completed_at=at + timedelta(seconds=1),
            reason_code='FIXTURE_FAILURE', reason='Caller-proven failure fixture'))
        if sys.argv[2] == 'before':
            os._exit(71)
    os._exit(72)
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(path), boundary],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == (71 if boundary == "before" else 72), process.stderr
    with SimulatorStorage(path) as database:
        journal = initialize(database)
        record = journal.lookup("command-A")
        restored = database.load_execution()
        if boundary == "before":
            assert restored == state
            assert record.result.status == "ACCEPTED"
        else:
            assert restored == step(state, run=True, admit=True).state
            assert restored.input_quantity == restored.wip_quantity == 1
            assert record.result.status == "FAILED"
        assert database.recovery_required
        assert database.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert database.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_persisted_projection_mismatch_is_not_returned_as_valid_receipt(stores):
    database, journal = stores
    with database.transaction():
        journal.submit(request(), received_at=AT, decide=accept)
        database.connection.execute("UPDATE command_journal SET canonical_payload='{}'")
    with pytest.raises(StorageCorruption):
        journal.lookup("command-A")
    assert database.recovery_required


def test_caught_journal_corruption_poisons_entire_composed_transaction(stores):
    database, journal = stores
    with database.transaction():
        journal.submit(request(), received_at=AT, decide=accept)
        database.connection.execute("CREATE TABLE fixture_companion(value INTEGER)")
        database.connection.execute("UPDATE command_journal SET canonical_payload='{}'")
    with pytest.raises(StorageCorruption):
        with database.transaction():
            database.connection.execute("INSERT INTO fixture_companion VALUES (1)")
            with pytest.raises(StorageCorruption):
                journal.submit(request(), received_at=AT, decide=accept)
    assert database.connection.execute("SELECT count(*) FROM fixture_companion").fetchone()[0] == 0
    assert database.recovery_required


def test_persisted_invalid_primary_key_is_corruption_but_invalid_lookup_input_is_not(stores):
    database, journal = stores
    with pytest.raises(ValueError):
        journal.lookup("")
    assert not database.recovery_required
    with database.transaction():
        journal.submit(request(), received_at=AT, decide=accept)
        database.connection.execute("UPDATE command_journal SET command_id=''")
    with pytest.raises(StorageCorruption):
        journal.pending_records()
    assert database.recovery_required


def test_unversioned_existing_table_is_not_silently_adopted(tmp_path):
    with SimulatorStorage(tmp_path / "simulator.sqlite3") as database:
        with database.transaction():
            database.connection.execute("CREATE TABLE command_journal(legacy TEXT)")
            with pytest.raises(ValueError, match="Unversioned"):
                CommandStore.initialize_schema(database.connection)
        assert (
            database.connection.execute("PRAGMA table_info(command_journal)").fetchone()[1]
            == "legacy"
        )
