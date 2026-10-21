# Gateway receipt and screen synchronization v1.0 (DEV-02d)

`transport.py` defines bounded, immutable Pydantic DTOs and pure binding checks.
It imports the existing event, UTC, identifier and lossless UInt64 types and the
checked-in 48-tag dictionary. `transport-*.schema.json` are Draft 2020-12 wire
schemas; matching examples include applied, quarantined, gap and screen resync
messages. Schema validation is necessary but not sufficient: Python validators
also enforce dictionary types, digests, gap intervals and cursor relationships.
This contract implements no HTTP/WebSocket endpoint, queue, DB transaction,
authorization or actual persistence.

## Immutable capture batch

`GatewayBatch` contains `body: BatchBody` and `content_sha256`. The body includes
schema version, batch/gateway/source/stream identities, capture generation and
sequence, capture time, samples, events and explicit missing intervals. It must
contain at least one sample, event or gap. Bounds are 48 samples (one per tag),
1000 events and 100 gap records per batch. Each gap lists at most 1000 affected
executions. Event IDs and sample IDs are unique within a batch. Events must have
the body's source/stream and strictly increasing sequences; gaps are ordered,
nonoverlapping inclusive ranges and cannot cover an included event. A batch may
contain out-of-order historical work relative to other batches. It need not
cover every intervening event sequence; completeness comes from stream progress.

The gateway assigns and commits each `sample_id` and `batch_id` before sending.
Retries preserve the entire original body and digest. Changing a batch ID also
changes the digest. A generation/sequence pair orders captures independently of
source timestamps or source event sequences. Maintain monotonically increasing
`capture_seq` within the durable gateway generation; ordinary restart preserves
both. Explicit capture-state replacement requires a strictly higher registered
generation and reconciliation. Never wrap either UInt64. API authorization must
bind the gateway to its source and accepted capture generation; an arbitrary
client-supplied higher generation cannot authorize takeover. Compare generation
and capture sequence before updating latest projections, so a delayed old batch
cannot rewind current values. Neither generation nor `counter_epoch` resets the
source event stream.

### Typed observations

Each sample has its immutable ID, dictionary `tag_id`, nullable original
`source_timestamp` and `server_timestamp`, mandatory `acquired_at`, raw UInt32
OPC `status_code`, and a `reading` discriminated by OPC `data_type`. `acquired_at`
is the gateway observation time (`observed_at` in the DB design); API storage
time is assigned on commit rather than supplied by the gateway. Preserve missing
OPC timestamps as null. Source/server times may precede, exceed or disagree with
observation time because clock problems are data-quality evidence, so validation
does not invent chronological order across those clocks.

All six dictionary data types have strict values. UInt64 values and capture/
event/revision numbers use canonical decimal JSON strings, preserving values
above JavaScript's safe integer range. Booleans cannot become numbers; numeric
strings cannot become Double values; NaN and infinity are invalid. String tag
values may be empty (for example no active execution), with at most 128
characters. This is distinct from an ID field, which must be nonempty and contain
no whitespace. Bad/Uncertain readings may preserve a typed value or null. Good
status (top two severity bits zero) requires a value. An unavailable or bad value
is never replaced with zero or a fabricated normal status. Quality/freshness
calculation remains application work. Unknown tags, wrong OPC types and unknown
PLC enum values fail this typed v1 contract; diagnostic/raw contract violations
need explicit error handling rather than silently coercing the value.

### Digest definition

`batch_digest(body)` is SHA-256 of UTF-8 `canonical_batch_body(body)`. First fully
validate/normalize the body through `BatchBody`: timestamps become UTC, defaults
become explicit, engineering numbers become floats, and UInt64 becomes canonical
decimal strings on serialization. Then serialize using Python
`json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
ensure_ascii=False)`. **Every body field, including batch ID and schema version,
is hashed.** Only the outer `content_sha256` is excluded. Object key order and
equivalent UTC offsets do not change the hash. Array order is significant and
must remain unchanged on retry. This is this Python gateway/API's specified
encoding, not an assertion of RFC 8785 interoperability with arbitrary encoders.
The hash detects content mismatches; service authentication establishes trust.

## Durable ACK and distinct progress

Only return `DurableAck` after one successful DB commit has preserved the full
batch's samples, raw event inbox, gap metadata, per-event results, receipt and
appropriate progress. Its only durability value is `COMMITTED`, with a mandatory
UTC `committed_at`. A pending transaction, invalid envelope, hash conflict or
failed authorization receives no success ACK. The DTO can check claims and
relationships but **cannot prove a DB commit happened**; crash/transaction tests
must establish that later.

Each included event has exactly one receipt: `APPLIED` (including an identical
event already applied) or `QUARANTINED`, with a required reason for quarantine.
Unknown execution IDs can be durably quarantined without attributing production
to the currently selected job. A committed quarantine receipt permits the
gateway to release its copy because the API owns the durable inbox/retry duty;
it does not mean production was applied or the run was settled.

`recorded_gaps` acknowledges the exact gap metadata from this immutable batch.
`progress.unresolved_gaps` describes missing intervals that remain open at the
receipt's progress snapshot. They are separate because a delayed gap report may
arrive after backfill has already resolved that interval. Persisting an old gap
report must not reopen repaired history or rewind progress. Resolving an
irrecoverable gap requires the explicit, durable reconciliation evidence and
report history required by [PROTOCOL.md](PROTOCOL.md); receipt alone cannot
resolve data loss.

Progress identifies source/stream and separates `verified_through_seq` (every
preceding event validated and transactionally applied) from
`latest_ingested_seq` (newer source position durably recorded/received). Verified
cannot exceed ingestion or cross an unresolved missing interval or a quarantined
event. Received events cannot also be missing. Higher received events, durable
quarantine or a recorded permanent gap do not advance the verified frontier.
Persist irrecoverable interval/source/affected-execution evidence before
continuing latest ingestion beyond lost history. API locking and stored history
must verify relationships with **other** batches, including earlier quarantine;
these DTOs alone cannot infer the entire source history.

Before releasing a buffered batch, call `validate_ack(batch, ack)`. It binds the
exact batch ID, gateway ID and content digest, source/stream, event ID/sequence
set and submitted gap metadata. Missing/wrong ACKs retain the original buffer
for retry. `(gateway_id,batch_id)` and the generation/sequence capture identity
must be durable unique keys. Same ID/hash returns its stored committed receipt;
same ID/different hash is a conflict, never an overwrite. An old receipt's
progress snapshot is not permission to rewind a more recent stored frontier.
Per-event deduplication and application/frontier updates must still be atomic,
as defined in [DATA_AUTH.md](../docs/architecture/DATA_AUTH.md).

## Screen protocol

`ScreenMessage` discriminates `SNAPSHOT`, `UPDATE` and `RESYNC_REQUIRED`. A snapshot
starts a subscription synchronization session with a fresh opaque `sync_id`,
current UInt64 revision, emission time and typed `LineView`. Updates carry that
same sync ID, `base_revision` and exactly `base_revision + 1`. They replace the
complete v1 `LineView`; no ambiguous partial dictionary patch is used. The view
contains line identity, physical/display state, original active execution ID,
control restriction and up to 48 current typed samples. UNKNOWN requires control
restriction. Duplicate current tag samples are invalid. This is the initial
small screen DTO; alarms, command tracking and MES panels need separately
versioned typed payload fields when implemented.

Call `validate_screen_update(previous, update)` before applying an update. It
requires the same sync ID and line, and base revision equal to the previous
snapshot/update revision. A skipped, duplicate, reordered, different-line or old
session message cannot be applied. Stop incremental application and obtain a
fresh snapshot whenever that check fails. Source event sequence, capture order
and screen revision serve different purposes and cannot substitute for each
other. Clients retain UInt64 revision strings or BigInt, never Number.

Every reconnect obtains a fresh authorized snapshot and sync ID before updates;
v1 has no cross-connection replay shortcut. The future server must make initial
snapshot/subscription attachment atomic to avoid a race between snapshot and
first update. Use a queue bounded to 32 outbound messages per subscription;
never build an unlimited backlog or silently drop a revision. On overflow,
invalidate the session and send `RESYNC_REQUIRED: BACKPRESSURE` if delivery is
still possible, otherwise close it. Gap, reconnect and server-restart reasons
also require a new snapshot. DTO cardinalities/string bounds keep an individual
message bounded; server frame/request limits and queue enforcement remain
runtime work. WebSocket messages are current-screen projections and never
replace durable ingest ACKs, commands or event settlement.

Authenticate the user and verify the requested line scope before snapshot or
subscription, and stop delivery on expiry or permission revocation. Service
tokens, session cookies, passwords and private keys have no payload fields here;
never place them in tag values, IDs or error text. Frontend control still needs
API authorization and PLC guards regardless of displayed `control_restricted`.

## Local validation and remaining work

Run `.venv/bin/python -m pytest tests/contract/test_transport.py`. Tests check
schema/example agreement, malformed captures, dictionary/quality/precision,
immutable digest identity, ACK binding/quarantine/gaps, repaired historical gap
reports and adjacent screen revisions. These are contract tests, not evidence of
real transaction durability, queue behavior, service/user authentication,
WebSocket delivery or recovery. DEV-09~12/13 still need those implementations,
commit-boundary crash tests, capture ownership enforcement and actual screen
snapshot/subscription synchronization.


## Capture clock evidence (schema 1.2)

Sample batches in 1.2 require `communication` and `capture_time`. The latter stores
`clock_epoch`, `monotonic_seconds`, and `utc_read_span_seconds`. Immediately after
the complete OPC read, the collector reads monotonic time, UTC, then monotonic time.
The final monotonic reading is the capture coordinate; their difference is the UTC
read span. Thus `captured_at` was read inside `[monotonic_seconds -
utc_read_span_seconds, monotonic_seconds]`, before subsequent event queries.
All samples keep that exact UTC acquisition time and original OPC timestamps.

The collector creates a fresh clock epoch on initialization or detected monotonic
regression. Regression discards prior read/heartbeat freshness, fails that capture,
and cannot restore GOOD health from clamped ages. Durable capture generation and
sequence remain independent and do not reset at ordinary collector restart.

These are raw coordinates, not a KNOWN UTC-quality or synchronization assertion.
Clock continuity, the project two-second warning policy, same-capture phase
normalization, historical replay classification and alarm qualification remain
subsequent adapter work. An explicitly configured deployment UTC reference or an
authenticated, bounded API reference probe may establish relative clock quality;
no new NTP requirement is introduced. Network latency or a stationary analog
SourceTimestamp alone does not establish clock skew.

Schemas 1.0/1.1 forbid `capture_time` even when null and omit it on serialization,
preserving existing canonical bodies and digests. Version 1.2 evidence is included
in the immutable body/digest and survives buffer reopen/retry. Eviction retains it
in loss metadata; older loss records without it remain readable. No DB migration
is required because the existing durable queue/API batch body stores full JSON.
