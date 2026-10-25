# Application protocol v1.0 (DEV-02b)

These are this project's contracts above OPC UA, not standard PLC methods.
`models.py` defines immutable Pydantic models, rejects unknown fields, and exports
Draft 2020-12 JSON Schemas in `schemas/`. `examples/` contains canonical wire
messages, including all six command kinds and rejection, expiry, outcome unknown,
retention gap, and lookup-not-found responses. Python model validation also checks
cross-field rules (timestamps, page continuity, quantity conservation) that JSON
Schema alone does not express. The simulator, gateway and API must apply both.

## Wire values

- Store instants in UTC. Every timestamp requires an explicit offset; inputs with
  other offsets normalize to UTC `Z`. Naive timestamps are invalid.
- **All UInt64 values are canonical decimal JSON strings**, including sequence
  numbers, control versions and production quantities: `"9007199254740993"`.
  Range is 0 through 18446744073709551615; event sequences and event quantities
  start at 1. No signs, leading zeros, fractions or JSON numeric tokens are
  accepted. Python callers may supply strict integers or canonical strings.
  JavaScript clients must retain strings or use `BigInt`, never `Number` for
  these fields. UInt32 counts and finite engineering values remain JSON numbers.
- IDs are opaque, case-sensitive, nonempty strings without whitespace, up to 128
  characters. `schema_version` is `"1.0"`. An incompatible change requires a new
  version; unknown fields and unknown command/event kinds are errors.
- Operating limits, authentication, authorization, lifecycle transitions and
  durable deduplication belong to subsequent application work. Structural
  validation does not authorize a physical action. Parameter models ensure
  finite nonnegative temperature and positive speed; PLC configuration supplies
  equipment-specific limits before commands execute.

## Methods and commands

| Method | Request | Response |
|---|---|---|
| SubmitCommand | CommandRequest | CommandSubmission |
| GetCommandResult | CommandResultRequest | CommandResult or CommandNotFound |
| ReadEvents | ReadEventsRequest | EventPage or EventReadError |
| GetRunSummary | RunSummaryRequest | RunSummary or RunNotFound |

`CommandRequest.command` is discriminated by `kind`. `LOAD_JOB` arguments freeze
work order, recipe version, target and actual parameters. `SET_PARAMETERS` supplies
a new recipe version and parameters. `START`, `PAUSE`, `END_RUN`, `RESET_FAULT`
have empty arguments. Execution ID is mandatory for the four run commands;
pre-run setting and fault reset may explicitly have no execution. During a run,
the application must check the execution ID against the actual active execution.
The target and requested user are always recorded; never derive the execution
from the currently selected screen work order.

Submission `ACCEPTED` means receipt, not physical success. Store the command and
submission before acknowledging. Final success needs `completed_at`, an applied
control version and completion evidence. The PLC verifies the command-specific
evidence (for example RUNNING for START, final fixed summary for END_RUN) and the
HMI compares physical feedback. Structural contracts do not implement that state
machine. See [STATE_MACHINES.md](STATE_MACHINES.md) for allowed transitions.

The gateway's initial undelivered validity is 5 seconds. `expires_at` must follow
`requested_at`; application policy supplies the duration. **EXPIRED means never
delivered**, and may not carry `delivered_at`. A delivered/accepted command whose
result wait elapses becomes `OUTCOME_UNKNOWN`, with no terminal `completed_at`.
Initial waits are START 10 seconds and END_RUN 60 seconds. Timeout does not prove
failure. Query the original command ID and reconcile equipment feedback; do not
automatically issue a fresh ID. An expired record remains expired after reconnect,
including replay of the same ID. Resolve an existing durable command before
checking whether a newly received retry's deadline has passed.

Lookup failure (`COMMAND_NOT_FOUND`) is separate from execution failure. After
possible delivery it does not prove that execution did not occur; reconciliation
and recovery policy must keep conflicting control actions restricted.

### Idempotency

Look up the durable `command_id` first. For the same ID compare
`canonical_command_payload()` against the stored normalized request. The function
serializes validated models with sorted object keys, explicit defaults, UTC
timestamps and normalized numeric fields, excluding only `command_id`. It
includes schema version, target, execution, kind, all arguments, requester,
request/expiry times and expected control version. Equivalent offsets, argument
key order, omitted empty args, and accepted numeric input forms compare equally.
Changed arguments, target, execution, expected version, requester or deadlines
conflict. Identical retries return the existing submission/result; different
content is rejected with `COMMAND_ID_CONFLICT` and does not mutate the original
record. Different command IDs remain different operations even when their
canonical payloads match. These helpers compare content; they do not implement a
durable registry, exactly-once physical execution, or recovery transactions.

## Event stream, retention and cursor policy

Events carry immutable `event_id`, `source_id`, `stream_id`, `seq`, equipment,
execution (nullable only for nonproduction), occurrence time and typed payload.
`PRODUCTION_GOOD`, `PRODUCTION_REJECT`, and `WIP_SCRAP` require the **original
execution_id**. Scrap additionally requires a reason. These event quantities are
the source for MES production totals; counter snapshots are only reconciliation
evidence and must not be added a second time. State and fault events may precede
job loading and explicitly use `execution_id: null`.

Each source owns one durable, contiguous, increasing stream across all event
kinds and equipment. Sequences start at 1; `(source_id, stream_id, seq)` and
`event_id` identify records. App restart retains the stream and sequence. Replacing
or intentionally rebuilding the durable source creates a fresh stream ID;
clients reconcile that change rather than reusing the old cursor. Never wrap
UInt64; stream replacement at exhaustion needs an explicit recovery boundary.
`counter_epoch` identifies a counter reset, **not a new event stream**. It must
not reset sequences. A reset or unexplained decrease blocks quantity settlement
until the application reconciles its cause and execution-specific totals.

`ReadEvents.after_seq` is exclusive. A normal page contains ascending consecutive
events of this source/stream, starting at `after_seq + 1`; unfiltered pages are
required because the cursor covers every kind. `page_last_seq` equals the last
event, or `after_seq` for an empty page. `has_more` reflects the returned retained
upper bound, captured consistently with the page. A nonfinal page must progress.
The maximum page is 1000 events. An empty new stream reports bounds 1 through 0;
an exhausted retained stream may report start equal to end + 1. Boundary reads
at `retained_from_seq - 1` are valid. Source retention is initially seven days.

If `after_seq + 1` is older than retained history, return `RETENTION_GAP` and the
exact inclusive missing range plus current bounds. Do not silently jump the
cursor or claim an empty successful page. A cursor beyond the source head returns
`CURSOR_AHEAD`; a different requested stream returns `STREAM_MISMATCH` with the
actual source stream ID and current bounds. Errors never acknowledge settlement.

The gateway/API must keep **separate durable positions** per source/stream:

1. The continuous validated frontier is the highest sequence for which every
   preceding event has been validated and transactionally applied exactly once.
   A missing, invalid or quarantined event stops this frontier. Receiving a later
   event, or persisting a raw/quarantined batch and ACK, does not advance it.
2. The latest ingestion position tracks where newer data is read or durably
   received. Normally it follows the validated frontier. If history has been
   irrecoverably lost, first durably record the missing range, source/stream,
   detection time, reason and affected executions, and raise the data-loss
   condition. Only then may ingestion resume at `retained_from_seq - 1` and move
   forward through new data. The continuous frontier remains before the hole.

Deduplication, production application and validated frontier advancement require
one transaction. Persisted/received progress may differ from verified/applied
progress. A permanent gap remains visible and keeps affected settlements and
reports provisional; continued ingestion does not make history complete.
Backfilled data uses source sequence and time and cannot move current tags
backward. A future explicit reconciliation procedure may resolve a recorded gap;
this contract contains no implicit cursor reset or completion shortcut.

## Run boundary

`RunSummary` is an execution-specific source snapshot containing applied recipe
version/values, counter epoch, quantities and last event sequence. The quantity
invariant is input = good + reject + scrap + current WIP. `last_event_seq` belongs
to the identified source/stream; production counts require a nonzero boundary.
The simulator must retain durable input/WIP item history as reconciliation
evidence; this DTO carries their aggregate and does not replace that history.

`final: true` requires `state: ENDED`, WIP zero, and `ended_at`. The source fixes
the final quantities and `last_event_seq` atomically after stopping new input and
draining or explicitly scrapping all remaining WIP. Subsequent queries for the
ended run preserve that boundary even while the global stream grows. An epoch
reset may not erase or rewrite this run's totals. A final **source** summary does
not itself mean MES completion: MES remains in ending/reconciliation until all
events through this boundary have been validated/applied and quantities match,
with no unresolved gap, epoch discrepancy or conflict affecting the execution.
Only then can the next job be connected. Late duplicates remain attached to the
original execution. Physical PLC state, source execution state, and MES work-order
settlement are distinct; an ended execution can still leave a physical fault.

## Validation and remaining implementation

Run `.venv/bin/python -m pytest tests/contract/test_protocol.py`. The suite validates
wire examples independently against JSON Schema, verifies schema export drift,
and checks semantic invalid inputs, idempotency, lossless UInt64, UTC, page bounds,
retention gaps, unknown results and final quantity boundaries. Regenerate schemas
with `export_schemas()` after intentional model changes and review the diff.
Persistence, PLC guards, method transport, authentication, transactional ACK,
recovery, cursor storage, MES reconciliation and T01–T12 application integration
remain later DEV work.

## CX-33 target-ending policy and compatible evidence

LOAD_JOB args now accept strict boolean auto_end_at_target, default true for a new
command; binding accepts the same optional policy and writes it explicitly into
its new LOAD request. False keeps the existing good+WIP admission cap and RUNNING
behavior until operator END_RUN. New policy-aware commands persist the explicit
resolved value. The PLC alone observes good output and begins the ending/draining
transition; no synthetic END_RUN request or command ID is created.

RunSummary adds optional end_reason TARGET_REACHED or OPERATOR for newly completed
runs. Unknown historical reason remains absent, never inferred. Known reason is
part of the immutable final-summary hash. Existing quantity, cursor, gap, epoch,
command-unknown and original-execution settlement guards remain.

Compatibility: an omitted LOAD policy is omitted by the specific nested serializer
so old canonical journal bytes remain unchanged. A new unseen request resolves
omission to explicit true before journaling. An omitted retry for a policy-aware
original means true; it cannot match an original explicit false. A policy-free old
ID retains its original identity and old accepted LOAD executes with false.
Previously stored PLC snapshots default to false, and historical final summaries
omit unknown end_reason so original hashes/attestations remain exact.

Deployment coordination is required before public use: Claude owns the UI's exact
binding intent/LOAD args parser and execution-summary parser. They must accept the
optional policy and end_reason while retaining legacy shapes. Upgrade API, gateway
and simulator contracts coherently before issuing new policy-aware LOADs; an old
strict parser will reject the new fields. Resolve outstanding commands by their
original IDs; do not create replacement requests to work around a mixed version.
