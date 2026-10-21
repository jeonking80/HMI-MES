"""Scoped final-run evidence transport; a committed receipt is not settlement."""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from apps.api.database import SCHEMA_LOCK_ID, SUPPORTED_SETTLEMENT_REVISIONS
from apps.api.settlement import SettlementScopeError, record_final_summary
from contracts.models import Identifier, RunSummary


class PendingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    after_execution_id: Identifier | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 100


def mount_settlement_routes(
    app: FastAPI, engine: sa.Engine, authenticate: Callable, clock: Callable[[], datetime]
) -> None:
    """Parent authenticates TLS/bearer; this adapter checks registered DB scope."""

    async def body(request: Request, model):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 65536:
                raise HTTPException(413, "run request exceeds bound")
            raw.extend(chunk)
        try:
            return TypeAdapter(model).validate_json(bytes(raw))
        except ValidationError:
            raise HTTPException(422, "invalid run payload") from None

    def schema(connection):
        connection.execute(
            sa.text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": SCHEMA_LOCK_ID}
        )
        versions = (
            connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()
        )
        if len(versions) != 1 or versions[0] not in SUPPORTED_SETTLEMENT_REVISIONS:
            raise HTTPException(503, "settlement schema unavailable")

    def pending(binding, payload):
        with engine.begin() as connection:
            schema(connection)
            owned = connection.execute(
                sa.text(
                    "SELECT 1 FROM source_streams WHERE gateway_id=:gateway "
                    "AND source_id=:source AND stream_id=:stream"
                ),
                {
                    "gateway": binding.gateway_id,
                    "source": binding.source_id,
                    "stream": binding.stream_id,
                },
            ).first()
            if owned is None:
                raise SettlementScopeError("Source is not registered to authenticated gateway")
            rows = (
                connection.execute(
                    sa.text(
                        "SELECT e.execution_id FROM production_executions e "
                        "LEFT JOIN run_reconciliations r USING(execution_id) "
                        "WHERE e.source_id=:source AND e.stream_id=:stream "
                        "AND (r.status IS NULL OR r.status<>'SETTLED') "
                        "AND (CAST(:after AS text) IS NULL OR e.execution_id "
                        'COLLATE "C">CAST(:after AS text) '
                        'COLLATE "C") ORDER BY e.execution_id COLLATE "C" LIMIT :limit'
                    ),
                    {
                        "source": binding.source_id,
                        "stream": binding.stream_id,
                        "after": payload.after_execution_id,
                        "limit": payload.limit + 1,
                    },
                )
                .scalars()
                .all()
            )
            ids = rows[: payload.limit]
            result = {
                "execution_ids": ids,
                "next_cursor": ids[-1] if len(rows) > payload.limit else None,
            }
        return result

    def summary(binding, payload):
        with engine.begin() as connection:
            schema(connection)
            result = record_final_summary(
                connection,
                payload,
                authenticated_gateway_id=binding.gateway_id,
                authenticated_source_id=binding.source_id,
                authenticated_stream_id=binding.stream_id,
                now=clock(),
            )
        # Both final evidence and reconciliation must COMMIT before this receipt.
        return {
            "committed": True,
            "execution_id": result.execution_id,
            "status": result.status,
            "reason_code": result.reason_code,
        }

    async def execute(operation, binding, payload):
        try:
            return await run_in_threadpool(operation, binding, payload)
        except SettlementScopeError:
            raise HTTPException(403, "run source scope is not allowed") from None
        except SQLAlchemyError:
            raise HTTPException(503, "settlement unavailable") from None
        except ValueError:
            raise HTTPException(422, "invalid run payload") from None

    @app.post("/internal/v1/runs/pending")
    async def pending_runs(request: Request):
        binding = authenticate(request)
        payload = await body(request, PendingRequest)
        return await execute(pending, binding, payload)

    @app.post("/internal/v1/runs/summary")
    async def final_summary(request: Request):
        binding = authenticate(request)
        payload = await body(request, RunSummary)
        if not payload.final:
            raise HTTPException(422, "final run evidence required")
        return await execute(summary, binding, payload)
