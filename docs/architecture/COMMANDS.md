# API durable command request journal

This implements the durable API journal, one-time dispatch reservation and PLC
receipt reconciliation from development plan §5.1. REQUESTED does not mean
delivered, PLC accepted, started or successful. All operations are SQL-only;
HTTP delivery, gateway authentication, OPC dispatch and job deployment remain
separate integrations.

The integration interface is:

```python
with auth.authorized_transaction(
    token,
    permission=command_permission(request),
    csrf_token=csrf_token,
    origin=origin,
) as authorized:
    result = enqueue_command(
        authorized.connection,
        request,
        principal=authorized.principal,
        line_id=line_id,
        source_id=source_id,
        stream_id=stream_id,
        now=now,
    )
```

The caller must use the live authorized transaction, its original connection and
principal. Never construct a principal from request JSON. The journal revalidates
the contract, exact requested_by/login match, password-change requirement and
command role as a second check. START/PAUSE/RESET_FAULT require operator;
LOAD_JOB/END_RUN allow operator or production_manager; SET_PARAMETERS requires
production_manager. These use the existing auth permission names operate,
load_job, end_run and parameters_manage. Return the CommandResult DTO through its
JSON serializer so UInt64 values retain their decimal string representation.

The source/stream must be registered to the selected line. Every non-null
execution_id must already belong to that source/stream; selecting the screen's
current execution does not rewrite a command's original execution. The existing
simulator PLC supports the literal target `Line` and rejects other target IDs.
Only that target is accepted here. No physical-ID alias or asset target is
silently rewritten; mapped targets require a later explicit PLC binding contract.
LOAD_JOB therefore needs a previously registered execution. Deployed work-order
validation and server-side execution allocation remain required future work.

Migration 0004_commands creates only command_requests and command_request_audit;
its downgrade removes only these tables. It freezes its DDL and does not import
mutable application metadata. A request stores its original contract JSON,
canonical_command_payload, source/stream/line scope, authenticated actor and
initial CommandResult. Actor audit is inserted in the same caller transaction.
Failure or rollback leaves neither request nor audit. This service never commits
or opens another connection.

A transaction-scoped advisory lock serializes the global command ID. Same ID,
same canonical payload and same scope return the stored result without another
audit entry. This check precedes expiry and unresolved-command checks. Any content
or scope collision raises CommandConflict and cannot overwrite the first request.

A line row is locked before its source_streams row, matching ingestion and source
configuration. This prevents a source/line foreign-key lock inversion when
ingestion and enqueue run concurrently. The global command-ID advisory lock is
acquired first; existing-ID replays return before taking these row locks.
A locked line row serializes new requests across every source/stream bound to
that physical line; independent lines remain independent. Any REQUESTED,
DELIVERED, ACCEPTED or OUTCOME_UNKNOWN command blocks new START, LOAD_JOB and
SET_PARAMETERS on the same line, including requests through a different bound
stream. PAUSE, END_RUN and RESET_FAULT remain journalable under normal
authorization and scope checks while another command is unresolved. This permits
safety-oriented requests to be recorded, but does not assert the PLC will allow
them or authorize dispatch. An old REQUESTED result remains unresolved until
claim_next expires it before reservation or reserves its possible handoff.

A new request arriving at or after expires_at is recorded as terminal EXPIRED,
with completed_at, a reason and no delivered_at. It cannot become eligible for
delivery by replaying its ID. Fresh requests start REQUESTED with no delivered_at.
No request is automatically retried with a new ID.

The service-side delivery interfaces accept a caller-owned SQLAlchemy connection
and explicit authenticated_gateway_id, authenticated_source_id,
authenticated_stream_id and now keyword arguments:

- `claim_next(connection, ...) -> SubmitWork | QueryWork | None`
- `apply_submission(connection, CommandSubmission, ...) -> CommandResult`
- `apply_result(connection, CommandResult | CommandNotFound, ...) -> CommandResult`

The trusted caller must authenticate these scope values; they cannot come from
unverified request fields. Every operation checks the current registered gateway
and source/stream binding. Receipt IDs must already belong to that exact scope;
completion evidence must preserve the original execution. Each transaction must
perform one journal operation, commit it, and only then perform external I/O.
The service never commits or performs that I/O itself.

claim_next chooses a candidate without holding row locks, then takes command-ID
advisory, line and source locks in that order, and rechecks state and scope. A
REQUESTED command is atomically changed to OUTCOME_UNKNOWN with reason
DISPATCH_RESERVED before returning `action="SUBMIT"` and the original request.
Only one committed reservation returns that submission payload. A rollback leaves
REQUESTED eligible; a lost committed response deliberately sacrifices dispatch
availability and permits only `action="QUERY"` with CommandResultRequest for the
original ID. There is no expiring reservation lease or automatic resubmission.

The current CommandResult contract requires delivered_at for OUTCOME_UNKNOWN.
For DISPATCH_RESERVED it records a **possible handoff boundary, not proof of PLC
delivery**. Actual PLC receipt/result timestamps later replace the complete result
together; API and PLC clock values are never mixed to build an invalid result.
Only REQUESTED can expire. A reserved, accepted or unknown command remains
queryable after its delivery TTL and never becomes EXPIRED or FAILED on timeout.
One expired candidate is recorded per poll; the next poll considers later work.

Pending requests take precedence over query work. Among unresolved commands,
durable last_poll_order uses the inserted audit sequence to rotate queries,
including when wall time is equal or moves backward. last_polled_at retains the
caller's actual time separately. Neither scheduling timestamp establishes PLC
heartbeat or physical freshness.

PLC submissions record ACCEPTED or REJECTED; results permit ACCEPTED, SUCCEEDED,
FAILED, REJECTED and OUTCOME_UNKNOWN. REQUESTED and EXPIRED cannot be supplied as
PLC outcomes. Terminal result payloads are immutable: identical terminal replays
return the original, conflicting terminal payloads raise CommandConflict, and
delayed acceptance cannot replace completion. Acceptance can resolve the initial
reservation, but cannot clear a later reconciled unknown state. CommandNotFound
retains OUTCOME_UNKNOWN and query-only behavior; it never authorizes resubmission.
The journal prioritizes state evidence over cross-process wall-clock ordering.

A PLC submission rejection with COMMAND_ID_CONFLICT is an identity collision,
not a terminal rejection of the API's original canonical request. It latches
OUTCOME_UNKNOWN and collision_latched, retaining the line's unresolved-command
guard. Later ID-only lookups can describe the other canonical command even when
execution_id matches, so their exact payloads are audited without changing the
latched result. Delayed acceptance, rejection or success cannot clear this latch.
Explicit canonical reconciliation is required and is not implemented here. A
collision receipt arriving after a known terminal result leaves that result
immutable, but still latches the independent identity conflict. The line guard
considers that latch even on a terminal row and blocks new start/job/settings
until explicit canonical reconciliation; no automatic clearing path exists.

Migration 0005_command_dispatch preserves frozen 0004 DDL and adds audit evidence
JSON, dispatch/reconciliation action names, a durable collision latch, poll order/time and a line
status index. Exact remote submission/result/not-found JSON is audited atomically
with state changes, including harmless delayed receipts. A downgrade after
dispatch evidence exists fails its old action constraint rather than deleting that
evidence. Terminal conflicts raise within the caller transaction and do not mutate
the journal or audit.

Run the real disposable PostgreSQL checks with:

```sh
uv run pytest -q tests/integration/test_commands.py
```

They exercise concurrent duplicates and competing commands, canonical/scope
collisions, actor and role checks, original execution isolation, initial expiry,
same-ID replay after expiry, request/audit rollback, scoped migration downgrade,
one concurrent SUBMIT, lost-response QUERY, claim rollback, late TTL, immutable
terminal evidence and durable query fairness. They do not establish an operational
HTTP/gateway/OPC control path or actual PLC acceptance.
