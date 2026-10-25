"""Authenticated item reads and immutable, protected item registration."""

import json
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from apps.api.auth import AuthorizationError, UserAuth
from apps.api.items import ItemDefinition, get_item, list_items, register_item
from apps.api.work_orders import RegistrationConflict, RegistrationNotFound, WorkOrdersUnavailable
from contracts.models import Identifier


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _queries(request, allowed=()):
    if set(request.query_params) - set(allowed) or any(
        len(request.query_params.getlist(name)) != 1 for name in request.query_params
    ):
        raise HTTPException(422, "invalid item query")


def mount_item_routes(app: FastAPI, auth: UserAuth, *, session_cookie: str = "hmi_session") -> None:
    async def mapped(operation):
        try:
            result = await run_in_threadpool(operation)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except RegistrationConflict as error:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "item identity conflicts with immutable content",
                    "reason_code": error.reason_code,
                },
                headers={"Cache-Control": "no-store"},
            )
        except RegistrationNotFound:
            raise HTTPException(404, "item not found") from None
        except (WorkOrdersUnavailable, SQLAlchemyError):
            raise HTTPException(503, "item master unavailable") from None

    @app.get("/api/v1/items")
    async def page(request: Request):
        _queries(request, ("after_id", "limit", "enabled"))
        raw_limit = request.query_params.get("limit", "50")
        raw_enabled = request.query_params.get("enabled")
        if not re.fullmatch(r"[1-9][0-9]{0,2}", raw_limit) or int(raw_limit) > 100:
            raise HTTPException(422, "invalid item page bounds")
        if raw_enabled is not None and raw_enabled not in {"true", "false"}:
            raise HTTPException(422, "invalid enabled filter")
        after_id = request.query_params.get("after_id")
        try:
            if after_id is not None:
                after_id = TypeAdapter(Identifier).validate_python(after_id)
        except ValidationError:
            raise HTTPException(422, "invalid item cursor") from None

        def read():
            with auth.authorized_transaction(
                request.cookies.get(session_cookie), permission="read", refresh_idle=False
            ) as tx:
                return list_items(
                    tx.connection,
                    after_id=after_id,
                    limit=int(raw_limit),
                    enabled=None if raw_enabled is None else raw_enabled == "true",
                )

        return await mapped(read)

    @app.get("/api/v1/items/{identity}")
    async def one(request: Request, identity: str):
        _queries(request)
        try:
            identity = TypeAdapter(Identifier).validate_python(identity)
        except ValidationError:
            raise HTTPException(422, "invalid item identity") from None

        def read():
            with auth.authorized_transaction(
                request.cookies.get(session_cookie), permission="read", refresh_idle=False
            ) as tx:
                return get_item(tx.connection, identity).model_dump(mode="json")

        return await mapped(read)

    @app.post("/api/v1/items")
    async def create(request: Request):
        _queries(request)
        if (
            len(request.headers.getlist("content-type")) != 1
            or request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(415, "JSON is required")
        if (
            len(request.headers.getlist("origin")) != 1
            or len(request.headers.getlist("x-csrf-token")) != 1
        ):
            raise AuthorizationError("item mutation protection required")
        raw = await request.body()
        if len(raw) > 16_384:
            raise HTTPException(413, "item request too large")
        try:
            json.loads(raw, object_pairs_hook=_unique_object)
            definition = ItemDefinition.model_validate_json(raw)
        except (ValueError, RecursionError):
            raise HTTPException(422, "invalid item registration") from None

        def save():
            with auth.authorized_transaction(
                request.cookies.get(session_cookie),
                permission="production_manage",
                csrf_token=request.headers.get("x-csrf-token"),
                origin=request.headers.get("origin"),
            ) as tx:
                return register_item(
                    tx.connection, definition, actor=tx.principal.login, now=auth._now()
                ).model_dump(mode="json")

        return await mapped(save)
