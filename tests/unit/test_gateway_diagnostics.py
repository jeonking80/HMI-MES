"""Gateway diagnostics preserve bounded class names without exception contents."""

import asyncio
import json
from types import SimpleNamespace

from apps.gateway import __main__ as gateway
from deploy.plant_runtime import sanitized_line


async def test_worker_status_and_supervisor_preserve_only_valid_error_classes(capsys):
    invalid = ["ValueError: secret", "secret\nValueError", "9Error", "E" * 129, {}, None]

    class Pump:
        async def run(self, stop, *, on_status):
            for error in ["GatewayReadError", *invalid]:
                on_status(
                    {
                        "operation": "capture",
                        "state": "blocked",
                        "error": error,
                        "message": "secret endpoint and token",
                    }
                )

    await gateway._run_workers(Pump(), None, asyncio.Event())
    lines = capsys.readouterr().out.splitlines()
    base = {"operation": "capture", "state": "blocked"}
    assert json.loads(lines[0]) == {**base, "error": "GatewayReadError"}
    assert json.loads(sanitized_line(lines[0])) == {**base, "error": "GatewayReadError"}
    for line, error in zip(lines[1:], invalid, strict=True):
        assert json.loads(line) == base
        # The supervisor independently validates untrusted child output.
        raw = json.dumps({**base, "error": error, "message": "secret endpoint and token"})
        assert json.loads(sanitized_line(raw)) == base
    assert sanitized_line(json.dumps({"operation": [], "state": "blocked"})) is None


def test_fatal_stderr_survives_supervisor_without_message_or_invalid_name(monkeypatch, capsys):
    monkeypatch.setattr(gateway, "parser", lambda: SimpleNamespace(parse_args=lambda _: None))
    for name in ["ValueError", "BadError: secret", "BadError\nsecret", "E" * 129]:
        error_type = type(name, (Exception,), {})

        async def fail(_args, exception_type=error_type):
            raise exception_type("secret endpoint and token")

        monkeypatch.setattr(gateway, "_serve", fail)
        assert gateway.main([]) == 1
        captured = capsys.readouterr()
        expected = {"operation": "gateway", "state": "stopped"}
        if name == "ValueError":
            expected["error"] = name
        assert captured.out == ""
        assert json.loads(captured.err) == expected
        assert json.loads(sanitized_line(captured.err)) == expected
