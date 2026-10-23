"""Gateway diagnostics preserve bounded classes and BufferFull codes without messages."""

import asyncio
import json
from types import SimpleNamespace

from apps.gateway import __main__ as gateway
from apps.gateway.buffer import BufferFull
from apps.gateway.worker import GatewayPump
from deploy.plant_runtime import sanitized_line


async def test_worker_status_and_supervisor_preserve_only_valid_error_classes(capsys):
    invalid = ["ValueError: secret", "secret\nValueError", "9Error", "E" * 129, {}, None]
    invalid_codes = ["secret endpoint", "CODE\nSECRET", "lowercase", "9CODE", "E" * 65, {}, None]
    cases = [
        ("GatewayReadError", "CAPTURE_CAPACITY_EXCEEDED"),
        *((error, "CAPTURE_CAPACITY_EXCEEDED") for error in invalid),
        ("BufferFull", "CAPTURE_CAPACITY_EXCEEDED"),
        ("BufferFull", "E" * 64),
        *(("BufferFull", code) for code in invalid_codes),
    ]

    class Pump:
        async def run(self, stop, *, on_status):
            for error, error_code in cases:
                on_status(
                    {
                        "operation": "capture",
                        "state": "blocked",
                        "error": error,
                        "error_code": error_code,
                        "message": "secret endpoint and token",
                    }
                )

    await gateway._run_workers(Pump(), None, asyncio.Event())
    lines = capsys.readouterr().out.splitlines()
    base = {"operation": "capture", "state": "blocked"}
    for line, (error, error_code) in zip(lines, cases, strict=True):
        expected = dict(base)
        if error in ("GatewayReadError", "BufferFull"):
            expected["error"] = error
        if error == "BufferFull" and error_code in ("CAPTURE_CAPACITY_EXCEEDED", "E" * 64):
            expected["error_code"] = error_code
        assert json.loads(line) == expected
        assert json.loads(sanitized_line(line)) == expected
        # The supervisor independently validates untrusted child output.
        raw = json.dumps(
            {**base, "error": error, "error_code": error_code, "message": "secret endpoint and token"}
        )
        assert json.loads(sanitized_line(raw)) == expected
    assert sanitized_line(json.dumps({"operation": [], "state": "blocked"})) is None

    # Exercise the actual collector callback before CLI/supervisor filtering.
    for exception in [
        BufferFull("CAPTURE_CAPACITY_EXCEEDED: secret endpoint and token"),
        BufferFull("CODE\nSECRET: secret endpoint and token"),
        BufferFull("E" * 65 + ": secret endpoint and token"),
        BufferFull("CAPTURE_CAPACITY_EXCEEDED"),
        ValueError("CAPTURE_CAPACITY_EXCEEDED: secret endpoint and token"),
    ]:
        stop = asyncio.Event()
        pump = object.__new__(GatewayPump)
        pump.opc = SimpleNamespace(config=SimpleNamespace(poll_interval_seconds=0.01))
        pump.measured_clock = False

        async def capture(error=exception):
            stop.set()
            raise error

        pump.capture_once = capture
        reports = []
        await pump.run(stop, on_status=reports.append)
        expected = {**base, "error": type(exception).__name__}
        if str(exception).startswith("CAPTURE_CAPACITY_EXCEEDED:") and isinstance(
            exception, BufferFull
        ):
            expected["error_code"] = "CAPTURE_CAPACITY_EXCEEDED"
        assert reports == [expected]


def test_fatal_stderr_survives_supervisor_without_message_or_invalid_name(monkeypatch, capsys):
    monkeypatch.setattr(gateway, "parser", lambda: SimpleNamespace(parse_args=lambda _: None))
    exceptions = [
        type(name, (Exception,), {})("CAPTURE_CAPACITY_EXCEEDED: secret endpoint and token")
        for name in ["ValueError", "BufferFull", "BadError: secret", "BadError\nsecret", "E" * 129]
    ]
    exceptions.extend(
        BufferFull(message)
        for message in [
            "CAPTURE_CAPACITY_EXCEEDED: secret endpoint and token",
            "E" * 64 + ": secret endpoint and token",
            "CODE\nSECRET: secret endpoint and token",
            "lowercase: secret endpoint and token",
            "E" * 65 + ": secret endpoint and token",
            "CAPTURE_CAPACITY_EXCEEDED",
        ]
    )
    for exception in exceptions:
        name = type(exception).__name__

        async def fail(_args, error=exception):
            raise error

        monkeypatch.setattr(gateway, "_serve", fail)
        assert gateway.main([]) == 1
        captured = capsys.readouterr()
        expected = {"operation": "gateway", "state": "stopped"}
        if name in {"ValueError", "BufferFull"}:
            expected["error"] = name
        if isinstance(exception, BufferFull):
            prefix, separator, _ = str(exception).partition(":")
            if separator and prefix in {"CAPTURE_CAPACITY_EXCEEDED", "E" * 64}:
                expected["error_code"] = prefix
        assert captured.out == ""
        assert json.loads(captured.err) == expected
        assert json.loads(sanitized_line(captured.err)) == expected
