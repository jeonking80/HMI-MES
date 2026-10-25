"""Head19 receipts survive canonical and rendered queue migration with minimal grants."""

import json
import secrets
from pathlib import Path

import psycopg
import pytest
from alembic import command
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from apps.api.auth import Principal, UserAuth
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.database import create_database_engine, migration_config
from apps.api.ingestion import IngestionService
from apps.api.line_queue import QueueUpdate, advance_line_once, read_queue, update_queue
from apps.api.registration_ids import register_recipe_request
from apps.api.registration_requests import RecipeRegistrationRequest
from apps.api.work_order_workflow import ReleaseIntent, release_work_order
from apps.api.work_orders import (
    RecipeDefinition,
    WorkOrderDefinition,
    register_recipe_version,
    register_work_order,
)
from deploy.generate_neon_init import connect
from tests.integration.test_clock_runtime_grants import head17 as head17
from tests.integration.test_clock_runtime_grants import head18 as head18
from tests.integration.test_kuls_runtime_grants import denied
from tests.integration.test_kuls_runtime_grants import installed as installed
from tests.integration.test_line_queue import definition, update
from tests.integration.test_neon_clock_http_migration import catalog, renderer, upgrade_alembic
from tests.integration.test_neon_http_migration import http_batch
from tests.integration.test_registration_runtime_grants import head19 as head19
from tests.integration.test_work_order_workflow import AT, ORIGIN, account
from tests.integration.test_work_order_workflow import state as state

ROOT = Path(__file__).resolve().parents[2]
GRANTS = ROOT / "deploy/neon-queue-grants.sql"


def test_rendered_queue_upgrade_equals_alembic_and_preserves_head19_receipts(head19):
    url, migrator = head19
    bootstrap_reference_data(migrator)
    auth = UserAuth(migrator, allowed_origins={ORIGIN})
    auth.bootstrap_user("manager", secrets.token_urlsafe(24), roles={"production_manager"})
    with migrator.begin() as db:
        register_recipe_request(
            db,
            RecipeRegistrationRequest(
                request_id="preserved-head19",
                recipe_id="recipe",
                item_id="item",
                parameters=dict(temperature_c=150.0, speed_m_per_min=6.0),
            ),
            actor="manager",
            now=AT,
        )
    with connect(url) as db:
        before = db.execute("SELECT * FROM registration_receipts ORDER BY request_id").fetchall()
        db.execute((ROOT / "deploy/neon-registration-grants.sql").read_text())
    upgrade_alembic(migrator, "0020_line_queue")
    canonical = catalog(url)
    with migrator.connect() as db:
        command.downgrade(migration_config(db), "0019_registration_ids")
    upgrade = json.loads((ROOT / "deploy/neon-http/upgrade-0019-0020.json").read_text())
    grants = json.loads((ROOT / "deploy/neon-http/grants-0020.json").read_text())
    assert upgrade == renderer.queue_statements()
    assert grants == renderer.queue_grant_statements()
    http_batch(url, upgrade)
    assert catalog(url) == canonical
    http_batch(url, grants)
    granted = catalog(url)
    with connect(url) as db:
        db.execute(GRANTS.read_text())
        assert (
            db.execute("SELECT * FROM registration_receipts ORDER BY request_id").fetchall()
            == before
        )
    assert catalog(url) == granted
    with pytest.raises(psycopg.errors.RaiseException):
        http_batch(url, upgrade)
    assert catalog(url) == granted


def test_used_queue_refuses_downgrade_without_losing_intent_or_receipts(state):
    receipt = update(state, definition(enabled=False))
    with pytest.raises(DBAPIError), state.engine.connect() as db:
        command.downgrade(migration_config(db), "0019_registration_ids")
    assert update(state, definition(enabled=False)) == receipt
    with state.engine.connect() as db:
        assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0020_line_queue"
        )


def test_actual_hmi_app_queue_intent_permissions_and_immutable_evidence(head19):
    url, migrator = head19
    with connect(url) as db:
        db.execute((ROOT / "deploy/neon-registration-grants.sql").read_text())
    upgrade_alembic(migrator, "0020_line_queue")
    with connect(url) as db:
        db.execute(GRANTS.read_text())
        db.execute(GRANTS.read_text())
        password = secrets.token_urlsafe(32)
        db.execute(sql.SQL("ALTER ROLE hmi_app LOGIN PASSWORD {}").format(sql.Literal(password)))
    bootstrap_reference_data(migrator)
    auth = UserAuth(migrator, allowed_origins={ORIGIN})
    account(auth, "manager", "production_manager")
    principal = Principal("manager", frozenset({"production_manager"}), False)
    with migrator.begin() as db:
        register_recipe_version(
            db,
            RecipeDefinition(
                id="runtime-recipe-v1",
                recipe_id="runtime-recipe",
                version="v1",
                item_id="item",
                parameters=dict(temperature_c=150.0, speed_m_per_min=6.0),
            ),
            actor="manager",
            now=AT,
        )
        register_work_order(
            db,
            WorkOrderDefinition(
                id="runtime-work",
                item_id="item",
                line_id=LINE_ID,
                recipe_version_id="runtime-recipe-v1",
                target_quantity=4,
                planned_date="2026-10-24",
            ),
            actor="manager",
            now=AT,
        )
        release_work_order(
            db,
            "runtime-work",
            ReleaseIntent(release_id="runtime-release", expected_row_version=1),
            principal=principal,
            clock=lambda: AT,
        )
    IngestionService(migrator).configure_source(
        source_id="source",
        stream_id="stream",
        gateway_id="gateway",
        line_id=LINE_ID,
        capture_generation=1,
        now=AT,
    )
    runtime = create_database_engine(url.set(username="hmi_app", password=password))
    try:
        with runtime.begin() as db:
            assert db.execute(text("SELECT current_user")).scalar_one() == "hmi_app"
            assert read_queue(db, LINE_ID)["auto_advance"] is False
            receipt = update_queue(
                db,
                LINE_ID,
                QueueUpdate(
                    request_id="runtime-queue",
                    expected_revision=0,
                    source_id="source",
                    stream_id="stream",
                    auto_advance=False,
                    entries=[dict(work_order_id="runtime-work", expected_row_version=2)],
                ),
                principal=principal,
                clock=lambda: AT,
            )
        with runtime.begin() as db:
            assert read_queue(db, LINE_ID) == receipt
            db.execute(text("UPDATE line_queue_policies SET revision=revision"))
            db.execute(text("UPDATE line_queue_entries SET position=position"))
            statement = text("SELECT count(*) FROM line_queue_updates")
            assert db.execute(statement).scalar_one() == 1
            assert db.execute(text("SELECT count(*) FROM line_queue_audit")).scalar_one() == 1
        for statement in (
            "UPDATE line_queue_updates SET actor=actor",
            "DELETE FROM line_queue_updates",
            "TRUNCATE line_queue_updates",
            "UPDATE line_queue_audit SET actor=actor",
            "DELETE FROM line_queue_audit",
            "TRUNCATE line_queue_audit",
            "DELETE FROM line_queue_policies",
            "DELETE FROM line_queue_entries",
            "TRUNCATE line_queue_entries",
            "SELECT setval('line_queue_audit_id_seq',100)",
            "CREATE TABLE public.queue_runtime_ddl(id int)",
        ):
            denied(runtime, statement)
        with runtime.connect() as db:
            for table in (
                "line_queue_policies",
                "line_queue_entries",
                "line_queue_updates",
                "line_queue_audit",
            ):
                assert not db.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_class c, LATERAL aclexplode("
                        "COALESCE(c.relacl,acldefault('r',c.relowner))) a "
                        "WHERE c.oid=to_regclass(:table) AND a.grantee=0)"
                    ),
                    {"table": "public." + table},
                ).scalar_one()
    finally:
        runtime.dispose()


def test_queue_grants_reject_wrong_revision_without_catalog_change(head19):
    url, _ = head19
    before = catalog(url)
    with connect(url) as db:
        with pytest.raises(psycopg.errors.RaiseException):
            db.execute(GRANTS.read_text())
    assert catalog(url) == before


@pytest.mark.parametrize(
    "revision", ["0005_command_dispatch", "0006_settlement", "0007_execution_plan"]
)
def test_old_dispatch_schema_has_no_queue_side_effects(migrated, revision):
    with migrated.connect() as db:
        command.downgrade(migration_config(db), revision)
    with migrated.begin() as db:
        assert (
            advance_line_once(
                db,
                authenticated_gateway_id="gateway",
                authenticated_source_id="source",
                authenticated_stream_id="stream",
                now=AT,
            )
            is None
        )
        assert db.execute(text("SELECT count(*) FROM command_requests")).scalar_one() == 0
