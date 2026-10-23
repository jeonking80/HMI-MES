"""Assistant deadline, quota and GET-only boundaries without a live model provider."""

import asyncio
import logging
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

import apps.api.assistant as assistant_module
from apps.api.assistant import AssistantConfig, _Limiter, _Tools, register_assistant_routes

ORIGIN = "https://assistant.test"
KEY = "test-key-fixture-confidential"
PROMPT = "prompt-fixture-confidential"
ANSWER = "answer-fixture-confidential"
SCOPE = dict(line_id="line", source_id="source", stream_id="stream")
NOW = datetime(2026, 10, 23, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_deadline_clock(monkeypatch):
    # Keep the production 50 s planning thresholds while accelerating only the
    # outer asyncio deadline in tests. No live provider or long test sleep.
    monkeypatch.setattr(assistant_module, "asyncio", SimpleNamespace(timeout=asyncio.timeout))


@pytest.mark.parametrize(
    "field,value",
    [
        ("timeout_seconds", float("nan")),
        ("timeout_seconds", float("inf")),
        ("deadline_seconds", 51),
        ("deadline_seconds", float("nan")),
        ("deadline_seconds", True),
        ("session_limit", 0),
        ("global_limit", float("inf")),
        ("guest_limit", -1),
        ("window_seconds", True),
        ("max_rounds", 1.5),
    ],
)
def test_finite_configuration_bounds(field, value):
    with pytest.raises(ValueError):
        AssistantConfig(api_key=KEY, **{field: value})


def test_guest_account_bucket_spans_sessions_and_window_expires():
    clock = [0.0]
    limiter = _Limiter(AssistantConfig(api_key=KEY), monotonic=lambda: clock[0])
    assert limiter.take("first-guest-session", guest=True)
    assert limiter.take("second-guest-session", guest=True)
    assert not limiter.take("third-new-session", guest=True)
    assert limiter.take("ordinary-viewer")
    clock[0] = 300
    assert limiter.take("third-new-session", guest=True)


async def test_alarm_tool_fixed_dictionary_get_scope_active_only_incomplete_counts():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.headers["cookie"] == "hmi_session=caller-session"
        params = request.url.params
        assert all(params[k] == v for k, v in SCOPE.items())
        assert params["view"] == "attention" and params["limit"] == "100"
        if params["asset_id"] in {"Infeed", "Outfeed"}:
            return httpx.Response(404, json={"detail": "not implemented"})

        def item(name, status):
            return dict(
                status=status,
                instance=dict(alarm_id=name, rule=dict(rule_id="temperature", severity="HIGH")),
            )

        return httpx.Response(
            200,
            json=dict(
                items=[
                    item("active", "ACTIVE_UNACKNOWLEDGED"),
                    item("returned", "RETURNED_UNACKNOWLEDGED"),
                ],
                has_more=True,
                coverage={},
            ),
        )

    async with httpx.AsyncClient(
        base_url=ORIGIN,
        transport=httpx.MockTransport(handler),
        cookies={"hmi_session": "caller-session"},
    ) as client:
        tools = _Tools(client, SCOPE, NOW)
        result = await tools.call("get_active_alarms", {})
        with pytest.raises(ValueError):
            await tools.call("get_active_alarms", {"asset_id": "arbitrary"})
    assert {r.url.params["asset_id"] for r in calls} == {"Line", "Infeed", "Sealer", "Outfeed"}
    assert len(calls) == len(tools.evidence) == 4
    assert result["complete"] is False
    for asset in result["assets"]:
        assert asset["complete"] is False
        if asset["status"] == 200:
            assert asset["active_count"] == 1
            assert asset["items"][0]["alarm_id"] == "active"
            assert asset["counts_basis"] == "OBSERVED_PAGE"
        else:
            assert asset["active_count"] is None


@pytest.mark.parametrize("minutes,resolution", [(30, "second"), (120, "minute")])
async def test_trend_fixed_contract_repeated_tags_and_resolution(minutes, resolution):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"series": []})

    async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as client:
        tool = _Tools(client, SCOPE, NOW)
        await tool.call(
            "get_trend_summary",
            {"minutes": minutes, "tags": ["Sealer.TemperaturePV", "Sealer.AirPressure"]},
        )
        with pytest.raises(ValueError):
            await tool.call("get_trend_summary", {"tags": [["nested"]]})
    assert seen[0].url.params["resolution"] == resolution
    assert seen[0].url.params.get_list("tag") == ["Sealer.TemperaturePV", "Sealer.AirPressure"]


class Auth:
    def __init__(self, delay=0):
        self.delay = delay
        self.calls = []

    @contextmanager
    def authorized_transaction(self, token, **kwargs):
        self.calls.append((token, kwargs))
        time.sleep(self.delay)
        yield SimpleNamespace(principal=SimpleNamespace(login="viewer"))


def app(provider, *, deadline=0.08, auth=None):
    auth = auth or Auth()
    application = FastAPI()
    config = AssistantConfig(api_key=KEY, transport=httpx.MockTransport(provider))
    assistant_module.asyncio.timeout = lambda configured: asyncio.timeout(deadline)
    register_assistant_routes(
        application, auth, config, origin=ORIGIN, session_cookie="hmi_session"
    )
    return application, auth


async def post(application):
    async with httpx.AsyncClient(
        base_url=ORIGIN,
        transport=httpx.ASGITransport(app=application),
        cookies={"hmi_session": "caller-session"},
    ) as client:
        return await client.post(
            "/api/v1/assistant/chat",
            json={"messages": [{"role": "user", "content": PROMPT}], "scope": SCOPE},
            headers={"Origin": ORIGIN, "X-CSRF-Token": "csrf"},
        )


@pytest.mark.parametrize("failure", ["success", "provider", "timeout", "tool"])
async def test_secret_free_logs_success_and_all_failure_paths(caplog, failure):
    caplog.set_level(logging.DEBUG)
    requests = []

    async def provider(request):
        requests.append(request)
        if failure == "provider":
            return httpx.Response(500, json={"error": KEY + PROMPT + ANSWER})
        if failure == "timeout":
            await asyncio.sleep(0.2)
        if failure == "tool" and len(requests) == 1:
            message = {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call",
                        "type": "function",
                        "function": {"name": "get_line_snapshot", "arguments": "{}"},
                    }
                ],
            }
        else:
            message = {"content": ANSWER}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    application, auth = app(provider)

    @application.get("/api/v1/snapshot")
    async def snapshot():
        return JSONResponse(status_code=500, content={KEY: PROMPT, "screen": ANSWER})

    response = await post(application)
    assert response.status_code == {"provider": 502, "timeout": 504}.get(failure, 200)
    if failure == "timeout":
        assert response.json()["code"] == "ASSISTANT_TIMEOUT"
    assert auth.calls[0][1]["permission"] == "read"
    assert auth.calls[0][1]["refresh_idle"] is False
    assert all(secret not in caplog.text for secret in (KEY, PROMPT, ANSWER))


async def test_one_deadline_spans_multiple_provider_rounds():
    calls = []

    async def provider(request):
        calls.append(request)
        await asyncio.sleep(0.035)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call",
                                    "function": {"name": "get_line_snapshot", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    application, _ = app(provider, deadline=0.06)

    @application.get("/api/v1/snapshot")
    async def snapshot():
        return {"health": {"state": "UNKNOWN"}}

    response = await post(application)
    assert response.status_code == 504
    assert response.json()["code"] == "ASSISTANT_TIMEOUT"
    assert len(calls) == 2


async def test_deadline_covers_local_get_and_caller_authorization():
    async def provider(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call",
                                    "function": {"name": "get_line_snapshot", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            },
        )

    application, _ = app(provider, deadline=0.03)

    @application.get("/api/v1/snapshot")
    async def snapshot(request: Request):
        assert request.cookies["hmi_session"] == "caller-session"
        await asyncio.sleep(0.2)
        return {}

    assert (await post(application)).status_code == 504
    application, _ = app(provider, deadline=0.02, auth=Auth(delay=0.06))
    assert (await post(application)).status_code == 504


async def test_provider_transport_timeout_is_distinct_and_opaque(caplog):
    caplog.set_level(logging.DEBUG)

    async def provider(request):
        raise httpx.ReadTimeout(KEY + PROMPT + ANSWER, request=request)

    application, _ = app(provider)
    response = await post(application)
    assert response.status_code == 504
    assert response.json()["code"] == "ASSISTANT_TIMEOUT"
    assert all(secret not in response.text + caplog.text for secret in (KEY, PROMPT, ANSWER))


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:secret@provider.test/v1",
        "https://provider.test/v1?key=secret",
        "https://provider.test/v1#secret",
    ],
)
def test_configuration_cannot_put_provider_credentials_in_logged_urls(endpoint):
    with pytest.raises(ValueError):
        AssistantConfig(api_key=KEY, base_url=endpoint)
