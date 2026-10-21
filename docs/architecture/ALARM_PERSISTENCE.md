# Alarm repository boundary

DEV-14's PostgreSQL repository is a bounded persistence unit. It uses the existing
pure lifecycle and analog qualification models. It does not subscribe to ingestion,
allocate capture order, assess clock trust, serve HTTP, render UI, or deliver alerts.
PLC faults, communications alarms and command-result alarms remain separate work.

## API and ownership

`apps/api/alarm_store.py` accepts a caller-owned SQLAlchemy `Connection` in an
active PostgreSQL transaction at revision `0010_alarm_store`. It never commits,
rolls back, opens a connection, reads a clock, or calls external services.

| Function | Caller supplies | Result |
| --- | --- | --- |
| `install_rule` | Scope, immutable temperature/pressure config, input ID, equipment-admin principal, UTC time | Initial rule/version plus receipt |
| `replace_rule` | Same plus expected current version | New immutable config; closes open instance as `CONFIG_CHANGED` |
| `apply_condition` | Trusted qualified condition and rule ID | Atomic lifecycle delta and receipt |
| `apply_observation` | Trusted normalized `RuleObservation`, rule ID, optional occurrence identity | Qualification checkpoint plus lifecycle delta |
| `break_continuity` | Explicit gap, restart or unsupported-capture identity, durable position and evidence reference | Resets pending dwell without inventing sample/return |
| `act_on_alarm` | Exact instance ID, authenticated principal, UTC time, ACK or note intent | Independent ACK or append-only note revision |
| `read_actions` | Exact scope/rule/instance, limit and optional prior action | Bounded append-only audit/note page |
| `read_instances` | Scope/rule, limit 1–200 and optional exact prior-page instance | Bounded keyset page ordered by occurrence time and identity |

Principals are server-owned. A future HTTP adapter must use
`UserAuth.authorized_transaction(permission='alarm_action')` for ACK/notes or
`equipment_manage` for settings, with its existing session, CSRF and Origin checks.
The repository stamps the principal's login; actor fields are not accepted from
client JSON. Role checks also run before replay lookup. These checks do not replace
live authentication: the caller must retain the auth locks through commit.

`apply_observation` is an internal trusted boundary. Its phase, durable position,
clock epoch/time assessment and historical classification must come from the
server's capture adapter. It must not be exposed as a raw HTTP body. The existing
`alarm_capture.normalize_alarm_capture` can produce the observation from a schema
1.2 capture and trusted immutable context. The ingestion integration that allocates
that context remains unimplemented. That future integration must check the durable
receipt using original input identity/content **before** regenerating clock or
classification context. The current repository does not claim raw-batch retry
handling; it guarantees exact retry of its normalized input.

## Storage and transaction order

Migration 0010 adds these tables without legacy backfill:

| Table | Durable purpose |
| --- | --- |
| `alarm_channels` | Scope/rule head, current version, live position/time, one bounded qualification checkpoint |
| `alarm_rule_versions` | Immutable full versioned configuration, actor, change identity/time |
| `alarm_instances` | One row per occurrence; immutable origin evidence and independent return/ACK fields in its payload |
| `alarm_actions` | Append-only exact-instance ACK, settings closure and note before/after revisions |
| `alarm_receipts` | Globally unique input identity, canonical SHA-256, original intent and original result |
| `alarm_outbox` | Exactly one creation record per new current occurrence; no delivery state yet |

The channel has composite foreign keys to the actual source/stream/line and the
line's Sealer asset. Instances reference the installed channel/version. Actions
and outbox reference the exact channel/instance pair. A partial unique index allows
one open occurrence per channel. Global instance and input primary keys prevent
reuse of omitted history. Immutable tables reject UPDATE and DELETE through
append-only triggers. Used alarm state refuses downgrade; an empty schema can be
downgraded and upgraded again.

Lock order is schema shared advisory lock, existing caller authorization locks,
then sorted channel/input advisory locks, then channel row lock. Configuration,
qualification, instance deltas, actions, original receipt and outbox insertion all
commit together. A conflicting immutable input identity fails. A byte-equivalent
canonical intent returns its stored result with `replayed=True` and
`notify_current=False`. The original created occurrence ID remains visible.

The repository hydrates only the open instance, an exact action target if needed,
the latest requested note revision, and the rule versions those instances need.
It persists changed instances and new actions rather than rewriting lifetime
history. The bounded qualification checkpoint retains one observation (including
its fixed-size tag proof), latched condition, candidate and clock continuity.
There is no growing `AlarmState` JSON document rewritten at 1 Hz.

## Continuity and history

Epoch changes reset pending dwell through the pure qualifier while preserving the
latched active condition. UNKNOWN never fabricates a normal return. Unsupported
legacy captures must call `break_continuity(reason='UNSUPPORTED_CAPTURE')` before
the consumer advances; silently skipping them could bridge a dwell interval.
A known gap or restart can use the same operation with its explicit reason.

Historical inputs must reference an immutable rule version installed for their exact
channel. Missing historical configuration raises `AlarmConflict` without progressing
the checkpoint; a future consumer must quarantine it. Installed old versions remain
valid historical evidence after replacement.

Historical or older-position inputs keep their original evidence in receipts,
leave the live cursor/checkpoint unchanged and never create a current outbox row.
This unit does not reconstruct a separate historical occurrence timeline or
perform the final recovery reconciliation that decides which restored alarms
remain current. Those are future ingestion/recovery responsibilities.

## Verification

Run:

```sh
.venv/bin/ruff check apps/api/alarm_store.py apps/api/migrations/versions/0010_alarm_store.py tests/integration/test_alarm_store.py
.venv/bin/pytest -q tests/integration/test_alarm_store.py
```

The integration suite uses the project's disposable authenticated PostgreSQL 17
fixture. It covers migration replay and used-state downgrade refusal, exact retry
and changed-input conflict, scope foreign keys, recurrence and ACK of an older
instance, UNKNOWN/history preservation, settings closure, note revisions,
qualification epoch/unsupported/gap resets, duplicate and revision contention,
forced commit failure rollback and one outbox row per current occurrence.
