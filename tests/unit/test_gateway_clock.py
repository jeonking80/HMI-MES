"""Injected measured probes, uninterrupted collection and durable immutable envelopes."""

import asyncio
import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from apps.gateway.__main__ import parser
from apps.gateway.buffer import BatchConflict, BufferCorruption, GatewayBuffer
from apps.gateway.http_client import DeliveryRejected, DeliveryUncertain, IntakeClient, IntakeConfig
from apps.gateway.opcua_client import GatewayHealth, GatewayObservation, GatewayOPCUAClient
from apps.gateway.worker import GatewayPump
from contracts.clock_evidence import ClockProbeRequest, ClockProbeResponse, MeasuredClockEvidence
from contracts.models import EventPage
from contracts.transport import CaptureTime, GatewayBatch, TagSample, gateway_batch_digest
from tests.integration.test_gateway_buffer import AT, make_ack, make_batch


def evidence(**changes):
    return MeasuredClockEvidence(
        **dict(
            gateway_id="gateway-A",
            source_id="source-A",
            stream_id="stream-A",
            capture_generation=1,
            clock_epoch="epoch",
            t0_utc=AT,
            t0_monotonic_seconds=100.0,
            nonce="single-use-nonce",
            api_utc=AT - timedelta(seconds=3.7),
            t1_utc=AT + timedelta(seconds=0.1),
            t1_monotonic_seconds=100.1,
        )
        | changes
    )


class Clocks:
    def __init__(self):
        self.seconds = 0.0

    def utc(self):
        return AT + timedelta(seconds=self.seconds)

    def mono(self):
        return 100.0 + self.seconds


class OPC(GatewayOPCUAClient):
    def __init__(self, clocks):
        super().__init__(
            SimpleNamespace(poll_interval_seconds=0.01),
            utc_clock=clocks.utc,
            monotonic_clock=clocks.mono,
        )
        self.clocks = clocks

    async def read_tags(self):
        self.clocks.seconds += 0.02
        epoch, at, mono = self.clock_reading()
        sample = TagSample(
            sample_id=f"sample-{mono}",
            tag_id="Sealer.TemperaturePV",
            acquired_at=at,
            source_timestamp=at,
            server_timestamp=None,
            status_code=0,
            reading=dict(data_type="Double", value=150.0),
        )
        return GatewayObservation(
            at,
            (sample,),
            GatewayHealth(
                "GOOD",
                True,
                0.0,
                tuple(
                    (tag, 0.0)
                    for tag in (
                        "Line.Heartbeat",
                        "Infeed.LinkHeartbeat",
                        "Sealer.LinkHeartbeat",
                        "Outfeed.LinkHeartbeat",
                    )
                ),
            ),
            CaptureTime(clock_epoch=epoch, monotonic_seconds=mono, utc_read_span_seconds=0.0),
        )

    async def read_events(self, request):
        return EventPage(
            source_id="source-A",
            stream_id="stream-A",
            after_seq=request.after_seq,
            page_last_seq=request.after_seq,
            retained_from_seq=1,
            retained_through_seq=request.after_seq,
            events=(),
            has_more=False,
        )


class Intake:
    def __init__(self, clocks):
        self.clocks = clocks
        self.calls = 0

    async def clock_probe(self, request):
        self.calls += 1
        self.clocks.seconds += 0.1
        return ClockProbeResponse(nonce=f"nonce-{self.calls}", api_utc=AT)

    async def deliver(self, batch):
        return make_ack(batch)


def test_wire_bodyhash_unchanged_and_proof_committed():
    original = make_batch()
    measured = original.model_copy(update={"clock_evidence": evidence()})
    assert measured.content_sha256 == original.content_sha256
    assert measured.body == original.body
    assert "clock_evidence" not in original.model_dump(mode="json")
    assert gateway_batch_digest(measured) != gateway_batch_digest(original)
    assert GatewayBatch.model_validate_json(measured.model_dump_json()) == measured
    with pytest.raises(ValidationError):
        evidence(t1_monotonic_seconds=float("inf"))
    option = next(
        a
        for a in parser()._subparsers._group_actions[0].choices["serve"]._actions
        if a.dest == "clock_measured_round_trip"
    )
    assert option.default is False


async def test_probe_next_capture_single_use_and_default_off(tmp_path):
    clocks = Clocks()
    opc, intake = OPC(clocks), Intake(clocks)
    with GatewayBuffer(tmp_path / "queue", gateway_id="gateway-A") as buffer:
        pump = GatewayPump("source-A", "stream-A", opc=opc, buffer=buffer, intake=intake)
        assert await pump.probe_once() is False
        pump.measured_clock = True
        await pump.capture_once()  # Already queued values can never gain a later probe.
        assert await pump.probe_once()
        assert not await pump.probe_once()
        await pump.capture_once()
        await pump.capture_once()
        batches = [buffer.lease_next()]
        buffer.acknowledge(make_ack(batches[0].batch))
        second = buffer.lease_next().batch
        assert second.clock_evidence is not None
        assert second.body.captured_at >= second.clock_evidence.t1_utc
        assert (
            second.body.capture_time.monotonic_seconds >= second.clock_evidence.t1_monotonic_seconds
        )
        assert batches[0].batch.clock_evidence is None
        buffer.acknowledge(make_ack(second))
        assert buffer.lease_next().batch.clock_evidence is None
        assert intake.calls == 1


async def test_hanging_probe_and_delivery_never_block_collection(tmp_path):
    clocks = Clocks()
    intake = Intake(clocks)
    started = asyncio.Event()

    async def blocked(*args):
        started.set()
        await asyncio.Event().wait()

    intake.clock_probe = blocked
    intake.deliver = blocked
    with GatewayBuffer(tmp_path / "queue", gateway_id="gateway-A") as buffer:
        pump = GatewayPump(
            "source-A",
            "stream-A",
            opc=OPC(clocks),
            buffer=buffer,
            intake=intake,
            measured_clock=True,
        )
        stop = asyncio.Event()
        collected = []

        def status(value):
            if value["operation"] == "capture" and value["state"] == "collected":
                collected.append(value)
                if len(collected) >= 4:
                    stop.set()

        await asyncio.wait_for(pump.run(stop, on_status=status), timeout=1)
        assert started.is_set()
        assert len(collected) >= 4
        assert buffer.capture_position[1] >= 4


@pytest.mark.parametrize("change", ["epoch", "stale", "slow"])
async def test_probe_invalidated_before_capture(tmp_path, change):
    clocks = Clocks()
    opc, intake = OPC(clocks), Intake(clocks)
    with GatewayBuffer(tmp_path / "queue", gateway_id="gateway-A") as buffer:
        pump = GatewayPump(
            "source-A", "stream-A", opc=opc, buffer=buffer, intake=intake, measured_clock=True
        )
        if change == "slow":

            async def slow(request):
                clocks.seconds += 3
                return ClockProbeResponse(nonce="late", api_utc=AT)

            intake.clock_probe = slow
            assert await pump.probe_once() is False
        else:
            assert await pump.probe_once()
            if change == "epoch":
                opc._clock_epoch = "changed-epoch"
            else:
                clocks.seconds += 61
        await pump.capture_once()
        assert buffer.lease_next().batch.clock_evidence is None


@pytest.mark.parametrize("acknowledged", [False, True])
def test_envelope_change_rejected_after_reopen_and_ack(tmp_path, acknowledged):
    path = tmp_path / "queue"
    original = make_batch().model_copy(update={"clock_evidence": evidence()})
    with GatewayBuffer(path, gateway_id="gateway-A") as buffer:
        buffer.enqueue(original, now=AT)
        if acknowledged:
            buffer.acknowledge(make_ack(original))
    with GatewayBuffer(path) as buffer:
        assert buffer.enqueue(original, now=AT).duplicate
        for replacement in [None, evidence(nonce="replacement")]:
            with pytest.raises(BatchConflict):
                buffer.enqueue(original.model_copy(update={"clock_evidence": replacement}), now=AT)


def test_queued_proof_tampering_detected_on_reopen(tmp_path):
    path = tmp_path / "queue"
    with GatewayBuffer(path, gateway_id="gateway-A") as buffer:
        buffer.enqueue(make_batch().model_copy(update={"clock_evidence": evidence()}), now=AT)
    with sqlite3.connect(path) as db:
        value = json.loads(db.execute("SELECT body FROM outbound_batches").fetchone()[0])
        value["clock_evidence"]["nonce"] = "changed-nonce"
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        db.execute(
            "UPDATE outbound_batches SET body=?,accounted_bytes=?",
            (encoded, len(encoded.encode()) + 512),
        )
    with pytest.raises(BufferCorruption):
        GatewayBuffer(path)


@pytest.mark.parametrize("acknowledged", [False, True])
def test_legacy_buffer_upgrades_only_known_absent_proof(tmp_path, acknowledged):
    path = tmp_path / "queue"
    original = make_batch()
    with GatewayBuffer(path, gateway_id="gateway-A") as buffer:
        buffer.enqueue(original, now=AT)
        if acknowledged:
            buffer.acknowledge(make_ack(original))
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE batch_ledger DROP COLUMN envelope_sha256")
        db.execute("DROP TABLE ledger_checkpoint")
        db.execute("PRAGMA user_version=1")
    with GatewayBuffer(path) as buffer:
        assert buffer.enqueue(original, now=AT).duplicate
        assert buffer._db.execute("SELECT envelope_sha256 FROM batch_ledger").fetchone()[0] == (
            gateway_batch_digest(original)
        )
        with pytest.raises(BatchConflict):
            buffer.enqueue(original.model_copy(update={"clock_evidence": evidence()}), now=AT)


async def test_probe_http_scope_bounded_and_secret_free():
    seen = []

    async def handler(request):
        assert request.url.path == "/internal/v1/clock-probes"
        assert request.headers["authorization"] == "Bearer " + "x" * 40
        seen.append(ClockProbeRequest.model_validate_json(request.content))
        return httpx.Response(
            200, json=ClockProbeResponse(nonce="n", api_utc=AT).model_dump(mode="json")
        )

    config = IntakeConfig("https://intake.example", "x" * 40)
    client = IntakeClient(config, transport=httpx.MockTransport(handler))
    request = ClockProbeRequest(
        **evidence().model_dump(include=set(ClockProbeRequest.model_fields))
    )
    try:
        assert (await client.clock_probe(request)).nonce == "n"
        assert seen == [request]
    finally:
        await client.close()
    for status, payload in [(401, b"secret"), (302, b"secret"), (200, b"x" * 9000)]:
        client = IntakeClient(
            config,
            transport=httpx.MockTransport(
                lambda request, status=status, payload=payload: httpx.Response(
                    status, content=payload
                )
            ),
        )
        try:
            with pytest.raises((DeliveryRejected, DeliveryUncertain)) as caught:
                await client.clock_probe(request)
            assert "secret" not in str(caught.value)
        finally:
            await client.close()


@pytest.mark.parametrize("proof_present", [False, True])
def test_buffer_never_guesses_unknown_existing_proof_identity(tmp_path, proof_present):
    path = tmp_path / "queue"
    value = make_batch().model_copy(
        update={"clock_evidence": evidence() if proof_present else None}
    )
    with GatewayBuffer(path, gateway_id="gateway-A") as buffer:
        buffer.enqueue(value, now=AT)
    with sqlite3.connect(path) as db:
        if proof_present:
            # A supposedly legacy ledger cannot contain a new measured envelope.
            db.execute("ALTER TABLE batch_ledger DROP COLUMN envelope_sha256")
        else:
            # An existing upgraded column with unknown identity cannot be backfilled.
            db.execute("UPDATE batch_ledger SET envelope_sha256=?", ("x" * 64,))
    with pytest.raises(BufferCorruption):
        GatewayBuffer(path)


async def test_probe_completed_during_capture_waits_for_next_capture(tmp_path):
    clocks = Clocks()
    intake, opc = Intake(clocks), OPC(clocks)
    with GatewayBuffer(tmp_path / "queue", gateway_id="gateway-A") as buffer:
        pump = GatewayPump(
            "source-A", "stream-A", opc=opc, buffer=buffer, intake=intake, measured_clock=True
        )
        original = opc.read_tags

        async def interrupted():
            assert await pump.probe_once()
            return await original()

        opc.read_tags = interrupted
        await pump.capture_once()
        first = buffer.lease_next().batch
        assert first.clock_evidence is None
        buffer.acknowledge(make_ack(first))
        opc.read_tags = original
        await pump.capture_once()
        assert buffer.lease_next().batch.clock_evidence is not None


async def test_probe_http_has_hard_timeout_without_secret_error():
    async def blocked(request):
        await asyncio.Event().wait()

    client = IntakeClient(
        IntakeConfig("https://intake.example", "x" * 40, timeout_seconds=0.01),
        transport=httpx.MockTransport(blocked),
    )
    request = ClockProbeRequest(
        **evidence().model_dump(include=set(ClockProbeRequest.model_fields))
    )
    try:
        with pytest.raises(DeliveryUncertain, match="No valid clock probe response"):
            await asyncio.wait_for(client.clock_probe(request), timeout=0.5)
    finally:
        await client.close()


def test_probe_uses_opc_clock_epoch_and_detects_monotonic_reset():
    clocks = Clocks()
    opc = OPC(clocks)
    epoch, utc, mono = opc.clock_reading()
    assert utc == clocks.utc() and mono == clocks.mono()
    clocks.seconds = -1
    with pytest.raises(ValueError, match="regressed"):
        opc.clock_reading()
    clocks.seconds = 1
    assert opc.clock_reading()[0] != epoch
