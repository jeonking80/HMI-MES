# Persistent test temperature sensor bias

App B's existing operator-only `/inputs` path now accepts
`PLCInputs.temperature_offset_c`: a finite signed value from -50 to +50°C,
defaulting to zero. It is a **test sensor bias**, not a recipe edit or physical
heater fault. It lets the real 100ms simulator sustain a temperature-deviation
scenario while retaining the original heater and material-quality physics.

The measured PLC/OPC temperature is:

```text
Sealer.TemperaturePV = max(0°C, physical process temperature + sensor offset)
```

The explicit zero floor preserves the existing nonnegative sensor range. The
physical process temperature remains separate; the process and projected
`Sealer.HeaterOn` thermostat both use that physical base, with the existing fault,
emergency-stop and recovery permission gates. The bias does not change recipe
setpoint, base temperature, quality outcomes or original production identities.
PLC readiness and START permission still use measured PV, so applying a bias can
immediately remove readiness. Injecting or clearing a bias is not a start, reset,
fault-clear or recovery action.

`SimulatorController.set_inputs` recomputes measured PV immediately from the
current stored process plus the requested offset. Tick uses the same helper.
With no process, the base is the controller's configured ambient temperature.
The previous measured value is never used as the next base; repeated snapshots,
repeated application and clearing therefore do not double the bias. Incoming
`temperature_c` is a derived readback at this controller boundary. Startup also
recomputes it before restart/recovery decisions, including stale legacy manual
PV overrides and existing V2 snapshots.

The console labels this field as test-only, shows physical temperature, measured
PV and active offset separately, and retains the existing operator, initial
password-change, Origin and CSRF checks and audit records. “편차 입력값 0으로”
changes only the form. The operator must then use the existing explicit “시험
입력 적용” action; the clear button sends no stale full-state HTTP write.

## Runtime component V1 → V2

This changes the controller's separate `runtime_schema_version` from 1 to 2.
SimulatorStorage's existing SQLite `PRAGMA user_version=3` remains unchanged.
New databases start with the V2 component. Existing V1 components migrate inside
the controller/storage owner's single transaction:

1. Require the recognized runtime table columns and exactly one singleton snapshot.
2. Require the complete known PLCState keys and exact legacy PLCInputs keys,
   without the new field or any extra/missing keys; validate the full typed state.
3. Add only `temperature_offset_c=0.0` to the current snapshot and advance the
   runtime component version. Historical audit JSON remains byte-for-byte intact.
4. Validate the process projection, derive the current sensor readback and enter
   the existing `RECOVERY_REQUIRED` startup state. Persisted offsets survive later
   restarts; motion never resumes automatically.

Unrecognized versions, malformed snapshots or inconsistent process projections
refuse startup. A missing V1 snapshot does not silently initialize a new PLC.
The internal `_before_runtime_migration_commit` failure seam verifies snapshot,
version and audit rollback together. V2 reads remain strict: the new input must
exist in persisted V2 JSON. There is no used-state downgrade or destructive reset.

## Verification

```sh
.venv/bin/pytest -q tests/unit/test_plc.py tests/unit/test_process.py tests/unit/test_simulator_tags.py tests/integration/test_simulator_controller.py tests/integration/test_simulator_storage.py tests/integration/test_simulator_transactions.py tests/integration/test_simulator_state_events.py tests/integration/test_simulator_console.py tests/integration/test_simulator_fault_inputs.py
.venv/bin/python scripts/check-simulator-browser.py --screenshot /tmp/hmi-simulator-temperature-offset.png
```

The new controller tests include an actual loaded/RUNNING process with 65 explicit
100ms ticks, persistent biased PV, immediate readiness change, repeated apply and
clear, physical thermostat independence, finite/range checks, zero saturation,
restart preservation and exact legacy migration/failure/corruption cases. Console
HTTP tests cover genuine operator writes, viewer/CSRF rejection, audit evidence,
readback and invalid-range rollback.

The Chromium script launches the actual CLI, signs in and changes the initial
password, recovers and starts a real job, applies both negative and positive bias,
observes at least 50 real ticker cycles while RUNNING, verifies physical/measured
separation, clears the form without server mutation, then explicitly applies zero
and continues the existing pause/end/settlement/replay/isolation checks. This is
App B proof; the full OPC UA → gateway → App A alarm acceptance and latency targets
require their separate integration tests. This feature alone does not establish
DEV-14 or T04–T06 completion.
