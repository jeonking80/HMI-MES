"""Authenticated C1 work-order cancellation and passive original-ID recovery."""

from fastapi import FastAPI, HTTPException, Request
from pydantic import TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool

from apps.api.auth import AuthorizationError, UserAuth
from apps.api.work_order_cancel import CancellationIntent, cancel_work_order, recover_cancellation
from apps.api.work_orders import RegistrationConflict, RegistrationNotFound, WorkOrdersUnavailable
from contracts.models import Identifier


def mount_work_order_cancel_routes(app: FastAPI, auth: UserAuth, *, session_cookie="hmi_session"):
    async def mapped(operation):
        try:
            return await run_in_threadpool(operation)
        except RegistrationConflict:
            raise HTTPException(
                409, "cancellation conflicts with original state or intent"
            ) from None
        except RegistrationNotFound:
            raise HTTPException(404, "work order or cancellation not found") from None
        except WorkOrdersUnavailable:
            raise HTTPException(503, "work-order cancellation unavailable") from None

    def identity(value):
        try:
            return TypeAdapter(Identifier).validate_python(value)
        except ValidationError:
            raise HTTPException(422, "invalid cancellation identity") from None

    async def cancel(work_order_id, request, *, query=False):
        work_order_id = identity(work_order_id)
        if request.query_params and not query:
            raise HTTPException(422, "cancellation does not accept query parameters")
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(415, "JSON is required")
        if (
            len(request.headers.getlist("origin")) != 1
            or len(request.headers.getlist("x-csrf-token")) != 1
        ):
            raise AuthorizationError("cancellation mutation protection required")
        raw = await request.body()
        if len(raw) > 16_384:
            raise HTTPException(413, "cancellation request too large")
        try:
            definition = CancellationIntent.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(422, "invalid cancellation intent") from None

        def write():
            with auth.authorized_transaction(
                request.cookies.get(session_cookie),
                permission="production_manage",
                csrf_token=request.headers.get("x-csrf-token"),
                origin=request.headers.get("origin"),
            ) as tx:
                return cancel_work_order(
                    tx.connection,
                    work_order_id,
                    definition,
                    principal=tx.principal,
                    clock=auth._now,
                )

        return await mapped(write)

    async def recover(work_order_id, cancellation_id, request, *, query=False):
        work_order_id, cancellation_id = identity(work_order_id), identity(cancellation_id)
        if request.query_params and not query:
            raise HTTPException(422, "recovery does not accept query parameters")

        def read():
            with auth.authorized_transaction(
                request.cookies.get(session_cookie), permission="read", refresh_idle=False
            ) as tx:
                return recover_cancellation(
                    tx.connection, work_order_id, cancellation_id, principal=tx.principal
                )

        return await mapped(read)

    @app.post("/api/v1/work-orders/{work_order_id}/cancel")
    async def cancel_path(work_order_id: str, request: Request):
        return await cancel(work_order_id, request)

    @app.get("/api/v1/work-orders/{work_order_id}/cancellations/{cancellation_id}")
    async def recover_path(work_order_id: str, cancellation_id: str, request: Request):
        return await recover(work_order_id, cancellation_id, request)

    def query_identity(request, names):
        if set(request.query_params) != set(names) or any(
            len(request.query_params.getlist(name)) != 1 for name in names
        ):
            raise HTTPException(422, "invalid cancellation identity query")
        return {name: identity(request.query_params[name]) for name in names}

    @app.post("/api/v1/work-order-actions/cancel")
    async def cancel_query(request: Request):
        values = query_identity(request, ("work_order_id",))
        return await cancel(values["work_order_id"], request, query=True)

    @app.get("/api/v1/work-order-receipts/cancellation")
    async def recover_query(request: Request):
        values = query_identity(request, ("work_order_id", "cancellation_id"))
        return await recover(
            values["work_order_id"], values["cancellation_id"], request, query=True
        )
