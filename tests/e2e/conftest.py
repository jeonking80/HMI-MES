"""Acceptance shares the existing disposable PostgreSQL and real socket fixtures."""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

INTEGRATION = Path(__file__).resolve().parents[1] / "integration"
sys.path.insert(0, str(INTEGRATION))
sys.path.insert(0, str(Path(__file__).parent))

_spec = importlib.util.spec_from_file_location(
    "acceptance_pg_fixtures", INTEGRATION / "conftest.py"
)
_pg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pg)
engine, migrated, postgres_url = _pg.engine, _pg.migrated, _pg.postgres_url
from acceptance_runtime import AcceptanceEvidence  # noqa: E402
from test_gateway_pipeline import pipeline  # noqa: E402,F401
from test_reports import state  # noqa: E402,F401


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, "acceptance_report_" + report.when, report)
    if report.when == "teardown":
        reports = [
            getattr(item, "acceptance_report_" + phase, None)
            for phase in ("setup", "call", "teardown")
        ]
        failed = [value for value in reports if value and value.failed]
        overall = (
            "error"
            if any(value.when != "call" for value in failed)
            else "failed"
            if failed
            else "skipped"
            if any(value and value.skipped for value in reports)
            else "passed"
        )
        for evidence in getattr(item, "acceptance_evidence_cases", []):
            evidence.record(pytest_phases={value.when: value.outcome for value in reports if value})
            evidence.finish(
                overall, failure="\n".join(str(value.longrepr) for value in failed) or None
            )
            item.user_properties.append(
                ("acceptance_assertions", json.dumps(evidence.data["assertions"], default=str))
            )
        report.user_properties = list(item.user_properties)


@pytest.fixture
def acceptance_evidence(request, tmp_path):
    cases = []

    def factory(case_id):
        evidence = AcceptanceEvidence(
            case_id, os.environ.get("HMI_ACCEPTANCE_ARTIFACTS", str(tmp_path / "evidence"))
        )
        cases.append(evidence)
        request.node.user_properties.append(("acceptance_id", evidence.data["acceptance_id"]))
        request.node.user_properties.append(("evidence_json", str(evidence.path)))
        return evidence

    request.node.acceptance_evidence_cases = cases
    return factory
