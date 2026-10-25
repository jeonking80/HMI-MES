"""Immutable item master on caller-owned, authorized PostgreSQL transactions."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StrictBool, TypeAdapter, field_validator
from sqlalchemy import Connection, text

from apps.api.database import SUPPORTED_ITEM_REVISIONS
from apps.api.work_orders import (
    RegistrationConflict,
    RegistrationNotFound,
    WorkOrdersUnavailable,
    _lock,
)
from apps.api.work_orders import (
    _ready as _registration_ready,
)
from contracts.models import ContractModel, Identifier, UTCTimestamp

_ID = TypeAdapter(Identifier)
_TIME = TypeAdapter(UTCTimestamp)


class ItemDefinition(ContractModel):
    id: Identifier
    name: Annotated[str, Field(strict=True, min_length=1, max_length=128)]
    unit: Annotated[str, Field(strict=True, min_length=1, max_length=32)]
    enabled: StrictBool = True

    @field_validator("name", "unit")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Item text must not be blank")
        return value


class Item(ItemDefinition):
    origin: Literal["USER", "MIGRATION"]
    created_by: Identifier | None
    created_at: UTCTimestamp


def _ready(connection):
    if _registration_ready(connection) not in SUPPORTED_ITEM_REVISIONS:
        raise WorkOrdersUnavailable("Unsupported item master schema")


def register_item(
    connection: Connection, definition: ItemDefinition, *, actor: str, now: datetime
) -> Item:
    definition = ItemDefinition.model_validate_json(definition.model_dump_json())
    actor, now = _ID.validate_python(actor), _TIME.validate_python(now)
    _ready(connection)
    _lock(connection, f"mes-item:{definition.id}")
    existing = (
        connection.execute(text("SELECT * FROM item_master WHERE id=:id"), {"id": definition.id})
        .mappings()
        .first()
    )
    if existing is not None:
        item = Item.model_validate(dict(existing))
        if (
            ItemDefinition.model_validate(item.model_dump(include=set(ItemDefinition.model_fields)))
            != definition
        ):
            raise RegistrationConflict("Item identity already has different content")
        return item
    values = definition.model_dump()
    values.update(actor=actor, now=now)
    row = (
        connection.execute(
            text(
                "INSERT INTO item_master(id,name,unit,enabled,origin,created_by,created_at) "
                "VALUES (:id,:name,:unit,:enabled,'USER',:actor,:now) RETURNING *"
            ),
            values,
        )
        .mappings()
        .one()
    )
    connection.execute(
        text(
            "INSERT INTO item_audit(item_id,actor,action,occurred_at,evidence) "
            "VALUES (:id,:actor,'CREATE',:now,CAST(:evidence AS jsonb))"
        ),
        {**values, "evidence": definition.model_dump_json()},
    )
    return Item.model_validate(dict(row))


def get_item(connection: Connection, identity: str) -> Item:
    identity = _ID.validate_python(identity)
    _ready(connection)
    row = (
        connection.execute(text("SELECT * FROM item_master WHERE id=:id"), {"id": identity})
        .mappings()
        .first()
    )
    if row is None:
        raise RegistrationNotFound("Item not found")
    return Item.model_validate(dict(row))


def list_items(
    connection: Connection,
    *,
    after_id: str | None = None,
    limit: int = 50,
    enabled: bool | None = None,
) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid item page bounds")
    if enabled is not None and type(enabled) is not bool:
        raise ValueError("Invalid enabled filter")
    after_id = _ID.validate_python(after_id) if after_id is not None else None
    _ready(connection)
    rows = (
        connection.execute(
            text(
                "SELECT * FROM item_master WHERE (CAST(:after AS text) IS NULL "
                'OR id COLLATE "C" > :after) AND '
                "(CAST(:enabled AS boolean) IS NULL OR enabled=:enabled) "
                'ORDER BY id COLLATE "C" LIMIT :limit'
            ),
            {"after": after_id, "enabled": enabled, "limit": limit + 1},
        )
        .mappings()
        .all()
    )
    selected = rows[:limit]
    return {
        "items": [Item.model_validate(dict(row)).model_dump(mode="json") for row in selected],
        "next_cursor": selected[-1]["id"] if len(rows) > limit else None,
    }
