# Explicit simulator controller retention

`SimulatorController.prune_events(*, limit=1000) -> RetentionResult` is a trusted
in-process maintenance operation. It has no HTTP route, CLI, background scheduler,
gateway ACK input or caller-supplied clock/cutoff. The storage owner controls its
seven-day recording age, monotonic startup hold, UTC disagreement freeze and
bounded contiguous-prefix deletion policy.

The method passes its bound current-state guard to storage.prune_events. Storage
opens the SQLite write unit, requires existing recovery completion and validates
durable source/checkpoint/process consistency before invoking that guard. A prior
cached stopped check or caller boolean cannot authorize deletion. Controller
validation, deletion, compact identity evidence and retention position changes
therefore share the same transaction.

The guard requires recovered STOPPED state, stopped outputs/feedback and zero
measured speed. Active/latched faults, open guard, emergency stop, unresolved
conflict, pending operation or any accepted/unknown command forbid maintenance.
Every durable execution must have a matching final ENDED zero-WIP runtime summary;
unfinished earlier executions block cleanup even if the current execution ended.
Final quantities, process configuration and the last applied recipe/parameters
must agree for inactive executions as well as the active one. Missing or corrupt
projections raise StorageCorruption and preserve source events while latching
recovery. Maintenance never fabricates completion or acknowledges PLC recovery.
Each final event boundary must also match the original successful END_RUN
completion evidence. Independent live and compacted production/scrap identities
must not lie beyond that boundary, so matching damaged summary/completion records
cannot hide later production. Missing or conflicting completion evidence blocks
retention as corruption rather than discarding its source evidence.

The guard does **not** require an ended run's immutable final event boundary to
equal the current stream head. Legitimate later state/fault events can exist after
that boundary. All of them still need to meet storage recording-age and prefix
rules before deletion. This maintenance rule does not change remote next-job
attestation, whose separate exact-final-boundary check remains in force.

Storage RetentionBlocked reasons, such as STARTUP_HOLD and CLOCK_FROZEN, propagate
unchanged. Controller safe blocks include RECOVERY_REQUIRED, UNRESOLVED_FAULT,
NOT_STOPPED, UNRESOLVED_COMMAND and UNFINISHED_EXECUTION. RecoveryRequired also
propagates when storage itself is unrecovered. Blocked maintenance deletes no
events; it does not report a fabricated successful zero-count cleanup.

Successful calls return pruned_count, pruned_through_seq and retained_from_seq.
Final summaries, result journals, execution snapshots, compact archived event keys
and quantity checkpoints survive. An old ReadEvents request receives the explicit
RETENTION_GAP contract response. Gateway ingestion/ACK positions do not change this
local source-retention authority.

Verification:

```sh
uv run pytest -q tests/integration/test_controller_retention.py tests/integration/test_simulator_controller.py
```

The controller tests use actual SQLite transactions with injected storage-owned
UTC/monotonic clocks. They cover stopped final pruning, bounded prefixes, gaps,
post-final state events, unfinished/faulted/moving/pending blocks, recovery refusal,
inactive projection damage and reopen of compacted evidence. Storage migration,
clock-policy and crash-boundary checks remain in the storage retention suite.
