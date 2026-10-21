"""Fair read-only original-run polling and bounded final-evidence delivery."""

from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from apps.gateway.http_client import DeliveryRejected, DeliveryUncertain, IntakeConfig
from contracts.models import Identifier, RunNotFound, RunSummary, RunSummaryRequest


class PendingRuns(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_ids: Annotated[tuple[Identifier, ...], Field(max_length=100)]
    next_cursor: Identifier | None

    @model_validator(mode="after")
    def consistent_page(self):
        if list(self.execution_ids) != sorted(set(self.execution_ids)):
            raise ValueError("Pending executions must be unique and ordered")
        if self.next_cursor is not None and (
            not self.execution_ids or self.next_cursor != self.execution_ids[-1]
        ):
            raise ValueError("Cursor must match the last execution in the page")
        return self


class RunSummaryAck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    committed: Annotated[bool, Field(strict=True)]
    execution_id: Identifier
    status: Literal["PENDING", "SETTLED", "MISMATCH", "CONFLICT"]
    reason_code: Identifier

    @model_validator(mode="after")
    def committed_only(self):
        if not self.committed:
            raise ValueError("Committed final-evidence receipt required")
        return self


class RunSummaryTransport:
    def __init__(self, config: IntakeConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            headers={"Authorization": f"Bearer {config.token}"},
            timeout=config.timeout_seconds,
            follow_redirects=False,
            trust_env=urlsplit(config.base_url).hostname not in {"localhost", "127.0.0.1", "::1"},
            transport=transport,
        )

    async def close(self):
        await self._client.aclose()

    async def _post(self, path, content):
        try:
            async with self._client.stream(
                "POST", path, content=content, headers={"Content-Type": "application/json"}
            ) as response:
                if response.status_code in {400, 401, 403, 409, 413, 422}:
                    raise DeliveryRejected("Final-run transport request rejected")
                if response.status_code != 200:
                    raise DeliveryUncertain("Final-run committed receipt unavailable")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > min(self.config.max_response_bytes, 65536):
                        raise DeliveryUncertain("Final-run response exceeds bound")
                    raw.extend(chunk)
                return bytes(raw)
        except httpx.HTTPError:
            raise DeliveryUncertain("Final-run transport unavailable") from None

    async def pending(self, *, after_execution_id=None, limit=100) -> PendingRuns:
        identifier = TypeAdapter(Identifier | None).validate_python(after_execution_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Pending page limit must be within 1..100")
        import json

        raw = await self._post(
            "/internal/v1/runs/pending",
            json.dumps({"after_execution_id": identifier, "limit": limit}),
        )
        try:
            page = PendingRuns.model_validate_json(raw)
            if len(page.execution_ids) > limit or any(
                identifier is not None and ident <= identifier for ident in page.execution_ids
            ):
                raise ValueError("Pending page did not advance requested cursor")
            return page
        except ValueError:
            raise DeliveryUncertain("Invalid pending-run response") from None

    async def report(self, summary: RunSummary) -> RunSummaryAck:
        value = RunSummary.model_validate_json(summary.model_dump_json())
        if not value.final:
            raise ValueError("Only final run summaries can be reported")
        raw = await self._post("/internal/v1/runs/summary", value.model_dump_json())
        try:
            ack = RunSummaryAck.model_validate_json(raw)
            if ack.execution_id != value.execution_id:
                raise ValueError("Receipt belongs to another original execution")
            return ack
        except ValueError:
            raise DeliveryUncertain("Invalid final-run committed receipt") from None


@dataclass(frozen=True)
class RunSummaryWorkResult:
    state: Literal["idle", "polled", "uncertain", "blocked"]
    queried: int = 0
    reported: int = 0
    skipped: int = 0
    uncertain: int = 0


_RESPONSE = TypeAdapter(RunSummary | RunNotFound)
_ID = TypeAdapter(Identifier)


class RunSummaryWorker:
    """No PLC writes; nonfinal/missing runs are normal and never fabricated finals."""

    def __init__(
        self,
        opc,
        transport: RunSummaryTransport,
        *,
        source_id: str,
        stream_id: str,
        page_limit: int = 100,
    ):
        self.opc, self.transport = opc, transport
        self.source_id, self.stream_id = (
            _ID.validate_python(source_id),
            _ID.validate_python(stream_id),
        )
        if type(page_limit) is not int or not 1 <= page_limit <= 100:
            raise ValueError("Pending page limit must be within 1..100")
        self.page_limit = page_limit
        self.next_cursor = None

    async def run_once(self) -> RunSummaryWorkResult:
        try:
            await self.opc.connect()
        except Exception:
            return RunSummaryWorkResult("blocked")
        try:
            page = await self.transport.pending(
                after_execution_id=self.next_cursor, limit=self.page_limit
            )
            page = PendingRuns.model_validate_json(page.model_dump_json())
        except (DeliveryRejected, DeliveryUncertain, OSError, ValueError):
            return RunSummaryWorkResult("uncertain", uncertain=1)
        # Advance despite nonfinal/erroring old runs so they cannot starve later IDs.
        self.next_cursor = page.next_cursor
        reported = skipped = uncertain = 0
        for execution_id in page.execution_ids:
            try:
                response = await self.opc.get_run_summary(
                    RunSummaryRequest(execution_id=execution_id)
                )
                value = _RESPONSE.validate_json(response.model_dump_json())
                if value.execution_id != execution_id:
                    raise ValueError("OPC response differs from original execution")
                if isinstance(value, RunNotFound):
                    skipped += 1
                    continue
                if (value.source_id, value.stream_id) != (self.source_id, self.stream_id):
                    raise ValueError("OPC response differs from configured source scope")
                if not value.final:
                    skipped += 1
                    continue
                ack = await self.transport.report(value)
                ack = RunSummaryAck.model_validate_json(ack.model_dump_json())
                if ack.execution_id != execution_id:
                    raise ValueError("Receipt differs from original execution")
                reported += 1
            except Exception:
                # Includes OPC read uncertainty; cancellation remains outside Exception.
                uncertain += 1
        return RunSummaryWorkResult(
            "polled" if page.execution_ids else "idle",
            len(page.execution_ids),
            reported,
            skipped,
            uncertain,
        )
