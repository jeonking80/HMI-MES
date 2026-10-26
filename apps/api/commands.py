"""Durable command journal and one-time handoff reservation; no network I/O."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

import sqlalchemy as sa
from pydantic import TypeAdapter

from apps.api.auth import PERMISSIONS, AuthorizationError, Principal
from apps.api.database import SUPPORTED_WORKFLOW_REVISIONS
from apps.api.ingestion import production_executions, source_streams
from apps.api.models import Line
from contracts.conflict_codes import ConflictError, ConflictReason
from contracts.models import (
    CommandNotFound,
    CommandRequest,
    CommandResult,
    CommandResultRequest,
    CommandSubmission,
    ContractModel,
    Identifier,
    UTCTimestamp,
    canonical_command_payload,
    canonical_command_retry_payload,
    materialize_load_job_policy,
)

_PERMISSION = {
    "START": "operate",
    "PAUSE": "operate",
    "RESET_FAULT": "operate",
    "LOAD_JOB": "load_job",
    "END_RUN": "end_run",
    "SET_PARAMETERS": "parameters_manage",
}
_ID = TypeAdapter(Identifier)
_TIME = TypeAdapter(UTCTimestamp)


class CommandConflict(ConflictError):
    """ID content/scope collision or unresolved command blocks new unsafe work."""

    default_reason_code = ConflictReason.ID_CONTENT_CONFLICT


class CommandScopeError(ConflictError):
    """Command source, target or execution is outside the registered line."""


def command_permission(request: CommandRequest) -> str:
    return _PERMISSION[request.command.kind]


def enqueue_command(
    connection: sa.Connection,
    request: CommandRequest,
    *,
    principal: Principal,
    line_id: str,
    source_id: str,
    stream_id: str,
    now: datetime,
    _managed_binding: bool = False,
) -> CommandResult:
    """Atomically journal on the caller's authorized transaction connection.

    Caller must obtain this connection/principal from authorized_transaction with
    command_permission(request), CSRF and Origin. This method rechecks actor and
    roles, but cannot authenticate a Principal constructed by an untrusted caller.
    """
    from apps.api.recovery import recovery_required

    if recovery_required(connection):
        raise CommandScopeError("RECOVERY_REQUIRED: restored database control is restricted")
    request = CommandRequest.model_validate_json(request.model_dump_json())
    now = _TIME.validate_python(now)
    line_id, source_id, stream_id = (
        _ID.validate_python(value) for value in (line_id, source_id, stream_id)
    )
    if (
        request.requested_by != principal.login
        or principal.must_change_password
        or not principal.roles & PERMISSIONS[command_permission(request)]
        or (
            request.command.kind == "LOAD_JOB"
            and request.command.args.start_when_ready
            and not principal.roles & PERMISSIONS["operate"]
        )
    ):
        raise AuthorizationError("Command actor or permission does not match authenticated user")
    canonical = canonical_command_payload(request)
    # Global identity serialization also protects duplicate requests on different
    # sources. Transaction-scoped advisory locks disappear on rollback/crash.
    connection.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
        {"key": "api-command:" + request.command_id},
    )
    existing = (
        connection.execute(
            sa.text(
                "SELECT line_id,source_id,stream_id,canonical_payload,result FROM command_requests "
                "WHERE command_id=:id"
            ),
            {"id": request.command_id},
        )
        .mappings()
        .first()
    )
    if existing is not None:
        canonical = canonical_command_retry_payload(request, existing["canonical_payload"])
        if (
            existing["line_id"],
            existing["source_id"],
            existing["stream_id"],
            existing["canonical_payload"],
        ) != (line_id, source_id, stream_id, canonical):
            raise CommandConflict("Command ID already belongs to different content or scope")
        return CommandResult.model_validate(existing["result"])
    request = materialize_load_job_policy(request)
    canonical = canonical_command_payload(request)
    # Ingestion/configuration also lock line before source. Taking source first
    # would invert that order when INSERT's line FK later takes a key-share lock.
    connection.execute(sa.select(Line.id).where(Line.id == line_id).with_for_update())
    source = (
        connection.execute(
            sa.select(source_streams)
            .where(
                source_streams.c.source_id == source_id,
                source_streams.c.stream_id == stream_id,
                source_streams.c.line_id == line_id,
            )
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if source is None or request.target_id != "Line":
        raise CommandScopeError("Command requires a registered source line and PLC target Line")
    if request.execution_id is not None:
        execution = connection.execute(
            sa.select(production_executions.c.execution_id).where(
                production_executions.c.execution_id == request.execution_id,
                production_executions.c.source_id == source_id,
                production_executions.c.stream_id == stream_id,
            )
        ).first()
        if execution is None:
            raise CommandScopeError("Original execution is not registered to this source/stream")
    guard_managed_command(connection, request, line_id, managed_binding=_managed_binding)
    expired = now >= request.expires_at
    if not expired and request.command.kind in {"START", "LOAD_JOB", "SET_PARAMETERS"}:
        unresolved = connection.execute(
            sa.text(
                "SELECT command_id FROM command_requests WHERE line_id=:line AND (status IN "
                "('REQUESTED','DELIVERED','ACCEPTED','OUTCOME_UNKNOWN') "
                "OR collision_latched) LIMIT 1"
            ),
            {"line": line_id},
        ).first()
        if unresolved is not None:
            raise CommandConflict(
                "Unresolved line command prevents new start/job/settings",
                reason_code=ConflictReason.STATE_NOT_ALLOWED,
            )
    result = CommandResult(
        command_id=request.command_id,
        status="EXPIRED" if expired else "REQUESTED",
        updated_at=now,
        completed_at=now if expired else None,
        reason_code="DELIVERY_TTL_EXPIRED" if expired else None,
        reason="Request expired before delivery" if expired else None,
    )
    connection.execute(
        sa.text(
            "INSERT INTO command_requests(command_id,line_id,source_id,stream_id,target_id,"
            "execution_id,actor,kind,request,canonical_payload,result,status,"
            "created_at,expires_at) "
            "VALUES (:id,:line,:source,:stream,:target,:execution,:actor,:kind,"
            "CAST(:request AS jsonb),:canonical,CAST(:result AS jsonb),:status,:now,:expires)"
        ),
        {
            "id": request.command_id,
            "line": line_id,
            "source": source_id,
            "stream": stream_id,
            "target": request.target_id,
            "execution": request.execution_id,
            "actor": principal.login,
            "kind": request.command.kind,
            "request": request.model_dump_json(),
            "canonical": canonical,
            "result": result.model_dump_json(),
            "status": result.status,
            "now": now,
            "expires": request.expires_at,
        },
    )
    connection.execute(
        sa.text(
            "INSERT INTO command_request_audit(command_id,actor,action,occurred_at) "
            "VALUES (:id,:actor,:action,:now)"
        ),
        {"id": request.command_id, "actor": principal.login, "action": result.status, "now": now},
    )
    return result


def guard_managed_command(connection, request, line_id, *, managed_binding=False):
    """Caller already holds line lock. Never take a work-order advisory lock here."""
    revisions = (
        connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()
    )
    if len(revisions) != 1 or revisions[0] not in SUPPORTED_WORKFLOW_REVISIONS:
        return
    if request.command.kind not in {"LOAD_JOB", "START"}:
        return
    reserved = connection.execute(
        sa.text("SELECT execution_id FROM line_execution_reservations WHERE line_id=:line"),
        {"line": line_id},
    ).scalar_one_or_none()
    if reserved is not None and reserved != request.execution_id:
        raise CommandConflict(
            "Another execution owns the line reservation",
            reason_code=ConflictReason.PHYSICAL_EXECUTION_MISMATCH,
        )
    linked = connection.execute(
        sa.text("SELECT line_id FROM work_order_execution_links WHERE execution_id=:id"),
        {"id": request.execution_id},
    ).scalar_one_or_none()
    if request.command.kind == "START" and linked is not None and reserved != request.execution_id:
        raise CommandConflict(
            "Managed execution no longer owns the line reservation",
            reason_code=ConflictReason.PHYSICAL_EXECUTION_MISMATCH,
        )
    if request.command.kind == "LOAD_JOB" and not managed_binding:
        catalog = connection.execute(
            sa.text("SELECT 1 FROM work_orders WHERE id=:id"),
            {"id": request.command.args.work_order_id},
        ).first()
        if linked is not None or catalog is not None:
            raise CommandConflict(
                "Managed work orders require the binding workflow",
                reason_code=ConflictReason.STATE_NOT_ALLOWED,
            )


class SubmitWork(ContractModel):
    action: Literal["SUBMIT"] = "SUBMIT"
    request: CommandRequest


class QueryWork(ContractModel):
    action: Literal["QUERY"] = "QUERY"
    request: CommandResultRequest


_TERMINAL = {"SUCCEEDED", "FAILED", "REJECTED", "EXPIRED"}


def _gateway_scope(connection, gateway, source, stream):
    from apps.api.recovery import recovery_required

    if recovery_required(connection):
        raise CommandScopeError("RECOVERY_REQUIRED: restored database control is restricted")
    gateway, source, stream = (_ID.validate_python(value) for value in (gateway, source, stream))
    row = (
        connection.execute(
            sa.select(source_streams).where(
                source_streams.c.gateway_id == gateway,
                source_streams.c.source_id == source,
                source_streams.c.stream_id == stream,
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise CommandScopeError("Authenticated gateway does not own registered source/stream")
    return row


def _gateway_command(connection, command_id, gateway, source, stream):
    connection.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
        {"key": "api-command:" + command_id},
    )
    row = (
        connection.execute(
            sa.text("SELECT * FROM command_requests WHERE command_id=:id"), {"id": command_id}
        )
        .mappings()
        .first()
    )
    if row is None or (row["source_id"], row["stream_id"]) != (source, stream):
        raise CommandScopeError("Command ID is not journaled in authenticated source/stream")
    connection.execute(sa.select(Line.id).where(Line.id == row["line_id"]).with_for_update())
    binding = (
        connection.execute(
            sa.select(source_streams)
            .where(
                source_streams.c.source_id == source,
                source_streams.c.stream_id == stream,
            )
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if binding is None or (binding["gateway_id"], binding["line_id"]) != (gateway, row["line_id"]):
        raise CommandScopeError("Current gateway binding does not match command line")
    return (
        connection.execute(
            sa.text("SELECT * FROM command_requests WHERE command_id=:id"), {"id": command_id}
        )
        .mappings()
        .one()
    )


def _record(connection, command_id, actor, action, now, evidence, result=None):
    if result is not None:
        connection.execute(
            sa.text(
                "UPDATE command_requests SET result=CAST(:result AS jsonb),status=:status "
                "WHERE command_id=:id"
            ),
            {"id": command_id, "result": result.model_dump_json(), "status": result.status},
        )
    return connection.execute(
        sa.text(
            "INSERT INTO command_request_audit(command_id,actor,action,occurred_at,evidence) "
            "VALUES (:id,:actor,:action,:now,CAST(:evidence AS jsonb)) RETURNING id"
        ),
        {
            "id": command_id,
            "actor": actor,
            "action": action,
            "now": now,
            "evidence": json.dumps(evidence),
        },
    ).scalar_one()


def claim_next(
    connection: sa.Connection,
    *,
    authenticated_gateway_id: str,
    authenticated_source_id: str,
    authenticated_stream_id: str,
    now: datetime,
) -> SubmitWork | QueryWork | None:
    """Reserve at most one submission, committed before any external handoff.

    Use one journal operation per caller transaction. A lost committed SUBMIT
    response is deliberately query-only forever; reservation is never a lease.
    """
    now = _TIME.validate_python(now)
    gateway, source, stream = (
        authenticated_gateway_id,
        authenticated_source_id,
        authenticated_stream_id,
    )
    _gateway_scope(connection, gateway, source, stream)
    candidate = connection.execute(
        sa.text(
            "SELECT command_id FROM command_requests WHERE source_id=:source AND stream_id=:stream "
            "AND status IN ('REQUESTED','DELIVERED','ACCEPTED','OUTCOME_UNKNOWN') "
            "ORDER BY CASE WHEN status='REQUESTED' THEN 0 ELSE 1 END, "
            "last_poll_order,created_at,command_id LIMIT 1"
        ),
        {"source": source, "stream": stream},
    ).scalar_one_or_none()
    if candidate is None:
        return None
    row = _gateway_command(connection, candidate, gateway, source, stream)
    old = CommandResult.model_validate(row["result"])
    if old.status in _TERMINAL:
        return None
    request = CommandRequest.model_validate(row["request"])
    if old.status == "REQUESTED":
        if now >= request.expires_at:
            result = CommandResult(
                command_id=candidate,
                status="EXPIRED",
                updated_at=now,
                completed_at=now,
                reason_code="DELIVERY_TTL_EXPIRED",
                reason="Request expired before dispatch reservation",
            )
            _record(
                connection,
                candidate,
                gateway,
                "EXPIRED",
                now,
                result.model_dump(mode="json"),
                result,
            )
            return None
        # Existing DTO requires delivered_at for UNKNOWN. This is the possible
        # handoff boundary, NOT proof of delivery; real PLC evidence replaces it.
        result = CommandResult(
            command_id=candidate,
            status="OUTCOME_UNKNOWN",
            updated_at=now,
            delivered_at=now,
            reason_code="DISPATCH_RESERVED",
            reason="Possible handoff reserved; query original ID after response loss",
        )
        poll_order = _record(
            connection,
            candidate,
            gateway,
            "DISPATCH_RESERVED",
            now,
            {"possible_handoff_at": now.isoformat()},
            result,
        )
        work = SubmitWork(request=request)
    else:
        poll_order = _record(
            connection, candidate, gateway, "QUERY", now, {"command_id": candidate}
        )
        work = QueryWork(request=CommandResultRequest(command_id=candidate))
    connection.execute(
        sa.text(
            "UPDATE command_requests SET last_polled_at=:now,last_poll_order=:ordering "
            "WHERE command_id=:id"
        ),
        {"now": now, "ordering": poll_order, "id": candidate},
    )
    return work


def apply_submission(
    connection: sa.Connection,
    submission: CommandSubmission,
    *,
    authenticated_gateway_id: str,
    authenticated_source_id: str,
    authenticated_stream_id: str,
    now: datetime,
) -> CommandResult:
    submission = CommandSubmission.model_validate_json(submission.model_dump_json())
    collision = (
        submission.disposition == "REJECTED" and submission.reason_code == "COMMAND_ID_CONFLICT"
    )
    result = CommandResult(
        command_id=submission.command_id,
        status=(
            "OUTCOME_UNKNOWN"
            if collision
            else "ACCEPTED"
            if submission.disposition == "ACCEPTED"
            else "REJECTED"
        ),
        updated_at=submission.received_at,
        delivered_at=submission.received_at,
        completed_at=(
            submission.received_at
            if submission.disposition == "REJECTED" and not collision
            else None
        ),
        reason_code=submission.reason_code,
        reason=submission.reason,
    )
    return _apply_remote(
        connection,
        result,
        action="SUBMISSION_RECORDED",
        evidence=submission.model_dump(mode="json"),
        gateway=authenticated_gateway_id,
        source=authenticated_source_id,
        stream=authenticated_stream_id,
        now=now,
    )


def apply_result(
    connection: sa.Connection,
    result: CommandResult | CommandNotFound,
    *,
    authenticated_gateway_id: str,
    authenticated_source_id: str,
    authenticated_stream_id: str,
    now: datetime,
) -> CommandResult:
    if isinstance(result, CommandNotFound):
        result = CommandNotFound.model_validate_json(result.model_dump_json())
    elif isinstance(result, CommandResult):
        result = CommandResult.model_validate_json(result.model_dump_json())
    else:
        raise TypeError("Expected validated PLC CommandResult or CommandNotFound")
    return _apply_remote(
        connection,
        result,
        action="RESULT_RECORDED",
        evidence=result.model_dump(mode="json"),
        gateway=authenticated_gateway_id,
        source=authenticated_source_id,
        stream=authenticated_stream_id,
        now=now,
    )


def _apply_remote(connection, remote, *, action, evidence, gateway, source, stream, now):
    now = _TIME.validate_python(now)
    _gateway_scope(connection, gateway, source, stream)
    row = _gateway_command(connection, remote.command_id, gateway, source, stream)
    old = CommandResult.model_validate(row["result"])
    if isinstance(remote, CommandResult):
        if remote.status not in {"ACCEPTED", "SUCCEEDED", "FAILED", "REJECTED", "OUTCOME_UNKNOWN"}:
            raise CommandConflict("Status is not a PLC submission or execution outcome")
        if remote.evidence is not None and remote.evidence.execution_id != row["execution_id"]:
            raise CommandScopeError("PLC completion evidence names another original execution")
    else:
        action = "RESULT_NOT_FOUND"
    collision = (
        action == "SUBMISSION_RECORDED"
        and evidence.get("disposition") == "REJECTED"
        and evidence.get("reason_code") == "COMMAND_ID_CONFLICT"
    )
    if old.status in _TERMINAL:
        if isinstance(remote, CommandResult) and remote.status in _TERMINAL and remote != old:
            raise CommandConflict("Terminal command result is immutable")
        if collision:
            connection.execute(
                sa.text("UPDATE command_requests SET collision_latched=true WHERE command_id=:id"),
                {"id": old.command_id},
            )
        _record(connection, old.command_id, gateway, action, now, evidence)
        return old
    if old.status == "REQUESTED":
        raise CommandConflict("PLC response requires an existing dispatch reservation")
    # An ID collision is not rejection of this original canonical request.
    # GetCommandResult(ID) may subsequently describe the OTHER payload, even
    # with identical execution_id. No automatic result can clear this latch.
    if row["collision_latched"]:
        _record(connection, old.command_id, gateway, action, now, evidence)
        return old
    if collision:
        connection.execute(
            sa.text("UPDATE command_requests SET collision_latched=true WHERE command_id=:id"),
            {"id": old.command_id},
        )
        _record(connection, old.command_id, gateway, action, now, evidence, remote)
        return remote
    if isinstance(remote, CommandNotFound):
        result = CommandResult(
            command_id=old.command_id,
            status="OUTCOME_UNKNOWN",
            updated_at=old.updated_at,
            delivered_at=old.delivered_at,
            reason_code="PLC_COMMAND_NOT_FOUND",
            reason="PLC has no result for original ID; possible handoff remains unresolved",
        )
        _record(connection, old.command_id, gateway, "RESULT_NOT_FOUND", now, evidence, result)
        return result
    # Priority is based on state, not comparing gateway/API/PLC wall clocks.
    # Reservation UNKNOWN can become ACCEPTED; reconciled UNKNOWN never regresses
    # to a delayed acceptance, and terminal results are always immutable above.
    if (
        old.status == "OUTCOME_UNKNOWN"
        and old.reason_code != "DISPATCH_RESERVED"
        and remote.status == "ACCEPTED"
    ):
        _record(connection, old.command_id, gateway, action, now, evidence)
        return old
    if old.status == "ACCEPTED" and remote.status == "ACCEPTED":
        _record(connection, old.command_id, gateway, action, now, evidence)
        return old
    if old == remote:
        _record(connection, old.command_id, gateway, action, now, evidence)
        return old
    _record(connection, old.command_id, gateway, action, now, evidence, remote)
    return remote
