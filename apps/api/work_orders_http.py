"""Authenticated immutable registration, publication, and durable LOAD binding API."""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool

from apps.api.auth import AuthorizationError, UserAuth
from apps.api.commands import CommandConflict, CommandScopeError
from apps.api.control_http import CommandPreflightConflict
from apps.api.ingestion import IngestConflict
from apps.api.registration_ids import (
    RegistrationInputError,
    recover_registration_receipt,
    register_recipe_request,
    register_work_order_request,
)
from apps.api.registration_requests import RecipeRegistrationRequest, WorkOrderRegistrationRequest
from apps.api.settlement import SettlementScopeError
from apps.api.work_order_workflow import (
    BindIntent,
    ReleaseIntent,
    bind_work_order,
    recover_work_order_receipt,
    release_work_order,
)
from apps.api.work_orders import (
    RegistrationConflict,
    RegistrationNotFound,
    WorkOrdersUnavailable,
    get_recipe_version,
    get_work_order,
    list_registrations,
)
from contracts.conflict_codes import ConflictReason
from contracts.models import Identifier


def mount_work_order_routes(
    app: FastAPI, auth: UserAuth, *, session_cookie: str = "hmi_session"
) -> None:
    def query_identity(request, names):
        if set(request.query_params) != set(names) or any(
            len(request.query_params.getlist(name)) != 1 for name in names
        ):
            raise HTTPException(422, "invalid identity query")
        try:
            return {
                name: TypeAdapter(Identifier).validate_python(request.query_params[name])
                for name in names
            }
        except ValidationError:
            raise HTTPException(422, "invalid identity query") from None

    async def mapped(operation):
        try:
            return await run_in_threadpool(operation)
        except CommandPreflightConflict as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "current evidence does not permit this command",
                    "reason_code": exc.reason_code,
                },
            )
        except (RegistrationConflict, CommandConflict, CommandScopeError) as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "registration conflicts with immutable content",
                    "reason_code": exc.reason_code,
                },
            )
        except (SettlementScopeError, IngestConflict) as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "registration conflicts with immutable content",
                    "reason_code": (
                        ConflictReason.ID_CONTENT_CONFLICT
                        if isinstance(exc, IngestConflict)
                        else ConflictReason.STATE_NOT_ALLOWED
                    ),
                },
            )
        except RegistrationNotFound:
            raise HTTPException(404, "registration or reference not found") from None
        except RegistrationInputError:
            raise HTTPException(422, "invalid generated registration identity") from None
        except WorkOrdersUnavailable:
            raise HTTPException(503, "work-order registration unavailable") from None

    async def create(request, kind):
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(415, "JSON is required")
        if (
            len(request.headers.getlist("origin")) != 1
            or len(request.headers.getlist("x-csrf-token")) != 1
        ):
            raise AuthorizationError("registration mutation protection required")
        raw = await request.body()
        if len(raw) > 16_384:
            raise HTTPException(413, "registration request too large")
        model = RecipeRegistrationRequest if kind == "recipe" else WorkOrderRegistrationRequest
        try:
            definition = model.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(422, "invalid registration") from None

        def save():
            permission = "parameters_manage" if kind == "recipe" else "production_manage"
            register = register_recipe_request if kind == "recipe" else register_work_order_request
            with auth.authorized_transaction(
                request.cookies.get(session_cookie),
                permission=permission,
                csrf_token=request.headers.get("x-csrf-token"),
                origin=request.headers.get("origin"),
            ) as tx:
                return register(
                    tx.connection, definition, actor=tx.principal.login, now=auth._now()
                ).model_dump(mode="json")

        return await mapped(save)

    async def read_one(request, identity, kind):
        def read():
            get = get_recipe_version if kind == "recipe" else get_work_order
            with auth.authorized_transaction(
                request.cookies.get(session_cookie), permission="read"
            ) as tx:
                return get(tx.connection, identity).model_dump(mode="json")

        return await mapped(read)

    async def registration_receipt(request, kind):
        values = query_identity(request, ("request_id",))

        def read():
            with auth.authorized_transaction(
                request.cookies.get(session_cookie), permission="read", refresh_idle=False
            ) as tx:
                return recover_registration_receipt(
                    tx.connection,
                    kind=kind,
                    request_id=values["request_id"],
                    actor=tx.principal.login,
                )

        result = await mapped(read)
        if isinstance(result, JSONResponse):
            result.headers["Cache-Control"] = "no-store"
            return result
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/registration-receipts/recipe")
    async def recipe_registration_receipt(request: Request):
        return await registration_receipt(request, "recipe")

    @app.get("/api/v1/registration-receipts/work-order")
    async def work_order_registration_receipt(request: Request):
        return await registration_receipt(request, "work_order")

    async def workflow(request, identity, kind):
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(415, "JSON is required")
        if (
            len(request.headers.getlist("origin")) != 1
            or len(request.headers.getlist("x-csrf-token")) != 1
        ):
            raise AuthorizationError("workflow mutation protection required")
        raw = await request.body()
        if len(raw) > 16_384:
            raise HTTPException(413, "workflow request too large")
        try:
            intent = (ReleaseIntent if kind == "release" else BindIntent).model_validate_json(raw)
        except ValidationError:
            raise HTTPException(422, "invalid workflow intent") from None

        def save():
            with auth.authorized_transaction(
                request.cookies.get(session_cookie),
                permission="production_manage" if kind == "release" else "load_job",
                csrf_token=request.headers.get("x-csrf-token"),
                origin=request.headers.get("origin"),
            ) as tx:
                action = release_work_order if kind == "release" else bind_work_order
                return action(
                    tx.connection, identity, intent, principal=tx.principal, clock=auth._now
                )

        return await mapped(save)

    @app.post("/api/v1/work-orders/{identity}/release")
    async def release(request: Request, identity: str):
        return await workflow(request, identity, "release")

    @app.post("/api/v1/work-orders/{identity}/bind")
    async def bind(request: Request, identity: str):
        return await workflow(request, identity, "bind")

    async def recover(request, identity, original_id, kind, *, query=False):
        if request.query_params and not query:
            raise HTTPException(422, "recovery does not accept query parameters")
        try:
            TypeAdapter(Identifier).validate_python(identity)
            TypeAdapter(Identifier).validate_python(original_id)
        except ValidationError:
            raise HTTPException(422, "invalid recovery identity") from None

        def read():
            with auth.authorized_transaction(
                request.cookies.get(session_cookie), permission="read", refresh_idle=False
            ) as tx:
                return recover_work_order_receipt(
                    tx.connection, identity, original_id, kind=kind, principal=tx.principal
                )

        return JSONResponse(await mapped(read), headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/work-orders/{identity}/releases/{release_id}")
    async def original_release(request: Request, identity: str, release_id: str):
        return await recover(request, identity, release_id, "release")

    @app.get("/api/v1/work-orders/{identity}/bindings/{command_id}")
    async def original_binding(request: Request, identity: str, command_id: str):
        return await recover(request, identity, command_id, "binding")

    async def read_page(request, kind):
        allowed = {"after_id", "limit", "item_id"} | (
            {"line_id"} if kind == "work_order" else set()
        )
        if set(request.query_params) - allowed or any(
            len(request.query_params.getlist(name)) != 1 for name in request.query_params
        ):
            raise HTTPException(422, "invalid registration filters")
        try:
            limit = int(request.query_params.get("limit", "50"))
        except ValueError:
            raise HTTPException(422, "invalid registration limit") from None

        def read():
            with auth.authorized_transaction(
                request.cookies.get(session_cookie), permission="read"
            ) as tx:
                return list_registrations(
                    tx.connection,
                    kind=kind,
                    limit=limit,
                    after_id=request.query_params.get("after_id"),
                    item_id=request.query_params.get("item_id"),
                    line_id=request.query_params.get("line_id"),
                )

        return await mapped(read)

    @app.post("/api/v1/recipe-versions")
    async def create_recipe(request: Request):
        return await create(request, "recipe")

    @app.get("/api/v1/recipe-versions")
    async def recipes(request: Request):
        return await read_page(request, "recipe")

    @app.get("/api/v1/recipe-versions/{identity}")
    async def recipe(request: Request, identity: str):
        return await read_one(request, identity, "recipe")

    @app.post("/api/v1/work-orders")
    async def create_order(request: Request):
        return await create(request, "work_order")

    @app.get("/api/v1/work-orders")
    async def orders(request: Request):
        return await read_page(request, "work_order")

    @app.get("/api/v1/work-orders/{identity}")
    async def order(request: Request, identity: str):
        return await read_one(request, identity, "work_order")

    @app.get("/api/v1/recipe-version-detail")
    async def recipe_query(request: Request):
        values = query_identity(request, ("recipe_version_id",))
        return await read_one(request, values["recipe_version_id"], "recipe")

    @app.get("/api/v1/work-order-detail")
    async def order_query(request: Request):
        values = query_identity(request, ("work_order_id",))
        return await read_one(request, values["work_order_id"], "work_order")

    @app.post("/api/v1/work-order-actions/release")
    async def release_query(request: Request):
        values = query_identity(request, ("work_order_id",))
        return await workflow(request, values["work_order_id"], "release")

    @app.post("/api/v1/work-order-actions/bind")
    async def bind_query(request: Request):
        values = query_identity(request, ("work_order_id",))
        return await workflow(request, values["work_order_id"], "bind")

    @app.get("/api/v1/work-order-receipts/release")
    async def release_receipt_query(request: Request):
        values = query_identity(request, ("work_order_id", "release_id"))
        return await recover(
            request, values["work_order_id"], values["release_id"], "release", query=True
        )

    @app.get("/api/v1/work-order-receipts/binding")
    async def binding_receipt_query(request: Request):
        values = query_identity(request, ("work_order_id", "command_id"))
        return await recover(
            request, values["work_order_id"], values["command_id"], "binding", query=True
        )
