"""Fast projections preserve the full-history snapshot and clock semantics."""

from datetime import timedelta

import pytest
from alembic import command
from sqlalchemy import event, select
from test_alarm_consumer import AT, capture
from test_reports import ingest
from test_reports import state as report_state

from apps.api.bootstrap import LINE_ID
from apps.api.database import migration_config
from apps.api.ingestion import ingest_batches
from apps.api.reports import read_execution_timeline
from apps.api.snapshot import read_snapshot
from contracts.models import EVENT_ADAPTER
from contracts.transport import GatewayBatch, batch_digest

SCOPE = dict(line_id=LINE_ID, source_id="source", stream_id="stream")


@pytest.fixture
def state(migrated):
    return report_state.__wrapped__(migrated)


def envelope(body):
    return GatewayBatch(body=body, content_sha256=batch_digest(body))


def snapshot_value(db, *, now, fast):
    value = read_snapshot(db, **SCOPE, now=now, prefer_complete_capture=fast).model_dump()
    value["screen"].pop("sync_id")
    return value


@pytest.mark.parametrize(
    "shape", ["full", "partial", "digital", "events_only", "stale_bad", "old_generation"]
)
def test_snapshot_complete_capture_opt_in_matches_history(state, shape):
    ingest(state, capture(1, seconds=0))
    body = capture(2, seconds=1).body
    if shape in {"partial", "digital"}:
        tag = "Sealer.TemperaturePV" if shape == "partial" else "Line.State"
        body = body.model_copy(
            update={"samples": tuple(s for s in body.samples if s.tag_id == tag)}
        )
    elif shape == "events_only":
        body = body.model_copy(
            update={
                "samples": (),
                "events": (
                    EVENT_ADAPTER.validate_python(
                        dict(
                            kind="PRODUCTION_GOOD",
                            event_id="event-only",
                            seq=1,
                            equipment_id="Outfeed",
                            execution_id="run",
                            source_id="source",
                            stream_id="stream",
                            occurred_at=AT + timedelta(seconds=1),
                            payload=dict(quantity=1, counter_epoch="epoch"),
                        )
                    ),
                ),
            }
        )
    elif shape == "stale_bad":
        samples = tuple(s.model_copy(update={"status_code": 0x80000000}) for s in body.samples)
        body = body.model_copy(update={"samples": samples})
    ingest(state, envelope(body))
    with state.engine.begin() as db:
        if shape == "old_generation":
            row = dict(db.execute(select(ingest_batches).limit(1)).mappings().one())
            foreign = capture(3, seconds=2).body.model_copy(update={"capture_generation": 2})
            row.update(
                batch_id=foreign.batch_id,
                capture_generation=2,
                capture_seq=3,
                body=foreign.model_dump(mode="json"),
                content_sha256=batch_digest(foreign),
                stored_at=AT + timedelta(seconds=2),
            )
            db.execute(ingest_batches.insert().values(**row))
        now = AT + timedelta(seconds=10 if shape == "stale_bad" else 2)
        statements = []
        event.listen(
            db, "before_cursor_execute", lambda c, cur, sql, p, ctx, many: statements.append(sql)
        )
        fast = snapshot_value(db, now=now, fast=True)
        fast_queries = list(statements)
        original = snapshot_value(db, now=now, fast=False)
    assert fast == original
    fallback_queries = [sql for sql in fast_queries if "FROM unnest(" in sql]
    assert not any("DISTINCT ON" in sql for sql in fast_queries)
    if shape in {"full", "events_only", "stale_bad", "old_generation"}:
        assert not fallback_queries
    else:
        assert len(fallback_queries) == 1
        query = fallback_queries[0]
        assert "body @> jsonb_build_object(" in query
        assert query.index("LIMIT 1") < query.index("jsonb_array_elements")


def test_legacy_projection_keeps_nonempty_nonreport_samples(state):
    # Actual head17 has no durable canonical proof. A sampled link failure must
    # remain sampled, even when none of its tags is used by the report itself.
    with state.engine.connect() as db:
        command.downgrade(migration_config(db), "0017_guest_viewer")
    body = capture(1, seconds=0, communication_age=5).body
    body = body.model_copy(
        update={"samples": tuple(s for s in body.samples if s.tag_id == "Sealer.State")}
    )
    ingest(state, envelope(body))
    with state.engine.begin() as db:
        report = read_execution_timeline(db, "run", **SCOPE, now=AT + timedelta(seconds=2))
    assert report["durations_seconds"]["running"] == 0
    assert all(s["class"] == "UNKNOWN" for s in report["segments"])
    assert all(s.get("unknown_time_basis") != "API_RECEIPT_FENCE" for s in report["segments"])
