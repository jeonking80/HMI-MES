"""Runner failure tests, never substituted for real-duration acceptance."""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/e2e"))
import long_run_runtime  # noqa: E402
from acceptance_runtime import AcceptanceEvidence  # noqa: E402
from long_run_runtime import distribution, extended_driver  # noqa: E402


def test_latency_empty_or_negative_observations_cannot_pass():
    for values in ([], [-1], [float("nan")], [float("inf")]):
        with pytest.raises(ValueError):
            distribution(values)
    assert distribution([0.2, 0.5, 1.2])["p95"] == 1.2


def test_transport_failure_preserves_error_but_redacts_credentials():
    failure = (
        "read ECONNRESET\n - cookie: simulator_session=secret\n"
        " - Authorization: bearer token\nopaque"
    )
    safe = long_run_runtime.redact(failure, ["opaque"])
    assert "ECONNRESET" in safe
    assert "secret" not in safe and "bearer token" not in safe and "opaque" not in safe


def test_driver_hook_is_read_only_and_preserves_default_physics(tmp_path):
    path = ROOT / "scripts/check-operable-system.py"
    before = path.read_bytes()
    evidence = AcceptanceEvidence("CX-19", tmp_path)

    async def hook(_live):
        raise RuntimeError("not executed")

    driver = extended_driver(hook, evidence)
    assert path.read_bytes() == before
    assert driver.long_run_hook is hook
    assert evidence.data["driver_sha256"]
    assert "4294967295" in str(driver.exercise.__code__.co_consts)


@pytest.mark.parametrize("port_open", [False, True])
def test_outage_failure_restarts_identical_api_under_driver_supervision(
    monkeypatch, tmp_path, port_open
):
    launches = []

    class Process:
        args = ["python", "-m", "apps.api", "serve"]
        returncode = None

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout):
            return self.returncode

        def poll(self):
            return self.returncode

    class Connection:
        closed = False

        def close(self):
            self.closed = True

    connection = Connection()

    def connect(*_args, **_kwargs):
        if port_open:
            return connection
        raise ConnectionRefusedError()

    replacement = Process()

    def launch(args, **kwargs):
        launches.append((args, kwargs))
        return replacement

    class Observer:
        def __init__(self, *_args):
            self.calls = 0

        def sample(self):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("disk read failed")
            return {}

    async def no_wait(_seconds):
        return

    evidence = AcceptanceEvidence("CX-19", tmp_path)
    monkeypatch.setattr(long_run_runtime, "Observation", Observer)
    monkeypatch.setattr(long_run_runtime.asyncio, "sleep", no_wait)
    monkeypatch.setattr(long_run_runtime.socket, "create_connection", connect)
    monkeypatch.setattr(long_run_runtime.subprocess, "Popen", launch)
    old_api = Process()
    with (tmp_path / "internal-api.log").open("wb") as log:
        live = {
            "children": [old_api],
            "handles": [log],
            "env": {"TEST": "opaque"},
            "intake_port": 12345,
        }
        with pytest.raises(
            AssertionError if port_open else RuntimeError,
            match="port remained open" if port_open else "disk read failed",
        ):
            asyncio.run(long_run_runtime.outage(live, evidence, tmp_path))
        assert live["children"] == [replacement]
        assert launches[0][0] == old_api.args
        assert launches[0][1]["env"] == live["env"]
        assert launches[0][1]["stdout"] is log
        assert launches[0][1]["stderr"] is log
    if port_open:
        assert connection.closed


def test_endurance_missing_hardware_fails_before_driver_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["runner", "--artifacts", str(tmp_path)])

    def forbidden(*_args):
        pytest.fail("Hardware gate must precede actual driver launch")

    monkeypatch.setattr(long_run_runtime, "extended_driver", forbidden)
    assert long_run_runtime.main("CX-21") == 1
    import json

    result = json.loads(next(tmp_path.glob("CX-21-*.json")).read_text())
    assert result["outcome"] == "blocked"
    assert result["assertions"][0]["name"] == "hardware_requirement_established"
