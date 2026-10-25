"""Trusted in-process simulator orchestration, without transport or authorization.

Every mutating boundary uses the storage owner's transaction. Values returned
from hooks inside that boundary are provisional until the outer commit succeeds.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime

from pydantic import TypeAdapter

from apps.simulator import plc
from apps.simulator.command_store import CommandStore, JournalSubmission
from apps.simulator.process import ProcessConfig, fresh_execution, scrap, step
from apps.simulator.storage import (
    FaultEventInput,
    RecoveryRequired,
    RetentionBlocked,
    RetentionResult,
    SimulatorStorage,
    StateEventInput,
    StorageCorruption,
    decode_uint64,
    encode_uint64,
)
from apps.simulator.tags import heater_enabled
from contracts.models import (
    CommandNotFound,
    CommandRequest,
    CommandResult,
    CommandSubmission,
    CompletionEvidence,
    Identifier,
    LineState,
    ReadEventsRequest,
    RunNotFound,
    RunSummary,
    StateEvent,
    UTCTimestamp,
)

_TIME = TypeAdapter(UTCTimestamp)
_ID = TypeAdapter(Identifier)
_VERSION = 2
_PROCESS_DEFAULT = ProcessConfig()
_LEGACY_STATE_FIELDS = frozenset(
    {
        "target_id",
        "state",
        "control_version",
        "control_mode",
        "operation_mode",
        "inputs",
        "recovery_required",
        "latched_fault_code",
        "pressure_ready",
        "permissive",
        "ready",
        "run_output",
        "execution_id",
        "work_order_id",
        "recipe_version",
        "parameters",
        "target_quantity",
        "run_state",
        "wip_quantity",
        "last_event_seq",
        "previous_execution_settled",
        "unresolved_conflict",
        "pending_command_id",
        "pending_kind",
        "stop_purpose",
    }
)
_V1_INPUT_FIELDS = frozenset(
    {
        "estop",
        "guard_closed",
        "fault_code",
        "fault_resettable",
        "equipment_ready",
        "temperature_c",
        "pressure_bar",
        "run_feedback",
        "speed_m_per_min",
    }
)


class SimulatorController:
    """One explicit 100ms tick at a time; never starts a background worker.

    The caller owns the store's lifetime and must serialize calls on its owning
    thread. Network adapters must authenticate and authorize before using these
    internal methods; requested_by is audit data, never a permission grant.
    """

    def __init__(
        self,
        storage: SimulatorStorage,
        *,
        now: datetime,
        process_config: ProcessConfig = _PROCESS_DEFAULT,
        plc_config: plc.PLCConfig = plc.DEFAULT_CONFIG,
        seed: int = 1,
    ) -> None:
        self.storage = storage
        self.process_config = process_config
        self.plc_config = plc_config
        self.seed = seed
        self._control_generation = None
        self._control_base = None
        now = _TIME.validate_python(now)
        with storage.transaction():
            CommandStore.initialize_schema(storage.connection)
            self._initialize_schema()
            self.commands = CommandStore(storage)
            row = storage.connection.execute(
                "SELECT snapshot_json FROM runtime_state WHERE singleton=1"
            ).fetchone()
            if row is None:
                if storage.load_execution() is not None:
                    raise StorageCorruption("Existing process has no controller snapshot")
                state = plc.PLCState(inputs=self._sensor_inputs(plc.PLCInputs(), None))
                self._save(state, now)
            else:
                before = self.state
                self._validate_projection(before)
                # Legacy manual PV overrides (and any stale V2 readback) cannot
                # survive startup into the next recovery/START decision.
                refreshed = plc.PLCState.model_validate(
                    {
                        **before.model_dump(),
                        "inputs": self._sensor_inputs(before.inputs, storage.load_execution()),
                    }
                )
                state = plc.restart(refreshed, config=plc_config)
                self._record_change(before, state, now=now, cause="PROCESS_RESTART")
                self._save(state, now)

    def _initialize_schema(self) -> None:
        db = self.storage.connection
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        names = {
            "runtime_schema_version",
            "runtime_state",
            "runtime_runs",
            "runtime_settings",
            "runtime_audit",
            "runtime_command_authority",
        }
        if "runtime_schema_version" in tables:
            versions = [tuple(row) for row in db.execute("SELECT * FROM runtime_schema_version")]
            if versions not in ([(1, 1)], [(1, _VERSION)]):
                raise ValueError("Unsupported runtime schema; explicit migration required")
            expected = {
                "runtime_schema_version": ("singleton", "version"),
                "runtime_state": ("singleton", "snapshot_json", "updated_at"),
                "runtime_runs": ("execution_id", "summary_json"),
                "runtime_settings": (
                    "execution_id",
                    "recipe_version",
                    "parameters_json",
                    "effective_tick",
                    "effective_seq",
                    "recorded_at",
                ),
                "runtime_audit": (
                    "audit_id",
                    "occurred_at",
                    "cause",
                    "execution_id",
                    "control_version",
                    "details_json",
                ),
                "runtime_command_authority": ("command_id", "authority"),
            }
            for table, columns in expected.items():
                if tuple(row[1] for row in db.execute(f"PRAGMA table_info({table})")) != columns:
                    raise ValueError("Invalid runtime schema; explicit migration required")
            if versions == [(1, 1)]:
                self._migrate_runtime_v1()
            return
        if names & tables:
            raise ValueError("Unversioned runtime component; explicit migration required")
        # Individual statements retain the owner's transaction (executescript commits implicitly).
        statements = (
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
        for statement in statements:
            db.execute(statement)
        db.execute("INSERT INTO runtime_schema_version VALUES (1,?)", (_VERSION,))

    def _migrate_runtime_v1(self) -> None:
        """Recognize frozen V1 snapshots and add conservative defaults atomically."""
        db = self.storage.connection
        rows = db.execute("SELECT singleton,snapshot_json FROM runtime_state").fetchall()
        if len(rows) != 1 or rows[0][0] != 1:
            self.storage.mark_corrupt()
            raise StorageCorruption("V1 controller snapshot is missing or ambiguous")
        try:
            payload = json.loads(rows[0][1])
            if not isinstance(payload, dict) or set(payload) != _LEGACY_STATE_FIELDS:
                raise ValueError("V1 snapshot requires exact versioned fields")
            if (
                not isinstance(payload["inputs"], dict)
                or set(payload["inputs"]) != _V1_INPUT_FIELDS
            ):
                raise ValueError("V1 inputs require exact recognized fields")
            payload["inputs"]["temperature_offset_c"] = 0.0
            payload.update(auto_end_at_target=False, end_reason=None)
            migrated = plc.PLCState.model_validate_json(json.dumps(payload, allow_nan=False))
        except (TypeError, ValueError) as error:
            self.storage.mark_corrupt()
            raise StorageCorruption("Unrecognized V1 controller snapshot") from error
        db.execute(
            "UPDATE runtime_state SET snapshot_json=? WHERE singleton=1",
            (migrated.model_dump_json(),),
        )
        db.execute("UPDATE runtime_schema_version SET version=? WHERE singleton=1", (_VERSION,))
        self._before_runtime_migration_commit()

    def _before_runtime_migration_commit(self) -> None:
        """Internal migration failure seam, before the owning transaction commits."""

    def _sensor_inputs(self, inputs: plc.PLCInputs, process) -> plc.PLCInputs:
        base = (
            process.temperature_c
            if process is not None
            else self.process_config.ambient_temperature_c
        )
        # The existing PLC sensor range is nonnegative Celsius. The signed test
        # bias saturates at zero without changing the stored physical temperature.
        return plc.PLCInputs.model_validate(
            {**inputs.model_dump(), "temperature_c": max(0.0, base + inputs.temperature_offset_c)}
        )

    @property
    def state(self) -> plc.PLCState:
        row = self.storage.connection.execute(
            "SELECT snapshot_json FROM runtime_state WHERE singleton=1"
        ).fetchone()
        if row is None:
            self.storage.mark_corrupt()
            raise StorageCorruption("Controller snapshot is missing")
        try:
            payload = json.loads(row[0])
            if not isinstance(payload, dict):
                raise ValueError("Persisted PLC snapshot must be an object")
            fields = set(plc.PLCState.model_fields)
            if set(payload) == _LEGACY_STATE_FIELDS:
                payload.update(auto_end_at_target=False, end_reason=None)
            elif set(payload) != fields:
                raise ValueError("Persisted PLC snapshot must contain every versioned field")
            if not isinstance(payload["inputs"], dict) or set(payload["inputs"]) != set(
                plc.PLCInputs.model_fields
            ):
                raise ValueError("Persisted PLC inputs must contain every versioned field")
            return plc.PLCState.model_validate_json(json.dumps(payload, allow_nan=False))
        except ValueError as error:
            self.storage.mark_corrupt()
            raise StorageCorruption("Invalid controller snapshot") from error

    @contextmanager
    def _unit(self):
        with self.storage.transaction():
            generation = self.storage.transaction_generation
            state = self.state
            self._validate_projection(state)
            if generation != self._control_generation:
                self._control_generation = generation
                self._control_base = state.control_version
            yield

    def _decision_state(self, state: plc.PLCState) -> plc.PLCState:
        """Evaluate each pure substep against the outer transaction's version budget."""
        if self._control_base is None:
            return state
        return plc.PLCState.model_validate(
            {
                **state.model_dump(),
                "control_version": self._control_base,
            }
        )

    def _head(self) -> int:
        return self.storage._bounds()[1]

    def _save(self, state: plc.PLCState, now: datetime) -> plc.PLCState:
        state = plc.PLCState.model_validate({**state.model_dump(), "last_event_seq": self._head()})
        self.storage.connection.execute(
            "INSERT INTO runtime_state VALUES (1,?,?) ON CONFLICT(singleton) "
            "DO UPDATE SET snapshot_json=excluded.snapshot_json,updated_at=excluded.updated_at",
            (state.model_dump_json(), now.isoformat()),
        )
        return state

    def _audit(self, state: plc.PLCState, now: datetime, cause: str, details: str) -> None:
        self.storage.connection.execute(
            "INSERT INTO runtime_audit "
            "(occurred_at,cause,execution_id,control_version,details_json) VALUES (?,?,?,?,?)",
            (
                now.isoformat(),
                cause,
                state.execution_id,
                encode_uint64(state.control_version),
                details,
            ),
        )

    def _record_change(
        self,
        before: plc.PLCState,
        after: plc.PLCState,
        *,
        now: datetime,
        cause: str,
    ) -> plc.PLCState:
        if self._control_base is not None:
            changed = (
                before.control_version != self._control_base
                or after.control_version != self._control_base
            )
            after = plc.PLCState.model_validate(
                {
                    **after.model_dump(),
                    "control_version": self._control_base + int(changed),
                }
            )
        events = []
        if before.state != after.state:
            events.append(
                StateEventInput(
                    occurred_at=now,
                    execution_id=after.execution_id,
                    payload={
                        "previous_state": before.state,
                        "state": after.state,
                        "control_version": after.control_version,
                    },
                )
            )
        if before.latched_fault_code != after.latched_fault_code:
            if before.latched_fault_code:
                events.append(
                    FaultEventInput(
                        occurred_at=now,
                        execution_id=after.execution_id,
                        payload={"fault_code": before.latched_fault_code, "active": False},
                    )
                )
            if after.latched_fault_code:
                events.append(
                    FaultEventInput(
                        occurred_at=now,
                        execution_id=after.execution_id,
                        payload={"fault_code": after.latched_fault_code, "active": True},
                    )
                )
        if events:
            self.storage.append_control_events(tuple(events))
        if before != after:
            self._audit(after, now, cause, after.model_dump_json())
        return self._save(after, now)

    def _validate_projection(self, state: plc.PLCState) -> None:
        pending = self.commands.pending_records()
        if len(pending) > 1:
            raise StorageCorruption("Multiple accepted commands conflict")
        if state.pending_command_id is not None and (
            not pending
            or pending[0].request.command_id != state.pending_command_id
            or pending[0].request.command.kind != state.pending_kind
            or pending[0].request.execution_id != state.execution_id
        ):
            raise StorageCorruption("PLC pending operation has no matching accepted journal row")
        for record in pending:
            self._authority(record.request.command_id)
        if state.last_event_seq != self._head():
            raise StorageCorruption("PLC event head disagrees with source stream")
        process = self.storage.load_execution()
        if process is None:
            if state.execution_id is not None:
                raise StorageCorruption("PLC execution has no process snapshot")
            return
        if (
            state.execution_id != process.execution_id
            or state.wip_quantity != process.wip_quantity
            or state.parameters is None
            or state.parameters.temperature_c != process.config.temperature_sp_c
            or state.parameters.speed_m_per_min != process.config.speed_m_per_min
            or state.last_event_seq != self._head()
        ):
            raise StorageCorruption("PLC and process projection disagree")
        row = self.storage.connection.execute(
            "SELECT summary_json FROM runtime_runs WHERE execution_id=?", (state.execution_id,)
        ).fetchone()
        if row is None:
            raise StorageCorruption("Active run summary is missing")
        summary = self.get_summary(state.execution_id)
        if (
            summary.recipe_version != state.recipe_version
            or summary.parameters != state.parameters
            or summary.state != state.run_state
        ):
            raise StorageCorruption("Run summary and PLC settings or lifecycle disagree")
        for field in (
            "input_quantity",
            "good_quantity",
            "reject_quantity",
            "scrap_quantity",
            "wip_quantity",
        ):
            if getattr(summary, field) != getattr(process, field):
                raise StorageCorruption("Run summary and process quantities disagree")

    def prune_events(self, *, limit: int = 1000) -> RetentionResult:
        """Explicit stopped maintenance; storage owns age, bounds and transaction.

        No gateway ACK, caller clock or cached permission grants deletion. Storage
        invokes this bound current-state guard inside its validated write unit.
        RetentionBlocked/RecoveryRequired propagate without deleting any events.
        """
        return self.storage.prune_events(controller_guard=self._retention_guard, limit=limit)

    def _retention_guard(self) -> None:
        state = self.state
        if self.storage.recovery_required or state.recovery_required:
            raise RetentionBlocked("RECOVERY_REQUIRED: controller has not completed recovery")
        if (
            state.inputs.fault_code
            or state.latched_fault_code
            or state.inputs.estop
            or not state.inputs.guard_closed
            or state.unresolved_conflict
        ):
            raise RetentionBlocked(
                "UNRESOLVED_FAULT: current fault or conflict forbids maintenance"
            )
        if (
            state.state != LineState.STOPPED
            or state.run_output
            or state.inputs.run_feedback
            or state.inputs.speed_m_per_min != 0
        ):
            raise RetentionBlocked("NOT_STOPPED: outputs and drive feedback must be stopped")
        if self.commands.pending_records() or state.pending_command_id is not None:
            raise RetentionBlocked("UNRESOLVED_COMMAND: accepted or unknown command remains")
        self._validate_projection(state)
        executions = {
            row[0] for row in self.storage.connection.execute("SELECT execution_id FROM executions")
        }
        summaries = {
            row[0]
            for row in self.storage.connection.execute("SELECT execution_id FROM runtime_runs")
        }
        if summaries != executions:
            raise StorageCorruption(
                "Retention requires a durable runtime summary for every execution"
            )
        completed_ends = {}
        for row in self.storage.connection.execute(
            "SELECT command_id FROM command_journal "
            "WHERE json_extract(request_json,'$.command.kind')='END_RUN'"
        ):
            record = self.commands.lookup(row[0])
            if record.result.status == "SUCCEEDED":
                if (
                    record.result.evidence.execution_id != record.request.execution_id
                    or record.result.evidence.last_event_seq is None
                ):
                    raise StorageCorruption("END_RUN completion lacks its original final boundary")
                completed_ends.setdefault(record.request.execution_id, []).append(
                    record.result.evidence.last_event_seq
                )
        for execution_id in sorted(executions):
            summary = self.get_summary(execution_id)
            if not summary.final or summary.state != "ENDED" or summary.wip_quantity != 0:
                raise RetentionBlocked(
                    "UNFINISHED_EXECUTION: every run must be final ENDED with zero WIP"
                )
            process = self.storage.load_execution(execution_id)
            if (
                any(
                    getattr(summary, field) != getattr(process, field)
                    for field in (
                        "input_quantity",
                        "good_quantity",
                        "reject_quantity",
                        "scrap_quantity",
                        "wip_quantity",
                    )
                )
                or summary.last_event_seq > self._head()
            ):
                raise StorageCorruption(
                    "Final runtime quantities or boundary disagree with durable process"
                )
            if summary.end_reason == "TARGET_REACHED":
                if completed_ends.get(execution_id):
                    raise StorageCorruption("Autonomous final has an unexpected END_RUN result")
                self._validate_target_end_proof(summary)
            elif completed_ends.get(execution_id) != [summary.last_event_seq]:
                raise StorageCorruption(
                    "Final boundary disagrees with immutable END_RUN completion"
                )
            for table, kind in (
                ("source_events", "json_extract(body,'$.kind')"),
                ("archived_event_keys", "kind"),
            ):
                if self.storage.connection.execute(
                    f"SELECT 1 FROM {table} WHERE execution_id=? AND seq>? "
                    f"AND {kind} IN ('PRODUCTION_GOOD','PRODUCTION_REJECT','WIP_SCRAP') LIMIT 1",
                    (execution_id, encode_uint64(summary.last_event_seq)),
                ).fetchone():
                    raise StorageCorruption("Production or scrap occurred after the final boundary")
            settings = self.storage.connection.execute(
                "SELECT recipe_version,parameters_json FROM runtime_settings WHERE execution_id=? "
                "ORDER BY effective_tick DESC,effective_seq DESC,recipe_version DESC LIMIT 1",
                (execution_id,),
            ).fetchone()
            try:
                applied = json.loads(settings[1]) if settings is not None else None
            except (ValueError, TypeError) as error:
                raise StorageCorruption("Invalid durable runtime settings") from error
            if (
                settings is None
                or settings[0] != summary.recipe_version
                or applied != summary.parameters.model_dump(mode="json")
                or process.config.temperature_sp_c != summary.parameters.temperature_c
                or process.config.speed_m_per_min != summary.parameters.speed_m_per_min
            ):
                raise StorageCorruption(
                    "Final runtime settings disagree with durable applied configuration"
                )

    def _target_load(self, execution_id: str):
        rows = self.storage.connection.execute(
            "SELECT command_id FROM command_journal "
            "WHERE json_extract(request_json,'$.command.kind')='LOAD_JOB' "
            "AND json_extract(request_json,'$.execution_id')=?",
            (execution_id,),
        ).fetchall()
        loads = [self.commands.lookup(row[0]) for row in rows]
        loads = [record for record in loads if record.result.status == "SUCCEEDED"]
        if len(loads) != 1:
            raise StorageCorruption(
                "Target final requires exactly one successful original LOAD_JOB"
            )
        record = loads[0]
        args = record.request.command.args
        if record.result.evidence.execution_id != execution_id:
            raise StorageCorruption("Original LOAD_JOB completion execution disagrees")
        if "auto_end_at_target" not in args.model_fields_set or not args.auto_end_at_target:
            raise StorageCorruption("Target final lacks an explicitly persisted LOAD_JOB policy")
        return record

    def _target_end_details(self, summary: RunSummary) -> dict:
        load = self._target_load(summary.execution_id)
        target = load.request.command.args.target_quantity
        if summary.good_quantity < target:
            raise StorageCorruption("Target final has insufficient good output")
        return {
            "execution_id": summary.execution_id,
            "load_command_id": load.request.command_id,
            "auto_end_at_target": True,
            "target_quantity": target,
            "good_quantity": summary.good_quantity,
            "last_event_seq": str(summary.last_event_seq),
            "canonical_final_sha256": self._final_digest(summary),
        }

    def _record_target_end_proof(
        self, state: plc.PLCState, summary: RunSummary, now: datetime
    ) -> None:
        details = self._target_end_details(summary)
        if state.target_quantity != details["target_quantity"] or not state.auto_end_at_target:
            raise StorageCorruption("Target policy disagrees with the original LOAD_JOB")
        self._audit(
            state,
            now,
            "TARGET_AUTO_END",
            json.dumps(details, sort_keys=True, separators=(",", ":")),
        )

    def _validate_target_end_proof(self, summary: RunSummary) -> None:
        rows = self.storage.connection.execute(
            "SELECT occurred_at,details_json FROM runtime_audit "
            "WHERE cause='TARGET_AUTO_END' AND execution_id=?",
            (summary.execution_id,),
        ).fetchall()
        try:
            if (
                len(rows) != 1
                or _TIME.validate_python(rows[0][0]) != summary.ended_at
                or json.dumps(json.loads(rows[0][1]), sort_keys=True, separators=(",", ":"))
                != json.dumps(
                    self._target_end_details(summary), sort_keys=True, separators=(",", ":")
                )
            ):
                raise ValueError("Target proof disagrees with original final boundary")
        except (ValueError, TypeError) as error:
            raise StorageCorruption("Invalid or missing autonomous target-end proof") from error

    def submit(
        self,
        request: CommandRequest,
        *,
        now: datetime,
        authority: plc.Mode = "REMOTE",
        authenticated_gateway_principal: str | None = None,
    ) -> JournalSubmission:
        now = _TIME.validate_python(now)
        if authority not in {"LOCAL", "REMOTE"}:
            raise ValueError("Unknown command authority")
        if authenticated_gateway_principal is not None:
            authenticated_gateway_principal = _ID.validate_python(authenticated_gateway_principal)

        def decide(candidate: CommandRequest) -> CommandSubmission:
            state = self.state
            previous_final = None
            preview = state
            if self.commands.pending_records():
                allowed, code, reason = False, "COMMAND_CONFLICT", "Resolve the existing command"
            else:
                if (
                    authority == "REMOTE"
                    and authenticated_gateway_principal is not None
                    and candidate.command.kind == "LOAD_JOB"
                    and state.execution_id is not None
                ):
                    previous_final = self._remote_settlement_final(state)
                    if previous_final is None:
                        return CommandSubmission(
                            command_id=candidate.command_id,
                            received_at=now,
                            disposition="REJECTED",
                            reason_code="REMOTE_SETTLEMENT_BLOCKED",
                            reason="Remote next job requires the exact stopped final run",
                        )
                    preview = plc.PLCState.model_validate(
                        {
                            **state.model_dump(),
                            "previous_execution_settled": True,
                        }
                    )
                decision = plc.check_command(
                    preview,
                    candidate,
                    now=now,
                    config=self.plc_config,
                    authority=authority,
                )
                allowed, code, reason = decision.allowed, decision.reason_code, decision.reason
                if (
                    allowed
                    and candidate.command.kind == "LOAD_JOB"
                    and (self.storage.load_execution(candidate.execution_id) is not None)
                ):
                    allowed, code, reason = (
                        False,
                        "EXECUTION_ID_REUSED",
                        "Execution ID already has durable history",
                    )
                if (
                    allowed
                    and candidate.command.kind == "SET_PARAMETERS"
                    and (
                        self.storage.connection.execute(
                            "SELECT 1 FROM runtime_settings "
                            "WHERE execution_id=? AND recipe_version=?",
                            (candidate.execution_id, candidate.command.args.recipe_version),
                        ).fetchone()
                        is not None
                    )
                ):
                    allowed, code, reason = (
                        False,
                        "RECIPE_VERSION_REUSED",
                        "Recipe version already has an applied interval",
                    )
                if allowed and previous_final is not None:
                    # This is a consistency recheck, never a recovery override:
                    # _remote_settlement_final already required both recovery
                    # flags clear. Recompute durable events/items/counters before
                    # committing the trusted API attestation and accepted journal.
                    self.storage.acknowledge_recovery()
                    digest = self._final_digest(previous_final)
                    self._save(preview, now)
                    self._audit(
                        preview,
                        now,
                        "REMOTE_SETTLEMENT_ATTESTATION",
                        json.dumps(
                            {
                                "command_id": candidate.command_id,
                                "authenticated_gateway_principal": authenticated_gateway_principal,
                                "asserted_requested_by": candidate.requested_by,
                                "previous_source_id": previous_final.source_id,
                                "previous_stream_id": previous_final.stream_id,
                                "previous_execution_id": previous_final.execution_id,
                                "previous_counter_epoch": previous_final.counter_epoch,
                                "canonical_final_sha256": digest,
                                "expected_control_version": str(candidate.expected_control_version),
                                **self._remote_state_tail(previous_final),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )
            return CommandSubmission(
                command_id=candidate.command_id,
                received_at=now,
                disposition="ACCEPTED" if allowed else "REJECTED",
                reason_code=code,
                reason=reason,
            )

        with self._unit():
            submission = self.commands.submit(request, received_at=now, decide=decide)
            if submission.created:
                self.storage.connection.execute(
                    "INSERT INTO runtime_command_authority VALUES (?,?)",
                    (request.command_id, authority),
                )
            return submission

    def _remote_settlement_final(self, state: plc.PLCState) -> RunSummary | None:
        """Physical and durable preconditions for trusting a remote API attestation.

        MES reconciliation is owned by the API. This method proves only the
        matching immutable PLC final boundary and simulator physical consistency.
        """
        if (
            self.storage.recovery_required
            or state.recovery_required
            or state.state != LineState.STOPPED
            or state.run_state != "ENDED"
            or state.wip_quantity != 0
            or state.run_output
            or state.inputs.run_feedback
            or state.inputs.speed_m_per_min != 0
            or state.pending_command_id is not None
            or state.unresolved_conflict
        ):
            return None
        final = self.get_summary(state.execution_id)
        if (
            not isinstance(final, RunSummary)
            or not final.final
            or final.state != "ENDED"
            or final.wip_quantity != 0
            or self._remote_state_tail(final) is None
        ):
            return None
        return final

    def _remote_state_tail(self, final: RunSummary) -> dict[str, str] | None:
        """Prove the entire retained post-final stream is a stopped recovery suffix."""
        head = self._head()
        identity = self.storage.identity
        if (
            final.last_event_seq > head
            or final.source_id != identity.source_id
            or final.stream_id != identity.stream_id
        ):
            return None
        expected = final.last_event_seq + 1
        events = []
        last_state = LineState.STOPPED
        allowed = {LineState.STOPPED, LineState.RECOVERY_REQUIRED}
        for row in self.storage.connection.execute(
            "SELECT * FROM source_events WHERE seq>? AND seq<=? ORDER BY seq",
            (encode_uint64(final.last_event_seq), encode_uint64(head)),
        ):
            try:
                event = StateEvent.model_validate_json(row["body"])
                if (
                    event.seq != expected
                    or decode_uint64(row["seq"]) != event.seq
                    or event.event_id != row["event_id"]
                    or event.source_id != identity.source_id
                    or event.stream_id != identity.stream_id
                    or row["source_id"] != event.source_id
                    or row["stream_id"] != event.stream_id
                    or row["execution_id"] != event.execution_id
                    or row["item_id"] is not None
                    or event.payload.previous_state not in allowed
                    or event.payload.state not in allowed
                ):
                    return None
            except (ValueError, TypeError, KeyError):
                return None
            events.append(event.model_dump(mode="json"))
            expected += 1
            last_state = event.payload.state
        if expected != head + 1 or last_state != LineState.STOPPED:
            return None
        return {
            "tail_end_seq": str(head),
            "tail_sha256": hashlib.sha256(
                json.dumps(
                    events, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")
            ).hexdigest(),
        }

    @staticmethod
    def _final_digest(final: RunSummary) -> str:
        canonical = final.model_dump(mode="json")
        canonical.pop("as_of")
        return hashlib.sha256(
            json.dumps(
                canonical,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()

    def _remote_attestation_current(self, state: plc.PLCState, request: CommandRequest) -> bool:
        row = self.storage.connection.execute(
            "SELECT details_json FROM runtime_audit WHERE cause='REMOTE_SETTLEMENT_ATTESTATION' "
            "AND json_extract(details_json,'$.command_id')=? ORDER BY audit_id DESC LIMIT 1",
            (request.command_id,),
        ).fetchone()
        if row is None:
            # Initial loads and unchanged explicit local-settlement callers do
            # not consume an implicit remote attestation.
            return True
        final = self._remote_settlement_final(state)
        if final is None:
            return False
        tail = self._remote_state_tail(final)
        if tail is None:
            return False
        details = json.loads(row[0])
        return all(
            details.get(key) == value
            for key, value in {
                "command_id": request.command_id,
                "asserted_requested_by": request.requested_by,
                "previous_source_id": final.source_id,
                "previous_stream_id": final.stream_id,
                "previous_execution_id": final.execution_id,
                "previous_counter_epoch": final.counter_epoch,
                "canonical_final_sha256": self._final_digest(final),
                "expected_control_version": str(request.expected_control_version),
                **tail,
            }.items()
        )

    def _authority(self, command_id: str) -> plc.Mode:
        row = self.storage.connection.execute(
            "SELECT authority FROM runtime_command_authority WHERE command_id=?",
            (command_id,),
        ).fetchone()
        if row is None or row[0] not in {"LOCAL", "REMOTE"}:
            raise StorageCorruption("Pending command authority is missing or invalid")
        return row[0]

    def get_result(self, command_id: str) -> CommandResult | CommandNotFound:
        record = self.commands.lookup(command_id)
        if record is None:
            return CommandNotFound(
                code="COMMAND_NOT_FOUND",
                command_id=command_id,
                reason="Command ID is unknown",
            )
        if record.result.status == "ACCEPTED" and self.state.recovery_required:
            row = self.storage.connection.execute(
                "SELECT updated_at FROM runtime_state WHERE singleton=1"
            ).fetchone()
            try:
                updated = _TIME.validate_python(datetime.fromisoformat(row[0]))
            except (TypeError, ValueError) as error:
                self.storage.mark_corrupt()
                raise StorageCorruption("Invalid persisted runtime timestamp") from error
            return CommandResult(
                command_id=command_id,
                status="OUTCOME_UNKNOWN",
                delivered_at=record.result.delivered_at,
                updated_at=max(record.result.updated_at, updated),
                reason_code="RESTART_RECONCILIATION_REQUIRED",
                reason="Original command requires explicit reconciliation and will not replay",
            )
        return record.result

    def _complete(self, request: CommandRequest, transition: plc.PLCTransition, now: datetime):
        if transition.status == "ACCEPTED":
            return
        record = self.commands.lookup(request.command_id)
        state = self.state
        succeeded = transition.status == "SUCCEEDED"
        self.commands.complete(
            request.command_id,
            CommandResult(
                command_id=request.command_id,
                status=transition.status,
                delivered_at=record.result.delivered_at,
                updated_at=now,
                completed_at=now,
                reason_code=transition.reason_code,
                reason=transition.reason,
                applied_control_version=state.control_version if succeeded else None,
                evidence=CompletionEvidence(
                    state=state.state,
                    execution_id=state.execution_id,
                    recipe_version=state.recipe_version,
                    last_event_seq=self._head(),
                )
                if succeeded
                else None,
            ),
        )

    def _settings(self, state: plc.PLCState, now: datetime) -> None:
        process = self.storage.load_execution()
        self.storage.connection.execute(
            "INSERT INTO runtime_settings VALUES (?,?,?,?,?,?)",
            (
                state.execution_id,
                state.recipe_version,
                state.parameters.model_dump_json(),
                encode_uint64(process.tick),
                encode_uint64(self._head()),
                now.isoformat(),
            ),
        )

    def _summary(self, state: plc.PLCState, now: datetime) -> RunSummary | None:
        if state.execution_id is None:
            return None
        old = self.get_summary(state.execution_id)
        if isinstance(old, RunSummary) and old.final:
            return old
        process = self.storage.load_execution(state.execution_id)
        final = state.run_state == "ENDED"
        summary = RunSummary(
            source_id=self.storage.identity.source_id,
            stream_id=self.storage.identity.stream_id,
            execution_id=state.execution_id,
            state=state.run_state,
            as_of=now,
            counter_epoch=self.storage.identity.counter_epoch,
            recipe_version=state.recipe_version,
            parameters=state.parameters,
            input_quantity=process.input_quantity,
            good_quantity=process.good_quantity,
            reject_quantity=process.reject_quantity,
            scrap_quantity=process.scrap_quantity,
            wip_quantity=process.wip_quantity,
            last_event_seq=self._head(),
            final=final,
            ended_at=now if final else None,
            end_reason=state.end_reason,
        )
        self.storage.connection.execute(
            "INSERT INTO runtime_runs VALUES (?,?) ON CONFLICT(execution_id) "
            "DO UPDATE SET summary_json=excluded.summary_json",
            (state.execution_id, summary.model_dump_json()),
        )
        return summary

    def tick(self, *, now: datetime) -> plc.PLCState:
        """Advance one 100ms process boundary and commit all resulting evidence."""
        now = _TIME.validate_python(now)
        with self._unit():
            state = self.state
            if self.storage.recovery_required or state.recovery_required:
                raise RecoveryRequired("Explicit recovery is required before ticking")
            pending = self.commands.pending_records()
            if len(pending) > 1:
                raise StorageCorruption("Multiple accepted commands conflict")
            request = pending[0].request if pending else None
            completion = None
            if request is not None and state.pending_command_id is None:
                transition = plc.execute_command(
                    self._decision_state(state),
                    request,
                    config=self.plc_config,
                    authority=self._authority(request.command_id),
                )
                if (
                    request.command.kind == "LOAD_JOB"
                    and self._authority(request.command_id) == "REMOTE"
                    and not self._remote_attestation_current(state, request)
                ):
                    invalidated = plc.PLCState.model_validate(
                        {
                            **(
                                transition.state if transition.status == "FAILED" else state
                            ).model_dump(),
                            "previous_execution_settled": False,
                        }
                    )
                    self._audit(
                        invalidated,
                        now,
                        "REMOTE_SETTLEMENT_INVALIDATED",
                        json.dumps(
                            {
                                "command_id": request.command_id,
                                "previous_execution_id": state.execution_id,
                                "current_event_seq": str(self._head()),
                                "reason_code": "REMOTE_SETTLEMENT_CHANGED",
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )
                    transition = plc.PLCTransition(
                        state=invalidated,
                        status="FAILED",
                        reason_code=(
                            transition.reason_code
                            if transition.status == "FAILED"
                            else "REMOTE_SETTLEMENT_CHANGED"
                        ),
                        reason=(
                            transition.reason
                            if transition.status == "FAILED"
                            else "Prior final boundary differs from admitted remote attestation"
                        ),
                    )
                after = transition.state
                if transition.status != "FAILED" and request.command.kind == "LOAD_JOB":
                    # Old accepted journals lack this field; their policy remains manual.
                    after = plc.PLCState.model_validate(
                        {
                            **after.model_dump(),
                            "auto_end_at_target": (
                                request.command.args.auto_end_at_target
                                if "auto_end_at_target" in request.command.args.model_fields_set
                                else False
                            ),
                        }
                    )
                if transition.status != "FAILED":
                    if request.command.kind == "LOAD_JOB":
                        config = replace(
                            self.process_config,
                            temperature_sp_c=after.parameters.temperature_c,
                            speed_m_per_min=after.parameters.speed_m_per_min,
                        )
                        self.storage.register_execution(
                            fresh_execution(
                                self.storage.load_execution(),
                                after.execution_id,
                                config=config,
                                seed=self.seed,
                            )
                        )
                    elif request.command.kind == "SET_PARAMETERS":
                        self.storage.update_process_parameters(
                            self.storage.load_execution(),
                            temperature_c=after.parameters.temperature_c,
                            speed_m_per_min=after.parameters.speed_m_per_min,
                        )
                state = self._record_change(state, after, now=now, cause=request.command_id)
                if transition.status != "FAILED" and request.command.kind in {
                    "LOAD_JOB",
                    "SET_PARAMETERS",
                }:
                    self._settings(state, now)
                self._summary(state, now)
                completion = transition
            process = self.storage.load_execution()
            if process is not None and state.run_state != "ENDED":
                moving = state.run_output and state.state in {LineState.RUNNING, LineState.STOPPING}
                result = step(
                    process,
                    run=moving,
                    admit=moving
                    and state.run_state != "ENDING"
                    # Target means good output. Existing WIP reserves capacity;
                    # only confirmed rejection/scrap makes room for replacement.
                    and process.good_quantity + process.wip_quantity < state.target_quantity,
                    heater_on=heater_enabled(state, process),
                )
                self.storage.apply_transition(process, result, occurred_at=now)
                process = result.state
                feedback = moving and not (
                    state.stop_purpose == "END_RUN" and process.wip_quantity == 0
                )
                inputs = self._sensor_inputs(
                    plc.PLCInputs.model_validate(
                        {
                            **state.inputs.model_dump(),
                            "run_feedback": feedback,
                            "speed_m_per_min": process.config.speed_m_per_min if feedback else 0.0,
                        }
                    ),
                    process,
                )
                after = plc.update_inputs(
                    self._decision_state(state),
                    inputs,
                    wip_quantity=process.wip_quantity,
                    last_event_seq=self._head(),
                    config=self.plc_config,
                )
                state = self._record_change(state, after, now=now, cause="PROCESS_TICK")
            if request is not None and state.pending_command_id == request.command_id:
                ready_to_end = (
                    request.command.kind == "END_RUN"
                    and state.wip_quantity == 0
                    and not state.inputs.run_feedback
                    and state.inputs.speed_m_per_min == 0
                )
                transition = plc.finish_command(
                    self._decision_state(state),
                    request,
                    final_boundary_durable=ready_to_end,
                    config=self.plc_config,
                    authority=self._authority(request.command_id),
                )
                state = self._record_change(
                    state,
                    transition.state,
                    now=now,
                    cause=request.command_id,
                )
                # Final STOPPED event is already in the stream before fixing this boundary.
                self._summary(state, now)
                self._before_boundary_commit()
                completion = transition
            if process is not None:
                after = plc.advance_target_end(
                    self._decision_state(state),
                    good_quantity=process.good_quantity,
                    config=self.plc_config,
                )
                target_final = state.run_state != "ENDED" and after.run_state == "ENDED"
                state = self._record_change(state, after, now=now, cause="TARGET_END")
                if target_final:
                    final = self._summary(state, now)
                    self._record_target_end_proof(state, final, now)
                    self._before_boundary_commit()
            self._summary(state, now)
            if completion is not None:
                self._complete(request, completion, now)
            self._before_tick_commit()
        return self.state

    def _before_boundary_commit(self) -> None:
        """Test seam: final summary exists provisionally, command result not yet written."""

    def _before_tick_commit(self) -> None:
        """Test seam: all process, PLC, event and command writes remain provisional."""

    def get_summary(self, execution_id: str) -> RunSummary | RunNotFound:
        execution_id = _ID.validate_python(execution_id)
        row = self.storage.connection.execute(
            "SELECT summary_json FROM runtime_runs WHERE execution_id=?", (execution_id,)
        ).fetchone()
        if row is None:
            return RunNotFound(
                code="RUN_NOT_FOUND",
                execution_id=execution_id,
                reason="Execution ID is unknown",
            )
        try:
            summary = RunSummary.model_validate_json(row[0])
            identity = self.storage.identity
            if (
                summary.execution_id != execution_id
                or summary.source_id != identity.source_id
                or summary.stream_id != identity.stream_id
                or summary.counter_epoch != identity.counter_epoch
            ):
                raise ValueError("Run summary identity disagrees with storage")
            return summary
        except (TypeError, ValueError) as error:
            self.storage.mark_corrupt()
            raise StorageCorruption("Invalid persisted run summary") from error

    def read_events(self, request: ReadEventsRequest):
        return self.storage.read_events(request)

    def set_inputs(
        self,
        inputs: plc.PLCInputs,
        *,
        now: datetime,
        control_mode: plc.Mode | None = None,
        operation_mode: plc.OperationMode | None = None,
    ) -> plc.PLCState:
        """Trusted console input boundary; does not authorize the caller."""
        now = _TIME.validate_python(now)
        inputs = plc.PLCInputs.model_validate_json(inputs.model_dump_json())
        with self._unit():
            before = self.state
            inputs = self._sensor_inputs(inputs, self.storage.load_execution())
            after = plc.update_inputs(
                self._decision_state(before),
                inputs,
                control_mode=control_mode,
                operation_mode=operation_mode,
                config=self.plc_config,
            )
            state = self._record_change(before, after, now=now, cause="LOCAL_INPUTS")
            self._summary(state, now)
        return self.state

    def reconcile_interrupted(
        self,
        command_id: str,
        *,
        now: datetime,
        checks: plc.RecoveryChecks,
    ) -> CommandResult:
        """Explicitly cancel interrupted work after stopped-position confirmation.

        This records a confirmed interrupted operation, never infers failure from
        a timeout and never claims that physical motion did not previously occur.
        """
        now = _TIME.validate_python(now)
        with self._unit():
            state = self.state
            if not state.recovery_required or not all(checks.model_dump().values()):
                raise RecoveryRequired("Explicit complete reconciliation checks are required")
            if state.inputs.run_feedback or state.inputs.speed_m_per_min != 0:
                raise RecoveryRequired("Interrupted work requires confirmed stopped positions")
            record = self.commands.lookup(command_id)
            if record is None or record.result.status != "ACCEPTED":
                raise ValueError("Only an interrupted accepted command can be reconciled")
            if state.pending_command_id not in {None, command_id}:
                raise StorageCorruption("Another command owns the persisted operation")
            after = plc.cancel_pending_after_recovery(
                self._decision_state(state),
                config=self.plc_config,
            )
            self._record_change(state, after, now=now, cause="INTERRUPTED_COMMAND_CONFIRMED")
            result = CommandResult(
                command_id=command_id,
                status="FAILED",
                updated_at=now,
                completed_at=now,
                delivered_at=record.result.delivered_at,
                reason_code="RESTART_INTERRUPTED",
                reason="Explicit reconciliation confirmed interruption with outputs stopped",
            )
            self.commands.complete(command_id, result)
            return result

    def recover(
        self,
        *,
        now: datetime,
        operator_id: str,
        checks: plc.RecoveryChecks,
    ) -> plc.PLCState:
        """Explicit local recovery gate; caller must authorize its operator."""
        now = _TIME.validate_python(now)
        operator_id = _ID.validate_python(operator_id)
        with self._unit():
            if self.commands.pending_records():
                raise RecoveryRequired("Reconcile original pending command IDs before recovery")
            before = self.state
            self._validate_projection(before)
            self.storage.acknowledge_recovery()
            after = plc.acknowledge_recovery(
                self._decision_state(before),
                checks,
                config=self.plc_config,
            )
            state = self._record_change(before, after, now=now, cause="EXPLICIT_RECOVERY")
            self._audit(state, now, "RECOVERY_OPERATOR", _ID.dump_json(operator_id).decode())
            self._summary(state, now)
        return self.state

    def acknowledge_settlement(
        self,
        execution_id: str,
        *,
        now: datetime,
        operator_id: str,
    ) -> plc.PLCState:
        """Attest local run/event reconciliation before explicitly loading another job.

        This local simulator operation supplies no claim that a remote MES has
        ingested or settled the stream. Its own delivery gate remains separate.
        """
        now = _TIME.validate_python(now)
        execution_id = _ID.validate_python(execution_id)
        operator_id = _ID.validate_python(operator_id)
        with self._unit():
            state = self.state
            if state.recovery_required or self.storage.recovery_required:
                raise RecoveryRequired("Recovery must finish before local settlement")
            if state.control_mode != "LOCAL":
                raise ValueError("Local settlement requires LOCAL control mode")
            if self.commands.pending_records():
                raise ValueError("Pending commands must be resolved before local settlement")
            summary = self.get_summary(execution_id)
            if (
                not isinstance(summary, RunSummary)
                or not summary.final
                or summary.wip_quantity != 0
                or state.execution_id != execution_id
                or state.run_state != "ENDED"
            ):
                raise ValueError("Local settlement requires the active final zero-WIP execution")
            self._validate_projection(state)
            # Recompute durable source totals, continuity, epoch and item ownership;
            # a caller attestation never supplies the quantities being reconciled.
            self.storage.acknowledge_recovery()
            after = plc.PLCState.model_validate(
                {
                    **state.model_dump(),
                    "previous_execution_settled": True,
                }
            )
            state = self._record_change(state, after, now=now, cause="LOCAL_SETTLEMENT")
            self._audit(state, now, "SETTLEMENT_OPERATOR", _ID.dump_json(operator_id).decode())
        return self.state

    def scrap_wip(
        self,
        item_ids: tuple[int, ...],
        *,
        reason: str,
        now: datetime,
        positions_verified: bool,
    ) -> plc.PLCState:
        """Explicit stopped local disposal; END_RUN itself never scraps WIP."""
        now = _TIME.validate_python(now)
        with self._unit():
            state = self.state
            if (
                positions_verified is not True
                or state.inputs.run_feedback
                or state.inputs.speed_m_per_min != 0
                or state.run_output
                or state.recovery_required
            ):
                raise ValueError("Scrap requires explicitly verified stationary positions")
            process = self.storage.load_execution()
            result = scrap(process, item_ids, reason=reason)
            self.storage.apply_transition(process, result, occurred_at=now)
            after = plc.update_inputs(
                self._decision_state(state),
                state.inputs,
                wip_quantity=result.state.wip_quantity,
                last_event_seq=self._head(),
                config=self.plc_config,
            )
            state = self._record_change(state, after, now=now, cause="EXPLICIT_WIP_SCRAP")
            self._summary(state, now)
        return self.state
