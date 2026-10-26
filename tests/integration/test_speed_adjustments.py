"""Durable PLC speed history through actual disposable PostgreSQL settlement."""

import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from apps.api.auth import UserAuth
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.ingestion import IngestionService
from apps.api.settlement import record_final_summary
from apps.api.user_http import UserHTTPConfig, create_user_app
from apps.api.work_orders import RecipeDefinition, register_recipe_version
from apps.simulator.controller import SimulatorController
from apps.simulator.plc import PLCInputs, RecoveryChecks
from apps.simulator.process import ProcessConfig
from apps.simulator.storage import SimulatorStorage
from contracts.models import (
    CommandRequest,
    Parameters,
    ParametersChangedEvent,
    ReadEventsRequest,
    RunSummary,
)
from contracts.tags.validate import load_contract
from contracts.transport import BatchBody, GatewayBatch, batch_digest
from tests.integration.item_seed import seed_item

AT = datetime(2026, 10, 25, tzinfo=UTC)
BASE = Parameters(temperature_c=150.0, speed_m_per_min=6.0)
FAST = ProcessConfig(stage_lengths_mm=(1, 1, 1), ambient_temperature_c=150.0)
CHECKS = RecoveryChecks(
    outputs_stopped=True,
    positions_verified=True,
    storage_reconciled=True,
    commands_reconciled=True,
    operator_confirmed=True,
)


def make_controller(store):
    controller = SimulatorController(store, now=AT, process_config=FAST)
    controller.recover(now=AT, operator_id="operator", checks=CHECKS)
    controller.set_inputs(
        PLCInputs(temperature_c=150.0), now=AT, control_mode="REMOTE", operation_mode="AUTO"
    )
    return controller


@pytest.fixture
def controller(tmp_path):
    with SimulatorStorage(tmp_path / "speed.sqlite3") as store:
        yield make_controller(store)


def command(controller, kind, identifier, *, execution="run-A", **args):
    return CommandRequest(
        command_id=identifier,
        target_id="Line",
        execution_id=execution,
        requested_by="manager",
        requested_at=AT,
        expires_at=AT + timedelta(seconds=5),
        expected_control_version=controller.state.control_version,
        command=dict(kind=kind, args=args),
    )


def apply(controller, kind, identifier, **args):
    request = command(controller, kind, identifier, **args)
    assert controller.submit(request, now=AT).submission.disposition == "ACCEPTED"
    controller.tick(now=AT)
    return request


def start(controller):
    apply(
        controller,
        "LOAD_JOB",
        "load",
        work_order_id="work-A",
        recipe_version="recipe-1",
        target_quantity=4,
        parameters=BASE.model_dump(),
        auto_end_at_target=False,
    )
    apply(controller, "START", "start")


def adjust(controller, identifier="adjust", speed=7.0):
    return apply(
        controller,
        "SET_PARAMETERS",
        identifier,
        recipe_version="recipe-1",
        parameters=dict(temperature_c=150.0, speed_m_per_min=speed),
    )


def events(controller):
    identity = controller.storage.identity
    return controller.read_events(
        ReadEventsRequest(
            source_id=identity.source_id, stream_id=identity.stream_id, after_seq=0, limit=1000
        )
    ).events


def changes(controller):
    return tuple(event for event in events(controller) if isinstance(event, ParametersChangedEvent))


def finish(controller):
    apply(controller, "END_RUN", "end")
    for _ in range(100):
        summary = controller.get_summary("run-A")
        if summary.final:
            return summary
        controller.tick(now=AT)
    pytest.fail("Actual PLC drain did not complete")


def test_running_and_paused_adjustments_keep_original_execution_and_recipe(controller):
    start(controller)
    adjust(controller)
    apply(controller, "PAUSE", "pause")
    for _ in range(20):
        if controller.state.run_state == "PAUSED":
            break
        controller.tick(now=AT)
    assert controller.state.run_state == "PAUSED"
    adjust(controller, "paused-adjust", 4.8)
    history = changes(controller)
    assert len(history) == 2
    assert all(event.execution_id == "run-A" for event in history)
    assert all(event.payload.baseline_parameters == BASE for event in history)
    assert history[0].payload.previous_parameters == BASE
    assert history[1].payload.previous_parameters == history[0].payload.parameters
    assert history[1].payload.parameters.speed_m_per_min == 4.8
    assert controller.get_summary("run-A").parameter_changes == history


def test_same_command_replay_emits_one_event_and_preserves_original_receipt(controller):
    start(controller)
    request = adjust(controller)
    before = tuple(
        controller.storage.connection.execute(
            "SELECT * FROM command_journal WHERE command_id='adjust'"
        ).fetchone()
    )
    original = controller.get_result("adjust")
    controller.submit(request, now=AT + timedelta(days=1))
    controller.tick(now=AT)
    assert len(changes(controller)) == 1
    assert controller.get_result("adjust") == original
    assert (
        tuple(
            controller.storage.connection.execute(
                "SELECT * FROM command_journal WHERE command_id='adjust'"
            ).fetchone()
        )
        == before
    )


def test_new_command_for_current_speed_finishes_without_inventing_change(controller):
    start(controller)
    adjust(controller, "first", 7.0)
    history = changes(controller)
    version = controller.state.control_version
    adjust(controller, "same-speed-new-id", 7.0)
    assert controller.get_result("same-speed-new-id").status == "SUCCEEDED"
    assert controller.state.control_version == version
    assert changes(controller) == history
    assert controller.get_summary("run-A").parameter_changes == history


def test_stopped_recipe_replacement_cannot_admit_unprovable_active_adjustment(controller):
    apply(
        controller,
        "LOAD_JOB",
        "load",
        work_order_id="work-A",
        recipe_version="recipe-1",
        target_quantity=4,
        parameters=BASE.model_dump(),
        auto_end_at_target=False,
    )
    apply(
        controller,
        "SET_PARAMETERS",
        "stopped-recipe-change",
        recipe_version="recipe-2",
        parameters=dict(temperature_c=150.0, speed_m_per_min=7.0),
    )
    apply(controller, "START", "start")
    request = command(
        controller,
        "SET_PARAMETERS",
        "unprovable-active-change",
        recipe_version="recipe-2",
        parameters=dict(temperature_c=150.0, speed_m_per_min=7.1),
    )
    outcome = controller.submit(request, now=AT)
    assert outcome.submission.disposition == "REJECTED"
    assert controller.state.pending_command_id is None
    controller.tick(now=AT)
    assert controller.state.parameters.speed_m_per_min == 7.0
    assert changes(controller) == ()


def test_tick_failure_rolls_back_speed_process_event_history_and_result(controller):
    start(controller)
    request = command(
        controller,
        "SET_PARAMETERS",
        "adjust",
        recipe_version="recipe-1",
        parameters=dict(temperature_c=150.0, speed_m_per_min=7.0),
    )
    controller.submit(request, now=AT)
    before = (
        controller.state,
        controller.storage.load_execution(),
        controller.get_summary("run-A"),
        events(controller),
        controller.get_result("adjust"),
    )

    def fail():
        raise RuntimeError("speed transaction interrupted")

    controller._before_tick_commit = fail
    with pytest.raises(RuntimeError, match="speed transaction interrupted"):
        controller.tick(now=AT)
    assert (
        controller.state,
        controller.storage.load_execution(),
        controller.get_summary("run-A"),
        events(controller),
        controller.get_result("adjust"),
    ) == before
    controller._before_tick_commit = lambda: None
    controller.tick(now=AT)
    assert len(changes(controller)) == 1


@pytest.mark.parametrize("seam", ["before_commit", "after_commit"])
def test_process_crash_keeps_parameter_boundary_atomic(tmp_path, seam):
    path = tmp_path / "crash.sqlite3"
    source = Path(__file__).resolve()
    root = source.parents[2]
    child = r"""
import os, runpy, sys
n = runpy.run_path(sys.argv[3])
s = n['SimulatorStorage'](sys.argv[1])
c = n['make_controller'](s)
n['start'](c)
r = n['command'](c, 'SET_PARAMETERS', 'adjust', recipe_version='recipe-1',
                 parameters=dict(temperature_c=150.0, speed_m_per_min=7.0))
assert c.submit(r, now=n['AT']).submission.disposition == 'ACCEPTED'
if sys.argv[2] == 'before_commit':
    c._before_tick_commit = lambda: os._exit(73)
c.tick(now=n['AT'])
os._exit(73 if sys.argv[2] == 'after_commit' else 99)
"""
    completed = subprocess.run(
        [sys.executable, "-c", child, str(path), seam, str(source)],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        timeout=20,
        check=False,
    )
    assert completed.returncode == 73
    with SimulatorStorage(path) as store:
        reopened = SimulatorController(store, now=AT, process_config=FAST)
        summary = reopened.get_summary("run-A")
        count = store.connection.execute(
            "SELECT count(*) FROM source_events "
            "WHERE json_extract(body,'$.kind')='PARAMETERS_CHANGED'"
        ).fetchone()[0]
        assert reopened.state.recovery_required and not reopened.state.run_output
        if seam == "before_commit":
            assert summary.parameters == BASE and summary.parameter_changes == () and count == 0
            assert reopened.get_result("adjust").status == "OUTCOME_UNKNOWN"
        else:
            assert summary.parameters.speed_m_per_min == 7.0
            assert len(summary.parameter_changes) == count == 1
            assert reopened.get_result("adjust").status == "SUCCEEDED"


def test_final_history_survives_reopen_and_source_event_retention(tmp_path):
    path = tmp_path / "retention.sqlite3"
    clock = dict(utc=AT, mono=0.0)
    with SimulatorStorage(
        path, utc_clock=lambda: clock["utc"], monotonic_clock=lambda: clock["mono"]
    ) as store:
        controller = make_controller(store)
        start(controller)
        adjust(controller)
        final = finish(controller)
        assert final.baseline_parameters == BASE and len(final.parameter_changes) == 1
        clock.update(utc=AT + timedelta(days=8), mono=8 * 24 * 60 * 60)
        assert controller.prune_events().pruned_count > 0
        assert controller.get_summary("run-A") == final
    with SimulatorStorage(path) as store:
        reopened = SimulatorController(store, now=AT + timedelta(days=8), process_config=FAST)
        assert reopened.get_summary("run-A") == final
        assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 4


class PreSpeedAdjustmentStorage(SimulatorStorage):
    """Construct the actual v3 event CHECK without the new event discriminator."""

    @staticmethod
    def _events_table_sql(name):
        return SimulatorStorage._events_table_sql(name).replace(
            "WHEN 'PARAMETERS_CHANGED' THEN execution_id IS NOT NULL AND item_id IS NULL", ""
        )

    def _migrate_schema_v3(self):
        pass


def migration_snapshot(db):
    names = (
        "source_events",
        "event_retention",
        "archived_event_keys",
        "retention_checkpoint",
        "execution_checkpoints",
    )
    return {
        name: [tuple(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY 1")]
        for name in names
    }


def make_legacy_storage(path):
    clock = dict(utc=AT, mono=0.0)
    with PreSpeedAdjustmentStorage(
        path, utc_clock=lambda: clock["utc"], monotonic_clock=lambda: clock["mono"]
    ) as store:
        controller = make_controller(store)
        start(controller)
        finish(controller)
        clock.update(utc=AT + timedelta(days=8), mono=8 * 24 * 60 * 60)
        assert controller.prune_events(limit=1).pruned_count == 1
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 3
        before = migration_snapshot(store.connection)
        assert before["event_retention"] and before["archived_event_keys"]
        schema = [
            tuple(row)
            for row in store.connection.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                "AND (tbl_name IN ('source_events','event_retention','archived_event_keys')) "
                "ORDER BY type,name"
            )
        ]
        assert all("PARAMETERS_CHANGED" not in sql for _, _, sql in schema)
    return before, schema


def test_actual_schema3_migration_preserves_retention_archives_checks_and_foreign_keys(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    before, schema = make_legacy_storage(path)
    with SimulatorStorage(path) as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert migration_snapshot(store.connection) == before
        assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for kind, name, sql in schema:
            migrated_sql = store.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type=? AND name=?", (kind, name)
            ).fetchone()[0]
            if name in {"source_events", "archived_event_keys"}:
                assert "PARAMETERS_CHANGED" in migrated_sql
                without_new_kind = migrated_sql.replace(
                    "WHEN 'PARAMETERS_CHANGED' THEN execution_id IS NOT NULL AND item_id IS NULL",
                    "",
                )
                assert without_new_kind.split() == sql.split()
            else:
                assert migrated_sql == sql
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store.connection.execute("UPDATE event_retention SET recorded_at='2000-01-01'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store.connection.execute("UPDATE archived_event_keys SET body_sha256=?", ("0" * 64,))
    with SimulatorStorage(path) as store:
        assert migration_snapshot(store.connection) == before


def test_schema3_migration_failure_rolls_back_tables_rows_triggers_and_version(tmp_path):
    path = tmp_path / "migration-failure.sqlite3"
    before, schema = make_legacy_storage(path)

    class FailingMigration(SimulatorStorage):
        def _before_parameters_migration_commit(self):
            raise RuntimeError("parameter migration interrupted")

    with pytest.raises(RuntimeError, match="parameter migration interrupted"):
        FailingMigration(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert migration_snapshot(db) == before
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                "AND (tbl_name IN ('source_events','event_retention','archived_event_keys')) "
                "ORDER BY type,name"
            )
        ] == schema
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    with SimulatorStorage(path) as store:
        assert migration_snapshot(store.connection) == before


def api_service(migrated, controller):
    bootstrap_reference_data(migrated)
    UserAuth(migrated, allowed_origins={"https://speed.test"}).bootstrap_user(
        "manager", "Speed-test-password-123!", roles={"production_manager"}
    )
    seed_item(migrated, "item", actor="manager", now=AT)
    with migrated.begin() as db:
        register_recipe_version(
            db,
            RecipeDefinition(
                id="recipe-1", recipe_id="recipe", version="v1", item_id="item", parameters=BASE
            ),
            actor="manager",
            now=AT,
        )
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
    service.register_execution(
        execution_id="run-A",
        source_id=identity.source_id,
        stream_id=identity.stream_id,
        work_order_id="work-A",
        recipe_version="recipe-1",
        parameters=BASE,
        counter_epoch=identity.counter_epoch,
        target_quantity=4,
        now=AT,
    )
    return service, dict(
        authenticated_gateway_id="gateway",
        authenticated_source_id=identity.source_id,
        authenticated_stream_id=identity.stream_id,
    )


def ingest(service, scope, source):
    body = BatchBody(
        batch_id="batch",
        gateway_id="gateway",
        source_id=scope["authenticated_source_id"],
        stream_id=scope["authenticated_stream_id"],
        capture_generation=1,
        capture_seq=1,
        captured_at=AT,
        events=source,
    )
    return service.ingest(
        GatewayBatch(body=body, content_sha256=batch_digest(body)), **scope, now=AT
    )


def test_actual_changed_run_settles_with_original_registered_parameters(migrated, controller):
    service, scope = api_service(migrated, controller)
    start(controller)
    adjust(controller)
    final = finish(controller)
    assert final.parameters != BASE and final.baseline_parameters == BASE
    assert ingest(service, scope, events(controller)).durability == "COMMITTED"
    with migrated.begin() as db:
        result = record_final_summary(db, final, **scope, now=AT)
    assert result.status == "SETTLED"
    with migrated.connect() as db:
        registered = db.execute(
            text("SELECT parameters FROM production_executions WHERE execution_id='run-A'")
        ).scalar_one()
    assert registered == BASE.model_dump(mode="json")
    assert service.get_totals("run-A")["good_quantity"] == final.good_quantity


@pytest.mark.parametrize(
    "damage", ["missing-summary", "forged-summary", "missing-inbox", "extra-inbox"]
)
def test_missing_forged_or_extra_history_prevents_settlement(migrated, controller, damage):
    service, scope = api_service(migrated, controller)
    start(controller)
    adjust(controller, "first", 7.0)
    adjust(controller, "second", 5.0)
    final = finish(controller)
    source = events(controller)
    assert ingest(service, scope, source).durability == "COMMITTED"
    body = final.model_dump(mode="json")
    if damage == "missing-summary":
        body["parameter_changes"] = []
    elif damage == "forged-summary":
        body["parameter_changes"][0]["payload"]["requested_by"] = "forged-manager"
    elif damage == "extra-inbox":
        # A self-consistent one-event summary must still prove every actual change.
        body["parameter_changes"] = body["parameter_changes"][:1]
        body["parameters"] = body["parameter_changes"][0]["payload"]["parameters"]
    with migrated.begin() as db:
        if damage == "missing-inbox":
            db.execute(
                text("DELETE FROM event_inbox WHERE event_id=:id"),
                {"id": final.parameter_changes[0].event_id},
            )
        result = record_final_summary(db, RunSummary.model_validate(body), **scope, now=AT)
    assert result.status != "SETTLED"


@pytest.mark.parametrize(
    "line_state,role,speed,expected",
    [
        (2, "production_manager", 7.0, 200),
        (3, "production_manager", 4.8, 200),
        (2, "production_manager", 7.21, 409),
        (0, "production_manager", 7.0, 409),
        (2, "operator", 7.0, 403),
    ],
)
async def test_http_active_speed_preflight_preserves_parameters_manage_role(
    migrated, controller, line_state, role, speed, expected
):
    service, scope = api_service(migrated, controller)
    start(controller)
    source = events(controller)
    assert ingest(service, scope, source).durability == "COMMITTED"
    values = {
        "Line.State": line_state,
        "Line.ControlMode": 1,
        "Line.ControlVersion": controller.state.control_version,
        "Line.EventSeq": source[-1].seq,
        "Line.ActiveExecutionId": "run-A",
        "Line.CounterEpoch": controller.storage.identity.counter_epoch,
    }
    samples = []
    for tag in load_contract()["tags"]:
        name, kind = tag["logical_name"], tag["data_type"]
        default = (
            False
            if kind == "Boolean"
            else ""
            if kind == "String"
            else 1.0
            if kind == "Double"
            else 0
        )
        samples.append(
            dict(
                sample_id=f"snapshot-{name}",
                tag_id=name,
                source_timestamp=AT,
                server_timestamp=AT,
                acquired_at=AT,
                status_code=0,
                reading=dict(data_type=kind, value=values.get(name, default)),
            )
        )
    capture = BatchBody(
        schema_version="1.1",
        batch_id="snapshot",
        gateway_id="gateway",
        source_id=scope["authenticated_source_id"],
        stream_id=scope["authenticated_stream_id"],
        capture_generation=1,
        capture_seq=2,
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
    assert (
        service.ingest(
            GatewayBatch(body=capture, content_sha256=batch_digest(capture)), **scope, now=AT
        ).durability
        == "COMMITTED"
    )
    origin = "https://speed.test"
    auth = UserAuth(migrated, allowed_origins={origin}, clock=lambda: AT)
    password = "Speed-http-password-123!"
    auth.bootstrap_user("browser", password, roles={role})
    challenge = auth.begin_login()
    initial = auth.login(
        "browser",
        password,
        challenge_token=challenge.token,
        csrf_token=challenge.csrf_token,
        origin=origin,
    )
    session = auth.change_password(
        initial.token, "Changed-speed-password-123!", csrf_token=initial.csrf_token, origin=origin
    )
    app = create_user_app(auth, UserHTTPConfig(origin=origin))
    request = command(
        controller,
        "SET_PARAMETERS",
        "browser-adjust",
        recipe_version="recipe-1",
        parameters=dict(temperature_c=150.0, speed_m_per_min=speed),
    )
    body = request.model_dump(mode="json")
    body["requested_by"] = "browser"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
        client.cookies.set("hmi_session", session.token)
        response = await client.post(
            "/api/v1/commands",
            json=dict(
                line_id=LINE_ID,
                source_id=scope["authenticated_source_id"],
                stream_id=scope["authenticated_stream_id"],
                request=body,
            ),
            headers={"Origin": origin, "X-CSRF-Token": session.csrf_token},
        )
    assert response.status_code == expected, response.text
    with migrated.connect() as db:
        count = db.execute(text("SELECT count(*) FROM command_requests")).scalar_one()
    assert count == (1 if expected == 200 else 0)
