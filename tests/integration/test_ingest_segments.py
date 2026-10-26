"""Append-only exact identities, schema23 compatibility and schema24 role boundaries."""

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import DBAPIError

from apps.api.bootstrap import LINE_ID
from apps.api.database import migration_config
from apps.api.ingest_retention import IngestRetentionExpired, maybe_prune_ingest
from deploy.generate_neon_init import connect
from tests.integration.test_ingest_retention import (
    OLD,
    SCOPE,
    batch,
    finish_compaction,
    prune,
    rows,
    submit,
)
from tests.integration.test_ingest_retention import observation_service as observation_service
from tests.integration.test_ingest_retention_migration import (
    ROOT,
    catalog,
    compaction_snapshot,
    configure,
    http_batch,
    renderer,
    role_engine,
    upgrade_alembic,
)
from tests.integration.test_ingest_retention_migration import head17 as head17
from tests.integration.test_ingest_retention_migration import head18 as head18
from tests.integration.test_ingest_retention_migration import head19 as head19
from tests.integration.test_ingest_retention_migration import head20 as head20
from tests.integration.test_ingest_retention_migration import head21 as head21
from tests.integration.test_ingest_retention_migration import head22 as head22
from tests.integration.test_ingest_retention_migration import installed as installed
from tests.integration.test_kuls_runtime_grants import denied

HEAD23 = "0023_ingest_compaction"
HEAD24 = "0024_ingest_retired_segments"
UUID_TEXT = "abcdef01-2345-6789-abcd-0123456789ab"


@pytest.fixture
def head23(head22):
    url, migrator = head22
    upgrade_alembic(migrator, HEAD23)
    with connect(url) as db:
        db.execute((ROOT / "deploy/neon-ingest-compaction-grants.sql").read_text())
    return url, migrator


def lookup(engine, identity, gateway="gateway-1"):
    with engine.connect() as db:
        return db.execute(
            sa.text("SELECT retired_ingest_batch_exists(:gateway,:batch)"),
            {"gateway": gateway, "batch": identity},
        ).scalar_one()


def retire(service, *values):
    for value in values:
        submit(service, value)
    result = finish_compaction(service.engine, prune(service.engine))
    assert result["deleted_count"] == result["compacted_count"] == len(values)
    return result


def segment_snapshot(engine):
    return rows(
        engine,
        "SELECT segment_id,gateway_id,uuid_ids,text_ids,xmin::text,ctid::text "
        "FROM ingest_retired_batch_segments ORDER BY segment_id",
    )


def test_canonical_uuid_uses_sixteen_byte_storage_and_text_variants_stay_distinct(
    observation_service,
):
    service = observation_service
    retire(service, batch(batch_id=UUID_TEXT))
    stored = rows(
        service.engine,
        "SELECT uuid_ids,text_ids,pg_column_size(uuid_ids[1]) AS bytes "
        "FROM ingest_retired_batch_segments",
    )
    assert len(stored) == 1
    assert stored[0]["uuid_ids"] == [UUID(UUID_TEXT)]
    assert stored[0]["text_ids"] == []
    assert stored[0]["bytes"] == 16
    assert lookup(service.engine, UUID_TEXT)
    assert not lookup(service.engine, UUID_TEXT.upper())
    assert not lookup(service.engine, UUID_TEXT.replace("-", ""))
    assert not lookup(service.engine, UUID_TEXT, gateway="other-gateway")
    # PostgreSQL accepts all three as UUIDs, but the transport IDs are exact text.
    variants = (UUID_TEXT.upper(), UUID_TEXT.replace("-", ""), "legacy-batch")
    values = [
        batch(capture_seq=index, batch_id=identity)
        for index, identity in enumerate(variants, start=2)
    ]
    retire(service, *values)
    segments = segment_snapshot(service.engine)
    assert sorted(identity for row in segments for identity in row["text_ids"]) == sorted(variants)
    assert [identity for row in segments for identity in row["uuid_ids"]] == [UUID(UUID_TEXT)]
    for identity in (UUID_TEXT, *variants):
        assert lookup(service.engine, identity)
        # Fresh capture coordinates force expiry to depend on the exact batch ID.
        with pytest.raises(IngestRetentionExpired):
            submit(service, batch(capture_seq=100, batch_id=identity))
    assert rows(service.engine, "SELECT * FROM ingest_conflicts") == []


@pytest.mark.parametrize("spelling", ["uppercase", "hyphenless"])
def test_noncanonical_uuid_text_does_not_alias_canonical_uuid(observation_service, spelling):
    service = observation_service
    identity = UUID_TEXT.upper() if spelling == "uppercase" else UUID_TEXT.replace("-", "")
    retire(service, batch(batch_id=identity))
    assert lookup(service.engine, identity)
    assert not lookup(service.engine, UUID_TEXT)
    value = batch(capture_seq=2, batch_id=UUID_TEXT)
    ack = submit(service, value, datetime.now(UTC))
    assert submit(service, value) == ack


def test_later_round_appends_without_rewriting_earlier_segment_rows(observation_service):
    service = observation_service
    retire(service, batch(capture_seq=1), batch(capture_seq=3))
    before = segment_snapshot(service.engine)
    retire(service, batch(capture_seq=2), batch(capture_seq=4))
    after = segment_snapshot(service.engine)
    assert after[: len(before)] == before
    assert len(after) > len(before)
    assert all(1 <= len(row["uuid_ids"]) + len(row["text_ids"]) <= 256 for row in after)
    assert sorted(identity for row in after for identity in row["text_ids"]) == [
        "batch-1",
        "batch-2",
        "batch-3",
        "batch-4",
    ]
    coverage = rows(
        service.engine,
        "SELECT from_capture_seq,through_capture_seq,deleted_count FROM ingest_retention_ranges",
    )
    assert coverage == [{"from_capture_seq": 1, "through_capture_seq": 4, "deleted_count": 4}]
    assert rows(service.engine, "SELECT * FROM ingest_retired_batch_chunks") == []


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ingest_retired_batch_segments SET text_ids=ARRAY['forged']",
        "DELETE FROM ingest_retired_batch_segments",
        "TRUNCATE ingest_retired_batch_segments",
    ],
)
def test_written_segments_are_immutable_even_for_owner(observation_service, statement):
    service = observation_service
    retire(service, batch())
    before = segment_snapshot(service.engine)
    with pytest.raises(DBAPIError, match="append-only"), service.engine.begin() as db:
        db.execute(sa.text(statement))
    assert segment_snapshot(service.engine) == before
    assert lookup(service.engine, "batch-1")


def test_segment_insert_failure_rolls_back_entire_retirement(observation_service):
    service = observation_service
    submit(service, batch())
    before = rows(service.engine, "SELECT * FROM ingest_retention_state")
    with service.engine.begin() as db:
        db.execute(
            sa.text("""CREATE FUNCTION fail_segment_insert() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected segment failure'; END $$""")
        )
        db.execute(
            sa.text("""CREATE TRIGGER fail_segment_insert
            BEFORE INSERT ON ingest_retired_batch_segments FOR EACH ROW
            EXECUTE FUNCTION fail_segment_insert()""")
        )
    result = maybe_prune_ingest(service.engine)
    assert result["status"] == "failed"
    assert rows(service.engine, "SELECT * FROM ingest_retention_state") == before
    assert rows(service.engine, "SELECT * FROM ingest_retention_ranges") == []
    assert segment_snapshot(service.engine) == []
    assert len(rows(service.engine, "SELECT * FROM ingest_batches")) == 1
    assert len(rows(service.engine, "SELECT * FROM capture_clock_evidence")) == 1
    assert rows(service.engine, "SELECT expired_at FROM ingest_receipts") == [{"expired_at": None}]
    with service.engine.begin() as db:
        db.execute(sa.text("DROP TRIGGER fail_segment_insert ON ingest_retired_batch_segments"))
    assert (
        finish_compaction(service.engine, maybe_prune_ingest(service.engine))["compacted_count"]
        == 1
    )


def test_gateway_scope_preserves_independent_batch_and_capture_identities(observation_service):
    service = observation_service
    retire(service, batch(batch_id=UUID_TEXT))
    service.configure_source(
        gateway_id="gateway-2",
        source_id="source-2",
        stream_id="stream-2",
        line_id=LINE_ID,
        capture_generation=1,
        now=OLD,
    )
    value = batch(
        batch_id=UUID_TEXT, gateway_id="gateway-2", source_id="source-2", stream_id="stream-2"
    )
    scope = SCOPE | {
        "authenticated_gateway_id": "gateway-2",
        "authenticated_source_id": "source-2",
        "authenticated_stream_id": "stream-2",
    }
    ack = service.ingest(value, **scope, now=datetime.now(UTC))
    assert service.ingest(value, **scope, now=datetime.now(UTC)) == ack
    assert not lookup(service.engine, UUID_TEXT, gateway="gateway-2")


def test_additive_upgrade_and_unused_downgrade_preserve_legacy_ids_and_exact_catalog(head23):
    url, migrator = head23
    service = configure(migrator)
    retire(service, batch(batch_id=UUID_TEXT))
    live = batch(capture_seq=2)
    ack = submit(service, live, datetime.now(UTC))
    before_rows = compaction_snapshot(migrator)
    before_chunks = rows(
        migrator, "SELECT *,xmin::text,ctid::text FROM ingest_retired_batch_chunks"
    )
    before_catalog = catalog(url)
    upgrade_alembic(migrator, HEAD24)
    with connect(url) as db:
        db.execute((ROOT / "deploy/neon-ingest-retired-segments-grants.sql").read_text())
    canonical = catalog(url)
    assert compaction_snapshot(migrator) == before_rows
    assert (
        rows(migrator, "SELECT *,xmin::text,ctid::text FROM ingest_retired_batch_chunks")
        == before_chunks
    )
    assert segment_snapshot(migrator) == []
    assert lookup(migrator, UUID_TEXT)
    for statement in (
        "UPDATE ingest_retired_batch_chunks SET batch_ids=ARRAY['forged']",
        "DELETE FROM ingest_retired_batch_chunks",
        "TRUNCATE ingest_retired_batch_chunks",
    ):
        with pytest.raises(DBAPIError), migrator.begin() as db:
            db.execute(sa.text(statement))
    assert (
        rows(migrator, "SELECT *,xmin::text,ctid::text FROM ingest_retired_batch_chunks")
        == before_chunks
    )
    with pytest.raises(IngestRetentionExpired):
        submit(service, batch(capture_seq=100, batch_id=UUID_TEXT))
    with migrator.connect() as db:
        command.downgrade(migration_config(db), HEAD23)
    assert catalog(url) == before_catalog
    runtime = role_engine(url, "hmi_app")
    try:
        with runtime.connect() as db:
            assert (
                db.execute(
                    sa.text("""SELECT to_regprocedure(
                'public.retired_ingest_batch_exists(text,text)')""")
                ).scalar()
                is None
            )
            assert db.execute(
                sa.text("""SELECT has_function_privilege(current_user,
                'public.prune_ingest_batches_hours(integer,integer,integer)','EXECUTE')""")
            ).scalar_one()
            assert (
                db.execute(sa.text("SELECT count(*) FROM ingest_retired_batch_chunks")).scalar()
                == 1
            )
    finally:
        runtime.dispose()
    assert compaction_snapshot(migrator) == before_rows
    assert (
        rows(migrator, "SELECT *,xmin::text,ctid::text FROM ingest_retired_batch_chunks")
        == before_chunks
    )
    assert submit(service, live) == ack
    # The HTTP path must recreate the same catalog and ACLs as Alembic + SQL grants.
    upgrade = json.loads((ROOT / "deploy/neon-http/upgrade-0023-0024.json").read_text())
    grants = json.loads((ROOT / "deploy/neon-http/grants-0024.json").read_text())
    assert renderer.retention_segments_statements() == upgrade
    assert renderer.retention_segments_grant_statements() == grants
    http_batch(url, upgrade)
    http_batch(url, grants)
    assert catalog(url) == canonical
    assert compaction_snapshot(migrator) == before_rows
    assert (
        rows(migrator, "SELECT *,xmin::text,ctid::text FROM ingest_retired_batch_chunks")
        == before_chunks
    )
    assert lookup(migrator, UUID_TEXT)
    retire(service, batch(capture_seq=3, batch_id="new-segment-id"))
    assert (
        rows(migrator, "SELECT *,xmin::text,ctid::text FROM ingest_retired_batch_chunks")
        == before_chunks
    )
    assert lookup(migrator, UUID_TEXT)
    assert lookup(migrator, "new-segment-id")
    assert not lookup(migrator, UUID_TEXT.upper())


def test_nonempty_segments_refuse_lossy_downgrade_without_changes(observation_service):
    service = observation_service
    retire(service, batch())
    before_rows = compaction_snapshot(service.engine)
    before_segments = segment_snapshot(service.engine)
    with pytest.raises((RuntimeError, DBAPIError)), service.engine.connect() as db:
        command.downgrade(migration_config(db), HEAD23)
    assert compaction_snapshot(service.engine) == before_rows
    assert segment_snapshot(service.engine) == before_segments
    assert lookup(service.engine, "batch-1")
    with service.engine.connect() as db:
        assert db.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() == HEAD24


def test_runtime_can_lookup_and_prune_but_cannot_write_segments_or_invoke_compactor(head23):
    url, migrator = head23
    upgrade_alembic(migrator, HEAD24)
    with connect(url) as db:
        db.execute((ROOT / "deploy/neon-ingest-retired-segments-grants.sql").read_text())
    runtime = role_engine(url, "hmi_app")
    try:
        service = configure(runtime)
        submit(service, batch(batch_id=UUID_TEXT))
        for statement in (
            "INSERT INTO ingest_retired_batch_segments(gateway_id,text_ids) "
            "VALUES('gateway-1',ARRAY['forged'])",
            "UPDATE ingest_retired_batch_segments SET text_ids=ARRAY['forged']",
            "DELETE FROM ingest_retired_batch_segments",
            "TRUNCATE ingest_retired_batch_segments",
            "SELECT nextval('ingest_retired_batch_segments_segment_id_seq')",
            "SELECT compact_ingest_receipts('gateway-1',ARRAY['batch-1'],NULL)",
            "SELECT compact_ingest_receipt('gateway-1','batch-1')",
        ):
            denied(runtime, statement)
        first = maybe_prune_ingest(runtime, retention_hours=3)
        assert first["status"] != "failed"
        result = finish_compaction(migrator, first)
        assert result["deleted_count"] == result["compacted_count"] == 1
        assert lookup(runtime, UUID_TEXT)
        with runtime.connect() as db:
            assert (
                db.execute(sa.text("SELECT count(*) FROM ingest_retired_batch_segments")).scalar()
                == 1
            )
        assert rows(migrator, "SELECT * FROM ingest_retired_batch_chunks") == []
    finally:
        runtime.dispose()


@pytest.mark.parametrize(
    "array_sql",
    [
        "NULL::text[]",
        "ARRAY[NULL]::text[]",
        "ARRAY(SELECT 'id-'||n FROM generate_series(1,257) n)",
    ],
)
def test_batch_compactor_rejects_invalid_bounds_without_mutation(observation_service, array_sql):
    service = observation_service
    submit(service, batch())
    before = rows(service.engine, "SELECT * FROM ingest_receipts")
    with (
        pytest.raises(DBAPIError, match="Invalid bounded compaction parameters"),
        service.engine.begin() as db,
    ):
        db.execute(sa.text("SELECT compact_ingest_receipts('gateway-1'," + array_sql + ",NULL)"))
    assert rows(service.engine, "SELECT * FROM ingest_receipts") == before
    assert segment_snapshot(service.engine) == []


def test_past_compaction_deadline_preserves_receipt_until_later_bounded_attempt(
    observation_service,
):
    service = observation_service
    submit(service, batch())
    with service.engine.begin() as db:
        db.execute(
            sa.text("""CREATE TABLE segment_receipt_reference (
            gateway_id text,batch_id text,
            FOREIGN KEY(gateway_id,batch_id) REFERENCES ingest_receipts(gateway_id,batch_id))""")
        )
        db.execute(sa.text("INSERT INTO segment_receipt_reference VALUES('gateway-1','batch-1')"))
    result = prune(service.engine)
    assert result["deleted_count"] == 1
    assert result["compacted_count"] == 0
    with service.engine.begin() as db:
        db.execute(sa.text("DELETE FROM segment_receipt_reference"))
    before = rows(service.engine, "SELECT * FROM ingest_receipts")
    with service.engine.begin() as db:
        count = db.execute(
            sa.text("""SELECT compact_ingest_receipts(
            'gateway-1',ARRAY['batch-1'],clock_timestamp()-interval '1 second')""")
        ).scalar_one()
    assert count == 0
    assert rows(service.engine, "SELECT * FROM ingest_receipts") == before
    assert segment_snapshot(service.engine) == []
    result = finish_compaction(service.engine, prune(service.engine))
    assert result["deleted_count"] == 0
    assert result["compacted_count"] == 1
    assert lookup(service.engine, "batch-1")
