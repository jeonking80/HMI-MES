# Opt-in capture-to-alarm consumer: DEV-14 slice A

`apps/api/alarm_consumer.py` connects the original authenticated ingestion batch to
the existing Sealer temperature/pressure repository. `IngestionService` defaults
to `alarm_consumer=None`; existing deployments do not silently enable alarms.
This slice has no new listener, rule HTTP route, automatic configuration,
notification delivery, remote clock synchronization protocol, or historical
occurrence reconstruction.

## Activation and transaction ownership

The caller supplies an explicit `AlarmConsumerConfig` with registered scope,
gateway/generation and one or both immutable analog configurations. Each kind and
rule identity is unique. Configured rules must already match their installed heads
exactly, including temperature setpoint, thresholds, severity and version.

```python
consumer = AlarmConsumer(config)  # fresh runtime epoch by default
with engine.begin() as connection:
    consumer.start(connection, now=trusted_api_utc)
service = IngestionService(engine, alarm_consumer=consumer)
```

`start` verifies authoritative source registration and installed rules. It takes
the schema shared lock, gateway advisory lock, line/source locks and sorted alarm
channel locks in ingestion order. It fences captures already admitted before
activation and records a durable RESTART continuity break. It preserves active
instances and clears candidate dwell. A new runtime takes over the frontier's
consumer epoch; the superseded process cannot process new captures or reactivate
its retired epoch. Repeated startup of the same active, unchanged epoch is
idempotent. Startup failure rolls back its fence, checkpoint and receipts together.

The ingestion hook runs after the raw `ingest_batches` insert and before commit or
ACK construction. It uses that exact caller-owned transaction. Raw batch,
independent processing allocator, immutable capture decision/context, qualification
checkpoint, lifecycle deltas, receipts and outbox commit together. A failure yields
no ACK and rolls back all those writes. A committed duplicate follows ingestion's
original ACK lookup. Direct consumer replay also returns its immutable decision
before loading current rules or evaluating clocks, with replayed status and no new
current notification.

Runtime changed-rule mismatch, missing startup, superseded epoch and unsupported
schema fail closed through the existing generic ingestion-unavailable response.
A rule change requires explicit runtime configuration reload/startup before new
captures can qualify. A retry of an older committed capture keeps its original
context and result across that change.

Existing Sealer CLI provisioning, protected configuration files, service startup
and authenticated epoch discovery are documented in [ALARM_RUN.md](ALARM_RUN.md).
They use real UserAuth equipment-admin authorization and do not synthesize
principals. Unit C now connects both existing Sealer JSON and explicit mixed
JSON through the source-wide coordinator. Its focused runtime24 tests and
independent auth/isolation review passed; actual OPC fault/RESET acceptance
remains a separate unit.

## Frozen identity, positions and schema 0011

`alarm_capture_frontiers` binds each channel to the actual registered
source/stream/gateway/generation. It stores its independent UInt64 allocator,
raw-capture high-water mark, startup fence, active runtime/config digest and
requires-restart safety latch.

`alarm_capture_decisions` stores one append-only decision for each channel and
original gateway/batch identity. Its exact composite FK references the admitted
raw batch's source, stream, generation, capture sequence and hash. Channel/source
and frontier/gateway/generation FKs prevent cross-scope attribution. Selected
installed rule version is an FK; unsupported or quarantined decisions explicitly
have no asserted selected configuration. Original context, declared clock policy,
classification/reason and original result remain immutable. Global capture
sequence and assigned-position uniqueness constrain omitted replay history.

Every new decision gets an allocator position. Historical decisions consume a
position without advancing the lifecycle's live cursor. Raw `capture_seq` is
compared only with the same gateway/generation's capture frontier. It is never
copied into lifecycle position or compared with production event sequence.
Exhaustion fails before any partial commit. Empty 0011 can downgrade; any used
frontier/decision refuses downgrade.

The exact raw-batch FK currently also prevents deletion of a referenced batch.
The 7-day raw retention and 180-day alarm evidence targets therefore require a
future compact immutable batch-identity ledger or payload-detachment migration
before pruning. This slice does not implement retention or weaken identity FKs.

## Explicit configured clock assumption

`ConfiguredClockPolicy` has no default offset bounds. Required fields are:

- Policy ID/version, reference ID, method `CONFIGURED_API_REFERENCE` and guarantee
  kind `SAME_HOST_SHARED_KERNEL_UTC`, with explicit guarantee-basis text.
- Gateway/source/stream/capture generation and exact gateway clock epoch.
- API-UTC validity start/end and finite ordered lower/upper offset bounds.

The bounds declare **API UTC minus gateway UTC**, uniformly over the stated
validity, including deployment clock uncertainty. This is an explicit shared-host
clock guarantee supplied by the operator. It is not a measured network offset,
proof of remote host synchronization or proof of absolute UTC accuracy.

Let raw gateway timestamp be `G`, read bracket span `s`, declared offset bounds
`[l,u]` and trusted API receive time `R`. The conservative capture API interval is:

```text
C = [G + l - s, G + u + s]
age = [R - C.upper, R - C.lower]
```

Datetime endpoints round outward to microseconds. The capture-specific assessment
carries `[l-s,u+s]`, binding the original body hash, gateway and clock epoch.
KNOWN requires the whole effective interval inside `[-2,+2]` seconds, the full
capture interval inside policy validity and receive time inside that validity.
The pure qualifier separately uses the original monotonic read bracket for dwell
and maximum-gap lower/upper bounds; it does not credit uncertain duration.

No policy, expiration, epoch mismatch, excessive uncertainty or validity crossing
produces UNKNOWN. No offset is inferred from delivery latency, local `now`, raw
SourceTimestamp or comparison of gateway/API timestamps. A gateway restart needs
an explicitly renewed policy for its new epoch. API restart may retain an
unexpired uniform deployment guarantee but must reset pending dwell through its
new consumer epoch. Changed registered generation/source requires explicit new
supported registration and corresponding configuration; automatic rebinding is
absent.

## Live, historical and unknown decisions

Already-fenced and older raw positions are historical. With trusted mapped time,
an entire age interval from zero up to but excluding 3 seconds proves freshness.
If even the youngest possible age is at least 3 seconds, the capture is delayed
history. Future or boundary-straddling intervals are UNKNOWN. History is classified
before applying missing-sequence or communication gates so delayed evidence does
not mutate the live cursor or qualification checkpoint.

A capture interval must lie wholly inside one installed configuration's time
window. Missing configuration, crossing a settings boundary, or equal-timestamp
configuration ambiguity produces explicit quarantine. An installed old version
can preserve historical evidence after a new head is installed. No current
configuration is substituted for unknown historical settings. Historical receipts
do not construct a separate past occurrence timeline or emit current outbox rows.

For a new forward capture, a skipped raw capture sequence records a durable UNKNOWN
break; that capture is preserved without qualifying its sample. The next contiguous
sample must begin fresh dwell. Missing/non-GOOD communication evidence also breaks
pending dwell even when all analog and phase samples are Good: frozen heartbeats
must not create or return an alarm. Schema 1.0/1.1, missing capture clock and
sample-less inputs use explicit UNSUPPORTED breaks, never fabricated samples or
normal returns. Historical unsupported evidence does not rewind the live cursor.

Source conflicts, reconciliation flags and original batch gaps conservatively
quarantine qualification and latch requires-restart. The collision ingestion hook
records a durable continuity break even when the conflicting raw batch is rejected.
Clearing a source flag alone cannot bridge old pending dwell: explicit reconciled
startup with a new consumer epoch is required. This is a conservative recovery
requirement, not automatic recovery completion. Existing active instances remain
latched throughout UNKNOWN/quarantine.

Temperature still requires the same capture's SP to equal immutable
`TemperatureRuleConfig.setpoint_c`. A job/recipe change from 150 to 160°C yields
`SETPOINT_VERSION_MISMATCH` UNKNOWN and resets pending dwell; it never quietly
changes the alarm config, creates a wrong occurrence or normally returns an active
alarm. Equipment-admin replacement with an explicit new setpoint/version closes
an existing instance as CONFIG_CHANGED and requires fresh dwell. Automatic
recipe-derived alarm configuration is outside this slice.

## Focused verification

```sh
.venv/bin/ruff check apps/api/alarm_consumer.py apps/api/migrations/versions/0011_alarm_capture_context.py tests/integration/test_alarm_consumer.py
.venv/bin/pytest -q tests/integration/test_alarm_consumer.py
```

The disposable PostgreSQL tests exercise the authenticated API positive path,
same-transaction rollback, original receipt replay, concurrency and exact FKs,
startup takeover/fencing and retired-epoch rejection, legacy and skipped captures,
frozen heartbeat and active preservation, delayed history checkpoint preservation,
changed recipe setpoint and rule reload, explicit clock assumptions and expiry,
read-bracket uncertainty, collision/reconciliation recovery latch, and migration
replay/refusal. They do not establish end-to-end latency, remote-clock verification,
notification delivery, acceptance T04–T06 completion, load or long-duration recovery.

## Source-wide mixed coordinator library: Unit B

`apps/api/alarm_coordinator.py` adds `AlarmCoordinator` as a trusted composition
boundary. It accepts either the existing validated `AlarmConsumerConfig` or the
explicit `MixedAlarmConsumerConfig`. Existing Sealer JSON can therefore retain
its exact configuration fingerprint while using the source-wide coordinator.
The subsequent Unit C public startup now uses this boundary for both legacy and
mixed JSON; [ALARM_RUN.md](ALARM_RUN.md) describes the explicit provisioning and
startup files. No consumer or runtime is enabled by default.

The mixed configuration has kind `MIXED_ALARM_RUNTIME`, one common line/source/
stream/gateway/generation, one optional existing declared clock policy, and one
to three channels `{asset_id,config}`. `StoredRuleConfig` and the target validator
allow Sealer temperature/pressure and logical Line `PLC_FAULT` only. Channel
identities and rule kinds cannot repeat. Wrong source policy, target, duplicates,
extra fields, processing contexts, positions and classifications are rejected.
Configuration JSON never supplies server-owned proof, activation fences, runtime
epochs, verified events or `ClockAssessment`.

The caller installs immutable rules explicitly and then starts the coordinator
inside its caller-owned PostgreSQL transaction before injecting it into
`IngestionService(engine, alarm_consumer=coordinator)`. Every new start, fresh
capture write and invalidate requires the exact explicit mixed feature revision
set (initially0014). The generic alarm-capture support set is insufficient.
See [MIXED_ALARM_SCHEMA.md](MIXED_ALARM_SCHEMA.md) for the older-binary barrier and
its downgrade boundary.

### Durable roster and activation

All coordinator methods actually acquire/re-enter the gateway and line/source
locks before querying **all** `alarm_capture_frontiers` rows for the source and
stream. The roster is not filtered by gateway/generation, requires_restart, alarm
presence or guessed liveness. Incoming startup cannot omit any persisted channel.
A legacy-shaped Sealer-only restart therefore cannot silently omit an already
activated Line fault channel, even if it currently has no alarm or needs restart.
There is no implicit channel retirement, deletion or automatic NORMAL return.

The complete sorted union of configured and persisted channels is locked before
any child store operation acquires a global input lock. Delegates may only
re-enter channels already owned by the coordinator; it does not fully process one
child before discovering and locking another child's channels. Fresh writes
require the complete roster and every channel's current coordinator epoch and
whole-config digest. A superseded subset writer cannot continue after another
startup expands the source roster.

Startup computes one accepted-capture fence under the source lock. Every channel
uses that same fence and server-selected consumer epoch, while processing
allocators remain per-channel. Exact installed heads and source binding are
checked. All frontier/RESTART/checkpoint/receipt changes commit or roll back
together; the coordinator never begins or commits a nested transaction. An
unchanged same-epoch start is idempotent; retired or unsafe epoch reuse is denied.

Invalidation checks complete ownership and breaks every channel conservatively,
including after a rule head changes. It does not require an obsolete runtime
configuration to match the new installed head before marking continuity unknown.
Fresh observations and startup still require exact installed configurations.
Source safety flags cannot be replaced by a caller-supplied `source` mapping.

### Original replay and new-channel history

A matching original capture decision is recovered before mutable rule, epoch,
roster ownership, source flags, clock, progress or restart checks can change its
meaning. Its channel/capture/hash identity is validated, its original context and
result are retained, replay flags are set and current notification is disabled.
An all-original replay allocates no new position and can be returned by an old
instance after a new epoch takes over. Different immutable body content conflicts.
Any missing decision requires all current write gates and ownership checks.

Raw duplicate intake still returns its original durable ACK without invoking the
consumer. Enabling Line does not backfill a previously accepted batch as live.
When a trusted direct coordinator call processes a previously missing channel,
its activation fence makes the older original historical/quarantined; the
existing channel's stored decision remains an exact replay. Historical Line
input preserves the entire current checkpoint, including event floor, latch,
last observation, candidate state and optional-field representation.

### Separate fault qualification and UNKNOWN ordering

The shared analog classifier retains the existing clock policy, interval rule
selection, age, source safety, raw gap and history semantics. Line qualification
remains in `apply_fault_capture`; it is not routed through the analog qualifier.
The ingestion hook still follows event application/progress and immutable raw
batch insertion, so the fault store can verify the exact original and derive its
Line state/fault proof from applied DB events below both Good Line.EventSeq and
the verified source frontier. No raw user-provided event list is accepted as proof.

A supported Line capture that is UNKNOWN still passes through fault qualification
when the source identity is usable. Communication/time UNKNOWN preserves a
verified Good EventSeq high-water floor while suppressing ACTIVE/NORMAL. Gap and
ambiguous freshness use the trusted optional reasons CAPTURE_SEQUENCE_GAP and
CAPTURE_FRESHNESS_UNKNOWN. A later smaller ceiling cannot reopen a resolved fault.
Line retains the actual valid clock assessment independently of a forced UNKNOWN
restriction, including overlapping freshness, interval and gap restrictions.
Existing analog UNKNOWN normalization retains its original clock-admission behavior.
Unsupported legacy captures do not manufacture proof; unsafe source identity
uses conservative continuity invalidation instead of bypassing store checks.

If no installed rule unambiguously governs the capture interval, the locked
current PLC_FAULT head may act only as the processing owner with
CONFIG_INTERVAL_UNKNOWN. The capture decision remains QUARANTINED with reason
NO_UNAMBIGUOUS_INSTALLED_CONFIG and no asserted governing `rule_version`.
`context.rule` identifies that owner, not a claim that its version governed the
capture. UNKNOWN and its original derived proof are durable; no false condition
or version is fabricated. Intake may continue conservatively. If the exact
installed owner is unavailable, processing fails and the caller rolls back.

New alarm identities are deterministic from the channel and original capture.
Original replay never regenerates source proof from later progress. A final
channel failure rolls back raw batch, event application/progress, both alarm
channels and the ACK boundary together. No notification is delivered and no PLC
command, reset or START is submitted by this library.

### Authenticated collision safety across replaced consumers

Ordinary `invalidate` retains complete current ownership checks. A separate
`invalidate_authenticated_collision` boundary is called only after authenticated
ingestion records the actual immutable-identity conflict and marks its source
conflicted. It verifies that persisted conflict ID against the locked registered
source and its actual conflict flag. A superseded instance can conservatively
break every current durable channel, including channels absent from its old
configuration, without becoming their owner or processing a capture.

The whole current source roster is locked before any input lock. Each break uses
the stored channel scope/rule and current frontier epoch, advances its allocator,
and sets `requires_restart`; installed-head changes do not suppress this safety
break. Conflict evidence, source flag and all channel breaks share the intake
transaction. Failure of the final break rolls all of them back. A stale generic
invalidation still refuses, and original duplicate ACKs remain read-only.

### Focused validation

```sh
.venv/bin/pytest -q tests/integration/test_alarm_coordinator.py \
  tests/integration/test_alarm_consumer.py tests/integration/test_alarm_runtime.py
```

Real PostgreSQL tests cover whole-source startup/omission, feature admission,
actual schema-lock races, channel-before-input ordering, original replay under
changed ownership/source flags, historical checkpoint preservation, supported
UNKNOWN floors, interval-owner quarantine and all-channel ingestion rollback.
These are library/database tests. Public mixed CLI composition has its separate
runtime24 verification; real OPC fault/RESET pipeline and screen-level T05
acceptance remain separate work.
