"""Read-only operations assistant (CX-08).

The model never touches the database. Each tool is an in-process GET to an existing
read API, sent with the caller's own session cookie, so it sees exactly what the
caller may read and can never mutate anything. Prompts and answers are not logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.responses import JSONResponse

from apps.api.auth import GUEST_LOGIN, AuthorizationError
from apps.api.minute_aggregation import ANALOG_UNITS
from contracts.tags.validate import load_contract

DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
SEOUL = ZoneInfo("Asia/Seoul")
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
_TOOL_RESULT_BYTES = 12_000
_ASSETS = tuple(dict.fromkeys(tag["group"] for tag in load_contract()["tags"]))

SYSTEM_PROMPT = """당신은 포장 라인 HMI/MES의 조회 전용 운영 도우미다.
규칙:
- 라인 데이터는 반드시 제공된 도구로 조회한 결과만 근거로 답한다. 추측하거나 지어내지 않는다.
- 운전 명령, 작업지시 변경, 알람 조치, 정지 사유 입력은 할 수 없다.
  요청받으면 화면에서 권한 있는 사용자가 직접 해야 한다고 안내한다.
- 답변에 근거 데이터의 조회 시각(generated_at 등)을 한국 시간으로 적는다.
- provisional(잠정), UNKNOWN·TIME_UNKNOWN·MISSING(불명·누락),
  STALE·NO_DATA(오래됨·데이터 없음)는 그대로 밝힌다.
  불명을 정상이나 0으로 바꿔 말하지 않는다.
- 도구가 오류를 돌려주면 조회하지 못했다고 말한다.
- 알람 요약의 complete가 false이면 전체 알람 수라고 단정하지 않는다.
  OBSERVED_PAGE는 조회한 페이지의 관측값이며 지원하지 않는 설비는 불명이다.
- 한국어로 간결하게 답한다. 숫자는 단위와 함께 쓴다."""


@dataclass(frozen=True)
class AssistantConfig:
    api_key: str = field(repr=False)
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    max_rounds: int = 4
    timeout_seconds: float = 20.0
    # Whole answer budget under the 60 s serverless limit (vercel.json); later rounds stop
    # calling tools.
    deadline_seconds: float = 50.0
    # Per session and per process; protects the shared free-tier key from one viewer.
    session_limit: int = 10
    global_limit: int = 30
    guest_limit: int = 2
    window_seconds: int = 300
    # Test seam for the model endpoint only; production uses the default HTTPS transport.
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if not isinstance(self.api_key, str) or not 8 <= len(self.api_key) <= 512:
            raise ValueError("assistant API key is missing or malformed")
        if any(c.isspace() for c in self.api_key):
            raise ValueError("assistant API key is missing or malformed")
        if not isinstance(self.model, str) or not _MODEL_NAME.fullmatch(self.model):
            raise ValueError("assistant model name is invalid")
        endpoint = urlsplit(self.base_url) if isinstance(self.base_url, str) else None
        if (
            endpoint is None
            or endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("assistant endpoint must be credential-free HTTPS")
        if type(self.max_rounds) is not int or not 1 <= self.max_rounds <= 6:
            raise ValueError("assistant round bound is out of range")
        for value, maximum in ((self.timeout_seconds, 25), (self.deadline_seconds, 50)):
            if (
                type(value) not in (float, int)
                or not math.isfinite(value)
                or not 0 < value <= maximum
            ):
                raise ValueError("assistant time bound is out of range")
        if self.timeout_seconds > self.deadline_seconds:
            raise ValueError("assistant request timeout exceeds whole answer budget")
        for value, maximum in (
            (self.session_limit, 1000),
            (self.global_limit, 10000),
            (self.guest_limit, 1000),
            (self.window_seconds, 3600),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("assistant rate bound is out of range")


class _Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: Annotated[str, Field(strict=True, min_length=1, max_length=2000)]


class _Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_id: Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r"^\S+$")]
    source_id: Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r"^\S+$")]
    stream_id: Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r"^\S+$")]


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: Annotated[list[_Turn], Field(min_length=1, max_length=12)]
    scope: _Scope | None = None


class _Limiter:
    def __init__(self, config: AssistantConfig, monotonic=time.monotonic):
        self.config = config
        self.monotonic = monotonic
        self.windows: OrderedDict[str, deque[float]] = OrderedDict()
        self.lock = threading.Lock()

    def take(self, session_key: str, *, guest: bool = False) -> bool:
        with self.lock:
            now = self.monotonic()
            for key in tuple(self.windows):
                window = self.windows[key]
                while window and window[0] <= now - self.config.window_seconds:
                    window.popleft()
                if not window:
                    del self.windows[key]
            if len(self.windows) > 4096:
                return False
            limits = {
                "session:" + session_key: self.config.session_limit,
                "*": self.config.global_limit,
            }
            if guest:
                # Dedicated account bucket spans all its sessions in this process.
                limits["account:" + GUEST_LOGIN] = self.config.guest_limit
            if any(len(self.windows.get(k, ())) >= n for k, n in limits.items()):
                return False
            for key in limits:
                self.windows.setdefault(key, deque()).append(now)
            return True


def _function(name, description, properties=None, required=()):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": list(required),
                "additionalProperties": False,
            },
        },
    }


_DATE = {"type": "string", "description": "한국 날짜 YYYY-MM-DD. 생략하면 오늘."}
TOOLS = [
    _function(
        "get_dashboard",
        "선택한 날짜의 생산 요약: 신선도, 양품·불량, 진행 작업, 가동·정지·불명 시간, 활성 알람 수",
        {"date": _DATE},
    ),
    _function("get_active_alarms", "선택 라인의 설비별 활성 알람과 조회 범위·불완전 여부"),
    _function("get_line_snapshot", "현재 라인·설비 상태와 통신 건강 상태(실시간 스냅샷)"),
    _function(
        "get_trend_summary",
        "아날로그 태그의 최근 추세 요약(최소·최대·평균·품질별 개수)",
        {
            "tags": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(ANALOG_UNITS)},
                "minItems": 1,
                "maxItems": 4,
            },
            "minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
        },
        ("tags",),
    ),
    _function(
        "list_execution_reports",
        "최근 실행(작업)별 실적: 수량, 수율, 정산 상태, 가동·불명 시간",
        {"days": {"type": "integer", "minimum": 1, "maximum": 31}},
    ),
    _function(
        "get_daily_report", "하루 운영 보고: 수량, 시간대별 생산, 정지 사유", {"date": _DATE}
    ),
    _function(
        "get_spc",
        "실행 하나의 SPC(X̄-R) 결과: 관리한계, Cp/Cpk, 규칙 위반, 추세 경고",
        {
            "execution_id": {"type": "string"},
            "tag": {"type": "string", "enum": ["Sealer.TemperaturePV", "Sealer.AirPressure"]},
        },
        ("execution_id",),
    ),
]


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _day(value, now) -> str:
    if value is None:
        return now.astimezone(SEOUL).date().isoformat()
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("date must be YYYY-MM-DD")
    return value


def _summarize_trend(body):
    series = []
    for item in body.get("series", []):
        values = [p["value"] for p in item["points"] if p.get("value") is not None]
        quality = {}
        for point in item["points"]:
            quality[point["quality"]] = quality.get(point["quality"], 0) + 1
        latest = next((p for p in reversed(item["points"]) if p.get("value") is not None), None)
        series.append(
            {
                "tag_id": item["tag_id"],
                "unit": item.get("unit"),
                "points": len(item["points"]),
                "quality_counts": quality,
                "min": min(values) if values else None,
                "max": max(values) if values else None,
                "mean": round(sum(values) / len(values), 3) if values else None,
                "latest": {"t": latest["t"], "value": latest["value"]} if latest else None,
            }
        )
    return {k: body.get(k) for k in ("from", "to", "resolution", "generated_at")} | {
        "series": series
    }


def _summarize_dashboard(body):
    # Empty hours add input tokens (and seconds) without information; keep their count.
    hourly = body.get("hourly") or []
    active = [
        h
        for h in hourly
        if h.get("good") not in (None, "0")
        or h.get("reject") not in (None, "0")
        or h.get("running_seconds")
    ]
    return {k: v for k, v in body.items() if k != "hourly"} | {
        "hourly_with_activity": active,
        "hourly_without_activity": len(hourly) - len(active),
    }


def _summarize_reports(body):
    keep = (
        "execution_id",
        "work_order_id",
        "item_id",
        "target_quantity",
        "started_at",
        "ended_at",
        "totals",
        "yield_ratio",
        "settlement_status",
        "provisional",
        "provisional_reasons",
        "durations_seconds",
    )
    return {
        "items": [{k: item.get(k) for k in keep} for item in body.get("items", [])[:20]],
        "more": bool(body.get("next_cursor")) or len(body.get("items", [])) > 20,
    }


def _summarize_snapshot(body):
    body = {k: v for k, v in body.items() if k != "evidence"}
    if len(json.dumps(body, ensure_ascii=False).encode()) > _TOOL_RESULT_BYTES:
        body = {
            "health": body.get("health"),
            "missing_tag_ids": body.get("missing_tag_ids"),
            "note": "태그 상세가 길어 생략됨",
        }
    return body


def _summarize_spc(body):
    return {k: v for k, v in body.items() if k not in ("subgroups", "trend")} | {
        "subgroup_count": len(body.get("subgroups", [])),
        "violations": sum(len(g.get("violations", [])) for g in body.get("subgroups", [])),
    }


def _summarize_daily(body):
    return {k: v for k, v in body.items() if k not in ("segments",)}


class _Tools:
    """Maps a model tool call to one whitelisted GET with the caller's cookie."""

    def __init__(self, client: httpx.AsyncClient, scope: dict | None, now: datetime):
        self.client = client
        self.scope = scope
        self.now = now
        self.evidence: list[dict] = []

    async def _scope(self):
        if self.scope is not None:
            return self.scope
        response = await self.client.get("/api/v1/sources")
        response.raise_for_status()
        sources = response.json()["sources"]
        if len(sources) != 1:
            raise LookupError("라인·수집원을 화면에서 먼저 선택해야 합니다.")
        self.scope = {k: sources[0][k] for k in ("line_id", "source_id", "stream_id")}
        return self.scope

    async def call(self, name: str, arguments: dict) -> dict:
        schema = next(
            (tool["function"]["parameters"] for tool in TOOLS if tool["function"]["name"] == name),
            None,
        )
        if (
            schema is None
            or not isinstance(arguments, dict)
            or (
                set(arguments) - set(schema["properties"])
                or not set(schema["required"]) <= set(arguments)
            )
        ):
            raise ValueError("invalid tool arguments")
        if name == "get_active_alarms":
            return await self._alarms()
        label, path, params, summarize = await self._route(name, arguments)
        response = await self.client.get(path, params=params)
        record = {"tool": name, "label": label, "status": response.status_code}
        if response.status_code != 200:
            self.evidence.append(record)
            return {"error": f"조회 실패(HTTP {response.status_code})"}
        body = response.json()
        record["generated_at"] = body.get("generated_at") if isinstance(body, dict) else None
        self.evidence.append(record)
        result = summarize(body)
        text = json.dumps(result, ensure_ascii=False)
        if len(text.encode()) > _TOOL_RESULT_BYTES:
            return {
                "error": "결과가 너무 커서 요약하지 못했습니다. 범위를 좁혀 다시 질문해 주세요."
            }
        return result

    async def _alarms(self):
        scope = await self._scope()
        assets = []
        for asset in _ASSETS:
            response = await self.client.get(
                "/api/v1/alarms",
                params=scope | {"asset_id": asset, "view": "attention", "limit": "100"},
            )
            self.evidence.append(
                dict(
                    tool="get_active_alarms",
                    label="활성 알람 " + asset,
                    status=response.status_code,
                    generated_at=_iso(self.now),
                )
            )
            if response.status_code != 200:
                assets.append(
                    dict(
                        asset_id=asset,
                        status=response.status_code,
                        active_count=None,
                        error="ALARM_SCOPE_UNAVAILABLE",
                        complete=False,
                    )
                )
                continue
            body = response.json()
            active = [
                item
                for item in body["items"]
                if item["status"] in {"ACTIVE_ACKNOWLEDGED", "ACTIVE_UNACKNOWLEDGED"}
            ]
            severities = {}
            for item in active:
                severity = item["instance"]["rule"]["severity"]
                severities[severity] = severities.get(severity, 0) + 1
            assets.append(
                dict(
                    asset_id=asset,
                    status=200,
                    active_count=len(active),
                    unacknowledged_count=sum(
                        item["status"] == "ACTIVE_UNACKNOWLEDGED" for item in active
                    ),
                    active_by_severity=severities,
                    complete=not body["has_more"],
                    counts_basis="OBSERVED_PAGE",
                    items_truncated=len(active) > 10,
                    items=[
                        dict(
                            alarm_id=item["instance"]["alarm_id"],
                            status=item["status"],
                            rule_id=item["instance"]["rule"]["rule_id"],
                            severity=item["instance"]["rule"]["severity"],
                        )
                        for item in active[:10]
                    ],
                    coverage={
                        key: body.get("coverage", {}).get(key)
                        for key in (
                            "installed_channel_count",
                            "consumer_status",
                            "runtime_configuration_status",
                        )
                    },
                )
            )
        result = dict(
            scope=scope,
            generated_at=_iso(self.now),
            assets=assets,
            complete=all(asset["complete"] for asset in assets),
        )
        if len(json.dumps(result, ensure_ascii=False).encode()) > _TOOL_RESULT_BYTES:
            return {"error": "알람 요약이 너무 큽니다. 알람 화면에서 조회해 주세요."}
        return result

    async def _route(self, name, arguments):
        if name == "get_dashboard":
            scope = await self._scope()
            day = _day(arguments.get("date"), self.now)
            return (
                f"대시보드 {day}",
                "/api/v1/dashboard",
                scope | {"date": day},
                _summarize_dashboard,
            )
        if name == "get_line_snapshot":
            scope = await self._scope()
            return "라인 스냅샷", "/api/v1/snapshot", scope, _summarize_snapshot
        if name == "get_trend_summary":
            scope = await self._scope()
            tags = arguments.get("tags")
            minutes = arguments.get("minutes", 60)
            if (
                not isinstance(tags, list)
                or not 1 <= len(tags) <= 4
                or any(not isinstance(tag, str) for tag in tags)
                or len(set(tags)) != len(tags)
                or any(tag not in ANALOG_UNITS for tag in tags)
                or type(minutes) is not int
                or not 1 <= minutes <= 1440
            ):
                raise ValueError("tags or minutes are invalid")
            step = 1 if minutes <= 60 else 60
            end = int(self.now.timestamp()) // step * step
            end_at = datetime.fromtimestamp(end, UTC)
            params = scope | {
                "tag": tags,
                "resolution": "second" if minutes <= 60 else "minute",
                "from": _iso(end_at - timedelta(minutes=minutes)),
                "to": _iso(end_at),
            }
            return f"트렌드 {minutes}분", "/api/v1/trends", params, _summarize_trend
        if name == "list_execution_reports":
            scope = await self._scope()
            days = arguments.get("days", 7)
            if type(days) is not int or not 1 <= days <= 31:
                raise ValueError("days is invalid")
            params = {
                "line_id": scope["line_id"],
                "from": _iso(self.now - timedelta(days=days)),
                "to": _iso(self.now + timedelta(seconds=1)),
                "limit": "50",
            }
            return f"실행 실적 {days}일", "/api/v1/reports/executions", params, _summarize_reports
        if name == "get_daily_report":
            scope = await self._scope()
            day = _day(arguments.get("date"), self.now)
            params = {"line_id": scope["line_id"], "date": day}
            return f"일일 보고 {day}", "/api/v1/reports/daily", params, _summarize_daily
        if name == "get_spc":
            scope = await self._scope()
            execution = arguments.get("execution_id")
            tag = arguments.get("tag", "Sealer.TemperaturePV")
            if (
                not isinstance(execution, str)
                or not 1 <= len(execution) <= 128
                or any(c.isspace() for c in execution)
            ):
                raise ValueError("execution_id is invalid")
            if tag not in ("Sealer.TemperaturePV", "Sealer.AirPressure"):
                raise ValueError("tag is invalid")
            params = scope | {"execution_id": execution, "tag": tag}
            return "SPC", "/api/v1/spc/xbar-r", params, _summarize_spc
        raise ValueError("unknown tool")


async def run_assistant(config, request_app, cookies, chat: ChatRequest, *, origin):
    """One answer with bounded tool rounds; returns answer and the evidence actually read."""
    now = datetime.now(UTC)
    local = now.astimezone(SEOUL)
    # The model has no clock; without this it guesses dates and queries the wrong day.
    today = (
        f"\n- 오늘은 한국 시간 {local.date().isoformat()}, 현재 시각은 {local:%H:%M} KST다."
        " 사용자가 날짜를 말하지 않으면 오늘을 조회한다."
    )
    messages = [{"role": "system", "content": SYSTEM_PROMPT + today}] + [
        {"role": t.role, "content": t.content} for t in chat.messages
    ]
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=request_app),
            # Same exact origin as the caller: the user app validates scheme and Host.
            base_url=origin,
            cookies=cookies,
            timeout=15,
        ) as internal,
        httpx.AsyncClient(
            base_url=config.base_url,
            transport=config.transport,
            timeout=config.timeout_seconds,
            headers={"Authorization": "Bearer " + config.api_key},
        ) as model,
    ):
        tools = _Tools(internal, chat.scope.model_dump() if chat.scope else None, now)
        started = time.monotonic()
        for round_index in range(config.max_rounds + 1):
            remaining = config.deadline_seconds - (time.monotonic() - started)
            if remaining < 3:
                raise TimeoutError("assistant deadline")
            # Without tools the model must answer from what it has already read.
            final_round = round_index == config.max_rounds or remaining < 2 * 8
            payload = {
                "model": config.model,
                "messages": messages,
                "temperature": 0.2,
                "max_tokens": 1024,
            }
            if "nemotron" in config.model:
                # Nemotron's reasoning phase took 20 s per round on real tool results and
                # could exhaust max_tokens before the answer; answers stay tool-grounded.
                payload["chat_template_kwargs"] = {"enable_thinking": False}
            if not final_round:
                payload |= {"tools": TOOLS, "tool_choice": "auto"}
            response = await model.post(
                "/chat/completions",
                json=payload,
                timeout=min(config.timeout_seconds, remaining),
            )
            remaining = config.deadline_seconds - (time.monotonic() - started)
            if response.status_code in {429, 500, 502, 503} and remaining > 10:
                # The shared endpoint fails transiently; retry once while time remains.
                response = await model.post(
                    "/chat/completions",
                    json=payload,
                    timeout=min(config.timeout_seconds, remaining - 1),
                )
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
            calls = (message.get("tool_calls") or [])[:4]
            if not calls or final_round:
                answer = (message.get("content") or "").strip()
                if not answer:
                    raise RuntimeError("empty assistant answer")
                return {
                    "answer": answer,
                    "evidence": tools.evidence,
                    "model": config.model,
                    "generated_at": _iso(now),
                }
            messages.append(
                {"role": "assistant", "content": message.get("content") or "", "tool_calls": calls}
            )
            for call in calls[:4]:
                try:
                    arguments = json.loads(call["function"].get("arguments") or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be an object")
                    result = await tools.call(call["function"]["name"], arguments)
                except (ValueError, LookupError, KeyError, httpx.HTTPError) as error:
                    message_text = str(error) if type(error) is LookupError else "잘못된 조회 요청"
                    result = {"error": message_text}
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
    raise RuntimeError("assistant round limit")


def register_assistant_routes(app, auth, config: AssistantConfig | None, *, origin, session_cookie):
    app.state.assistant_enabled = config is not None
    if config is None:
        return
    limiter = _Limiter(config)

    async def perform_chat(request: Request):
        if request.query_params:
            raise HTTPException(422, "unexpected query parameters")
        if request.headers.getlist("origin") != [origin]:
            raise AuthorizationError("origin is not allowed")
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(415, "JSON is required")
        raw = await request.body()
        if len(raw) > 16_384:
            raise HTTPException(413, "request too large")
        try:
            body = ChatRequest.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(422, "invalid assistant request") from None
        if body.messages[-1].role != "user":
            raise HTTPException(422, "the last message must be from the user")
        token = request.cookies.get(session_cookie)

        def authorize():
            # CSRF is required: this route spends a shared external quota.
            with auth.authorized_transaction(
                token,
                permission="read",
                csrf_token=request.headers.get("x-csrf-token"),
                origin=request.headers.get("origin"),
                require_csrf=True,
                refresh_idle=False,
            ) as tx:
                return tx.principal.login == GUEST_LOGIN

        guest = await run_in_threadpool(authorize)
        if not limiter.take(hashlib.sha256(token.encode()).hexdigest(), guest=guest):
            raise HTTPException(429, "assistant request limit reached")
        try:
            return await run_assistant(config, app, {session_cookie: token}, body, origin=origin)
        except httpx.TimeoutException:
            raise TimeoutError from None
        except (httpx.HTTPError, RuntimeError, KeyError, IndexError, TypeError, ValueError):
            # Never echo the provider error: it may contain the prompt or account details.
            raise HTTPException(502, "assistant unavailable") from None

    @app.post("/api/v1/assistant/chat")
    async def chat(request: Request):
        try:
            # One budget includes request validation, caller authorization, every
            # provider round and every local GET. A tool round never resets it.
            async with asyncio.timeout(config.deadline_seconds):
                return await perform_chat(request)
        except TimeoutError:
            return JSONResponse(
                status_code=504,
                content={"detail": "assistant request timed out", "code": "ASSISTANT_TIMEOUT"},
            )
