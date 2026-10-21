"""Service-scoped command polling; an uncertain submission is never repeated here."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from apps.gateway.http_client import DeliveryRejected, DeliveryUncertain, IntakeConfig
from apps.gateway.opcua_client import (
    GatewayMethodUncertain,
    GatewayOPCUAClient,
)
from contracts.models import (
    CommandNotFound,
    CommandRequest,
    CommandResult,
    CommandResultRequest,
    CommandSubmission,
    Identifier,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubmitWork(_Strict):
    action: Literal["SUBMIT"]
    request: CommandRequest


class QueryWork(_Strict):
    action: Literal["QUERY"]
    request: CommandResultRequest


CommandWork = Annotated[SubmitWork | QueryWork, Field(discriminator="action")]
_WORK = TypeAdapter(CommandWork)
_RESULT = TypeAdapter(CommandResult | CommandNotFound)


class CommandClaim(_Strict):
    work: CommandWork | None


class CommandReportAck(_Strict):
    committed: Annotated[bool, Field(strict=True)]
    command_id: Identifier

    @model_validator(mode="after")
    def committed_only(self):
        if not self.committed:
            raise ValueError("A committed journal receipt is required")
        return self


class CommandTransport:
    """No request retry: a lost claim response can already have reserved SUBMIT remotely."""

    def __init__(self, config: IntakeConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            headers={"Authorization": f"Bearer {config.token}"},
            timeout=config.timeout_seconds,
            follow_redirects=False,
            trust_env=urlsplit(config.base_url).hostname not in {"127.0.0.1", "localhost", "::1"},
            transport=transport,
        )

    async def close(self):
        await self._client.aclose()

    async def _post(self, path: str, content: str) -> bytes:
        try:
            async with self._client.stream(
                "POST", path, content=content, headers={"Content-Type": "application/json"}
            ) as response:
                if response.status_code in {400, 401, 403, 409, 413, 422}:
                    raise DeliveryRejected("Command journal rejected the service request")
                if response.status_code != 200:
                    raise DeliveryUncertain("No committed command journal response")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > self.config.max_response_bytes:
                        raise DeliveryUncertain("Command journal response exceeds configured bound")
                    raw.extend(chunk)
                return bytes(raw)
        except httpx.HTTPError:
            raise DeliveryUncertain("Command journal response unavailable") from None

    async def claim(self) -> SubmitWork | QueryWork | None:
        raw = await self._post("/internal/v1/commands/claim", "{}")
        try:
            return CommandClaim.model_validate_json(raw).work
        except ValueError:
            raise DeliveryUncertain("Invalid command claim response") from None

    async def _report(self, suffix: str, payload) -> CommandReportAck:
        raw = await self._post("/internal/v1/commands/" + suffix, payload.model_dump_json())
        try:
            ack = CommandReportAck.model_validate_json(raw)
            if ack.command_id != payload.command_id:
                raise ValueError("Journal receipt belongs to another command")
            return ack
        except ValueError:
            raise DeliveryUncertain("No matching committed command journal receipt") from None

    async def report_submission(self, submission: CommandSubmission) -> CommandReportAck:
        valid = CommandSubmission.model_validate_json(submission.model_dump_json())
        return await self._report("submission", valid)

    async def report_result(self, result: CommandResult | CommandNotFound) -> CommandReportAck:
        valid = _RESULT.validate_json(result.model_dump_json())
        return await self._report("result", valid)


@dataclass(frozen=True)
class CommandWorkResult:
    # 'reported' describes journal reporting, never a PLC success verdict.
    state: Literal["idle", "blocked", "deferred", "reported", "uncertain"]
    command_id: str | None = None
    submission_reported: bool = False
    result_reported: bool = False


_UNCERTAIN = (DeliveryRejected, DeliveryUncertain, GatewayMethodUncertain, OSError, ValueError)


class CommandWorker:
    def __init__(
        self,
        opc: GatewayOPCUAClient,
        transport: CommandTransport,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.opc, self.transport, self.clock = opc, transport, clock

    async def run_once(self) -> CommandWorkResult:
        try:
            await self.opc.connect()
            await self.opc.read_tags()
            health = self.opc.health()
            if not health.connected or health.state != "GOOD":
                return CommandWorkResult("blocked")
        except Exception:
            # Includes certificate/session setup failures; cancellation still propagates.
            return CommandWorkResult("blocked")
        try:
            claimed = await self.transport.claim()
            if claimed is None:
                return CommandWorkResult("idle")
            work = _WORK.validate_json(claimed.model_dump_json())
        except _UNCERTAIN:
            return CommandWorkResult("uncertain")
        command_id = work.request.command_id
        submitted = False
        if isinstance(work, SubmitWork):
            now = self.clock()
            if now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("Command worker clock requires a timezone-aware timestamp")
            if now >= work.request.expires_at:
                # The API already reserved OUTCOME_UNKNOWN; only a later QUERY can resolve it.
                return CommandWorkResult("deferred", command_id)
            health = self.opc.health()
            if not health.connected or health.state != "GOOD":
                return CommandWorkResult("deferred", command_id)
            try:
                response = await self.opc.submit_command(work.request)
                submission = CommandSubmission.model_validate_json(response.model_dump_json())
                if submission.command_id != command_id:
                    raise ValueError("OPC submission belongs to another command")
                ack = await self.transport.report_submission(submission)
                ack = CommandReportAck.model_validate_json(ack.model_dump_json())
                submitted = ack.command_id == command_id
            except _UNCERTAIN:
                # No resubmission, including when delivery_possible=False or report ACK was lost.
                pass
        try:
            response = await self.opc.get_command_result(
                CommandResultRequest(command_id=command_id)
            )
            result = _RESULT.validate_json(response.model_dump_json())
            if result.command_id != command_id:
                raise ValueError("OPC result belongs to another command")
            ack = await self.transport.report_result(result)
            ack = CommandReportAck.model_validate_json(ack.model_dump_json())
            if ack.command_id != command_id:
                raise ValueError("Journal receipt belongs to another command")
            return CommandWorkResult("reported", command_id, submitted, True)
        except _UNCERTAIN:
            return CommandWorkResult("uncertain", command_id, submitted, False)
