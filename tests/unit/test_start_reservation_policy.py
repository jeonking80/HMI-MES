"""Strict opt-in start policy and independent canonical compatibility boundaries."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from apps.api.work_order_workflow import BindIntent
from apps.simulator.command_store import CommandStore
from apps.simulator.storage import SimulatorStorage
from contracts.models import (
    CommandRequest,
    CommandSubmission,
    LoadJobArguments,
    canonical_command_payload,
    canonical_command_retry_payload,
    materialize_load_job_policy,
)

AT = datetime(2026, 10, 25, tzinfo=UTC)
ARGS = dict(
    work_order_id="work",
    recipe_version="recipe",
    target_quantity=10,
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


def test_start_is_opt_in_and_materializes_independently_of_target_policy():
    omitted = request()
    assert omitted.command.args.start_when_ready is False
    assert "start_when_ready" not in omitted.model_dump()["command"]["args"]
    for policy in ({}, {"auto_end_at_target": False}, {"start_when_ready": True}):
        stored = materialize_load_job_policy(request(**policy))
        assert stored.command.args.start_when_ready is policy.get("start_when_ready", False)
        assert stored.command.args.auto_end_at_target is policy.get("auto_end_at_target", True)
        assert {"start_when_ready", "auto_end_at_target"} <= stored.command.args.model_fields_set


@pytest.mark.parametrize("value", [0, 1, "true", "false", None, [], {}])
def test_load_and_binding_start_require_a_real_boolean(value):
    with pytest.raises(ValidationError):
        LoadJobArguments(**ARGS, start_when_ready=value)
    with pytest.raises(ValidationError):
        BindIntent(
            command_id="load",
            line_id="line",
            source_id="source",
            stream_id="stream",
            expected_work_order_version=1,
            expected_control_version=0,
            start_when_ready=value,
        )


def test_cx33_canonical_default_is_preserved_per_flag():
    cx33 = request(auto_end_at_target=True)
    original = canonical_command_payload(cx33)
    assert "start_when_ready" not in json.loads(original)["command"]["args"]
    assert canonical_command_retry_payload(request(), original) == original
    assert canonical_command_retry_payload(request(start_when_ready=True), original) != original
    assert canonical_command_retry_payload(request(auto_end_at_target=False), original) != original


@pytest.mark.parametrize("legacy", [False, True])
def test_sqlite_original_load_replay_preserves_exact_receipt_and_rejects_changed_start(
    tmp_path, legacy
):
    with SimulatorStorage(tmp_path / "policy.sqlite3") as storage:
        storage.acknowledge_recovery()
        with storage.transaction():
            CommandStore.initialize_schema(storage.connection)
        journal = CommandStore(storage)
        original = request(auto_end_at_target=True) if legacy else request(start_when_ready=True)

        def accept(command):
            return CommandSubmission(
                command_id=command.command_id, disposition="ACCEPTED", received_at=AT
            )

        with storage.transaction():
            first = journal.submit(original, received_at=AT, decide=accept)
            if legacy:
                storage.connection.execute(
                    "UPDATE command_journal SET request_json=?,canonical_payload=?",
                    (original.model_dump_json(), canonical_command_payload(original)),
                )
        before = tuple(storage.connection.execute("SELECT * FROM command_journal").fetchone())
        with storage.transaction():
            replay = journal.submit(
                request() if legacy else original,
                received_at=AT + timedelta(days=1),
                decide=accept,
            )
            assert not replay.created and replay.result == first.result
            collision = journal.submit(
                request(start_when_ready=True) if legacy else request(),
                received_at=AT + timedelta(days=1),
                decide=accept,
            )
            assert collision.submission.reason_code == "COMMAND_ID_CONFLICT"
        assert (
            tuple(storage.connection.execute("SELECT * FROM command_journal").fetchone()) == before
        )
