"""Browser command boundary: authorize, validate durable evidence, then journal."""

from __future__ import annotations

from datetime import timedelta

import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from apps.api.auth import AuthorizationError, UserAuth
from apps.api.commands import (
    CommandConflict,
    CommandScopeError,
    command_permission,
    enqueue_command,
    guard_managed_command,
)
from apps.api.database import SCHEMA_LOCK_ID
from apps.api.ingestion import (
    event_inbox,
    production_executions,
    source_streams,
    stream_gaps,
    stream_progress,
)
from apps.api.models import Line
from apps.api.settlement import SettlementScopeError, reconcile_execution
from apps.api.snapshot import SnapshotNotFound, read_snapshot
from contracts.conflict_codes import ConflictReason
from contracts.models import CommandRequest, ContractModel, Identifier, Parameters
from contracts.parameter_history import validate_running_adjustment
from contracts.tags.validate import load_contract

_TAGS = {tag["logical_name"] for tag in load_contract()["tags"]}
_HEARTBEATS = {
    "Line.Heartbeat",
    "Infeed.LinkHeartbeat",
    "Sealer.LinkHeartbeat",
    "Outfeed.LinkHeartbeat",
}


class _Scope(ContractModel):
    line_id: Identifier
    source_id: Identifier
    stream_id: Identifier


class _Submission(_Scope):
    request: CommandRequest


class _Lookup(_Scope):
    command_id: Identifier


def _configured(connection, scope, *, lock=False):
    if lock:
        connection.execute(sa.select(Line.id).where(Line.id == scope.line_id).with_for_update())
    statement = sa.select(source_streams).where(
        source_streams.c.line_id == scope.line_id,
        source_streams.c.source_id == scope.source_id,
        source_streams.c.stream_id == scope.stream_id,
    )
    if lock:
        statement = statement.with_for_update()
    row = connection.execute(statement).mappings().first()
    if row is None:
        raise HTTPException(404, "command scope not found")
    return row


class CommandPreflightConflict(CommandConflict):
    """Browser preflight denial, retaining its fixed public detail."""


def _deny(reason_code=ConflictReason.STATE_NOT_ALLOWED):
    raise CommandPreflightConflict(
        "current evidence does not permit this command", reason_code=reason_code
    )


def _preflight(connection, body, source, now):
    request = body.request
    if (
        abs((now - request.requested_at).total_seconds()) > 2
        or not timedelta(0) < request.expires_at - now <= timedelta(seconds=5)
        or request.expires_at - request.requested_at > timedelta(seconds=5)
    ):
        _deny(ConflictReason.FRESH_SOURCE_REQUIRED)
    try:
        snapshot = read_snapshot(
            connection,
            line_id=body.line_id,
            source_id=body.source_id,
            stream_id=body.stream_id,
            now=now,
            prefer_complete_capture=True,
        )
    except SnapshotNotFound:
        _deny(ConflictReason.FRESH_SOURCE_REQUIRED)
    samples = {sample.tag_id: sample for sample in snapshot.screen.view.samples}
    if (
        snapshot.health.state != "CAPTURE_FRESH"
        or snapshot.health.heartbeat != "GOOD"
        or snapshot.health.source_conflicted
        or snapshot.health.reconciliation_required
        or snapshot.missing_tag_ids
        or set(samples) != _TAGS
        or any(not item.fresh for item in snapshot.evidence)
    ):
        _deny(ConflictReason.FRESH_SOURCE_REQUIRED)
    if any(
        sample.status_code >> 30 != 0
        or sample.reading.value is None
        or sample.source_timestamp is None
        or (sample.source_timestamp - sample.acquired_at).total_seconds() > 2
        or (
            sample.tag_id in _HEARTBEATS
            and abs((sample.acquired_at - sample.source_timestamp).total_seconds()) > 2
        )
        for sample in samples.values()
    ):
        _deny(ConflictReason.FRESH_SOURCE_REQUIRED)
    values = {name: sample.reading.value for name, sample in samples.items()}
    if values["Line.ControlMode"] != 1:
        _deny()
    if values["Line.ControlVersion"] != request.expected_control_version:
        _deny(ConflictReason.CONTROL_VERSION_STALE)
    progress = (
        connection.execute(
            sa.select(stream_progress).where(
                stream_progress.c.source_id == body.source_id,
                stream_progress.c.stream_id == body.stream_id,
            )
        )
        .mappings()
        .first()
    )
    if progress is None or values["Line.EventSeq"] > progress["verified_through_seq"]:
        _deny(ConflictReason.FRESH_SOURCE_REQUIRED)
    if (
        connection.execute(
            sa.select(stream_gaps.c.from_seq)
            .where(
                stream_gaps.c.source_id == body.source_id,
                stream_gaps.c.stream_id == body.stream_id,
            )
            .limit(1)
        ).first()
        or connection.execute(
            sa.select(event_inbox.c.event_id)
            .where(
                event_inbox.c.source_id == body.source_id,
                event_inbox.c.stream_id == body.stream_id,
                event_inbox.c.applied_at.is_(None),
            )
            .limit(1)
        ).first()
    ):
        _deny(ConflictReason.FRESH_SOURCE_REQUIRED)
    execution = None
    if request.execution_id is not None:
        execution = (
            connection.execute(
                sa.select(production_executions).where(
                    production_executions.c.execution_id == request.execution_id,
                    production_executions.c.source_id == body.source_id,
                    production_executions.c.stream_id == body.stream_id,
                )
            )
            .mappings()
            .first()
        )
        if execution is None:
            _deny(ConflictReason.PHYSICAL_EXECUTION_MISMATCH)
    active = values["Line.ActiveExecutionId"] or None
    kind = request.command.kind
    if kind in {"START", "PAUSE", "END_RUN"}:
        if (
            active != request.execution_id
            or execution["counter_epoch"] != values["Line.CounterEpoch"]
        ):
            _deny(ConflictReason.PHYSICAL_EXECUTION_MISMATCH)
    elif kind == "RESET_FAULT":
        if active != request.execution_id:
            _deny(ConflictReason.PHYSICAL_EXECUTION_MISMATCH)
        if execution is not None and execution["counter_epoch"] != values["Line.CounterEpoch"]:
            _deny(ConflictReason.PHYSICAL_EXECUTION_MISMATCH)
    elif kind == "SET_PARAMETERS":
        if active is not None or request.execution_id is not None:
            if (
                execution is None
                or active != request.execution_id
                or execution["counter_epoch"] != values["Line.CounterEpoch"]
            ):
                _deny(ConflictReason.PHYSICAL_EXECUTION_MISMATCH)
            if values["Line.State"] not in {2, 3}:
                _deny()
            args = request.command.args
            if args.recipe_version != execution["recipe_version"]:
                _deny()
            try:
                validate_running_adjustment(
                    args.parameters, Parameters.model_validate(execution["parameters"])
                )
            except ValueError:
                _deny()
    elif kind == "LOAD_JOB":
        if values["Line.State"] != 0 or values["Line.WipCount"] != 0:
            _deny()
        args = request.command.args
        target = connection.execute(
            sa.text("SELECT target_quantity FROM execution_plans WHERE execution_id=:id"),
            {"id": request.execution_id},
        ).scalar_one_or_none()
        if execution["counter_epoch"] != values["Line.CounterEpoch"]:
            _deny(ConflictReason.PHYSICAL_EXECUTION_MISMATCH)
        if (
            target is None
            or target != args.target_quantity
            or execution["work_order_id"] != args.work_order_id
            or execution["recipe_version"] != args.recipe_version
            or execution["parameters"] != args.parameters.model_dump(mode="json")
        ):
            _deny(ConflictReason.ID_CONTENT_CONFLICT)
        if active is not None:
            if active == request.execution_id:
                _deny()
            previous = reconcile_execution(
                connection,
                active,
                authenticated_gateway_id=source["gateway_id"],
                authenticated_source_id=body.source_id,
                authenticated_stream_id=body.stream_id,
                now=now,
            )
            if previous.status != "SETTLED":
                _deny(ConflictReason.RESERVED_EXECUTION_NOT_SETTLED)


def mount_control_routes(
    app: FastAPI, auth: UserAuth, *, session_cookie: str = "hmi_session"
) -> None:
    """Attach to the browser app; existing authentication handlers remain in force."""

    @app.get("/api/v1/commands/{command_id}")
    async def command_read(command_id: str, request: Request):
        try:
            scope = _Lookup.model_validate(
                {
                    "command_id": command_id,
                    **{
                        name: request.query_params.get(name)
                        for name in ("line_id", "source_id", "stream_id")
                    },
                }
            )
        except ValidationError:
            raise HTTPException(422, "invalid command scope") from None

        def read():
            with auth.authorized_transaction(
                request.cookies.get(session_cookie), permission="read", refresh_idle=False
            ) as tx:
                _configured(tx.connection, scope)
                row = (
                    tx.connection.execute(
                        sa.text(
                            "SELECT request,result FROM command_requests WHERE command_id=:id "
                            "AND line_id=:line AND source_id=:source AND stream_id=:stream"
                        ),
                        {
                            "id": scope.command_id,
                            "line": scope.line_id,
                            "source": scope.source_id,
                            "stream": scope.stream_id,
                        },
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    raise HTTPException(404, "command scope not found")
                return dict(row)

        return await run_in_threadpool(read)

    @app.get("/api/v1/command-record")
    async def command_query(request: Request):
        names = {"command_id", "line_id", "source_id", "stream_id"}
        if set(request.query_params) != names or any(
            len(request.query_params.getlist(name)) != 1 for name in names
        ):
            raise HTTPException(422, "invalid command scope query")
        return await command_read(request.query_params["command_id"], request)

    @app.post("/api/v1/commands")
    async def command_submit(request: Request):
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(415, "JSON is required")
        raw = await request.body()
        if len(raw) > 16_384:
            raise HTTPException(413, "request too large")
        try:
            body = _Submission.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(422, "invalid command request") from None
        if (
            len(request.headers.getlist("origin")) != 1
            or len(request.headers.getlist("x-csrf-token")) != 1
        ):
            raise AuthorizationError("command mutation protection required")

        def submit():
            with auth.authorized_transaction(
                request.cookies.get(session_cookie),
                permission=command_permission(body.request),
                csrf_token=request.headers.get("x-csrf-token"),
                origin=request.headers.get("origin"),
            ) as tx:
                if body.request.requested_by != tx.principal.login:
                    raise AuthorizationError("command actor does not match authenticated user")
                db = tx.connection
                db.execute(
                    sa.text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": SCHEMA_LOCK_ID}
                )
                db.execute(
                    sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                    {"key": "api-command:" + body.request.command_id},
                )
                exists = db.execute(
                    sa.text("SELECT 1 FROM command_requests WHERE command_id=:id"),
                    {"id": body.request.command_id},
                ).first()
                if exists is not None:
                    return enqueue_command(
                        db,
                        body.request,
                        principal=tx.principal,
                        line_id=body.line_id,
                        source_id=body.source_id,
                        stream_id=body.stream_id,
                        now=auth._now(),
                    ).model_dump(mode="json")
                source = _configured(db, body, lock=True)
                guard_managed_command(db, body.request, body.line_id)
                _preflight(db, body, source, auth._now())
                result = enqueue_command(
                    db,
                    body.request,
                    principal=tx.principal,
                    line_id=body.line_id,
                    source_id=body.source_id,
                    stream_id=body.stream_id,
                    now=auth._now(),
                )
                return result.model_dump(mode="json")

        try:
            return await run_in_threadpool(submit)
        except CommandPreflightConflict as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "current evidence does not permit this command",
                    "reason_code": exc.reason_code,
                },
            )
        except (CommandConflict, CommandScopeError) as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "command conflicts with current durable evidence",
                    "reason_code": exc.reason_code,
                },
            )
        except SettlementScopeError:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "command conflicts with current durable evidence",
                    "reason_code": ConflictReason.STATE_NOT_ALLOWED,
                },
            )
