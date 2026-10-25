"""Actual warmup reservations, durable LOAD provenance and permission boundaries."""

import json
import os
import secrets
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from apps.api.auth import Principal, UserAuth
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.commands import CommandConflict, enqueue_command
from apps.api.ingestion import IngestionService
from apps.api.user_http import UserHTTPConfig, create_user_app
from apps.api.work_order_workflow import BindIntent, ReleaseIntent, release_work_order
from apps.api.work_orders import (
    RecipeDefinition,
    WorkOrderDefinition,
    register_recipe_version,
    register_work_order,
)
from apps.simulator.controller import SimulatorController
from apps.simulator.plc import PLCInputs, RecoveryChecks
from apps.simulator.process import ProcessConfig
from apps.simulator.storage import SimulatorStorage, StorageCorruption
from contracts.models import CommandRequest, LineState, Parameters, canonical_command_payload
from contracts.tags.validate import load_contract
from contracts.transport import BatchBody, GatewayBatch, batch_digest
from tests.integration.item_seed import seed_item

AT = datetime(2026, 10, 25, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://reservation.example.test"
COLD = ProcessConfig(ambient_temperature_c=140.0, heating_c_per_s=10.0)
CHECKS = RecoveryChecks(
    outputs_stopped=True,
    positions_verified=True,
    storage_reconciled=True,
    commands_reconciled=True,
    operator_confirmed=True,
)
PARAMETERS = Parameters(temperature_c=150.0, speed_m_per_min=6.0)
LOAD_ARGS = dict(
    work_order_id="work",
    recipe_version="recipe",
    target_quantity=100,
    parameters=PARAMETERS.model_dump(),
)


def make_controller(store):
    controller = SimulatorController(store, now=AT, process_config=COLD)
    controller.recover(now=AT, operator_id="operator", checks=CHECKS)
    controller.set_inputs(PLCInputs(), now=AT, control_mode="REMOTE", operation_mode="AUTO")
    return controller


@pytest.fixture
def controller(tmp_path):
    with SimulatorStorage(tmp_path / "reservation.sqlite3") as store:
        yield make_controller(store)


def request(controller, kind="LOAD_JOB", identifier="load", **args):
    return CommandRequest(
        command_id=identifier,
        target_id="Line",
        execution_id="run",
        requested_by="operator",
        requested_at=AT,
        expires_at=AT + timedelta(seconds=5),
        expected_control_version=controller.state.control_version,
        command={"kind": kind, "args": args},
    )


def apply(controller, kind="LOAD_JOB", identifier="load", **args):
    command = request(controller, kind, identifier, **args)
    assert controller.submit(command, now=AT).submission.disposition == "ACCEPTED"
    controller.tick(now=AT)
    return command


def load(controller, **policy):
    return apply(controller, **LOAD_ARGS, **policy)


def warm(controller):
    for _ in range(20):
        controller.tick(now=AT)
        if controller.state.state in {LineState.READY, LineState.RUNNING}:
            return controller.state
    pytest.fail("Actual process warmup did not reach READY/RUNNING")


def audit(controller, cause):
    return [
        json.loads(row[0])
        for row in controller.storage.connection.execute(
            "SELECT details_json FROM runtime_audit WHERE cause=? ORDER BY audit_id", (cause,)
        )
    ]


def durable(controller):
    return tuple(
        tuple(tuple(row) for row in controller.storage.connection.execute(f"SELECT * FROM {table}"))
        for table in (
            "runtime_state",
            "runtime_runs",
            "runtime_audit",
            "source_events",
            "command_journal",
        )
    ), controller.storage.load_execution()


@pytest.mark.parametrize("policy", [{}, {"start_when_ready": False}])
def test_default_and_explicit_false_warm_to_ready_and_wait_for_manual_start(controller, policy):
    load(controller, **policy)
    assert controller.state.start_reservation is None
    assert warm(controller).state == LineState.READY
    for _ in range(5):
        controller.tick(now=AT)
    assert controller.state.state == LineState.READY and not controller.state.run_output
    assert not audit(controller, "START_RESERVATION_FIRED")


def test_warmup_fires_once_with_original_load_provenance_and_no_start_command(controller):
    original = load(controller, start_when_ready=True)
    assert controller.get_result("load").status == "SUCCEEDED"
    assert controller.state.start_reservation.command_id == original.command_id
    assert controller.state.state != LineState.RUNNING
    controller.set_inputs(PLCInputs(temperature_offset_c=0.5), now=AT)
    assert controller.state.start_reservation is not None
    assert warm(controller).state == LineState.RUNNING
    assert controller.state.run_state == "RUNNING" and controller.state.run_output
    assert controller.state.start_reservation is None
    for cause in ("START_RESERVED", "START_RESERVATION_FIRED"):
        proof = audit(controller, cause)
        assert len(proof) == 1
        assert proof[0]["command_id"] == "load"
        assert proof[0]["requested_by"] == "operator" and proof[0]["authority"] == "REMOTE"
    before = durable(controller)
    # Reconcile an unknown outcome using the same LOAD, even after its TTL expires.
    replay = controller.submit(original, now=AT + timedelta(days=1))
    assert not replay.created and replay.result == controller.get_result("load")
    assert durable(controller) == before
    assert [
        row[0]
        for row in controller.storage.connection.execute("SELECT command_id FROM command_journal")
    ] == ["load"]


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"guard_closed": False}, "SAFETY_INPUT_CHANGED"),
        ({"estop": True}, "SAFETY_INPUT_CHANGED"),
        ({"pressure_bar": 3.0}, "SAFETY_INPUT_CHANGED"),
        ({"equipment_ready": False}, "SAFETY_INPUT_CHANGED"),
        ({"control_mode": "LOCAL"}, "CONTROL_MODE_CHANGED"),
        ({"operation_mode": "MANUAL"}, "OPERATION_MODE_CHANGED"),
    ],
)
def test_safety_or_mode_change_cancels_reservation_with_durable_reason(controller, change, reason):
    original = load(controller, start_when_ready=True)
    modes = {key: value for key, value in change.items() if key.endswith("mode")}
    inputs = {key: value for key, value in change.items() if key not in modes}
    controller.set_inputs(PLCInputs(**inputs), now=AT, **modes)
    assert controller.state.start_reservation is None
    assert controller.state.start_reservation_cancel_reason == reason
    proof = audit(controller, "START_RESERVATION_CANCELLED")
    assert len(proof) == 1 and proof[0]["reason_code"] == reason
    assert proof[0]["command_id"] == original.command_id
    controller.set_inputs(PLCInputs(), now=AT, control_mode="REMOTE", operation_mode="AUTO")
    assert controller.submit(original, now=AT).result == controller.get_result("load")
    assert controller.state.start_reservation is None
    assert not audit(controller, "START_RESERVATION_FIRED")


def test_manual_end_cancels_and_original_load_cannot_rearm(controller):
    original = load(controller, start_when_ready=True)
    apply(controller, "END_RUN", "manual-end")
    assert controller.state.start_reservation is None
    assert controller.state.start_reservation_cancel_reason == "MANUAL_END"
    assert audit(controller, "START_RESERVATION_CANCELLED")[0]["command_id"] == "load"
    controller.submit(original, now=AT)
    assert controller.state.start_reservation is None and not controller.state.run_output


def test_manual_start_cancels_reservation_and_runs_under_original_manual_id(controller):
    load(controller, start_when_ready=True)
    controller.set_inputs(PLCInputs(temperature_offset_c=10.0), now=AT)
    assert controller.state.state == LineState.READY
    apply(controller, "START", "manual-start")
    assert controller.state.state == LineState.RUNNING
    assert controller.state.start_reservation is None
    assert controller.state.start_reservation_cancel_reason == "MANUAL_START"
    assert audit(controller, "START_RESERVATION_CANCELLED")[0]["reason_code"] == "MANUAL_START"
    assert not audit(controller, "START_RESERVATION_FIRED")
    assert controller.get_result("manual-start").status == "SUCCEEDED"


def test_rejected_manual_start_while_cold_leaves_original_reservation_armed(controller):
    load(controller, start_when_ready=True)
    reservation = controller.state.start_reservation
    manual = request(controller, "START", "cold-start")
    rejected = controller.submit(manual, now=AT)
    assert rejected.submission.disposition == "REJECTED"
    assert controller.state.start_reservation == reservation
    assert not audit(controller, "START_RESERVATION_CANCELLED")
    assert warm(controller).state == LineState.RUNNING
    assert len(audit(controller, "START_RESERVATION_FIRED")) == 1


def test_restart_cancels_durably_and_recovery_and_replay_do_not_rearm(tmp_path):
    path = tmp_path / "restart.sqlite3"
    with SimulatorStorage(path) as store:
        controller = make_controller(store)
        original = load(controller, start_when_ready=True)
    with SimulatorStorage(path) as store:
        controller = SimulatorController(store, now=AT, process_config=COLD)
        assert controller.state.recovery_required and not controller.state.run_output
        assert controller.state.start_reservation is None
        assert controller.state.start_reservation_cancel_reason == "RESTART"
        assert audit(controller, "START_RESERVATION_CANCELLED")[0]["reason_code"] == "RESTART"
        controller.recover(now=AT, operator_id="operator", checks=CHECKS)
        controller.set_inputs(PLCInputs(), now=AT, control_mode="REMOTE", operation_mode="AUTO")
        controller.submit(original, now=AT)
        assert warm(controller).state == LineState.READY
        assert not audit(controller, "START_RESERVATION_FIRED")


def account(auth, role):
    password = secrets.token_urlsafe(24)
    auth.bootstrap_user(role, password, roles={role})
    challenge = auth.begin_login()
    session = auth.login(
        role,
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=ORIGIN,
    )
    return auth.change_password(
        session.token, secrets.token_urlsafe(24), csrf_token=session.csrf_token, origin=ORIGIN
    )


@pytest.fixture
def api(migrated):
    bootstrap_reference_data(migrated)
    auth = UserAuth(migrated, allowed_origins={ORIGIN}, clock=lambda: AT)
    auth.bootstrap_user("manager", secrets.token_urlsafe(24), roles={"production_manager"})
    seed_item(migrated, "item", actor="manager", now=AT)
    service = IngestionService(migrated)
    service.configure_source(
        source_id="source",
        stream_id="stream",
        gateway_id="gateway",
        line_id=LINE_ID,
        capture_generation=1,
        now=AT,
    )
    service.register_execution(
        execution_id="legacy",
        source_id="source",
        stream_id="stream",
        work_order_id="legacy-work",
        recipe_version="recipe",
        counter_epoch="epoch",
        target_quantity=100,
        parameters=PARAMETERS,
        now=AT,
    )
    with migrated.begin() as db:
        register_recipe_version(
            db,
            RecipeDefinition(
                id="recipe",
                recipe_id="recipe-family",
                version="v1",
                item_id="item",
                parameters=PARAMETERS,
            ),
            actor="manager",
            now=AT,
        )
        register_work_order(
            db,
            WorkOrderDefinition(
                id="work",
                item_id="item",
                line_id=LINE_ID,
                recipe_version_id="recipe",
                target_quantity=100,
                planned_date="2026-10-25",
            ),
            actor="manager",
            now=AT,
        )
        release_work_order(
            db,
            "work",
            ReleaseIntent(release_id="release", expected_row_version=1),
            principal=Principal("manager", frozenset({"production_manager"}), False),
            clock=lambda: AT,
        )
    values = {
        "Line.State": 0,
        "Line.ControlMode": 1,
        "Line.ControlVersion": 42,
        "Line.CounterEpoch": "epoch",
        "Line.ActiveExecutionId": "",
        "Line.WipCount": 0,
    }
    samples = []
    for tag in load_contract()["tags"]:
        name, kind = tag["logical_name"], tag["data_type"]
        value = values.get(
            name,
            False
            if kind == "Boolean"
            else ""
            if kind == "String"
            else 0.0
            if kind == "Double"
            else 0,
        )
        samples.append(
            dict(
                sample_id=f"s-{name}",
                tag_id=name,
                source_timestamp=AT,
                server_timestamp=AT,
                acquired_at=AT,
                status_code=0,
                reading={"data_type": kind, "value": value},
            )
        )
    batch = BatchBody(
        schema_version="1.1",
        batch_id="capture",
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        capture_generation=1,
        capture_seq=1,
        captured_at=AT,
        samples=samples,
        communication=dict(
            state="GOOD",
            connected=True,
            read_age_seconds=0.0,
            heartbeat_ages_seconds={
                name: 0.0
                for name in (
                    "Line.Heartbeat",
                    "Infeed.LinkHeartbeat",
                    "Sealer.LinkHeartbeat",
                    "Outfeed.LinkHeartbeat",
                )
            },
        ),
    )
    service.ingest(
        GatewayBatch(body=batch, content_sha256=batch_digest(batch)),
        authenticated_gateway_id="gateway",
        authenticated_source_id="source",
        authenticated_stream_id="stream",
        now=AT,
    )
    return auth, create_user_app(auth, UserHTTPConfig(origin=ORIGIN))


@pytest.mark.parametrize("endpoint", ["direct", "workflow"])
@pytest.mark.parametrize("role,status", [("production_manager", 403), ("operator", 200)])
async def test_armed_load_requires_load_and_operate_permissions_over_http(
    migrated, api, endpoint, role, status
):
    auth, app = api
    session = account(auth, role)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": session.csrf_token}
    if endpoint == "workflow":
        path = "/api/v1/work-orders/work/bind"
        body = BindIntent(
            command_id="http-load",
            line_id=LINE_ID,
            source_id="source",
            stream_id="stream",
            expected_work_order_version=2,
            expected_control_version=42,
            start_when_ready=True,
        ).model_dump(mode="json")
    else:
        path = "/api/v1/commands"
        command = CommandRequest(
            command_id="http-load",
            target_id="Line",
            execution_id="legacy",
            requested_by=role,
            requested_at=AT,
            expires_at=AT + timedelta(seconds=5),
            expected_control_version=42,
            command={
                "kind": "LOAD_JOB",
                "args": {
                    **LOAD_ARGS,
                    "work_order_id": "legacy-work",
                    "start_when_ready": True,
                },
            },
        )
        body = dict(
            line_id=LINE_ID,
            source_id="source",
            stream_id="stream",
            request=command.model_dump(mode="json"),
        )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        client.cookies.set("hmi_session", session.token)
        response = await client.post(path, headers=headers, json=body)
    assert response.status_code == status, response.text
    with migrated.connect() as db:
        assert db.execute(text("SELECT count(*) FROM command_requests")).scalar_one() == (
            1 if status == 200 else 0
        )
        assert db.execute(text("SELECT count(*) FROM work_order_execution_links")).scalar_one() == (
            1 if status == 200 and endpoint == "workflow" else 0
        )
        if status == 200:
            stored = db.execute(text("SELECT request FROM command_requests")).scalar_one()
            assert stored["command"]["args"]["start_when_ready"] is True
            assert stored["requested_by"] == role


@pytest.mark.parametrize("legacy", [False, True])
def test_postgres_start_policy_retry_uses_original_per_flag_canonical_receipt(
    migrated, api, legacy
):
    original = CommandRequest(
        command_id="api-load",
        target_id="Line",
        execution_id="legacy",
        requested_by="operator",
        requested_at=AT,
        expires_at=AT + timedelta(seconds=5),
        expected_control_version=42,
        command={
            "kind": "LOAD_JOB",
            "args": {
                **LOAD_ARGS,
                "work_order_id": "legacy-work",
                "auto_end_at_target": True,
                **({} if legacy else {"start_when_ready": True}),
            },
        },
    )
    scope = dict(
        principal=Principal("operator", frozenset({"operator"}), False),
        line_id=LINE_ID,
        source_id="source",
        stream_id="stream",
    )
    with migrated.begin() as db:
        first = enqueue_command(db, original, **scope, now=AT)
        if legacy:
            db.execute(
                text(
                    "UPDATE command_requests SET request=CAST(:request AS jsonb),"
                    "canonical_payload=:canonical WHERE command_id='api-load'"
                ),
                dict(
                    request=original.model_dump_json(),
                    canonical=canonical_command_payload(original),
                ),
            )
    with migrated.connect() as db:
        before = db.execute(text("SELECT * FROM command_requests")).one()
    wire = original.model_dump(mode="json")
    wire["command"]["args"].pop("start_when_ready", None)
    omitted = CommandRequest.model_validate(wire)
    with migrated.begin() as db:
        assert enqueue_command(db, original, **scope, now=AT + timedelta(days=1)) == first
        if legacy:
            assert enqueue_command(db, omitted, **scope, now=AT + timedelta(days=1)) == first
    if not legacy:
        with migrated.begin() as db, pytest.raises(CommandConflict):
            enqueue_command(db, omitted, **scope, now=AT + timedelta(days=1))
    with migrated.connect() as db:
        assert db.execute(text("SELECT * FROM command_requests")).one() == before


def test_firing_exception_rolls_back_process_state_events_summary_and_audit(controller):
    load(controller, start_when_ready=True)
    while controller.state.inputs.temperature_c < 144.0:
        controller.tick(now=AT)
    before = durable(controller)

    def fail():
        raise RuntimeError("reservation commit interrupted")

    controller._before_tick_commit = fail
    with pytest.raises(RuntimeError, match="reservation commit interrupted"):
        controller.tick(now=AT)
    assert durable(controller) == before
    assert controller.state.start_reservation is not None
    assert not audit(controller, "START_RESERVATION_FIRED")
    controller._before_tick_commit = lambda: None
    assert warm(controller).state == LineState.RUNNING
    assert len(audit(controller, "START_RESERVATION_FIRED")) == 1


def test_arm_failure_rolls_back_load_result_process_and_reservation_audit(controller):
    command = request(controller, **LOAD_ARGS, start_when_ready=True)
    assert controller.submit(command, now=AT).submission.disposition == "ACCEPTED"
    before = durable(controller)

    def fail():
        raise RuntimeError("arm transaction interrupted")

    controller._before_tick_commit = fail
    with pytest.raises(RuntimeError, match="arm transaction interrupted"):
        controller.tick(now=AT)
    assert durable(controller) == before
    assert controller.get_result("load").status == "ACCEPTED"
    assert controller.state.start_reservation is None
    controller._before_tick_commit = lambda: None
    controller.tick(now=AT)
    assert controller.get_result("load").status == "SUCCEEDED"
    assert len(audit(controller, "START_RESERVED")) == 1


def test_cancel_exception_rolls_back_safety_state_and_cancellation_audit(controller, monkeypatch):
    load(controller, start_when_ready=True)
    before = durable(controller)
    record = controller._record_change

    def fail_after_record(*args, **kwargs):
        record(*args, **kwargs)
        raise RuntimeError("cancel transaction interrupted")

    monkeypatch.setattr(controller, "_record_change", fail_after_record)
    with pytest.raises(RuntimeError, match="cancel transaction interrupted"):
        controller.set_inputs(PLCInputs(guard_closed=False), now=AT)
    assert durable(controller) == before
    assert controller.state.start_reservation is not None
    assert not audit(controller, "START_RESERVATION_CANCELLED")
    monkeypatch.setattr(controller, "_record_change", record)
    controller.set_inputs(PLCInputs(guard_closed=False), now=AT)
    assert controller.state.start_reservation is None
    assert len(audit(controller, "START_RESERVATION_CANCELLED")) == 1


@pytest.mark.parametrize(
    "field,value",
    [("command_id", "unknown-load"), ("requested_by", "other"), ("authority", "LOCAL")],
)
def test_tampered_original_load_provenance_cannot_fire(controller, field, value):
    load(controller, start_when_ready=True)
    snapshot = json.loads(
        controller.storage.connection.execute("SELECT snapshot_json FROM runtime_state").fetchone()[
            0
        ]
    )
    snapshot["start_reservation"][field] = value
    controller.storage.connection.execute(
        "UPDATE runtime_state SET snapshot_json=?", (json.dumps(snapshot),)
    )
    with pytest.raises(StorageCorruption):
        controller.tick(now=AT)
    assert not audit(controller, "START_RESERVATION_FIRED")


def test_reservation_requires_explicit_true_in_original_successful_load(controller):
    load(controller, start_when_ready=True)
    row = controller.storage.connection.execute(
        "SELECT request_json FROM command_journal WHERE command_id='load'"
    ).fetchone()
    original = json.loads(row[0])
    original["command"]["args"].pop("start_when_ready")
    legacy = CommandRequest.model_validate(original)
    controller.storage.connection.execute(
        "UPDATE command_journal SET request_json=?,canonical_payload=? WHERE command_id='load'",
        (legacy.model_dump_json(), canonical_command_payload(legacy)),
    )
    with pytest.raises(StorageCorruption):
        controller.tick(now=AT)
    assert not audit(controller, "START_RESERVATION_FIRED")


def test_pre_start_policy_v2_snapshot_reopens_conservatively_without_rewriting_load(tmp_path):
    path = tmp_path / "cx33.sqlite3"
    with SimulatorStorage(path) as store:
        controller = make_controller(store)
        load(controller)
        snapshot = json.loads(
            store.connection.execute("SELECT snapshot_json FROM runtime_state").fetchone()[0]
        )
        for field in (
            "start_when_ready",
            "start_reservation",
            "start_reservation_cancel_reason",
            "recipe_baseline",
            "recipe_baseline_version",
        ):
            snapshot.pop(field)
        store.connection.execute(
            "UPDATE runtime_state SET snapshot_json=?", (json.dumps(snapshot),)
        )
        journal = [tuple(row) for row in store.connection.execute("SELECT * FROM command_journal")]
    with SimulatorStorage(path) as store:
        controller = SimulatorController(store, now=AT, process_config=COLD)
        assert controller.state.start_when_ready is False
        assert controller.state.start_reservation is None
        assert controller.state.recovery_required and not controller.state.run_output
        assert [
            tuple(row) for row in store.connection.execute("SELECT * FROM command_journal")
        ] == journal


@pytest.mark.parametrize("phase", ["arm", "cancel", "fire"])
@pytest.mark.parametrize("after_commit", [False, True])
def test_process_death_commits_reservation_state_and_proof_together(tmp_path, phase, after_commit):
    path = tmp_path / "crash.sqlite3"
    child = r"""
import os, runpy, sys
n = runpy.run_path(sys.argv[2])
s = n['SimulatorStorage'](sys.argv[1])
c = n['make_controller'](s)
phase, after = sys.argv[3], sys.argv[4] == 'True'
if phase == 'arm':
    if not after:
        c._before_tick_commit = lambda: os._exit(73)
    n['load'](c, start_when_ready=True)
else:
    n['load'](c, start_when_ready=True)
    if phase == 'cancel':
        if not after:
            record = c._record_change
            def die_after_record(*args, **kwargs):
                record(*args, **kwargs)
                os._exit(73)
            c._record_change = die_after_record
        c.set_inputs(n['PLCInputs'](guard_closed=False), now=n['AT'])
    else:
        while c.state.inputs.temperature_c < 144.0:
            c.tick(now=n['AT'])
        if not after:
            c._before_tick_commit = lambda: os._exit(73)
        c.tick(now=n['AT'])
os._exit(73)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            child,
            str(path),
            str(Path(__file__).resolve()),
            phase,
            str(after_commit),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        timeout=20,
        check=False,
    )
    assert result.returncode == 73
    with SimulatorStorage(path) as store:
        snapshot = json.loads(
            store.connection.execute("SELECT snapshot_json FROM runtime_state").fetchone()[0]
        )
        cause = {
            "arm": "START_RESERVED",
            "cancel": "START_RESERVATION_CANCELLED",
            "fire": "START_RESERVATION_FIRED",
        }[phase]
        count = store.connection.execute(
            "SELECT count(*) FROM runtime_audit WHERE cause=?", (cause,)
        ).fetchone()[0]
        assert count == int(after_commit)
        assert snapshot["run_output"] is (phase == "fire" and after_commit)
        has_reservation = after_commit if phase == "arm" else not after_commit
        assert (snapshot["start_reservation"] is not None) is has_reservation
        if phase == "cancel":
            assert snapshot["inputs"]["guard_closed"] is (not after_commit)
            assert snapshot["start_reservation_cancel_reason"] == (
                "SAFETY_INPUT_CHANGED" if after_commit else None
            )
        controller = SimulatorController(store, now=AT, process_config=COLD)
        assert controller.state.recovery_required and not controller.state.run_output
        assert controller.state.start_reservation is None
