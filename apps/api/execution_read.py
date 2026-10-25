"""One-statement passive execution evidence; never reconciliation or control permission."""

from typing import Annotated, Literal

from pydantic import Field, ValidationError
from sqlalchemy import Connection, text

from apps.api.database import SUPPORTED_WORKFLOW_REVISIONS
from apps.api.ingestion import _exact
from apps.api.parameter_history import validate_parameter_evidence
from apps.api.work_order_workflow import BindingRecovery
from apps.api.work_orders import WorkOrdersUnavailable, _ready
from contracts.models import (
    CommandRequest,
    CommandResult,
    ContractModel,
    Identifier,
    Parameters,
    Reason,
    RunSummary,
    StrictBool,
    UInt64,
    UTCTimestamp,
)


class ExecutionNotFound(LookupError):
    pass


class ExecutionScope(ContractModel):
    line_id: Identifier
    source_id: Identifier
    stream_id: Identifier


class ExecutionLookup(ExecutionScope):
    execution_id: Identifier


class ExecutionIdentity(ContractModel):
    execution_id: Identifier
    work_order_id: Identifier
    recipe_version: Identifier
    parameters: Parameters
    counter_epoch: Identifier
    registered_at: UTCTimestamp
    target_quantity: Annotated[int, Field(strict=True, gt=0, le=2**32 - 1)] | None


class ExecutionTotals(ContractModel):
    good_quantity: UInt64
    reject_quantity: UInt64
    scrap_quantity: UInt64


class SourceEvidence(ContractModel):
    gateway_id: Identifier
    capture_generation: UInt64
    conflicted: StrictBool
    reconciliation_required: StrictBool
    verified_through_seq: UInt64
    latest_ingested_seq: UInt64
    control_restricted: StrictBool


class StoredSettlement(ContractModel):
    status: Literal["PENDING", "SETTLED", "MISMATCH", "CONFLICT"]
    reason_code: Reason
    checked_at: UTCTimestamp | None
    received_at: UTCTimestamp | None
    summary: RunSummary | None


class OriginalLoad(ContractModel):
    request: CommandRequest
    result: CommandResult
    collision_latched: StrictBool


class ExecutionView(ContractModel):
    execution: ExecutionIdentity
    scope: ExecutionScope
    binding: BindingRecovery | None
    load: OriginalLoad | None
    settlement: StoredSettlement
    totals: ExecutionTotals
    source: SourceEvidence


# All business evidence comes from one PostgreSQL READ COMMITTED statement snapshot.
_SELECT = text("""
SELECT e.*, s.line_id, s.gateway_id, s.capture_generation, s.conflicted,
 s.reconciliation_required, p.target_quantity,
 t.good_quantity,t.reject_quantity,t.scrap_quantity,
 progress.verified_through_seq,progress.latest_ingested_seq,
 links.execution_id AS linked_execution_id,links.work_order_id AS linked_work_order_id,
 links.line_id AS linked_line_id,links.source_id AS linked_source_id,
 links.stream_id AS linked_stream_id,links.load_command_id,
 links.original_intent,links.original_request,links.receipt AS binding_receipt,
 c.request AS load_request,c.result AS load_result,c.collision_latched,
 r.status AS settlement_status,r.reason_code,r.checked_at,r.received_at,r.final_summary,
 COALESCE((SELECT jsonb_agg(jsonb_build_object(
  'event_id',i.event_id,'source_id',i.source_id,'stream_id',i.stream_id,
  'execution_id',i.execution_id,'kind',i.kind,'seq',i.seq,'body',i.body,
  'content_sha256',i.content_sha256,'applied',i.applied_at IS NOT NULL) ORDER BY i.seq)
  FROM event_inbox i WHERE i.source_id=e.source_id AND i.stream_id=e.stream_id
   AND (i.execution_id=e.execution_id OR i.body->>'execution_id'=e.execution_id)
   AND (i.kind='PARAMETERS_CHANGED' OR i.body->>'kind'='PARAMETERS_CHANGED')
   AND i.seq <= (r.final_summary->>'last_event_seq')::numeric), '[]'::jsonb)
 AS parameter_evidence
FROM production_executions e
JOIN source_streams s ON s.source_id=e.source_id AND s.stream_id=e.stream_id
LEFT JOIN execution_totals t ON t.execution_id=e.execution_id
LEFT JOIN execution_plans p ON p.execution_id=e.execution_id
LEFT JOIN stream_progress progress ON progress.source_id=e.source_id
 AND progress.stream_id=e.stream_id
LEFT JOIN work_order_execution_links links ON links.execution_id=e.execution_id
LEFT JOIN command_requests c ON c.command_id=links.load_command_id
LEFT JOIN run_reconciliations r ON r.execution_id=e.execution_id
WHERE e.execution_id=:execution_id AND e.source_id=:source_id
 AND e.stream_id=:stream_id AND s.line_id=:line_id
""")


def read_execution(db: Connection, lookup: ExecutionLookup) -> dict:
    lookup = ExecutionLookup.model_validate_json(lookup.model_dump_json())
    _ready(db)
    revisions = db.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    if len(revisions) != 1 or revisions[0] not in SUPPORTED_WORKFLOW_REVISIONS:
        raise WorkOrdersUnavailable("Execution read schema required")
    row = db.execute(_SELECT, lookup.model_dump()).mappings().first()
    if row is None:
        raise ExecutionNotFound("Execution not found in exact scope")
    try:
        return _view(row, lookup)
    except (ValueError, TypeError, KeyError, ValidationError):
        raise WorkOrdersUnavailable("Stored execution evidence unavailable") from None


def _view(row, lookup):
    identity = ExecutionIdentity.model_validate(
        {key: row[key] for key in ExecutionIdentity.model_fields}
    )
    totals = ExecutionTotals(**{key: _exact(row[key]) for key in ExecutionTotals.model_fields})
    verified, latest = _exact(row["verified_through_seq"]), _exact(row["latest_ingested_seq"])
    if verified > latest:
        raise ValueError("Invalid progress evidence")
    source = SourceEvidence(
        gateway_id=row["gateway_id"],
        capture_generation=_exact(row["capture_generation"]),
        conflicted=row["conflicted"],
        reconciliation_required=row["reconciliation_required"],
        verified_through_seq=verified,
        latest_ingested_seq=latest,
        control_restricted=row["conflicted"] or row["reconciliation_required"] or verified < latest,
    )
    binding = load = None
    if row["linked_execution_id"] is not None:
        binding = BindingRecovery.model_validate(
            {"intent": row["original_intent"], "receipt": row["binding_receipt"]}
        )
        load = OriginalLoad(
            request=row["load_request"],
            result=row["load_result"],
            collision_latched=row["collision_latched"],
        )
        original, receipt, request = binding.intent, binding.receipt, load.request
        if not (
            row["linked_execution_id"]
            == receipt.execution_id
            == request.execution_id
            == identity.execution_id
            and row["linked_work_order_id"]
            == original.work_order_id
            == receipt.work_order_id
            == identity.work_order_id
            and row["load_command_id"]
            == original.command_id
            == receipt.command_id
            == request.command_id
            == load.result.command_id
            and original.actor == request.requested_by
            and original.expected_control_version == request.expected_control_version
            and original.expected_work_order_version + 1 == receipt.work_order_row_version
            and row["original_request"]
            == row["load_request"]
            == row["binding_receipt"]["load_request"]
            and all(
                getattr(original, key)
                == getattr(receipt, key)
                == getattr(lookup, key)
                == row["linked_" + key]
                for key in ("line_id", "source_id", "stream_id")
            )
            and request.command.kind == "LOAD_JOB"
            and request.command.args.work_order_id == identity.work_order_id
            and request.command.args.recipe_version == identity.recipe_version
            and request.command.args.parameters == identity.parameters
            and request.command.args.target_quantity == identity.target_quantity
        ):
            raise ValueError("Incoherent managed binding")
        if load.result.status == "SUCCEEDED" and (
            load.result.evidence.execution_id != identity.execution_id
            or load.result.evidence.recipe_version != identity.recipe_version
        ):
            raise ValueError("Original LOAD success belongs to different execution")
    settlement = StoredSettlement(
        status=row["settlement_status"] or "PENDING",
        reason_code=row["reason_code"] if row["settlement_status"] else "FINAL_SUMMARY_REQUIRED",
        checked_at=row["checked_at"],
        received_at=row["received_at"],
        summary=row["final_summary"],
    )
    summary = settlement.summary
    if row["settlement_status"] is not None and (
        summary is None or settlement.checked_at is None or settlement.received_at is None
    ):
        raise ValueError("Incomplete stored settlement")
    if summary is not None:
        if (summary.execution_id, summary.source_id, summary.stream_id) != (
            identity.execution_id,
            lookup.source_id,
            lookup.stream_id,
        ):
            raise ValueError("Stored summary belongs to another scope")
        if settlement.status == "SETTLED" and not (
            summary.final
            and summary.counter_epoch == identity.counter_epoch
            and summary.recipe_version == identity.recipe_version
        ):
            raise ValueError("Settled summary does not match original execution")
        if settlement.status == "SETTLED":
            validate_parameter_evidence(
                summary, identity.parameters, identity.recipe_version, row["parameter_evidence"]
            )
    result = ExecutionView(
        execution=identity,
        scope=ExecutionScope(**lookup.model_dump(exclude={"execution_id"})),
        binding=binding,
        load=load,
        settlement=settlement,
        totals=totals,
        source=source,
    ).model_dump(mode="json")
    if binding is not None:
        result["binding"] = {"intent": row["original_intent"], "receipt": row["binding_receipt"]}
        result["load"]["request"] = row["load_request"]
        result["load"]["result"] = row["load_result"]
    return result
