"""Real PLC target completion, durable proof, and PostgreSQL settlement boundaries."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from apps.api.auth import Principal
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.commands import CommandConflict, apply_result, claim_next, enqueue_command
from apps.api.ingestion import IngestionService
from apps.api.settlement import reconcile_execution, record_final_summary
from apps.simulator.controller import SimulatorController
from apps.simulator.plc import PLCInputs, RecoveryChecks
from apps.simulator.process import ProcessConfig
from apps.simulator.storage import SimulatorStorage, StorageCorruption
from contracts.models import (
    CommandRequest,
    LineState,
    Parameters,
    ProductionEvent,
    ReadEventsRequest,
    canonical_command_payload,
)
from contracts.transport import BatchBody, GatewayBatch, batch_digest

AT = datetime(2026, 10, 25, tzinfo=UTC)
FAST = ProcessConfig(stage_lengths_mm=(1, 1, 1), ambient_temperature_c=150.0)
CHECKS = RecoveryChecks(
    outputs_stopped=True,
    positions_verified=True,
    storage_reconciled=True,
    commands_reconciled=True,
    operator_confirmed=True,
)
ROOT = Path(__file__).resolve().parents[2]


def make_controller(store):
    controller = SimulatorController(store, now=AT, process_config=FAST)
    controller.recover(now=AT, operator_id="operator", checks=CHECKS)
    controller.set_inputs(
        PLCInputs(temperature_c=150.0), now=AT, control_mode="REMOTE", operation_mode="AUTO"
    )
    return controller


@pytest.fixture
def controller(tmp_path):
    with SimulatorStorage(tmp_path / "target.sqlite3") as store:
        yield make_controller(store)


def command(controller, kind, identifier, *, execution="run-A", **args):
    return CommandRequest(
        command_id=identifier,
        target_id="Line",
        execution_id=execution,
        requested_by="operator",
        requested_at=AT,
        expires_at=AT + timedelta(seconds=5),
        expected_control_version=controller.state.control_version,
        command={"kind": kind, "args": args},
    )


def apply(controller, kind, identifier, *, execution="run-A", **args):
    request = command(controller, kind, identifier, execution=execution, **args)
    assert controller.submit(request, now=AT).submission.disposition == "ACCEPTED"
    controller.tick(now=AT)
    return request


def load(controller, *, target=4, execution="run-A", **policy):
    return apply(
        controller,
        "LOAD_JOB",
        f"load-{execution}",
        execution=execution,
        work_order_id=f"work-{execution}",
        recipe_version="recipe-1",
        target_quantity=target,
        parameters=dict(temperature_c=150.0, speed_m_per_min=6.0),
        **policy,
    )


def finish(controller, execution="run-A"):
    for _ in range(100):
        summary = controller.get_summary(execution)
        if summary.final:
            return summary
        controller.tick(now=AT)
    pytest.fail("PLC did not complete the actual target/drain boundary")


def events(controller):
    identity = controller.storage.identity
    return controller.read_events(
        ReadEventsRequest(
            source_id=identity.source_id,
            stream_id=identity.stream_id,
            after_seq=0,
            limit=1000,
        )
    ).events


def journal(controller):
    return [
        tuple(row)
        for row in controller.storage.connection.execute(
            "SELECT * FROM command_journal ORDER BY command_id"
        )
    ]


def test_default_auto_end_replaces_rejects_then_fixes_real_counts_without_synthetic_command(
    controller,
):
    load(controller)
    assert controller.state.auto_end_at_target is True
    apply(controller, "START", "start")
    final = finish(controller)
    assert final.end_reason == "TARGET_REACHED"
    assert (
        final.input_quantity,
        final.good_quantity,
        final.reject_quantity,
        final.wip_quantity,
    ) == (5, 4, 1, 0)
    assert controller.state.state == LineState.STOPPED
    assert not controller.state.run_output and controller.state.pending_command_id is None
    assert [row[0] for row in journal(controller)] == ["load-run-A", "start"]
    production = [event for event in events(controller) if isinstance(event, ProductionEvent)]
    assert sum(event.payload.quantity for event in production) == final.input_quantity
    assert final.last_event_seq == events(controller)[-1].seq
    before = journal(controller)
    for _ in range(5):
        controller.tick(now=AT)
    assert controller.get_summary("run-A") == final
    assert journal(controller) == before


def test_false_policy_keeps_running_belt_at_target_until_operator_end(controller):
    load(controller, target=1, auto_end_at_target=False)
    apply(controller, "START", "start")
    for _ in range(12):
        controller.tick(now=AT)
    observed = controller.get_summary("run-A")
    assert observed.good_quantity == 1 and observed.wip_quantity == 0 and not observed.final
    assert controller.state.state == LineState.RUNNING and controller.state.run_output
    apply(controller, "END_RUN", "operator-end")
    final = finish(controller)
    assert final.end_reason == "OPERATOR"
    assert controller.get_result("operator-end").status == "SUCCEEDED"


def test_manual_end_before_target_records_operator_reason_and_stops_replacement(controller):
    load(controller, target=10)
    apply(controller, "START", "start")
    count = controller.storage.load_execution().input_quantity
    apply(controller, "END_RUN", "operator-end")
    final = finish(controller)
    assert final.end_reason == "OPERATOR"
    assert final.input_quantity == count and final.good_quantity < 10
    assert controller.get_result("operator-end").evidence.last_event_seq == final.last_event_seq


def test_legacy_accepted_load_without_policy_executes_with_legacy_false(controller):
    old = command(
        controller,
        "LOAD_JOB",
        "legacy-load",
        work_order_id="work-run-A",
        recipe_version="recipe-1",
        target_quantity=1,
        parameters=dict(temperature_c=150.0, speed_m_per_min=6.0),
    )
    assert controller.submit(old, now=AT).submission.disposition == "ACCEPTED"
    # Reconstruct the exact accepted LOAD persisted by the old policy-free journal.
    controller.storage.connection.execute(
        "UPDATE command_journal SET request_json=?,canonical_payload=? "
        "WHERE command_id='legacy-load'",
        (old.model_dump_json(), canonical_command_payload(old)),
    )
    before = tuple(
        controller.storage.connection.execute(
            "SELECT request_json,canonical_payload FROM command_journal "
            "WHERE command_id='legacy-load'"
        ).fetchone()
    )
    controller.tick(now=AT)
    assert controller.state.auto_end_at_target is False
    apply(controller, "START", "legacy-start")
    for _ in range(12):
        controller.tick(now=AT)
    assert controller.get_summary("run-A").good_quantity == 1
    assert not controller.get_summary("run-A").final
    assert controller.state.state == LineState.RUNNING and controller.state.run_output
    assert (
        tuple(
            controller.storage.connection.execute(
                "SELECT request_json,canonical_payload FROM command_journal "
                "WHERE command_id='legacy-load'"
            ).fetchone()
        )
        == before
    )


@pytest.mark.parametrize("version", [1, 2])
def test_prepolicy_controller_snapshots_reopen_with_conservative_policy(tmp_path, version):
    path = tmp_path / f"legacy-v{version}.sqlite3"
    with SimulatorStorage(path) as store:
        controller = make_controller(store)
        load(controller, auto_end_at_target=False)
        snapshot = json.loads(
            store.connection.execute("SELECT snapshot_json FROM runtime_state").fetchone()[0]
        )
        snapshot.pop("auto_end_at_target")
        snapshot.pop("end_reason")
        if version == 1:
            snapshot["inputs"].pop("temperature_offset_c")
        store.connection.execute(
            "UPDATE runtime_state SET snapshot_json=?", (json.dumps(snapshot),)
        )
        store.connection.execute("UPDATE runtime_schema_version SET version=?", (version,))
        before = journal(controller)
    with SimulatorStorage(path) as store:
        reopened = SimulatorController(store, now=AT, process_config=FAST)
        assert reopened.state.auto_end_at_target is False and reopened.state.end_reason is None
        assert reopened.state.recovery_required and not reopened.state.run_output
        assert reopened.get_summary("run-A").good_quantity == 0
        assert journal(reopened) == before


@pytest.mark.parametrize("seam", ["_before_boundary_commit", "_before_tick_commit", "after_commit"])
def test_process_crash_at_automatic_final_boundary_is_atomic(tmp_path, seam):
    path = tmp_path / "crash.sqlite3"
    child = r"""
import os, runpy, sys
n = runpy.run_path(sys.argv[3])
s = n['SimulatorStorage'](sys.argv[1])
c = n['make_controller'](s)
n['load'](c, target=1)
n['apply'](c, 'START', 'start')
for _ in range(20):
    if c.state.run_state == 'ENDING':
        break
    c.tick(now=n['AT'])
assert c.state.run_state == 'ENDING'
if sys.argv[2] != 'after_commit':
    setattr(c, sys.argv[2], lambda: os._exit(73))
c.tick(now=n['AT'])
os._exit(73 if sys.argv[2] == 'after_commit' else 99)
"""
    completed = subprocess.run(
        [sys.executable, "-c", child, str(path), seam, str(Path(__file__).resolve())],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        timeout=20,
        check=False,
    )
    assert completed.returncode == 73
    with SimulatorStorage(path) as store:
        controller = SimulatorController(store, now=AT, process_config=FAST)
        summary = controller.get_summary("run-A")
        proof_count = store.connection.execute(
            "SELECT count(*) FROM runtime_audit WHERE cause='TARGET_AUTO_END'"
        ).fetchone()[0]
        # Crash evidence is inspected before recovery; public ReadEvents stays gated.
        produced = [
            ProductionEvent.model_validate_json(row[0])
            for row in store.connection.execute(
                "SELECT body FROM source_events "
                "WHERE json_extract(body,'$.kind') IN ('PRODUCTION_GOOD','PRODUCTION_REJECT')"
            )
        ]
        assert [row[0] for row in journal(controller)] == ["load-run-A", "start"]
        if seam == "after_commit":
            assert summary.final and summary.end_reason == "TARGET_REACHED"
            assert summary.good_quantity == len(produced) == proof_count == 1
        else:
            assert not summary.final and summary.state == "ENDING" and proof_count == 0
            assert summary.good_quantity == len(produced) == summary.input_quantity == 1
            assert summary.wip_quantity == 0
        assert controller.state.recovery_required and not controller.state.run_output


def test_auto_boundary_exception_rolls_back_proof_process_summary_and_source(controller):
    load(controller, target=1)
    apply(controller, "START", "start")
    for _ in range(20):
        if controller.state.run_state == "ENDING":
            break
        controller.tick(now=AT)
    assert controller.state.run_state == "ENDING"
    before = (
        controller.state,
        controller.storage.load_execution(),
        controller.get_summary("run-A"),
        events(controller),
        journal(controller),
    )

    def fail():
        raise RuntimeError("automatic final boundary failed")

    controller._before_boundary_commit = fail
    with pytest.raises(RuntimeError, match="automatic final boundary failed"):
        controller.tick(now=AT)
    assert (
        controller.state,
        controller.storage.load_execution(),
        controller.get_summary("run-A"),
        events(controller),
        journal(controller),
    ) == before
    assert (
        controller.storage.connection.execute(
            "SELECT count(*) FROM runtime_audit WHERE cause='TARGET_AUTO_END'"
        ).fetchone()[0]
        == 0
    )
    controller._before_boundary_commit = lambda: None
    assert finish(controller).end_reason == "TARGET_REACHED"


@pytest.mark.parametrize(
    "damage", [None, "missing", "corrupt", "hash", "seq", "target", "good", "policy", "execution"]
)
def test_retention_requires_durable_auto_end_proof(tmp_path, damage):
    clock = dict(utc=AT, mono=0.0)
    with SimulatorStorage(
        tmp_path / "retention.sqlite3",
        utc_clock=lambda: clock["utc"],
        monotonic_clock=lambda: clock["mono"],
    ) as store:
        controller = make_controller(store)
        load(controller, target=1)
        apply(controller, "START", "start")
        final = finish(controller)
        assert final.end_reason == "TARGET_REACHED"
        before = [tuple(row) for row in store.connection.execute("SELECT * FROM source_events")]
        assert (
            store.connection.execute(
                "SELECT count(*) FROM runtime_audit WHERE cause='TARGET_AUTO_END'"
            ).fetchone()[0]
            == 1
        )
        if damage == "missing":
            store.connection.execute("DELETE FROM runtime_audit WHERE cause='TARGET_AUTO_END'")
        elif damage == "corrupt":
            store.connection.execute(
                "UPDATE runtime_audit SET details_json='{}' WHERE cause='TARGET_AUTO_END'"
            )
        elif damage:
            details = json.loads(
                store.connection.execute(
                    "SELECT details_json FROM runtime_audit WHERE cause='TARGET_AUTO_END'"
                ).fetchone()[0]
            )
            key, value = {
                "hash": ("canonical_final_sha256", "0" * 64),
                "seq": ("last_event_seq", "1"),
                "target": ("target_quantity", 2),
                "good": ("good_quantity", 2),
                "policy": ("auto_end_at_target", False),
                "execution": ("execution_id", "run-B"),
            }[damage]
            details[key] = value
            store.connection.execute(
                "UPDATE runtime_audit SET details_json=? WHERE cause='TARGET_AUTO_END'",
                (json.dumps(details),),
            )
        clock.update(utc=AT + timedelta(days=8), mono=8 * 24 * 60 * 60)
        if damage:
            with pytest.raises(StorageCorruption):
                controller.prune_events()
            assert store.recovery_required
            assert [
                tuple(row) for row in store.connection.execute("SELECT * FROM source_events")
            ] == before
        else:
            assert controller.prune_events().pruned_count == len(before)
            assert controller.get_summary("run-A") == final


def api_service(migrated, controller):
    bootstrap_reference_data(migrated)
    identity = controller.storage.identity
    service = IngestionService(migrated)
    service.configure_source(
        source_id=identity.source_id,
        stream_id=identity.stream_id,
        gateway_id="gateway",
        line_id=LINE_ID,
        capture_generation=1,
        now=AT,
    )
    return service, dict(
        authenticated_gateway_id="gateway",
        authenticated_source_id=identity.source_id,
        authenticated_stream_id=identity.stream_id,
    )


def register(service, controller, execution="run-A"):
    identity = controller.storage.identity
    service.register_execution(
        execution_id=execution,
        source_id=identity.source_id,
        stream_id=identity.stream_id,
        work_order_id=f"work-{execution}",
        recipe_version="recipe-1",
        counter_epoch=identity.counter_epoch,
        parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
        target_quantity=4,
        now=AT,
    )


def ingest(service, scope, source_events, *, identifier="batch"):
    body = BatchBody(
        batch_id=identifier,
        gateway_id="gateway",
        source_id=scope["authenticated_source_id"],
        stream_id=scope["authenticated_stream_id"],
        capture_generation=1,
        capture_seq=1,
        captured_at=AT,
        events=tuple(source_events),
    )
    return service.ingest(
        GatewayBatch(body=body, content_sha256=batch_digest(body)), **scope, now=AT
    )


def test_real_auto_summary_waits_for_late_original_events_and_settles_actual_counts(
    migrated, controller
):
    service, scope = api_service(migrated, controller)
    register(service, controller)
    load(controller)
    apply(controller, "START", "start")
    final = finish(controller)
    source = events(controller)
    with migrated.begin() as db:
        pending = record_final_summary(db, final, **scope, now=AT)
    assert pending.status == "PENDING"
    # A later registered execution cannot take ownership of delayed A production.
    register(service, controller, "run-B")
    ack = ingest(service, scope, source)
    assert ack.durability == "COMMITTED"
    with migrated.begin() as db:
        settled = reconcile_execution(db, "run-A", **scope, now=AT)
    assert settled.status == "SETTLED" and settled.summary.end_reason == "TARGET_REACHED"
    totals = service.get_totals("run-A")
    assert totals["good_quantity"] == final.good_quantity
    assert totals["reject_quantity"] == final.reject_quantity
    assert service.get_totals("run-B")["good_quantity"] == 0
    with migrated.connect() as db:
        assert db.execute(text("SELECT count(*) FROM command_requests")).scalar_one() == 0


def test_auto_final_cannot_resolve_unknown_start_until_original_id_result_arrives(
    migrated, controller
):
    service, scope = api_service(migrated, controller)
    register(service, controller)
    load(controller)
    start = command(controller, "START", "original-start")
    actor = Principal("operator", frozenset({"operator"}), False)
    with migrated.begin() as db:
        enqueue_command(
            db,
            start,
            principal=actor,
            line_id=LINE_ID,
            source_id=scope["authenticated_source_id"],
            stream_id=scope["authenticated_stream_id"],
            now=AT,
        )
    with migrated.begin() as db:
        assert claim_next(db, **scope, now=AT).action == "SUBMIT"
    assert controller.submit(start, now=AT).submission.disposition == "ACCEPTED"
    controller.tick(now=AT)
    final = finish(controller)
    ingest(service, scope, events(controller))
    with migrated.begin() as db:
        pending = record_final_summary(db, final, **scope, now=AT)
    assert (pending.status, pending.reason_code) == ("PENDING", "EXECUTION_COMMAND_UNRESOLVED")
    with migrated.connect() as db:
        assert (
            db.execute(text("SELECT status FROM command_requests")).scalar_one()
            == "OUTCOME_UNKNOWN"
        )
    with migrated.begin() as db:
        apply_result(db, controller.get_result("original-start"), **scope, now=AT)
    with migrated.begin() as db:
        assert reconcile_execution(db, "run-A", **scope, now=AT).status == "SETTLED"
    with migrated.connect() as db:
        assert db.execute(text("SELECT command_id FROM command_requests")).scalars().all() == [
            "original-start"
        ]


@pytest.mark.parametrize("policy", [{}, {"auto_end_at_target": False}])
def test_postgres_command_policy_retry_preserves_original_receipt(migrated, controller, policy):
    service, scope = api_service(migrated, controller)
    register(service, controller)
    original = command(
        controller,
        "LOAD_JOB",
        "api-load",
        work_order_id="work-run-A",
        recipe_version="recipe-1",
        target_quantity=4,
        parameters=dict(temperature_c=150.0, speed_m_per_min=6.0),
        **policy,
    )
    actor = Principal("operator", frozenset({"operator"}), False)

    def enqueue(request, now=AT):
        with migrated.begin() as db:
            return enqueue_command(
                db,
                request,
                principal=actor,
                line_id=LINE_ID,
                source_id=scope["authenticated_source_id"],
                stream_id=scope["authenticated_stream_id"],
                now=now,
            )

    result = enqueue(original)
    with migrated.connect() as db:
        before = db.execute(
            text("SELECT request,canonical_payload,result FROM command_requests")
        ).one()
    assert before.request["command"]["args"]["auto_end_at_target"] is policy.get(
        "auto_end_at_target", True
    )
    assert enqueue(original, AT + timedelta(days=1)) == result
    wire = original.model_dump(mode="json")
    wire["command"]["args"].pop("auto_end_at_target", None)
    omitted = CommandRequest.model_validate(wire)
    if policy:
        with pytest.raises(CommandConflict):
            enqueue(omitted, AT + timedelta(days=1))
    else:
        assert enqueue(omitted, AT + timedelta(days=1)) == result
    with migrated.connect() as db:
        assert (
            db.execute(text("SELECT request,canonical_payload,result FROM command_requests")).one()
            == before
        )


def test_legacy_postgres_canonical_request_replays_without_rewriting_original(migrated, controller):
    service, scope = api_service(migrated, controller)
    register(service, controller)
    old = command(
        controller,
        "LOAD_JOB",
        "old-api-load",
        work_order_id="work-run-A",
        recipe_version="recipe-1",
        target_quantity=4,
        parameters=dict(temperature_c=150.0, speed_m_per_min=6.0),
    )
    actor = Principal("operator", frozenset({"operator"}), False)
    args = dict(
        principal=actor,
        line_id=LINE_ID,
        source_id=scope["authenticated_source_id"],
        stream_id=scope["authenticated_stream_id"],
    )
    with migrated.begin() as db:
        original = enqueue_command(db, old, **args, now=AT)
        db.execute(
            text(
                "UPDATE command_requests SET request=CAST(:request AS jsonb),"
                "canonical_payload=:canonical "
                "WHERE command_id=:id"
            ),
            dict(
                request=old.model_dump_json(),
                canonical=canonical_command_payload(old),
                id=old.command_id,
            ),
        )
    with migrated.connect() as db:
        before = db.execute(text("SELECT * FROM command_requests")).one()
    with migrated.begin() as db:
        assert enqueue_command(db, old, **args, now=AT + timedelta(days=1)) == original
    with migrated.connect() as db:
        assert db.execute(text("SELECT * FROM command_requests")).one() == before
