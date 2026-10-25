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
from tests.integration.test_ingest_retention import OLD, batch, prune, submit
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
