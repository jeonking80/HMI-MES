"""Injected OPC/HTTP unit tests; these do not establish network integration."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from apps.gateway.commands import (
    CommandReportAck,
    CommandTransport,
    CommandWorker,
    QueryWork,
    SubmitWork,
)
from apps.gateway.http_client import DeliveryRejected, DeliveryUncertain, IntakeConfig
from apps.gateway.opcua_client import GatewayHealth, GatewayMethodUncertain
from contracts.models import (
    CommandNotFound,
    CommandRequest,
    CommandResult,
    CommandResultRequest,
    CommandSubmission,
)

AT = datetime(2026, 10, 21, tzinfo=UTC)


def request():
    return CommandRequest(
        command_id="original-command",
        target_id="Line",
        execution_id="execution",
        command={"kind": "PAUSE"},
        requested_by="operator",
        requested_at=AT,
        expires_at=AT + timedelta(seconds=10),
        expected_control_version=1,
    )


def submission(command_id="original-command"):
    return CommandSubmission(command_id=command_id, disposition="ACCEPTED", received_at=AT)


def result(command_id="original-command"):
    return CommandResult(command_id=command_id, status="ACCEPTED", updated_at=AT, delivered_at=AT)


class OPC:
    def __init__(self):
        self.calls = []
        self.current_health = GatewayHealth("GOOD", True, 0, ())
        self.submit_error = None
        self.submit_response = submission()
        self.result_response = result()
        self.submit_entered = None
        self.submit_release = None

    async def connect(self):
        self.calls.append("connect")

    async def read_tags(self):
        self.calls.append("read")

    def health(self):
        return self.current_health

    async def submit_command(self, command):
        self.calls.append(("submit", command.command_id))
        if self.submit_entered is not None:
            self.submit_entered.set()
            await self.submit_release.wait()
        if self.submit_error:
            raise self.submit_error
        return self.submit_response

    async def get_command_result(self, command):
        self.calls.append(("query", command.command_id))
        return self.result_response


class Journal:
    def __init__(self, opc, *, submit=True):
        self.opc = opc
        self.work = [SubmitWork(action="SUBMIT", request=request())] if submit else []
        self.work.append(
            QueryWork(action="QUERY", request=CommandResultRequest(command_id="original-command"))
        )
        self.reports = []
        self.report_loss = False
        self.on_claim = None
        self.wrong_ack = False

    async def claim(self):
        assert self.opc.calls[-2:] == ["connect", "read"]
        work = self.work.pop(0) if self.work else None
        if self.on_claim:
            self.on_claim()
        return work

    async def _report(self, kind, response):
        self.reports.append((kind, response))
        if self.report_loss:
            raise DeliveryUncertain("unit injected response loss")
        return CommandReportAck(
            committed=True, command_id="other" if self.wrong_ack else response.command_id
        )

    async def report_submission(self, response):
        return await self._report("submission", response)

    async def report_result(self, response):
        return await self._report("result", response)


async def test_submission_report_and_query_use_original_id_without_resubmission():
    opc = OPC()
    journal = Journal(opc)
    worker = CommandWorker(opc, journal, clock=lambda: AT)
    first = await worker.run_once()
    assert first.state == "reported" and first.submission_reported and first.result_reported
    assert [kind for kind, _ in journal.reports] == ["submission", "result"]
    assert journal.reports[0][1] == submission()
    assert journal.reports[1][1] == result()
    assert (await worker.run_once()).state == "reported"
    assert opc.calls.count(("submit", "original-command")) == 1
    assert opc.calls.count(("query", "original-command")) == 2


async def test_report_loss_never_repeats_submit_and_requeries_original_id():
    opc = OPC()
    journal = Journal(opc)
    journal.report_loss = True
    worker = CommandWorker(opc, journal, clock=lambda: AT)
    assert (await worker.run_once()).state == "uncertain"
    journal.report_loss = False
    assert (await worker.run_once()).state == "reported"
    assert opc.calls.count(("submit", "original-command")) == 1
    assert opc.calls.count(("query", "original-command")) == 2


@pytest.mark.parametrize("delivery_possible", [False, True])
async def test_opc_submission_uncertainty_only_queries_original_id(delivery_possible):
    opc = OPC()
    opc.submit_error = GatewayMethodUncertain(
        "SubmitCommand", "original-command", delivery_possible=delivery_possible
    )
    opc.result_response = CommandNotFound(
        command_id="original-command", code="COMMAND_NOT_FOUND", reason="No original result"
    )
    journal = Journal(opc)
    worker = CommandWorker(opc, journal, clock=lambda: AT)
    assert (await worker.run_once()).state == "reported"
    assert journal.reports == [("result", opc.result_response)]
    await worker.run_once()
    assert opc.calls.count(("submit", "original-command")) == 1


async def test_ttl_elapsing_during_claim_does_not_submit_or_fabricate_terminal_result():
    opc = OPC()
    journal = Journal(opc)
    clock = [AT]
    journal.on_claim = lambda: clock.__setitem__(0, AT + timedelta(seconds=10))
    worker = CommandWorker(opc, journal, clock=lambda: clock[0])
    assert (await worker.run_once()).state == "deferred"
    assert opc.calls == ["connect", "read"]
    assert journal.reports == []
    assert (await worker.run_once()).state == "reported"
    assert ("query", "original-command") in opc.calls
    assert not any(isinstance(call, tuple) and call[0] == "submit" for call in opc.calls)


async def test_cancelled_submit_leaves_reservation_for_query_without_resubmission():
    opc = OPC()
    opc.submit_entered, opc.submit_release = asyncio.Event(), asyncio.Event()
    journal = Journal(opc)
    worker = CommandWorker(opc, journal, clock=lambda: AT)
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(opc.submit_entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert journal.reports == []
    assert (await worker.run_once()).state == "reported"
    assert opc.calls.count(("submit", "original-command")) == 1


@pytest.mark.parametrize("stage", ["submission", "result", "ack"])
async def test_cross_command_opc_or_report_response_is_never_accepted(stage):
    opc = OPC()
    journal = Journal(opc)
    if stage == "submission":
        opc.submit_response = submission("other")
    elif stage == "result":
        opc.result_response = result("other")
    else:
        journal.wrong_ack = True
    outcome = await CommandWorker(opc, journal, clock=lambda: AT).run_once()
    if stage == "submission":
        assert not outcome.submission_reported and outcome.result_reported
        assert [kind for kind, _ in journal.reports] == ["result"]
    else:
        assert outcome.state == "uncertain" and not outcome.result_reported
        if stage == "result":
            assert [kind for kind, _ in journal.reports] == ["submission"]


@pytest.mark.parametrize("state", ["UNKNOWN", "DELAYED", "ALARM"])
async def test_unhealthy_read_does_not_claim(state):
    opc = OPC()
    opc.current_health = GatewayHealth(state, True, 4, ())
    journal = Journal(opc)
    assert (await CommandWorker(opc, journal).run_once()).state == "blocked"
    assert len(journal.work) == 2


async def test_health_expiring_while_claiming_defers_without_submit():
    opc = OPC()
    journal = Journal(opc)
    journal.on_claim = lambda: setattr(opc, "current_health", GatewayHealth("DELAYED", True, 4, ()))
    assert (await CommandWorker(opc, journal, clock=lambda: AT).run_once()).state == "deferred"
    assert not journal.reports


def transport(handler, **config):
    return CommandTransport(
        IntakeConfig("http://127.0.0.1:8000", "t" * 43, allow_loopback_http=True, **config),
        transport=httpx.MockTransport(handler),
    )


async def test_http_claim_and_reports_use_exact_paths_payloads_and_bound_ack():
    calls = []

    async def handler(http_request):
        calls.append(http_request.url.path)
        assert http_request.headers["authorization"] == "Bearer " + "t" * 43
        assert http_request.headers["content-type"] == "application/json"
        if http_request.url.path.endswith("claim"):
            assert json.loads(http_request.content) == {}
            return httpx.Response(
                200,
                json={"work": {"action": "SUBMIT", "request": request().model_dump(mode="json")}},
            )
        assert json.loads(http_request.content) == (
            submission().model_dump(mode="json")
            if http_request.url.path.endswith("submission")
            else result().model_dump(mode="json")
        )
        return httpx.Response(200, json={"committed": True, "command_id": "original-command"})

    client = transport(handler)
    try:
        assert await client.claim() == SubmitWork(action="SUBMIT", request=request())
        await client.report_submission(submission())
        await client.report_result(result())
        assert calls == [
            "/internal/v1/commands/" + part for part in ("claim", "submission", "result")
        ]
    finally:
        await client.close()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"work": None, "extra": 1},
        {"work": {"action": "RETRY", "request": {}}},
        {"work": {"action": "QUERY", "request": {"command_id": "original-command"}, "extra": 1}},
        {"work": {"action": "QUERY", "request": {"command_id": "original-command", "extra": 1}}},
        {"work": {"action": "SUBMIT", "request": {"command_id": "original-command"}}},
    ],
)
async def test_invalid_claim_shapes_are_uncertain(payload):
    client = transport(lambda _: httpx.Response(200, json=payload))
    try:
        with pytest.raises(DeliveryUncertain):
            await client.claim()
    finally:
        await client.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"committed": True, "command_id": "other"},
        {"committed": False, "command_id": "original-command"},
        {"committed": 1, "command_id": "original-command"},
        {"committed": "true", "command_id": "original-command"},
        {"committed": True, "command_id": "original-command", "extra": 1},
    ],
)
async def test_report_ack_requires_strict_true_and_exact_id(payload):
    client = transport(lambda _: httpx.Response(200, json=payload))
    try:
        with pytest.raises(DeliveryUncertain):
            await client.report_result(result())
    finally:
        await client.close()


@pytest.mark.parametrize("status", [302, 401, 503])
async def test_http_does_not_redirect_retry_or_expose_error_body(status):
    calls = []

    async def handler(http_request):
        calls.append(http_request)
        return httpx.Response(
            status,
            headers={"Location": "https://other.invalid"},
            json={"detail": "sensitive-detail"},
        )

    client = transport(handler)
    try:
        with pytest.raises(DeliveryRejected if status == 401 else DeliveryUncertain) as error:
            await client.claim()
        assert "sensitive-detail" not in str(error.value)
        assert len(calls) == 1
    finally:
        await client.close()


async def test_http_response_bound_and_lost_claim_response_do_not_retry():
    calls = []

    async def oversized(http_request):
        calls.append(http_request)
        return httpx.Response(200, content=b"x" * 1025)

    client = transport(oversized, max_response_bytes=1024)
    try:
        with pytest.raises(DeliveryUncertain):
            await client.claim()
        assert len(calls) == 1
    finally:
        await client.close()

    async def lost(http_request):
        calls.append(http_request)
        raise httpx.ReadError("sensitive-detail")

    client = transport(lost)
    try:
        with pytest.raises(DeliveryUncertain) as error:
            await client.claim()
        assert "sensitive-detail" not in str(error.value)
        assert len(calls) == 2
    finally:
        await client.close()
