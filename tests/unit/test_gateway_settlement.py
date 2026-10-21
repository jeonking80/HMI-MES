"""Injected read-only OPC and HTTP transport checks; no live-network claims."""

import asyncio
import json
import secrets
from datetime import UTC, datetime

import httpx
import pytest

from apps.gateway.http_client import DeliveryRejected, DeliveryUncertain, IntakeConfig
from apps.gateway.settlement import (
    PendingRuns,
    RunSummaryAck,
    RunSummaryTransport,
    RunSummaryWorker,
)
from contracts.models import RunNotFound, RunSummary

AT = datetime(2026, 10, 21, tzinfo=UTC)


def summary(execution_id="original", *, final=True, source_id="source"):
    return RunSummary(
        source_id=source_id,
        stream_id="stream",
        execution_id=execution_id,
        state="ENDED" if final else "RUNNING",
        as_of=AT,
        ended_at=AT if final else None,
        counter_epoch="epoch",
        recipe_version="recipe",
        parameters={"temperature_c": 150.0, "speed_m_per_min": 6.0},
        input_quantity=1,
        good_quantity=1,
        reject_quantity=0,
        scrap_quantity=0,
        wip_quantity=0,
        last_event_seq=1,
        final=final,
    )


def config(**changes):
    return IntakeConfig(base_url="https://intake.test", token=secrets.token_urlsafe(32), **changes)


def ack(execution_id="original"):
    return RunSummaryAck(
        committed=True, execution_id=execution_id, status="PENDING", reason_code="EVENTS_PENDING"
    )


class OPC:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def connect(self):
        self.calls.append("connect")

    async def get_run_summary(self, request):
        self.calls.append(("GetRunSummary", request.execution_id))
        value = self.responses[request.execution_id]
        if isinstance(value, BaseException):
            raise value
        return value


class Journal:
    def __init__(self, pages):
        self.pages = pages
        self.cursors = []
        self.reports = []
        self.lose_ack = False

    async def pending(self, *, after_execution_id, limit):
        self.cursors.append(after_execution_id)
        return self.pages[after_execution_id]

    async def report(self, value):
        self.reports.append(value.model_dump_json())
        if self.lose_ack:
            self.lose_ack = False
            raise DeliveryUncertain("receipt lost")
        return ack(value.execution_id)


def worker(opc, journal):
    return RunSummaryWorker(opc, journal, source_id="source", stream_id="stream", page_limit=2)


async def test_lost_ack_retries_original_final_not_a_new_execution():
    opc = OPC({"original": summary()})
    journal = Journal({None: PendingRuns(execution_ids=["original"], next_cursor=None)})
    journal.lose_ack = True
    polling = worker(opc, journal)
    first = await polling.run_once()
    assert first.uncertain == 1 and first.reported == 0
    second = await polling.run_once()
    assert second.reported == 1
    assert journal.reports[0] == journal.reports[1]
    assert opc.calls == ["connect", ("GetRunSummary", "original")] * 2


async def test_cursor_fairness_nonfinal_and_missing_are_normal_skips():
    opc = OPC(
        {
            "active": summary("active", final=False),
            "original": summary(),
            "missing": RunNotFound(code="RUN_NOT_FOUND", execution_id="missing", reason="gone"),
        }
    )
    journal = Journal(
        {
            None: PendingRuns(execution_ids=["active", "missing"], next_cursor="missing"),
            "missing": PendingRuns(execution_ids=["original"], next_cursor=None),
        }
    )
    polling = worker(opc, journal)
    first = await polling.run_once()
    assert first.skipped == 2 and first.uncertain == 0 and not journal.reports
    assert (await polling.run_once()).reported == 1
    assert (await polling.run_once()).skipped == 2
    assert journal.cursors == [None, "missing", None]


@pytest.mark.parametrize(
    "value",
    [
        summary("foreign"),
        summary(source_id="foreign"),
        RunNotFound(code="RUN_NOT_FOUND", execution_id="foreign", reason="gone"),
    ],
)
async def test_opc_identity_mismatch_never_reports(value):
    opc = OPC({"original": value})
    journal = Journal({None: PendingRuns(execution_ids=["original"], next_cursor=None)})
    result = await worker(opc, journal).run_once()
    assert result.uncertain == 1 and not journal.reports


async def test_cancellation_propagates_and_never_reports():
    opc = OPC({"original": asyncio.CancelledError()})
    journal = Journal({None: PendingRuns(execution_ids=["original"], next_cursor=None)})
    with pytest.raises(asyncio.CancelledError):
        await worker(opc, journal).run_once()
    assert not journal.reports


async def test_real_transport_bounds_strict_acks_and_auth_header():
    cfg = config()
    requests = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer " + cfg.token
        requests.append(request.url.path)
        if request.url.path.endswith("pending"):
            assert json.loads(request.content) == {"after_execution_id": None, "limit": 2}
            return httpx.Response(200, json={"execution_ids": ["original"], "next_cursor": None})
        body = RunSummary.model_validate_json(request.content)
        return httpx.Response(200, json=ack(body.execution_id).model_dump(mode="json"))

    transport = RunSummaryTransport(cfg, transport=httpx.MockTransport(handler))
    try:
        assert (await transport.pending(limit=2)).execution_ids == ("original",)
        assert (await transport.report(summary())).status == "PENDING"
        with pytest.raises(ValueError):
            await transport.report(summary(final=False))
    finally:
        await transport.close()
    assert requests == ["/internal/v1/runs/pending", "/internal/v1/runs/summary"]


@pytest.mark.parametrize(
    "body",
    [
        {
            "committed": False,
            "execution_id": "original",
            "status": "PENDING",
            "reason_code": "WAIT",
        },
        {"committed": "true", "execution_id": "original", "status": "SETTLED", "reason_code": "OK"},
        {"committed": True, "execution_id": "foreign", "status": "SETTLED", "reason_code": "OK"},
        {
            "committed": True,
            "execution_id": "original",
            "status": "SETTLED",
            "reason_code": "OK",
            "extra": 1,
        },
    ],
)
async def test_false_mismatched_and_extra_ack_are_uncertain(body):
    transport = RunSummaryTransport(
        config(), transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    )
    try:
        with pytest.raises(DeliveryUncertain):
            await transport.report(summary())
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "body",
    [
        {"execution_ids": ["z", "a"], "next_cursor": "a"},
        {"execution_ids": ["a", "a"], "next_cursor": None},
        {"execution_ids": ["a"], "next_cursor": "b"},
        {"execution_ids": ["a", "b", "c"], "next_cursor": None},
    ],
)
async def test_invalid_or_oversized_pending_pages_are_uncertain(body):
    transport = RunSummaryTransport(
        config(), transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    )
    try:
        with pytest.raises(DeliveryUncertain):
            await transport.pending(limit=2)
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "status,body,error",
    [
        (503, b"failure", DeliveryUncertain),
        (403, b"rejected", DeliveryRejected),
        (200, b"x" * 1025, DeliveryUncertain),
    ],
)
async def test_http_rejection_failure_and_response_bound(status, body, error):
    transport = RunSummaryTransport(
        config(max_response_bytes=1024),
        transport=httpx.MockTransport(lambda request: httpx.Response(status, content=body)),
    )
    try:
        with pytest.raises(error):
            await transport.report(summary())
    finally:
        await transport.close()
