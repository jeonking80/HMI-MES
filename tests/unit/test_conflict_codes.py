"""Public HTTP conflict fields do not depend on internal exception diagnostics."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from apps.api import control_http, work_orders_http
from apps.api.commands import CommandConflict, CommandScopeError
from apps.api.control_http import CommandPreflightConflict
from apps.api.work_orders import RegistrationConflict
from contracts.conflict_codes import ConflictReason
from contracts.models import CommandRequest


@pytest.mark.parametrize(
    "exception_type,route,reason,detail",
    [
        (
            RegistrationConflict,
            "registration",
            ConflictReason.WORK_ORDER_VERSION_CHANGED,
            "registration conflicts with immutable content",
        ),
        (
            CommandConflict,
            "control",
            ConflictReason.ID_CONTENT_CONFLICT,
            "command conflicts with current durable evidence",
        ),
        (
            CommandScopeError,
            "control",
            ConflictReason.STATE_NOT_ALLOWED,
            "command conflicts with current durable evidence",
        ),
        (
            CommandPreflightConflict,
            "control",
            ConflictReason.FRESH_SOURCE_REQUIRED,
            "current evidence does not permit this command",
        ),
    ],
)
async def test_http_conflict_does_not_echo_or_classify_private_diagnostic(
    monkeypatch, exception_type, route, reason, detail
):
    # A misleading code in diagnostic text must not determine the public code.
    diagnostic = "password=private-test-secret; CONTROL_VERSION_STALE; sql=private-query"

    async def failed_operation(operation):
        raise exception_type(diagnostic, reason_code=reason)

    app = FastAPI()
    if route == "registration":
        monkeypatch.setattr(work_orders_http, "run_in_threadpool", failed_operation)
        work_orders_http.mount_work_order_routes(app, None)
        method, path, options = "GET", "/api/v1/work-orders/work-A", {}
    else:
        monkeypatch.setattr(control_http, "run_in_threadpool", failed_operation)
        control_http.mount_control_routes(app, None)
        now = datetime(2026, 10, 25, tzinfo=UTC)
        request = CommandRequest(
            command_id="command-A",
            target_id="Line",
            execution_id="execution-A",
            command={"kind": "START", "args": {}},
            requested_by="operator",
            requested_at=now,
            expires_at=now + timedelta(seconds=5),
            expected_control_version=42,
        )
        method, path = "POST", "/api/v1/commands"
        options = {
            "headers": {"Origin": "https://example.test", "X-CSRF-Token": "fixture-token"},
            "json": {
                "line_id": "line-A",
                "source_id": "source-A",
                "stream_id": "stream-A",
                "request": request.model_dump(mode="json"),
            },
        }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://example.test"
    ) as client:
        response = await client.request(method, path, **options)
    assert response.status_code == 409
    assert response.json() == {"detail": detail, "reason_code": reason.value}
    assert "private-test-secret" not in response.text
    assert "private-query" not in response.text
    assert "CONTROL_VERSION_STALE" not in response.text
