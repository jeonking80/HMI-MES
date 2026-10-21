# Browser command boundary

`mount_control_routes(app, auth, session_cookie="hmi_session")` attaches the browser
routes to the existing user application. The user HTTP factory mounts it with the
same HttpOnly session cookie and its existing Host, body-size and sanitized error
handlers. Service bearer tokens do not authenticate these routes.

The current App A endpoints are:

| Endpoint | Exact query / behavior |
|---|---|
| `GET /api/v1/command-record` | `command_id`, `line_id`, `source_id`, `stream_id`; stored original request/result |
| `POST /api/v1/commands` | Existing scoped CommandRequest JSON and mutation protection |
| `GET /api/v1/commands/{command_id}` | Preserved legacy path; existing line/source/stream query |

The fixed `command-record` alias requires each of its four Identifier query keys
exactly once. Missing, repeated, unknown or invalid keys return422. The legacy
path behavior is preserved; its existing query parsing is not broadened or
claimed to enforce the new alias's exact allowlist. App A operation and LOAD
trackers consistently use the fixed alias and `URLSearchParams`, encoding values
once without double decoding or Unicode normalization. Slash, standalone `.`/`..`
and percent-literal IDs remain exact query values rather than URL path segments.
No Identifier/DB restriction, automatic path fallback, POST or replacement ID is
introduced. The alias shares the original read handler and transaction.

The fixed GET and preserved legacy GET require a
live session with read permission and a changed password. It returns only stored
`request` and `result` for a command in that registered line/source/stream. Unknown
and foreign scope use the same 404 response; they do not return the foreign record.
UInt64 request/result fields remain canonical JSON decimal strings.

`POST /api/v1/commands` accepts JSON with line_id, source_id, stream_id and a
validated CommandRequest in request. It requires the live browser cookie, exactly
one allowed Origin and session-bound X-CSRF-Token. The request actor must equal the
current authenticated login. START/PAUSE/RESET_FAULT require operator;
LOAD_JOB/END_RUN allow operator or production_manager; SET_PARAMETERS requires
production_manager. Equipment administrator has no implicit operator authority.

One authorized transaction holds session/user checks, command-ID advisory lock,
line lock and source lock through preflight, settlement and journal writes. The
schema shared lock precedes authentication table access as well as these business locks. There is no network I/O inside
this transaction. Scoped same-ID replay invokes the journal's canonical identity
check before freshness and TTL preflight; a lost browser response can therefore
retrieve its original result after the request TTL without creating another row.
Different canonical payload or scope cannot overwrite the original command.

New requests require all 48 contract tags, fresh Good values, no missing/null
values, CAPTURE_FRESH stored capture health and actual persisted gateway heartbeat
GOOD. Source conflict/reconciliation flags, any stream gap or quarantined event,
and an EventSeq ahead of the durable verified cursor restrict control. Every
sample must carry a known SourceTimestamp and must not claim a source time more
than two seconds in the future. For the four changing heartbeat tags, source time
must be within two seconds of acquisition as current clock evidence. Stable Good
values may retain an older SourceTimestamp: unchanged values do not establish
communication failure or clock skew. Capture/read/heartbeat evidence still governs
freshness. These checks preserve raw values and do not use source time for capture ordering.

ControlMode must be REMOTE and expected_control_version must equal the current
Good ControlVersion. Requested time must be within two seconds of the API clock;
the positive remaining delivery TTL and the requested TTL must each be at most
five seconds. Expired browser requests are rejected before a new journal row is
written. Journal-level ingestion and replay expiry rules remain unchanged.

START, PAUSE and END_RUN require the currently active original execution and its
registered counter epoch. RESET_FAULT permits an empty execution before any job,
or requires matching current execution/epoch when one exists. It never requests
an automatic restart. SET_PARAMETERS is rejected while an execution is active and
cannot target an immutable registered execution; versioned active-work parameter
changes require a later explicit workflow.

LOAD_JOB requires its registered execution, work-order ID, recipe version,
parameters and the exact durable execution_plans target quantity. Missing plans
restrict control. Initial empty active execution is allowed. If a previous
execution remains active, it must differ from the new execution and
reconcile_execution must return SETTLED in this same transaction using durable
final source evidence and current ingested events. Reading a previously cached
SETTLED flag is insufficient.

Successful POST returns the journal's CommandResult, initially REQUESTED, with no
delivered_at or fabricated success. PLC checks, service dispatch reservation and
actual outcome reconciliation remain separate authorities. The snapshot's
read-only control_restricted field does not grant this mutation permission.

Run actual PostgreSQL and ASGI checks with:

```sh
uv run pytest -q tests/integration/test_control_http.py
```

Coverage includes authenticated request/read scope, viewer/production/admin role
denials, CSRF/Origin/actor checks, all-tag quality/freshness and clock evidence,
remote mode/control version, event frontier/quarantine, durable LOAD metadata and
same-transaction prior settlement, immutable active configuration, concurrent
duplicate writes and exact replay after TTL. These adapter tests do not establish
an end-to-end browser/gateway/PLC operating cycle.

LOAD_JOB also requires the immutable registered counter_epoch to equal the live Good Line.CounterEpoch; otherwise it is rejected before journaling or PLC handoff.

## Query-identity validation record

The permanent six-case Identifier alias suite plus existing work-order/cancel/
workflow regressions passed **106 tests**; existing control HTTP regressions
passed **35 tests separately**. Passive original record reads retain read roles,
exact registered scope, no idle renewal and no business writes. Separate
execution/alarm work passed **34 alias tests plus67 existing regressions**; these
are not counted as part of the106 work-order tests.

App A passed **61 pure web tests, typecheck, lint and build**, then the full actual
Chromium/disposable PostgreSQL/API regression. The browser covers original
command/LOAD tracking after response loss, all prior control/WS/auth gates and
six-case MES registration/recovery/cancellation. Special command IDs themselves
are checked by the permanent PostgreSQL six-case and pure URL tests; they are
not represented as six new PLC/browser command executions. Log:
`.local/qa/identifier-browser.log`; identity evidence:
`.local/qa/app-a-identifiers-run-identifiers.png`. This is not an OPC/PLC cycle.
