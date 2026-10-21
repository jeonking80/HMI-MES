# Remote next-job settlement attestation

The API owns MES event reconciliation. Its browser LOAD_JOB boundary rechecks the
previous execution with reconcile_execution in the same authorized transaction
and requires SETTLED before journaling a new job. The simulator cannot query the
MES database and does not independently prove that reconciliation.

An authenticated remote LOAD_JOB therefore implicitly attests that the trusted
API completed this gate. The existing OPC boundary admits the pinned gateway
certificate and passes its configured authenticated principal to
`SimulatorController.submit(..., authority="REMOTE",
authenticated_gateway_principal=...)`. The controller never treats requested_by
as this authenticated identity. Direct in-process REMOTE calls without that
trusted principal retain the existing settlement requirement. LOCAL command and
explicit local-settlement behavior remain unchanged.

Attestation is evaluated only inside the durable command journal's first-seen
decision callback, after immutable same-ID comparison and delivery expiry. Exact
replays and ID/content collisions never create another attestation. Expired first
receipts retain their normal rejected journal result without changing the flag.

For a previous execution, the controller requires the exact durable final ENDED
summary, zero WIP, STOPPED state, stopped output and drive feedback, zero measured
speed, and matching source/stream/counter epoch. The final event boundary must
still equal the current durable source head. Recovery, pending commands and
unresolved conflict forbid attestation. The normal controller projection checks
and durable event/item/counter consistency recheck still apply.

The controller previews previous_execution_settled=true solely to evaluate all
existing PLC version, target, mode, physical, recipe-range and job-reuse guards.
Only when every admission guard passes does the same outer SQLite transaction
store the flag, accepted command journal, command authority and a
REMOTE_SETTLEMENT_ATTESTATION audit entry. Rejected admission creates no
attestation or settlement change; its normal deduplication receipt remains
journaled. Outer rollback removes the provisional flag, audit and acceptance
together. Attestation adds no fabricated source events or successful result.

The audit binds the new command ID, authenticated gateway principal, separately
asserted requested_by, previous source/stream/execution/epoch, expected control
version and canonical final-summary SHA-256. The hash serializes every normalized
RunSummary field except as_of with sorted keys and compact UTF-8 JSON. Excluding
the query-time field does not omit the immutable ended_at or final event boundary.

Execution remains a later tick. It rechecks normal physical/mode guards and the
exact recorded prior final boundary/hash before loading another execution. A
transient fault that clears physically but adds old-run events invalidates the
attestation; the original command fails with REMOTE_SETTLEMENT_CHANGED and does
not create the new execution. The same tick atomically clears the settlement flag
and writes REMOTE_SETTLEMENT_INVALIDATED audit evidence with the failed result.
Invalidation also runs when the physical guard already rejects execution; its
physical failure reason is preserved while the stale settlement flag is cleared.
Switching to LOCAL cannot reuse that stale flag: a local next job still needs
the existing explicit local-settlement acknowledgement. Tick rollback preserves
the original accepted result and flag together instead of partially invalidating.
These checks do not reapply delivery TTL or compare
the admission version to a new live version. Other changed physical conditions
also may fail the request without loading another execution.
Successful LOAD_JOB resets previous_execution_settled=false for the new job. The
previous final summary and production ownership remain unchanged.

This trust interpretation requires an ordered rollout. Disable command dispatch
and drain or quarantine every pre-upgrade queued LOAD_JOB before enabling the
attesting OPC boundary. Deploy the API reconciliation gate first, verify the
gateway certificate/principal binding, then enable the simulator admission rule
and resume dispatch. Do not reinterpret older queued requests as proof of the new
API gate, auto-reissue uncertain requests with new IDs, or clear unresolved
command outcomes to make deployment proceed. Preserve their original-ID query
and explicit reconciliation path.

Focused checks:

```sh
uv run pytest -q tests/integration/test_simulator_controller.py
```

They cover two remote jobs, exact audit binding, rejected physical/version and
untrusted/local paths, expiry/dedup/collision behavior, outer rollback and tick-time
recheck. OPC certificate authentication and the complete HTTP/gateway/MES cycle
are separate integration checks.
