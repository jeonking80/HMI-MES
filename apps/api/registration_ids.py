"""Atomic server identities and actor-bound immutable registration receipts."""

import json
import re

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import text

from apps.api.database import SUPPORTED_AUTO_REGISTRATION_REVISIONS
from apps.api.registration_requests import RecipeRegistrationRequest, WorkOrderRegistrationRequest
from apps.api.work_orders import (
    RecipeDefinition,
    RecipeVersion,
    RegistrationConflict,
    RegistrationNotFound,
    WorkOrder,
    WorkOrderDefinition,
    WorkOrdersUnavailable,
    _lock,
    _ready,
    lock_registration_namespace,
    register_recipe_version,
    register_work_order,
)
from contracts.models import Identifier, UTCTimestamp

_ID = TypeAdapter(Identifier)
_TIME = TypeAdapter(UTCTimestamp)
_MAX_COUNTER = 2**63 - 1


class RegistrationInputError(ValueError):
    """A server-generated identity cannot fit the public registration contract."""


def _receipt_ready(connection):
    _ready(connection)
    revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    if revision not in SUPPORTED_AUTO_REGISTRATION_REVISIONS:
        raise WorkOrdersUnavailable("Registration receipt schema required")


def _response(kind, response):
    model = RecipeVersion if kind == "recipe" else WorkOrder
    try:
        return model.model_validate_json(json.dumps(response))
    except (ValidationError, TypeError, ValueError):
        raise WorkOrdersUnavailable("Original registration receipt unavailable") from None


def _receipt(connection, kind, request_id):
    return (
        connection.execute(
            text(
                "SELECT actor, original_request, response FROM registration_receipts "
                "WHERE kind=:kind AND request_id=:request"
            ),
            {"kind": kind, "request": request_id},
        )
        .mappings()
        .first()
    )


def recover_registration_receipt(connection, *, kind, request_id, actor):
    if kind not in {"recipe", "work_order"}:
        raise ValueError("Invalid registration kind")
    request_id, actor = _ID.validate_python(request_id), _ID.validate_python(actor)
    _receipt_ready(connection)
    previous = _receipt(connection, kind, request_id)
    if previous is None or previous["actor"] != actor:
        raise RegistrationNotFound("Original registration receipt not found")
    return _response(kind, previous["response"]).model_dump(mode="json")


def _counter(connection, kind, namespace, existing):
    last = connection.execute(
        text(
            "SELECT last_value FROM registration_id_counters "
            "WHERE kind=:kind AND namespace=:namespace"
        ),
        {"kind": kind, "namespace": namespace},
    ).scalar_one_or_none()
    return max(int(last or 0), existing)


def _advance(connection, kind, namespace, number):
    connection.execute(
        text(
            "INSERT INTO registration_id_counters(kind,namespace,last_value) "
            "VALUES (:kind,:namespace,:number) ON CONFLICT(kind,namespace) "
            "DO UPDATE SET last_value=EXCLUDED.last_value"
        ),
        {"kind": kind, "namespace": namespace, "number": number},
    )


def _next(number):
    if number >= _MAX_COUNTER:
        raise RegistrationInputError("Registration number limit reached")
    return number + 1


def _identity(value):
    try:
        return _ID.validate_python(value)
    except ValidationError:
        raise RegistrationInputError("Generated registration identity is too long") from None


def _allocate_recipe(connection, request):
    versions = connection.execute(
        text("SELECT version FROM recipe_versions WHERE recipe_id=:recipe"),
        {"recipe": request.recipe_id},
    ).scalars()
    existing = max((int(v[1:]) for v in versions if re.fullmatch(r"v[0-9]+", v)), default=0)
    number = _counter(connection, "recipe", request.recipe_id, existing)
    while True:
        number = _next(number)
        version = f"v{number}"
        identity = _identity(f"{request.recipe_id}-{version}")
        occupied = connection.execute(
            text(
                "SELECT 1 FROM recipe_versions WHERE id=:id "
                "OR (recipe_id=:recipe AND version=:version)"
            ),
            {"id": identity, "recipe": request.recipe_id, "version": version},
        ).first()
        if occupied is None:
            _advance(connection, "recipe", request.recipe_id, number)
            return {"id": identity, "version": version}


def _allocate_order(connection, request):
    # planned_date is already the caller's KST calendar date, not a UTC instant.
    planned = request.planned_date
    namespace = f"{planned.year:04d}{planned.month:02d}{planned.day:02d}"
    prefix = f"WO-{namespace}-"
    identities = connection.execute(
        text("SELECT id FROM work_orders WHERE id LIKE :prefix"),
        {"prefix": prefix + "%"},
    ).scalars()
    existing = max(
        (
            int(identity[len(prefix) :])
            for identity in identities
            if re.fullmatch(r"[0-9]+", identity[len(prefix) :])
        ),
        default=0,
    )
    number = _counter(connection, "work_order", namespace, existing)
    while True:
        number = _next(number)
        # Three digits is a minimum width; 1000 remains a valid issued suffix.
        identity = _identity(f"{prefix}{number:03d}")
        if (
            connection.execute(
                text("SELECT 1 FROM work_orders WHERE id=:id"), {"id": identity}
            ).first()
            is None
        ):
            _advance(connection, "work_order", namespace, number)
            return {"id": identity}


def _register(connection, request, *, kind, actor, now):
    request_model = RecipeRegistrationRequest if kind == "recipe" else WorkOrderRegistrationRequest
    request = request_model.model_validate_json(request.model_dump_json())
    actor, now = _ID.validate_python(actor), _TIME.validate_python(now)
    writer = register_recipe_version if kind == "recipe" else register_work_order
    definition_model = RecipeDefinition if kind == "recipe" else WorkOrderDefinition
    original = request.model_dump(mode="json", exclude={"request_id"})
    if request.request_id is None:
        return writer(connection, definition_model.model_validate(original), actor=actor, now=now)

    _receipt_ready(connection)
    _lock(
        connection,
        json.dumps(["mes-registration-request", kind, request.request_id], separators=(",", ":")),
    )
    previous = _receipt(connection, kind, request.request_id)
    if previous is not None:
        if previous["actor"] != actor or previous["original_request"] != original:
            raise RegistrationConflict("Registration request identity has different content")
        return _response(kind, previous["response"])

    lock_registration_namespace(connection, kind)
    values = dict(original)
    if request.id is None:
        allocator = _allocate_recipe if kind == "recipe" else _allocate_order
        values.update(allocator(connection, request))
    result = writer(connection, definition_model.model_validate(values), actor=actor, now=now)
    connection.execute(
        text(
            "INSERT INTO registration_receipts(kind,request_id,actor,original_request,response,"
            "recipe_version_id,work_order_id,created_at) VALUES "
            "(:kind,:request,:actor,CAST(:original AS jsonb),CAST(:response AS jsonb),"
            ":recipe,:work_order,:now)"
        ),
        {
            "kind": kind,
            "request": request.request_id,
            "actor": actor,
            "original": json.dumps(original),
            "response": result.model_dump_json(),
            "recipe": result.id if kind == "recipe" else None,
            "work_order": result.id if kind == "work_order" else None,
            "now": now,
        },
    )
    return result


def register_recipe_request(connection, request, *, actor, now):
    return _register(connection, request, kind="recipe", actor=actor, now=now)


def register_work_order_request(connection, request, *, actor, now):
    return _register(connection, request, kind="work_order", actor=actor, now=now)
