"""Actual migrator/runtime roles and rendered HTTP migration on disposable PG17."""

import json
import secrets
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import DBAPIError

from apps.api.auth import UserAuth
from apps.api.bootstrap import LINE_ID
from apps.api.database import migration_config
from apps.api.ingestion import IngestionService
from apps.api.registration_ids import register_recipe_request
from apps.api.registration_requests import RecipeRegistrationRequest
from deploy.generate_neon_init import connect
from tests.integration.item_seed import seed_item
from tests.integration.test_clock_runtime_grants import head17 as head17
from tests.integration.test_clock_runtime_grants import head18 as head18
from tests.integration.test_ingest_retention import OLD, batch, submit
from tests.integration.test_item_migration import head20 as head20
from tests.integration.test_kuls_runtime_grants import denied, role_engine
from tests.integration.test_kuls_runtime_grants import installed as installed
from tests.integration.test_neon_clock_http_migration import catalog, renderer, upgrade_alembic
from tests.integration.test_neon_http_migration import http_batch
from tests.integration.test_registration_runtime_grants import head19 as head19
from tests.integration.test_work_orders import recipe

ROOT = Path(__file__).resolve().parents[2]
HEAD = "0022_ingest_retention"


@pytest.fixture
def head21(head20):
    url, migrator = head20
    upgrade_alembic(migrator, "0021_item_master")
    with connect(url) as db:
        db.execute((ROOT / "deploy/neon-item-grants.sql").read_text())
    return url, migrator


def configure(engine):
    service = IngestionService(engine)
    service.configure_source(
        gateway_id="gateway-1",
        source_id="simulator-1",
        stream_id="stream-1",
        line_id=LINE_ID,
        capture_generation=1,
        now=OLD,
    )
    return service


def prune(engine):
    """Schema22 uses its frozen day-based function; application maintenance is 23-only."""
    with engine.begin() as db:
        return db.execute(sa.text("SELECT prune_ingest_batches(7,200,400)")).scalar_one()


def snapshot(engine):
    with engine.connect() as db:
        return tuple(
            db.execute(sa.text(query)).all()
            for query in (
                "SELECT * FROM ingest_batches ORDER BY gateway_id,batch_id",
                "SELECT * FROM capture_clock_evidence ORDER BY gateway_id,batch_id",
                "SELECT * FROM registration_receipts ORDER BY kind,request_id",
                "SELECT * FROM registration_id_counters ORDER BY kind,namespace",
                "SELECT * FROM mes_management_audit ORDER BY id",
            )
        )


def test_http_catalog_and_grants_equal_alembic_and_untouched_downgrade(head21):
    url, migrator = head21
    original = catalog(url)
    upgrade_alembic(migrator, HEAD)
    canonical = catalog(url)
    with migrator.connect() as db:
        command.downgrade(migration_config(db), "0021_item_master")
    assert catalog(url) == original
    upgrade = json.loads((ROOT / "deploy/neon-http/upgrade-0021-0022.json").read_text())
    grants = json.loads((ROOT / "deploy/neon-http/grants-0022.json").read_text())
    assert renderer.retention_statements() == upgrade
    assert renderer.retention_grant_statements() == grants
    http_batch(url, upgrade)
    assert catalog(url) == canonical
    http_batch(url, grants)
    granted = catalog(url)
    with connect(url) as db:
        db.execute((ROOT / "deploy/neon-ingest-retention-grants.sql").read_text())
    assert catalog(url) == granted


def test_existing_raw_and_receipts_counters_survive_upgrade_without_deletion(head21):
    _, migrator = head21
    service = configure(migrator)
    value = batch()
    ack = submit(service, value)
    UserAuth(migrator, allowed_origins={"https://retention.test"}).bootstrap_user(
        "manager",
        secrets.token_urlsafe(24),
        roles={"production_manager"},
    )
    seed_item(migrator, "item-A", actor="manager", now=OLD)
    request = RecipeRegistrationRequest(
        request_id="before-retention",
        recipe_id="retention-family",
        item_id="item-A",
        parameters=recipe().parameters,
    )
    with migrator.begin() as db:
        original_recipe = register_recipe_request(db, request, actor="manager", now=OLD)
    before = snapshot(migrator)
    assert before[2] and before[3]
    upgrade_alembic(migrator, HEAD)
    assert snapshot(migrator) == before
    assert submit(service, value) == ack
    with migrator.begin() as db:
        assert register_recipe_request(db, request, actor="manager", now=OLD) == original_recipe
    with migrator.connect() as db:
        receipt = db.execute(sa.text("SELECT * FROM ingest_receipts")).mappings().one()
        assert receipt["content_sha256"] == value.content_sha256
        assert receipt["expired_at"] is None
        assert db.execute(sa.text("SELECT count(*) FROM ingest_retention_ranges")).scalar() == 0
    assert prune(migrator)["deleted_count"] == 1
    with pytest.raises(DBAPIError), migrator.connect() as db:
        command.downgrade(migration_config(db), "0021_item_master")
    with migrator.connect() as db:
        assert db.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() == HEAD
        assert db.execute(sa.text("SELECT count(*) FROM ingest_receipts")).scalar() == 1


def test_real_runtime_grants_deny_direct_metadata_or_delete_even_with_guc(head21):
    url, migrator = head21
    upgrade_alembic(migrator, HEAD)
    with connect(url) as db:
        db.execute((ROOT / "deploy/neon-ingest-retention-grants.sql").read_text())
    runtime = role_engine(url, "hmi_app")
    try:
        service = configure(runtime)
        submit(service, batch())
        for statement in (
            "DELETE FROM ingest_batches",
            "DELETE FROM capture_clock_evidence",
            "DELETE FROM capture_clock_probes",
            "DELETE FROM ingest_receipts",
            "UPDATE ingest_receipts SET expired_at=clock_timestamp()",
            "DELETE FROM ingest_retention_ranges",
            "TRUNCATE ingest_retention_ranges",
            "UPDATE ingest_retention_state SET last_attempt_at=NULL",
        ):
            denied(runtime, statement)
        with runtime.begin() as db:
            db.execute(sa.text("SET LOCAL hmi.ingest_retention='on'"))
            with pytest.raises(DBAPIError):
                db.execute(sa.text("DELETE FROM ingest_batches"))
        assert prune(migrator)["deleted_count"] == 1
        with runtime.connect() as db:
            assert db.execute(sa.text("SELECT count(*) FROM ingest_retention_ranges")).scalar() == 1
            assert db.execute(sa.text("SELECT count(*) FROM ingest_receipts")).scalar() == 1
    finally:
        runtime.dispose()


COMPACTION_HEAD = "0023_ingest_compaction"


@pytest.fixture
def head22(head21):
    url, migrator = head21
    upgrade_alembic(migrator, HEAD)
    with connect(url) as db:
        db.execute((ROOT / "deploy/neon-ingest-retention-grants.sql").read_text())
    return url, migrator


def compaction_snapshot(engine):
    with engine.connect() as db:
        return tuple(
            db.execute(sa.text(query)).all()
            for query in (
                "SELECT * FROM ingest_batches ORDER BY gateway_id,batch_id",
                "SELECT * FROM ingest_receipts ORDER BY gateway_id,batch_id",
                "SELECT * FROM capture_clock_evidence ORDER BY gateway_id,batch_id",
                "SELECT * FROM alarm_capture_decisions ORDER BY gateway_id,batch_id",
                "SELECT * FROM ingest_retention_ranges ORDER BY gateway_id,from_capture_seq",
            )
        )


@pytest.mark.parametrize("transport", ["alembic", "http"])
def test_compaction_upgrade_preserves_live_and_expired_receipts_and_refuses_downgrade(
    head22, transport
):
    url, migrator = head22
    service = configure(migrator)
    old = batch()
    submit(service, old)
    assert prune(migrator)["deleted_count"] == 1
    recent = batch(capture_seq=2)
    ack = submit(service, recent)
    before = compaction_snapshot(migrator)
    if transport == "alembic":
        upgrade_alembic(migrator, COMPACTION_HEAD)
    else:
        http_batch(url, renderer.retention_compaction_statements())
    after = compaction_snapshot(migrator)
    assert after[:4] == before[:4]
    # Only appended metadata columns differ on pre-existing expiry ranges.
    assert [row[: len(before[4][0])] for row in after[4]] == before[4]
    assert submit(service, recent) == ack
    with migrator.connect() as db:
        assert db.execute(sa.text("SELECT count(*) FROM ingest_retired_batch_chunks")).scalar() == 0
    # Existing 22-expired rows are compacted by a bounded later maintenance call.
    from tests.integration.test_ingest_retention import prune as prune_hours

    result = prune_hours(migrator)
    assert result["compacted_count"] == 2
    with migrator.connect() as db:
        assert db.execute(sa.text("SELECT count(*) FROM ingest_receipts")).scalar() == 0
    compacted = compaction_snapshot(migrator)
    with pytest.raises(DBAPIError, match="losslessly downgraded"), migrator.connect() as db:
        command.downgrade(migration_config(db), HEAD)
    assert compaction_snapshot(migrator) == compacted
    with migrator.connect() as db:
        assert (
            db.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
            == COMPACTION_HEAD
        )


def test_compaction_rendered_upgrade_and_grants_are_reproducible(head22):
    url, migrator = head22
    upgrade = json.loads((ROOT / "deploy/neon-http/upgrade-0022-0023.json").read_text())
    grants = json.loads((ROOT / "deploy/neon-http/grants-0023.json").read_text())
    assert renderer.retention_compaction_statements() == upgrade
    assert renderer.retention_compaction_grant_statements() == grants
    http_batch(url, upgrade)
    http_batch(url, grants)
    granted = catalog(url)
    with connect(url) as db:
        db.execute((ROOT / "deploy/neon-ingest-compaction-grants.sql").read_text())
    assert catalog(url) == granted
    runtime = role_engine(url, "hmi_app")
    try:
        service = configure(runtime)
        submit(service, batch())
        for statement in (
            "DELETE FROM ingest_retired_batch_chunks",
            "TRUNCATE ingest_retired_batch_chunks",
            "INSERT INTO ingest_retired_batch_chunks VALUES('gateway-1',1,0,ARRAY['forged'])",
            "SELECT compact_ingest_receipt('gateway-1','batch-1')",
            "SELECT ingest_receipt_referenced('gateway-1','batch-1')",
            "SELECT prune_ingest_batches(7,200,400)",
        ):
            denied(runtime, statement)
        with runtime.begin() as db:
            result = db.execute(
                sa.text("SELECT prune_ingest_batches_hours(3,200,400)")
            ).scalar_one()
        assert result["deleted_count"] == result["compacted_count"] == 1
        with runtime.connect() as db:
            assert db.execute(sa.text("SELECT count(*) FROM ingest_receipts")).scalar() == 0
            assert (
                db.execute(sa.text("SELECT count(*) FROM ingest_retired_batch_chunks")).scalar()
                == 1
            )
    finally:
        runtime.dispose()


def test_unused_compaction_downgrade_restores_exact_catalog_data_and_runtime_capability(head22):
    from datetime import UTC, datetime, timedelta

    from apps.api.ingest_retention import IngestRetentionExpired
    from tests.integration.test_trend_history import batch as trend_batch

    url, migrator = head22
    service = configure(migrator)
    old = trend_batch(1, OLD)
    submit(service, old)
    assert prune(migrator)["deleted_count"] == 1
    recent = trend_batch(2, OLD + timedelta(seconds=1))
    ack = submit(service, recent, datetime.now(UTC))
    before_catalog = catalog(url)
    before_rows = compaction_snapshot(migrator)

    def projections():
        with migrator.connect() as db:
            return tuple(
                db.execute(sa.text(query)).all()
                for query in (
                    "SELECT * FROM analog_history_raw ORDER BY capture_seq,tag_id",
                    "SELECT * FROM analog_history_minutes ORDER BY minute,tag_id",
                    "SELECT * FROM analog_history_clock_state "
                    "ORDER BY source_id,stream_id,clock_epoch",
                )
            )

    before_projections = projections()
    upgrade_alembic(migrator, COMPACTION_HEAD)
    with connect(url) as db:
        db.execute((ROOT / "deploy/neon-ingest-compaction-grants.sql").read_text())
    runtime = role_engine(url, "hmi_app")
    try:
        with runtime.connect() as db:
            assert not db.execute(
                sa.text("""SELECT has_function_privilege(current_user,
                'public.prune_ingest_batches(integer,integer,integer)','EXECUTE')""")
            ).scalar_one()
            assert db.execute(
                sa.text("""SELECT has_function_privilege(current_user,
                'public.prune_ingest_batches_hours(integer,integer,integer)','EXECUTE')""")
            ).scalar_one()
            assert (
                db.execute(sa.text("SELECT count(*) FROM ingest_retired_batch_chunks")).scalar()
                == 0
            )
        with migrator.connect() as db:
            command.downgrade(migration_config(db), HEAD)
        assert catalog(url) == before_catalog
        assert compaction_snapshot(migrator) == before_rows
        assert projections() == before_projections
        assert submit(service, recent) == ack
        with pytest.raises(IngestRetentionExpired):
            submit(service, old)
        with runtime.connect() as db:
            assert db.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() == HEAD
            assert db.execute(
                sa.text("""SELECT has_function_privilege(current_user,
                'public.prune_ingest_batches(integer,integer,integer)','EXECUTE')""")
            ).scalar_one()
            assert (
                db.execute(
                    sa.text("""SELECT to_regprocedure(
                'public.prune_ingest_batches_hours(integer,integer,integer)')""")
                ).scalar()
                is None
            )
    finally:
        runtime.dispose()
