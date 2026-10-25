"""Explicit registered item fixtures, retaining historical pre-master schemas."""

from datetime import UTC, datetime

from sqlalchemy import text

from apps.api.database import SUPPORTED_ITEM_REVISIONS
from apps.api.items import ItemDefinition, register_item


def seed_item(engine, identity, *, actor, now=None, enabled=True):
    with engine.begin() as db:
        revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        if revision not in SUPPORTED_ITEM_REVISIONS:
            return None
        return register_item(
            db,
            ItemDefinition(id=identity, name=identity, unit="EA", enabled=enabled),
            actor=actor,
            now=now or datetime(2026, 10, 22, tzinfo=UTC),
        )
