"""Real schema20 item backfill, immutable evidence and dedicated runtime privileges."""

import json
import secrets
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.script import ScriptDirectory
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from apps.api.auth import UserAuth
from apps.api.bootstrap import bootstrap_reference_data
from apps.api.database import create_database_engine, migration_config
from apps.api.items import ItemDefinition, register_item
from apps.api.registration_ids import register_recipe_request, register_work_order_request
from apps.api.registration_requests import RecipeRegistrationRequest, WorkOrderRegistrationRequest
from apps.api.user_http import UserHTTPConfig, create_user_app
from apps.api.work_orders import (
    WorkOrdersUnavailable,
    register_recipe_version,
    register_work_order,
)
from deploy.generate_neon_init import connect
from tests.integration.test_clock_runtime_grants import head17 as head17
from tests.integration.test_clock_runtime_grants import head18 as head18
from tests.integration.test_items import ITEM
from tests.integration.test_kuls_runtime_grants import denied
from tests.integration.test_kuls_runtime_grants import installed as installed
from tests.integration.test_line_queue import definition, update
from tests.integration.test_neon_clock_http_migration import catalog, renderer, upgrade_alembic
from tests.integration.test_neon_http_migration import http_batch
from tests.integration.test_registration_runtime_grants import head19 as head19
from tests.integration.test_work_order_workflow import state as state
from tests.integration.test_work_orders import AT, order, recipe
from tests.integration.test_work_orders_http import ORDER, ORIGIN, RECIPE, account, protection

ROOT = Path(__file__).resolve().parents[2]
# Frozen item migration and renderer cases intentionally target schema21.
HEAD = "0021_item_master"
CURRENT_HEAD = ScriptDirectory.from_config(migration_config()).get_current_head()


@pytest.fixture
def head20(head19):
    url, migrator = head19
    with connect(url) as db:
        db.execute((ROOT / "deploy/neon-registration-grants.sql").read_text())
    upgrade_alembic(migrator, "0020_line_queue")
    with connect(url) as db:
        db.execute((ROOT / "deploy/neon-queue-grants.sql").read_text())
    return url, migrator


def snapshot(engine):
    with engine.connect() as db:
        return tuple(
            db.execute(text(statement)).all()
            for statement in (
                "SELECT * FROM recipe_versions ORDER BY id",
                "SELECT * FROM work_orders ORDER BY id",
                "SELECT kind,request_id,actor,original_request::text,response::text,"
                "recipe_version_id,work_order_id,created_at FROM registration_receipts "
                "ORDER BY kind,request_id",
                "SELECT * FROM registration_id_counters ORDER BY kind,namespace",
                "SELECT * FROM mes_management_audit ORDER BY id",
            )
        )


def test_real_schema20_definitions_backfill_distinct_items_preserving_receipts(engine):
    upgrade_alembic(engine, "0020_line_queue")
    bootstrap_reference_data(engine)
    UserAuth(engine, allowed_origins={ORIGIN}).bootstrap_user(
        "manager", secrets.token_urlsafe(24), roles={"production_manager"}
    )
    with engine.begin() as db:
        legacy_recipe = register_recipe_version(db, recipe(), actor="manager", now=AT)
        legacy_order = register_work_order(db, order(), actor="manager", now=AT)
        register_recipe_version(
            db,
            recipe(id="recipe-B", recipe_id="family-B", item_id="item-B"),
            actor="manager",
            now=AT,
        )
        recipe_request = RecipeRegistrationRequest(
            request_id="old-recipe",
            recipe_id="family-C",
            item_id="item-C",
            parameters=recipe().parameters,
        )
        auto_recipe = register_recipe_request(db, recipe_request, actor="manager", now=AT)
        order_request = WorkOrderRegistrationRequest.model_validate(
            {
                **order().model_dump(mode="json", exclude={"id"}),
                "request_id": "old-order",
            }
        )
        auto_order = register_work_order_request(db, order_request, actor="manager", now=AT)
    before = snapshot(engine)
    with pytest.raises(WorkOrdersUnavailable), engine.begin() as db:
        register_item(db, ItemDefinition(**ITEM), actor="manager", now=AT)
    upgrade_alembic(engine, HEAD)
    assert snapshot(engine) == before
    with engine.begin() as db:
        rows = db.execute(
            text("SELECT id,name,unit,enabled,origin,created_by FROM item_master ORDER BY id")
        ).all()
        assert [tuple(row) for row in rows] == [
            (identity, identity, "EA", True, "MIGRATION", None)
            for identity in ("item-A", "item-B", "item-C")
        ]
        assert db.execute(text("SELECT count(*) FROM item_audit")).scalar_one() == 0
        assert (
            db.execute(
                text("SELECT count(*) FROM item_master WHERE created_at IS NULL")
            ).scalar_one()
            == 0
        )
        assert register_recipe_version(db, recipe(), actor="manager", now=AT) == legacy_recipe
        assert register_work_order(db, order(), actor="manager", now=AT) == legacy_order
        assert register_recipe_request(db, recipe_request, actor="manager", now=AT) == auto_recipe
        assert register_work_order_request(db, order_request, actor="manager", now=AT) == auto_order
    assert snapshot(engine) == before
    with pytest.raises(DBAPIError, match="restore a backup"), engine.connect() as db:
        command.downgrade(migration_config(db), "0020_line_queue")
    assert snapshot(engine) == before


def test_empty_rendered_upgrade_and_grants_match_alembic_catalog(head20):
    url, migrator = head20
    upgrade_alembic(migrator, HEAD)
    canonical = catalog(url)
    with migrator.connect() as db:
        command.downgrade(migration_config(db), "0020_line_queue")
    upgrade = json.loads((ROOT / "deploy/neon-http/upgrade-0020-0021.json").read_text())
    grants = json.loads((ROOT / "deploy/neon-http/grants-0021.json").read_text())
    assert upgrade == renderer.item_statements()
    assert grants == renderer.item_grant_statements()
    http_batch(url, upgrade)
    assert catalog(url) == canonical
    http_batch(url, grants)
    granted = catalog(url)
    with connect(url) as db:
        db.execute((ROOT / "deploy/neon-item-grants.sql").read_text())
    assert catalog(url) == granted
    with pytest.raises(Exception, match="unexpected schema revision"):
        http_batch(url, upgrade)
    assert catalog(url) == granted


async def test_actual_hmi_app_creates_and_reads_items_with_minimal_grants(head20):
    url, migrator = head20
    upgrade_alembic(migrator, HEAD)
    with connect(url) as db:
        db.execute((ROOT / "deploy/neon-item-grants.sql").read_text())
    bootstrap_reference_data(migrator)
    session = account(UserAuth(migrator, allowed_origins={ORIGIN}), "production_manager")
    password = secrets.token_urlsafe(32)
    with connect(url) as db:
        db.execute(sql.SQL("ALTER ROLE hmi_app LOGIN PASSWORD {}").format(sql.Literal(password)))
    runtime = create_database_engine(url.set(username="hmi_app", password=password))
    try:
        app = create_user_app(
            UserAuth(runtime, allowed_origins={ORIGIN}), UserHTTPConfig(origin=ORIGIN)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as client:
            client.cookies.set("hmi_session", session.token)
            first = await client.post("/api/v1/items", json=ITEM, headers=protection(session))
            assert first.status_code == 200, first.text
            replay = await client.post("/api/v1/items", json=ITEM, headers=protection(session))
            assert replay.status_code == 200 and replay.json() == first.json()
            assert (await client.get("/api/v1/items/item-A")).json() == first.json()
            assert (await client.get("/api/v1/items")).json()["items"] == [first.json()]
            for path, body in (("recipe-versions", RECIPE), ("work-orders", ORDER)):
                response = await client.post(
                    "/api/v1/" + path, json=body, headers=protection(session)
                )
                assert response.status_code == 200, response.text
                assert response.json()["item_id"] == ITEM["id"]
        with runtime.connect() as db:
            assert db.execute(text("SELECT current_user")).scalar_one() == "hmi_app"
            assert db.execute(text("SELECT count(*) FROM item_audit")).scalar_one() == 1
            for table in ("item_master", "item_audit"):
                for privilege in ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                    assert not db.execute(
                        text("SELECT has_table_privilege(current_user,:table,:privilege)"),
                        {"table": table, "privilege": privilege},
                    ).scalar_one()
                assert not db.execute(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_class c,LATERAL aclexplode("
                        "COALESCE(c.relacl,acldefault('r',c.relowner))) a "
                        "WHERE c.oid=to_regclass(:table) AND a.grantee=0)"
                    ),
                    {"table": table},
                ).scalar_one()
        for statement in (
            "UPDATE item_master SET name=name",
            "DELETE FROM item_master",
            "TRUNCATE item_master",
            "UPDATE item_audit SET actor=actor",
            "DELETE FROM item_audit",
            "TRUNCATE item_audit",
            "CREATE TABLE public.item_runtime_ddl(id int)",
            "ALTER TABLE item_master ADD COLUMN tamper int",
        ):
            denied(runtime, statement)
    finally:
        runtime.dispose()


def test_new_item_without_recipe_also_refuses_lossy_downgrade(migrated):
    UserAuth(migrated, allowed_origins={ORIGIN}).bootstrap_user(
        "manager", secrets.token_urlsafe(24), roles={"production_manager"}
    )
    with migrated.begin() as db:
        register_item(db, ItemDefinition(**ITEM), actor="manager", now=AT)
    with pytest.raises(DBAPIError, match="restore a backup"), migrated.connect() as db:
        command.downgrade(migration_config(db), "0020_line_queue")
    with migrated.connect() as db:
        assert (
            db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == CURRENT_HEAD
        )
        assert db.execute(text("SELECT count(*) FROM item_master")).scalar_one() == 1
        assert db.execute(text("SELECT count(*) FROM item_audit")).scalar_one() == 1


def test_queue_updates_continue_on_new_item_head(state):
    with state.engine.connect() as db:
        assert (
            db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == CURRENT_HEAD
        )
    receipt = update(state, definition(enabled=False))
    assert update(state, definition(enabled=False)) == receipt
