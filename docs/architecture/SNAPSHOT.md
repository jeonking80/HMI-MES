# Durable snapshot read service

`apps.api.snapshot.read_snapshot(connection, *, line_id, source_id, stream_id, now)`
reads inside the caller's authorized SQLAlchemy transaction. It neither opens a
connection nor commits. A missing source/stream/line binding raises
`SnapshotNotFound`. HTTP integration must authenticate and authorize before calling.

The result embeds the existing `ScreenSnapshot` contract as `screen`. The existing
screen contract cannot express never-observed tags, storage evidence or health;
the service response adds `missing_tag_ids`, per-tag `evidence` and `health` without
changing that transport contract. Serialize with `model_dump(mode="json")` to retain
canonical decimal UInt64 strings, including tag values and capture positions.

Each enabled line tag is selected from durable `ingest_batches.body.samples` by
descending capture generation and capture sequence, with the currently configured
source/stream/gateway/generation binding. Arrival order and SourceTimestamp do not
choose the current value. Partial batches retain each tag's latest observation;
an older replay cannot replace a newer capture. Generation rotation excludes old
generation observations until the new generation observes them. Raw null, Bad,
Uncertain, source/server timestamps and original acquired time remain intact.
Never-observed tags are listed explicitly and have no fabricated samples or zeros.

Per-tag freshness requires capture, acquisition and durable storage ages all within
the dictionary's 3000 ms limit. Future clock evidence fails closed. A newly stored
old capture remains stale. Derived state/execution use only fresh Good observations;
the original samples remain visible when these derived values become unknown.

Health distinguishes NEVER_OBSERVED, STALE, DEGRADED and CAPTURE_FRESH. The last
means only that every enabled tag has a recent Good stored observation. It does
not itself assert communication or advancing heartbeat. Version 1.1 batches also
carry validated gateway monotonic health. The newest sample capture supplies this
evidence; a newer legacy capture cannot borrow older GOOD evidence. Health ages by
the greater of capture age and durable receipt age, plus the gateway monotonic ages.
Future evidence stays UNKNOWN, and a forward gateway clock cannot resurrect old
stored evidence when wall time catches up. Delay is 3 seconds and alarm is 5 seconds.
UNKNOWN/ALARM communication masks the derived line state; raw observations remain.
Legacy 1.0 has UNKNOWN heartbeat. Control remains restricted pending the separate
command authorization/preflight integration. Source conflicts/reconciliation also mark
observed data degraded. This read endpoint provides no control authority.

Every response starts a new opaque screen sync ID at revision `"0"`; capture/event
positions are independent of subscription revisions. WebSocket updates and resume
continuity are outside this read service.

Run the real disposable PostgreSQL checks with:

```sh
uv run pytest -q tests/integration/test_snapshot.py
```

These tests cover committed ingestion, delayed replay/source clock ordering,
source isolation, missing observations, Bad/null, maximum UInt64 wire precision,
old/future captures, generation rotation and caller transaction ownership. They do
not establish end-to-end OPC or browser subscription completion.
