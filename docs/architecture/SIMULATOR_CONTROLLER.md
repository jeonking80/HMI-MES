# Simulator runtime controller · DEV-05~07 integration

`apps/simulator/controller.py` joins the pure PLC and deterministic three-stage
process to the simulator's SQLite owner and command journal. These are trusted
internal methods. They do not authenticate an operator or authorize an OPC UA
client. An ingress adapter must enforce its own identity, authorization, origin
and request validation before calling them. `requested_by` is audit data.

The runtime never starts a worker by itself. A caller invokes `tick(now=...)` for
one simulated 100ms boundary. The constructor takes an already open
`SimulatorStorage` and an explicit UTC time. All calls must be serialized on the
connection's owning thread. The storage file retains its exclusive process lock.

## Command and production boundary

`submit(request, now=..., authority="REMOTE")` stores the original normalized
request, receipt, initial result and trusted ingress authority in one transaction.
It returns `JournalSubmission`; adapters return its `submission` as the protocol
receipt. An accepted receipt schedules work for a later explicit tick; it is not
success. A duplicate resolves the original record before TTL, mode, version and
conflict checks. Different content under the same ID returns a conflict receipt
without replacing the original record. Another accepted operation blocks a new
conflicting command.

A tick rechecks the stored command's original LOCAL/REMOTE authority and all PLC
conditions. LOCAL operations use the same conditions and require AUTO for START.
They never temporarily change the saved mode. LOAD_JOB registers a fresh process
execution, START requires observed simulator drive feedback, and PAUSE fixes
stationary WIP before success. Good output plus current WIP reserves target
capacity; rejected output may be replaced. CX-33 adds PLC target completion for
new LOADs with auto_end_at_target=true: it drains and finalizes the original run
without creating an END_RUN command ID. False retains manual ending. CX-34 adds
explicit start_when_ready reservations; safety/mode changes and restart cancel
them. See GOOD_QUANTITY_TARGET.md and START_RESERVATION.md for compatibility.

END_RUN first disables admission and drains existing items under their original
execution ID. Fault or emergency stop prevents motion and leaves ENDING pending.
It never disposes of WIP or clears a physical cause. Explicit stationary
`scrap_wip(..., reason=..., positions_verified=True)` creates source scrap events.
Finalization requires zero WIP and stopped feedback. A safety-state END_RUN may
finish its execution while the PLC remains FAULT or ESTOP.

One outer SQLite transaction commits process items, WIP, counters, source events,
PLC state, command completion, run summary, setting intervals and audit evidence.
Nested component operations use savepoints. Final counts come from the stored
process; no public controller method accepts summary counts. The final STOPPED
source event, if any, precedes the fixed `last_event_seq` in both summary and
END_RUN completion evidence. A single commit can contain STOPPING and PAUSED
observations; both use the one control version allocated to that transaction.
The storage transaction generation also shares that allocation across controller
operations composed inside an authenticated console's outer transaction. Pure
PLC substeps do not consume additional version increments in the same commit.

Production, state and fault records share the same contiguous source sequence.
Control audit rows additionally retain the cause/command ID, time, execution,
version and full observed decision. Counter-only and temperature-only changes
do not increment the control version unless they change readiness.

## Runtime persistence

The runtime owns an additive, explicitly versioned component schema:

| Table | Durable content |
|---|---|
| `runtime_schema_version` | Component version 1; unknown/unversioned layouts fail closed |
| `runtime_state` | Complete validated PLC JSON snapshot and boundary timestamp |
| `runtime_command_authority` | Original trusted LOCAL/REMOTE source per command ID |
| `runtime_runs` | Current per-execution summary; final summaries cannot be updated or deleted |
| `runtime_settings` | Immutable recipe version, actual parameters and effective tick/source boundary |
| `runtime_audit` | Local controls, original command causes and explicit recovery/settlement operators |

This extension does not change the storage owner's `user_version`. It never
silently reconstructs a controller snapshot for an already existing process.
Malformed persisted snapshots, summary identities and timestamps raise
`StorageCorruption` and latch recovery. A caught corruption inside a nested unit
still poisons the outer commit. External validation errors remain distinct.

SET_PARAMETERS uses the storage owner's explicit parameter transition. WIP,
positions, quantities and random state survive it. Each applied version retains
its own interval; reusing an earlier version in the same execution is rejected.
The reported heater permission and actual process heater command share
`tags.heater_enabled`, including fault, emergency stop and recovery inhibition.

## Restart and explicit gates

Opening a controller always inhibits motion and latches PLC recovery while
retaining the execution, WIP, journal and original source identities. Observed
drive feedback is retained until the local simulator confirms stopped positions;
construction does not invent stopped feedback. No persisted accepted request is
replayed. `get_result` exposes such an interrupted request as OUTCOME_UNKNOWN
while the original journal row remains ACCEPTED pending explicit reconciliation.

`recover(now=..., operator_id=..., checks=RecoveryChecks(...))` requires stopped
outputs, verified positions, validated storage, resolved original command IDs and
explicit confirmation. Its storage gate, PLC gate and operator audit commit
together. It leaves STOPPED with output off; running again requires a separate
START. Failed checks leave the recovery gate in place.

`reconcile_interrupted(command_id, now=..., checks=...)` is an explicit local
cancellation after stopped-position reconciliation. It marks the original
accepted operation FAILED with `RESTART_INTERRUPTED`, clears its pending motion
intent and preserves quantities and safety causes. It does not infer failure
from elapsed time or claim that earlier motion never occurred. The internal
method requires a trusted, authorized ingress to establish its local evidence.

The completed run remains unsettled until an operator calls
`acknowledge_settlement(execution_id, now=..., operator_id=...)` in LOCAL mode.
The controller verifies the active final zero-WIP summary and recomputes source
totals/continuity/epoch/item ownership through the storage gate. The final summary
stays immutable; a later LOAD_JOB uses a new execution ID and zero counters.
This is local simulator reconciliation. A remote MES must independently verify
its ingestion/settlement before transmitting the next LOAD_JOB.

## Internal interfaces and validation

Read interfaces are `state`, `get_result(command_id)`,
`get_summary(execution_id)` and `read_events(ReadEventsRequest)`; absent IDs use
the protocol's explicit not-found models. `set_inputs(PLCInputs, now=...,
control_mode=..., operation_mode=...)` supplies a trusted simulator observation.
On each process tick, deterministic physical temperature and drive motion replace
the corresponding process measurements. All IDs and UTC times are validated.

Run the actual SQLite and subprocess integration evidence with:

```sh
.venv/bin/pytest tests/integration/test_simulator_controller.py -q
.venv/bin/ruff check apps/simulator/controller.py tests/integration/test_simulator_controller.py
```

The tests exercise a complete load/start/pause/resume/end cycle, source-derived
final quantities, identical and conflicting IDs, setting intervals, fault during
END_RUN and explicit scrap, local source rechecks, repeated jobs with explicit
settlement, restart without automatic motion, unresolved command gates, version
aggregation, corruption poisoning, and subprocess termination immediately before
and after the combined final commit. They establish this runtime slice, not full
project acceptance or real equipment safety/performance.

CX-35 adds active speed-only SET_PARAMETERS with durable PARAMETERS_CHANGED
history, storage v4 and runtime v3. Original recipe baseline stays immutable;
latest parameters and complete source evidence justify settlement. See
SPEED_ADJUSTMENTS.md. These 14th-order extensions supersede earlier stopped-only
setting/explicit-only motion descriptions without weakening safety or recovery.
