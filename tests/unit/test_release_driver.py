"""Compile the actual release wrapper; fake Popen never starts a child or service."""

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).parents[2] / "scripts/check-release.py"
SPEC = importlib.util.spec_from_file_location("release_driver", PATH)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def run_wrapper(tmp_path, monkeypatch, policies, *, count=1, driver_exit=0):
    driver = tmp_path / "driver.py"
    proof = tmp_path / "proof.json"
    driver.write_text(
        "import subprocess,sys\n"
        "def main():\n"
        "    env={'RELEASE_TEST': 'preserve'}\n"
        "    subprocess.Popen([sys.executable,'-m','apps.api','serve-user'], env=env)\n"
        "    subprocess.Popen([sys.executable,'-m','apps.gateway','serve'], env=env)\n"
        f"    for _ in range({count}):\n"
        "        subprocess.Popen([sys.executable,'-m','apps.api','serve',"
        f"'--port','12345',*{policies!r}], env=env, stdout=-1)\n"
        f"    return {driver_exit}\n"
    )
    original_sha = hashlib.sha256(driver.read_bytes()).hexdigest()
    calls = []

    def fake_popen(arguments, *args, **kwargs):
        calls.append((arguments, args, kwargs))
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sys, "argv", ["wrapper.py", str(driver), str(proof)])
    try:
        exec(compile(release.INDEPENDENT_DRIVER_WRAPPER, "wrapper.py", "exec"), {})
    except (SystemExit, RuntimeError) as error:
        result = error
    assert subprocess.Popen is fake_popen
    data = json.loads(proof.read_text())
    assert data["original_driver_sha256"] == original_sha
    assert data["original_driver_unchanged"] is True
    for index, module in enumerate(("apps.api", "apps.gateway")):
        command, positional, kwargs = calls[index]
        assert command == [sys.executable, "-m", module, "serve-user" if index == 0 else "serve"]
        assert positional == ()
        assert kwargs == {"env": {"RELEASE_TEST": "preserve"}}
    return result, data, calls


@pytest.mark.parametrize(
    "policy,action",
    [
        ([], "appended"),
        (["--trend-clock-policy", "same-host"], "reused"),
        (["--trend-clock-policy=same-host"], "reused"),
    ],
)
def test_wrapper_appends_only_if_absent_and_reports_actual_action(
    tmp_path, monkeypatch, policy, action
):
    result, proof, calls = run_wrapper(tmp_path, monkeypatch, policy)
    assert isinstance(result, SystemExit) and result.code == 0
    assert proof["exit_code"] == 0
    assert proof["extensions"] == [
        {
            "module": "apps.api",
            "subcommand": "serve",
            "action": action,
            "policy": "same-host",
            "appended": ["--trend-clock-policy", "same-host"] if action == "appended" else [],
        }
    ]
    command, positional, kwargs = calls[2]
    assert command == [
        sys.executable,
        "-m",
        "apps.api",
        "serve",
        "--port",
        "12345",
        *(policy or ["--trend-clock-policy", "same-host"]),
    ]
    assert positional == ()
    assert kwargs == {"env": {"RELEASE_TEST": "preserve"}, "stdout": -1}


@pytest.mark.parametrize(
    "policy",
    [
        ["--trend-clock-policy", "unknown"],
        ["--trend-clock-policy"],
        ["--trend-clock-policy", "--port", "54321"],
        ["--trend-clock-policy="],
        ["--trend-clock-policy=unknown"],
        ["--trend-clock-policy-invalid", "same-host"],
        ["--trend-clock-pol", "same-host"],
        ["--trend-clock-policy", "same-host", "--trend-clock-policy", "same-host"],
        ["--trend-clock-policy=same-host", "--trend-clock-policy", "same-host"],
    ],
)
def test_wrapper_refuses_conflict_malformed_and_duplicate_before_launch(
    tmp_path, monkeypatch, policy
):
    result, proof, calls = run_wrapper(tmp_path, monkeypatch, policy)
    assert isinstance(result, RuntimeError)
    assert proof["exit_code"] == 1
    assert proof["extensions"] == []
    assert len(calls) == 2


@pytest.mark.parametrize("count", [0, 2])
def test_wrapper_requires_exactly_one_internal_serve(tmp_path, monkeypatch, count):
    result, proof, _ = run_wrapper(tmp_path, monkeypatch, [], count=count)
    assert isinstance(result, SystemExit) and result.code == 1
    assert proof["exit_code"] == 1


def test_wrapper_preserves_driver_failure(tmp_path, monkeypatch):
    result, proof, _ = run_wrapper(tmp_path, monkeypatch, [], driver_exit=7)
    assert isinstance(result, SystemExit) and result.code == 7
    assert proof["exit_code"] == 7
