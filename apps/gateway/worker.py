"""One bounded collection/delivery cycle; no simulator database or automatic control retry."""

import asyncio
from dataclasses import dataclass
from uuid import uuid4

from pydantic import TypeAdapter

from apps.gateway.buffer import GatewayBuffer
from apps.gateway.http_client import DeliveryRejected, DeliveryUncertain, IntakeClient
from apps.gateway.opcua_client import GatewayMethodUncertain, GatewayOPCUAClient
from contracts.models import UINT64_MAX, EventReadError, Identifier, ReadEventsRequest
from contracts.transport import (
    BatchBody,
    GatewayBatch,
    GatewayCommunication,
    StreamGap,
    batch_digest,
)


class SourceCursorError(ValueError):
    """Configured source identity or persisted cursor requires explicit reconciliation."""


@dataclass(frozen=True)
class CaptureResult:
    batch_id: str
    sample_count: int
    event_count: int
    gap_count: int
    event_query_uncertain: bool


@dataclass(frozen=True)
class CycleResult:
    capture: CaptureResult
    delivered: bool
    delivery_error: str | None


class GatewayPump:
    def __init__(
        self,
        source_id: str,
        stream_id: str,
        *,
        opc: GatewayOPCUAClient,
        buffer: GatewayBuffer,
        intake: IntakeClient,
    ):
        self.source_id = TypeAdapter(Identifier).validate_python(source_id)
        self.stream_id = TypeAdapter(Identifier).validate_python(stream_id)
        self.opc, self.buffer, self.intake = opc, buffer, intake
        self._capture_lock = asyncio.Lock()
        self._delivery_lock = asyncio.Lock()

    def _enqueue(
        self,
        *,
        captured_at,
        communication,
        capture_time=None,
        samples=(),
        events=(),
        gaps=(),
        latest_read_seq,
    ):
        generation, capture = self.buffer.capture_position
        if capture == UINT64_MAX:
            raise SourceCursorError("Capture sequence exhausted; explicit migration required")
        body = BatchBody(
            schema_version="1.2" if capture_time is not None else "1.1",
            **({"capture_time": capture_time} if capture_time is not None else {}),
            communication=communication,
            batch_id=str(uuid4()),
            gateway_id=self.buffer.gateway_id,
            source_id=self.source_id,
            stream_id=self.stream_id,
            capture_generation=generation,
            capture_seq=capture + 1,
            captured_at=captured_at,
            samples=samples,
            events=events,
            gaps=gaps,
        )
        batch = GatewayBatch(body=body, content_sha256=batch_digest(body))
        self.buffer.enqueue(batch, now=captured_at, latest_read_seq=latest_read_seq)
        return batch

    async def capture_once(self) -> CaptureResult:
        async with self._capture_lock:
            observation = await self.opc.read_tags()
            communication = GatewayCommunication(
                state=observation.health.state,
                connected=observation.health.connected,
                read_age_seconds=observation.health.read_age_seconds,
                heartbeat_ages_seconds=dict(observation.health.heartbeat_ages_seconds),
            )
            progress = self.buffer.progress(self.source_id, self.stream_id)
            pending = self.buffer.pending_event_gaps(self.source_id, self.stream_id)
            if pending:
                # Gap report first; an event cannot simultaneously appear as present and missing.
                batch = self._enqueue(
                    captured_at=observation.acquired_at,
                    communication=communication,
                    capture_time=observation.capture_time,
                    samples=observation.samples,
                    gaps=pending,
                    latest_read_seq=progress.latest_read_seq,
                )
                return CaptureResult(
                    batch.body.batch_id, len(observation.samples), 0, len(pending), False
                )
            recoverable = [
                gap for gap in progress.api_progress.unresolved_gaps if not gap.irrecoverable
            ]
            after = recoverable[0].from_seq - 1 if recoverable else progress.latest_read_seq
            events, gaps, uncertain = (), (), False
            latest_read = progress.latest_read_seq
            try:
                page = await self.opc.read_events(
                    ReadEventsRequest(
                        source_id=self.source_id,
                        stream_id=self.stream_id,
                        after_seq=after,
                        limit=1000,
                    )
                )
                if isinstance(page, EventReadError):
                    if page.code != "RETENTION_GAP":
                        raise SourceCursorError(
                            "Source identity/cursor mismatch; no automatic reset"
                        )
                    gaps = (
                        StreamGap(
                            from_seq=page.missing_from_seq,
                            through_seq=page.missing_through_seq,
                            irrecoverable=True,
                            detected_at=observation.acquired_at,
                            reason_code="SOURCE_RETENTION_GAP",
                            reason="Original source explicitly cannot replay this interval",
                        ),
                    )
                    latest_read = max(latest_read, page.missing_through_seq)
                else:
                    events = page.events
                    latest_read = max(latest_read, page.page_last_seq)
            except GatewayMethodUncertain:
                # Preserve observations without inventing events, gaps or cursor progress.
                uncertain = True
            batch = self._enqueue(
                captured_at=observation.acquired_at,
                communication=communication,
                capture_time=observation.capture_time,
                samples=observation.samples,
                events=events,
                gaps=gaps,
                latest_read_seq=latest_read,
            )
            return CaptureResult(
                batch.body.batch_id, len(observation.samples), len(events), len(gaps), uncertain
            )

    async def deliver_once(self) -> bool:
        async with self._delivery_lock:
            queued = self.buffer.lease_next()
            if queued is None:
                return False
            try:
                ack = await self.intake.deliver(queued.batch)
                self.buffer.acknowledge(ack)
                return True
            except BaseException:
                # A cancelled HTTP wait can have committed remotely. Retry the immutable batch ID.
                self.buffer.retry(queued.batch.body.batch_id)
                raise

    async def cycle(self) -> CycleResult:
        """Continue collecting during API failure, preserving the original queued payload."""
        delivered, delivery_error = False, None
        try:
            delivered = await self.deliver_once()
        except (DeliveryUncertain, DeliveryRejected) as error:
            delivery_error = type(error).__name__
        capture = await self.capture_once()
        return CycleResult(capture, delivered, delivery_error)

    async def run(self, stop_event: asyncio.Event, *, on_status=None) -> None:
        """Independent collectors/uploaders: a slow or unavailable API cannot stall polling.

        Callback receives only operation/state/error class, never exception contents or secrets.
        Storage corruption and source identity mismatches stop both loops for reconciliation.
        """
        from apps.gateway.buffer import BufferCorruption
        from apps.gateway.opcua_client import GatewayReadError

        def report(operation, state, error=None):
            if on_status is not None:
                on_status(
                    {
                        "operation": operation,
                        "state": state,
                        "error": type(error).__name__ if error else None,
                    }
                )

        async def pause(seconds):
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=seconds)
            except TimeoutError:
                pass

        async def collect():
            interval = self.opc.config.poll_interval_seconds
            while not stop_event.is_set():
                started = asyncio.get_running_loop().time()
                try:
                    result = await self.capture_once()
                    report("capture", "uncertain" if result.event_query_uncertain else "collected")
                except (BufferCorruption, SourceCursorError):
                    raise
                except (GatewayReadError, GatewayMethodUncertain, OSError, ValueError) as error:
                    report("capture", "blocked", error)
                await pause(max(0.01, interval - (asyncio.get_running_loop().time() - started)))

        async def deliver():
            failures = 0
            while not stop_event.is_set():
                try:
                    sent = await self.deliver_once()
                    failures = 0
                    report("delivery", "committed" if sent else "idle")
                    await pause(0.01 if sent else 0.2)
                except (DeliveryUncertain, DeliveryRejected) as error:
                    failures = min(failures + 1, 4)
                    report("delivery", "blocked", error)
                    await pause(min(5, 2**failures))

        tasks = [
            asyncio.create_task(collect()),
            asyncio.create_task(deliver()),
            asyncio.create_task(stop_event.wait()),
        ]
        try:
            completed, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in completed:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
