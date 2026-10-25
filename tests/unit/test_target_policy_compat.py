"""Target policy defaults preserve legacy canonical evidence and strict wire types."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from apps.api.work_order_workflow import BindIntent
from apps.simulator.command_store import CommandStore
from apps.simulator.plc import PLCState
from apps.simulator.storage import SimulatorStorage
from contracts.models import (
    CommandRequest,
    CommandSubmission,
    LoadJobArguments,
    RunSummary,
    canonical_command_payload,
)

AT = datetime(2026, 10, 25, tzinfo=UTC)
ARGS = dict(
    work_order_id="work",
    recipe_version="recipe",
    target_quantity=1,
    parameters=dict(temperature_c=150.0, speed_m_per_min=6.0),
)


def request(**policy):
    return CommandRequest(
        command_id="load",
        target_id="Line",
        execution_id="run",
        requested_by="operator",
        requested_at=AT,
        expires_at=AT + timedelta(seconds=5),
        expected_control_version=0,
        command={"kind": "LOAD_JOB", "args": ARGS | policy},
    )


def accept(command):
    return CommandSubmission(command_id=command.command_id, disposition="ACCEPTED", received_at=AT)


def test_omitted_policy_defaults_true_without_rewriting_legacy_wire_shape():
    command = request()
    assert command.command.args.auto_end_at_target is True
    assert "auto_end_at_target" not in command.model_dump()["command"]["args"]
    assert "auto_end_at_target" not in json.loads(command.model_dump_json())["command"]["args"]
    expected = command.model_dump(mode="json", exclude={"command_id"})
    expected["command"]["args"].pop("auto_end_at_target", None)
    assert canonical_command_payload(command) == json.dumps(
        expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert request(auto_end_at_target=False).command.args.auto_end_at_target is False


@pytest.mark.parametrize("value", [0, 1, "true", "false", None, [], {}])
def test_load_and_binding_policies_reject_non_booleans(value):
    with pytest.raises(ValidationError):
        LoadJobArguments(**ARGS, auto_end_at_target=value)
    with pytest.raises(ValidationError):
        BindIntent(
            command_id="load",
            line_id="line",
            source_id="source",
            stream_id="stream",
            expected_work_order_version=1,
            expected_control_version=0,
            auto_end_at_target=value,
        )


def test_legacy_plc_and_final_summary_keep_unknown_reason_and_original_hash():
    assert PLCState.model_validate({}).auto_end_at_target is False
    body = json.loads(
        (Path(__file__).resolve().parents[2] / "contracts/examples/run-summary.json").read_text()
    )
    original = json.dumps(body, sort_keys=True, separators=(",", ":"))
    summary = RunSummary.model_validate(body)
    assert summary.end_reason is None
    assert "end_reason" not in summary.model_dump()
    restored = json.dumps(summary.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(restored.encode()).digest() == hashlib.sha256(original.encode()).digest()


@pytest.mark.parametrize("policy", [{}, {"auto_end_at_target": False}])
def test_new_sqlite_receipt_materializes_default_and_detects_policy_collision(tmp_path, policy):
    with SimulatorStorage(tmp_path / "policy.sqlite3") as storage:
        storage.acknowledge_recovery()
        with storage.transaction():
            CommandStore.initialize_schema(storage.connection)
        journal = CommandStore(storage)
        original = request(**policy)
        with storage.transaction():
            first = journal.submit(original, received_at=AT, decide=accept)
        row = tuple(storage.connection.execute("SELECT * FROM command_journal").fetchone())
        stored = journal.lookup("load").request
        assert "auto_end_at_target" in stored.command.args.model_fields_set
        assert stored.command.args.auto_end_at_target is policy.get("auto_end_at_target", True)
        with storage.transaction():
            duplicate = journal.submit(original, received_at=AT + timedelta(days=1), decide=accept)
            assert not duplicate.created and duplicate.result == first.result
            omitted = journal.submit(request(), received_at=AT + timedelta(days=1), decide=accept)
            if policy:
                assert omitted.submission.reason_code == "COMMAND_ID_CONFLICT"
                assert omitted.result is None
            else:
                assert omitted.result == first.result
            opposite = journal.submit(
                request(auto_end_at_target=not stored.command.args.auto_end_at_target),
                received_at=AT + timedelta(days=1),
                decide=accept,
            )
            assert opposite.submission.reason_code == "COMMAND_ID_CONFLICT"
        assert tuple(storage.connection.execute("SELECT * FROM command_journal").fetchone()) == row


def test_old_sqlite_canonical_receipt_survives_lookup_and_late_retry_unchanged(tmp_path):
    with SimulatorStorage(tmp_path / "old-policy.sqlite3") as storage:
        storage.acknowledge_recovery()
        with storage.transaction():
            CommandStore.initialize_schema(storage.connection)
        journal = CommandStore(storage)
        old = request()
        with storage.transaction():
            journal.submit(old, received_at=AT, decide=accept)
            # Reconstruct a pre-policy journal row using its exact old canonical bytes.
            storage.connection.execute(
                "UPDATE command_journal SET request_json=?,canonical_payload=? "
                "WHERE command_id='load'",
                (old.model_dump_json(), canonical_command_payload(old)),
            )
        before = tuple(storage.connection.execute("SELECT * FROM command_journal").fetchone())
        assert (
            "auto_end_at_target" not in journal.lookup("load").request.command.args.model_fields_set
        )
        with storage.transaction():
            replay = journal.submit(old, received_at=AT + timedelta(days=1), decide=accept)
            assert not replay.created and replay.result.status == "ACCEPTED"
        assert (
            tuple(storage.connection.execute("SELECT * FROM command_journal").fetchone()) == before
        )
        assert not storage.recovery_required
