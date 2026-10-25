"""Draft registration on caller-owned PostgreSQL transactions; no PLC or execution writes."""

import json
import re
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, TypeAdapter
from sqlalchemy import Connection, text

from apps.api.database import SCHEMA_LOCK_ID, SUPPORTED_REGISTRATION_REVISIONS
from contracts.conflict_codes import ConflictError, ConflictReason
from contracts.models import ContractModel, Identifier, Parameters, UInt64, UTCTimestamp

_ID = TypeAdapter(Identifier)
_TIME = TypeAdapter(UTCTimestamp)


def _date(value):
    if type(value) is date:
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("planned_date requires an ISO calendar date")
    return date.fromisoformat(value)


class RecipeDefinition(ContractModel):
    id: Identifier
    recipe_id: Identifier
    version: Identifier
    item_id: Identifier
    parameters: Parameters


class RecipeVersion(RecipeDefinition):
    created_by: Identifier
    created_at: UTCTimestamp


class WorkOrderDefinition(ContractModel):
    id: Identifier
    item_id: Identifier
    line_id: Identifier
    recipe_version_id: Identifier
    target_quantity: Annotated[int, Field(strict=True, gt=0, le=2**32 - 1)]
    planned_date: Annotated[date, BeforeValidator(_date)]


class WorkOrder(WorkOrderDefinition):
    status: Literal["DRAFT", "RELEASED", "CANCELLED"]
    row_version: UInt64
    created_by: Identifier
    created_at: UTCTimestamp


class RegistrationConflict(ConflictError):
    """Identity already belongs to another immutable definition."""

    default_reason_code = ConflictReason.ID_CONTENT_CONFLICT


class RegistrationNotFound(LookupError):
    """Requested registration or referenced line/recipe was not found."""


class WorkOrdersUnavailable(RuntimeError):
    """Caller did not select the supported PostgreSQL registration schema."""


def _ready(connection):
    if connection.dialect.name != "postgresql" or not connection.in_transaction():
        raise WorkOrdersUnavailable("Caller-owned PostgreSQL transaction required")
    connection.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": SCHEMA_LOCK_ID})
    revisions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    if len(revisions) != 1 or revisions[0] not in SUPPORTED_REGISTRATION_REVISIONS:
        raise WorkOrdersUnavailable("Unsupported work-order registration schema")


def _lock(connection, key):
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": key})


def lock_registration_namespace(connection, kind):
    """Serialize manual and automatic writers before any identity locks."""
    if kind not in {"recipe", "work_order"}:
        raise ValueError("Invalid registration kind")
    _lock(connection, json.dumps(["mes-registration-namespace", kind], separators=(",", ":")))


def _recipe(row):
    return RecipeVersion(
        id=row["id"],
        recipe_id=row["recipe_id"],
        version=row["version"],
        item_id=row["item_id"],
        parameters=Parameters(
            temperature_c=row["temperature_c"], speed_m_per_min=row["speed_m_per_min"]
        ),
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


def _order(row):
    return WorkOrder(**{**row, "row_version": int(row["row_version"])})


def _audit(connection, action, definition, actor, now):
    connection.execute(
        text(
            "INSERT INTO mes_management_audit(actor,action,recipe_version_id,work_order_id,"
            "occurred_at,evidence) "
            "VALUES (:actor,:action,:recipe,:work_order,:now,CAST(:evidence AS jsonb))"
        ),
        {
            "actor": actor,
            "action": action,
            "recipe": definition.id if action == "REGISTER_RECIPE" else None,
            "work_order": definition.id if action == "REGISTER_WORK_ORDER" else None,
            "now": now,
            "evidence": definition.model_dump_json(),
        },
    )


def register_recipe_version(
    connection: Connection, definition: RecipeDefinition, *, actor: str, now: datetime
) -> RecipeVersion:
    definition = RecipeDefinition.model_validate_json(definition.model_dump_json())
    actor, now = _ID.validate_python(actor), _TIME.validate_python(now)
    _ready(connection)
    lock_registration_namespace(connection, "recipe")
    keys = [
        json.dumps(["mes-recipe-id", definition.id], separators=(",", ":")),
        json.dumps(
            ["mes-recipe-pair", definition.recipe_id, definition.version], separators=(",", ":")
        ),
    ]
    for key in sorted(keys):
        _lock(connection, key)
    existing = (
        connection.execute(
            text(
                "SELECT * FROM recipe_versions WHERE id=:id "
                "OR (recipe_id=:recipe_id AND version=:version)"
            ),
            definition.model_dump(exclude={"parameters"}),
        )
        .mappings()
        .all()
    )
    if existing:
        if (
            len(existing) != 1
            or RecipeDefinition.model_validate(
                _recipe(existing[0]).model_dump(include=set(RecipeDefinition.model_fields))
            )
            != definition
        ):
            raise RegistrationConflict("Recipe identity or version already has different content")
        return _recipe(existing[0])
    values = definition.model_dump(exclude={"parameters"})
    values.update(definition.parameters.model_dump())
    values.update(created_by=actor, created_at=now)
    row = (
        connection.execute(
            text(
                "INSERT INTO recipe_versions VALUES "
                "(:id,:recipe_id,:version,:item_id,:temperature_c,:speed_m_per_min,"
                ":created_by,:created_at) "
                "RETURNING *"
            ),
            values,
        )
        .mappings()
        .one()
    )
    _audit(connection, "REGISTER_RECIPE", definition, actor, now)
    return _recipe(row)


def register_work_order(
    connection: Connection, definition: WorkOrderDefinition, *, actor: str, now: datetime
) -> WorkOrder:
    definition = WorkOrderDefinition.model_validate_json(definition.model_dump_json())
    actor, now = _ID.validate_python(actor), _TIME.validate_python(now)
    _ready(connection)
    lock_registration_namespace(connection, "work_order")
    # Global identity first, even when two requests select different lines.
    _lock(connection, json.dumps(["mes-work-order", definition.id], separators=(",", ":")))
    existing = (
        connection.execute(text("SELECT * FROM work_orders WHERE id=:id"), {"id": definition.id})
        .mappings()
        .first()
    )
    if existing is not None:
        if (
            WorkOrderDefinition.model_validate(
                _order(existing).model_dump(include=set(WorkOrderDefinition.model_fields))
            )
            != definition
        ):
            raise RegistrationConflict("Work-order identity already has different content")
        return _order(existing)
    if (
        connection.execute(
            text("SELECT id FROM lines WHERE id=:id FOR UPDATE"), {"id": definition.line_id}
        ).first()
        is None
    ):
        raise RegistrationNotFound("Registered line required")
    recipe = connection.execute(
        text("SELECT item_id FROM recipe_versions WHERE id=:id"),
        {"id": definition.recipe_version_id},
    ).scalar_one_or_none()
    if recipe is None:
        raise RegistrationNotFound("Registered recipe version required")
    if recipe != definition.item_id:
        raise RegistrationConflict("Recipe version belongs to another item")
    values = definition.model_dump()
    values.update(created_by=actor, created_at=now)
    row = (
        connection.execute(
            text(
                "INSERT INTO work_orders(id,item_id,line_id,recipe_version_id,target_quantity,"
                "planned_date,created_by,created_at) VALUES "
                "(:id,:item_id,:line_id,:recipe_version_id,:target_quantity,"
                ":planned_date,:created_by,:created_at) RETURNING *"
            ),
            values,
        )
        .mappings()
        .one()
    )
    _audit(connection, "REGISTER_WORK_ORDER", definition, actor, now)
    return _order(row)


def get_recipe_version(connection: Connection, identity: str) -> RecipeVersion:
    identity = _ID.validate_python(identity)
    _ready(connection)
    row = (
        connection.execute(text("SELECT * FROM recipe_versions WHERE id=:id"), {"id": identity})
        .mappings()
        .first()
    )
    if row is None:
        raise RegistrationNotFound("Recipe version not found")
    return _recipe(row)


def get_work_order(connection: Connection, identity: str) -> WorkOrder:
    identity = _ID.validate_python(identity)
    _ready(connection)
    row = (
        connection.execute(text("SELECT * FROM work_orders WHERE id=:id"), {"id": identity})
        .mappings()
        .first()
    )
    if row is None:
        raise RegistrationNotFound("Work order not found")
    return _order(row)


def list_registrations(
    connection: Connection,
    *,
    kind: Literal["recipe", "work_order"],
    after_id: str | None = None,
    limit: int = 50,
    item_id: str | None = None,
    line_id: str | None = None,
) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100 or kind not in {"recipe", "work_order"}:
        raise ValueError("Invalid registration page bounds")
    if kind == "recipe" and line_id is not None:
        raise ValueError("Recipe pages do not have a line filter")
    values = {
        name: _ID.validate_python(value) if value is not None else None
        for name, value in (("after", after_id), ("item", item_id), ("line", line_id))
    }
    values["limit"] = limit + 1
    _ready(connection)
    table = "recipe_versions" if kind == "recipe" else "work_orders"
    clauses = [
        '(CAST(:after AS text) IS NULL OR id COLLATE "C" > :after)',
        "(CAST(:item AS text) IS NULL OR item_id=:item)",
    ]
    if kind == "work_order":
        clauses.append("(CAST(:line AS text) IS NULL OR line_id=:line)")
    rows = (
        connection.execute(
            text(
                f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} "
                'ORDER BY id COLLATE "C" LIMIT :limit'
            ),
            values,
        )
        .mappings()
        .all()
    )
    selected = rows[:limit]
    convert = _recipe if kind == "recipe" else _order
    return {
        "items": [convert(row).model_dump(mode="json") for row in selected],
        "next_cursor": selected[-1]["id"] if len(rows) > limit else None,
    }
