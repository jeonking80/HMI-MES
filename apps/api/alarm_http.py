"""Authenticated scoped alarm center reads and exact-instance operator actions."""

from fastapi import FastAPI, HTTPException, Request
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from apps.api.alarm_lifecycle import AlarmConflict, AlarmScope
from apps.api.alarm_store import (
    OPERATOR_INTENT,
    AlarmCursorError,
    AlarmNotFound,
    AlarmStoreUnavailable,
    read_action_page,
    read_alarm_page,
    read_instance,
    read_operator_receipt,
    submit_operator_action,
)
from apps.api.auth import AuthorizationError, UserAuth
from contracts.models import Identifier

_ID = TypeAdapter(Identifier)
_SCOPE = frozenset(AlarmScope.model_fields)


def mount_alarm_routes(
    app: FastAPI, auth: UserAuth, *, session_cookie: str = "hmi_session"
) -> None:
    async def mapped(operation):
        try:
            return await run_in_threadpool(operation)
        except AlarmNotFound:
            raise HTTPException(404, "alarm or action receipt not found") from None
        except AlarmCursorError:
            raise HTTPException(422, "invalid alarm page cursor") from None
        except AlarmConflict:
            raise HTTPException(409, "alarm action conflicts with immutable content") from None
        except (AlarmStoreUnavailable, SQLAlchemyError):
            raise HTTPException(503, "alarm persistence unavailable") from None
        except (ValueError, ValidationError):
            raise HTTPException(422, "invalid alarm request") from None

    def query(request, *, rule_required=False, page=None, identity_keys=()):
        allowed = _SCOPE | {"rule_id"} | set(identity_keys)
        if page == "alarms":
            allowed |= {"limit", "before_alarm_id", "view", "severity"}
        elif page == "actions":
            allowed |= {"limit", "before_action_id"}
        if set(request.query_params) - allowed or any(
            len(request.query_params.getlist(name)) != 1 for name in request.query_params
        ):
            raise HTTPException(422, "invalid alarm filters")
        try:
            for name in identity_keys:
                _ID.validate_python(request.query_params[name])
            scope = AlarmScope.model_validate({name: request.query_params[name] for name in _SCOPE})
            rule = request.query_params.get("rule_id")
            if rule_required or rule is not None:
                rule = _ID.validate_python(rule)
            severity = request.query_params.get("severity")
            if severity is not None:
                severity = _ID.validate_python(severity)
            limit = int(request.query_params.get("limit", "50"))
            if not 1 <= limit <= 100:
                raise ValueError
            cursor_name = "before_alarm_id" if page == "alarms" else "before_action_id"
            cursor = request.query_params.get(cursor_name)
            if cursor is not None:
                cursor = _ID.validate_python(cursor)
            view = request.query_params.get("view", "attention")
            if view not in {"attention", "all"}:
                raise ValueError
        except (ValidationError, ValueError, KeyError):
            raise HTTPException(422, "invalid alarm scope or page") from None
        return scope, rule, limit, cursor, view, severity

    async def read(request, operation):
        def perform():
            with auth.authorized_transaction(
                request.cookies.get(session_cookie), permission="read", refresh_idle=False
            ) as tx:
                return operation(tx.connection)

        return await mapped(perform)

    @app.get("/api/v1/alarms")
    async def alarms(request: Request):
        scope, rule, limit, cursor, view, severity = query(request, page="alarms")
        return await read(
            request,
            lambda db: read_alarm_page(
                db,
                scope,
                rule_id=rule,
                limit=limit,
                before_alarm_id=cursor,
                view=view,
                severity=severity,
            ),
        )

    async def instance_read(request, alarm_id, *, identity_keys=()):
        scope, rule, *_ = query(request, rule_required=True, identity_keys=identity_keys)

        def get(db):
            value = read_instance(db, scope, rule, alarm_id)
            return {"instance": value, "status": value.status}

        return await read(request, get)

    async def actions_read(request, alarm_id, *, identity_keys=()):
        scope, rule, limit, cursor, _, _ = query(
            request, rule_required=True, page="actions", identity_keys=identity_keys
        )
        return await read(
            request,
            lambda db: read_action_page(
                db, scope, rule, alarm_id, limit=limit, before_action_id=cursor
            ),
        )

    async def receipt_read(request, alarm_id, action_id, *, identity_keys=()):
        scope, rule, *_ = query(request, rule_required=True, identity_keys=identity_keys)
        return await read(
            request, lambda db: read_operator_receipt(db, scope, rule, alarm_id, action_id)
        )

    async def save_action(request, alarm_id=None):
        if request.query_params:
            raise HTTPException(422, "alarm action scope belongs in the immutable intent")
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
            raise AuthorizationError("alarm mutation protection required")
        raw = await request.body()
        if len(raw) > 16_384:
            raise HTTPException(413, "alarm action request too large")
        try:
            intent = OPERATOR_INTENT.validate_json(raw)
            if alarm_id is not None and intent.alarm_id != alarm_id:
                raise ValueError
        except (ValidationError, ValueError):
            raise HTTPException(422, "invalid alarm action intent") from None

        def save():
            with auth.authorized_transaction(
                request.cookies.get(session_cookie),
                permission="alarm_action",
                csrf_token=request.headers.get("x-csrf-token"),
                origin=request.headers.get("origin"),
            ) as tx:
                return submit_operator_action(
                    tx.connection, intent, principal=tx.principal, clock=auth._now
                )

        return await mapped(save)

    @app.get("/api/v1/alarms/{alarm_id}")
    async def instance(request: Request, alarm_id: str):
        return await instance_read(request, alarm_id)

    @app.get("/api/v1/alarm-instance")
    async def instance_alias(request: Request):
        return await instance_read(
            request, request.query_params.get("alarm_id"), identity_keys=("alarm_id",)
        )

    @app.get("/api/v1/alarms/{alarm_id}/actions")
    async def actions(request: Request, alarm_id: str):
        return await actions_read(request, alarm_id)

    @app.get("/api/v1/alarm-actions")
    async def actions_alias(request: Request):
        return await actions_read(
            request, request.query_params.get("alarm_id"), identity_keys=("alarm_id",)
        )

    @app.get("/api/v1/alarms/{alarm_id}/action-receipts/{action_id}")
    async def receipt(request: Request, alarm_id: str, action_id: str):
        return await receipt_read(request, alarm_id, action_id)

    @app.get("/api/v1/alarm-action-receipts")
    async def receipt_alias(request: Request):
        return await receipt_read(
            request,
            request.query_params.get("alarm_id"),
            request.query_params.get("action_id"),
            identity_keys=("alarm_id", "action_id"),
        )

    @app.post("/api/v1/alarms/{alarm_id}/actions")
    async def act(request: Request, alarm_id: str):
        return await save_action(request, alarm_id)

    @app.post("/api/v1/alarm-actions")
    async def act_alias(request: Request):
        return await save_action(request)
