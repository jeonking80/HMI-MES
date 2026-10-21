"""Protocol examples plus semantic boundary and compatibility checks."""

import json
import re
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import TypeAdapter, ValidationError

from contracts import (
    EVENT_ADAPTER,
    CommandRequest,
    CommandResult,
    CommandSubmission,
    EventPage,
    EventReadError,
    RunSummary,
    canonical_command_payload,
    export_schemas,
    same_command_payload,
)
from contracts.models import SCHEMA_MODELS, UINT64_MAX, UInt64

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"


def example(name):
    return json.loads((CONTRACTS / "examples" / f"{name}.json").read_text())


@pytest.mark.parametrize("name", SCHEMA_MODELS)
def test_exported_schema_is_current(name):
    checked_in = json.loads((CONTRACTS / "schemas" / f"{name}.schema.json").read_text())
    assert checked_in == export_schemas()[name]
    Draft202012Validator.check_schema(checked_in)
    Draft202012Validator(checked_in, format_checker=FormatChecker()).validate(example(name))


@pytest.mark.parametrize("name,model", SCHEMA_MODELS.items())
def test_normal_wire_examples_validate_and_round_trip(name, model):
    raw = (CONTRACTS / "examples" / f"{name}.json").read_text()
    if isinstance(model, TypeAdapter):
        parsed = model.validate_json(raw)
        assert model.validate_json(model.dump_json(parsed)) == parsed
    else:
        parsed = model.model_validate_json(raw)
        assert model.model_validate_json(parsed.model_dump_json()) == parsed


def test_all_six_commands_have_valid_examples():
    requests = [CommandRequest.model_validate_json(json.dumps(raw)) for raw in example("commands")]
    validator = Draft202012Validator(export_schemas()["command-request"])
    for raw in example("commands"):
        validator.validate(raw)
    assert {request.command.kind for request in requests} == {
        "LOAD_JOB",
        "START",
        "PAUSE",
        "END_RUN",
        "SET_PARAMETERS",
        "RESET_FAULT",
    }


@pytest.mark.parametrize(
    "name,schema_name,model",
    [
        ("command-rejected", "command-submission", CommandSubmission),
        ("command-expired", "command-result", CommandResult),
        ("command-outcome-unknown", "command-result", CommandResult),
    ],
)
def test_additional_error_examples_match_their_wire_schema(name, schema_name, model):
    raw = example(name)
    Draft202012Validator(export_schemas()[schema_name], format_checker=FormatChecker()).validate(
        raw
    )
    model.model_validate_json(json.dumps(raw))


def test_same_command_semantics_normalize_utc_numbers_and_defaults():
    original = next(raw for raw in example("commands") if raw["command"]["kind"] == "LOAD_JOB")
    equivalent = deepcopy(original)
    equivalent["requested_at"] = "2026-10-20T21:00:00+09:00"
    equivalent["expires_at"] = "2026-10-20T21:00:05+09:00"
    equivalent["expected_control_version"] = 42
    equivalent["command"]["args"]["parameters"]["temperature_c"] = 180
    left = CommandRequest.model_validate(original)
    right = CommandRequest.model_validate(equivalent)
    assert same_command_payload(left, right)
    assert canonical_command_payload(left) == canonical_command_payload(right)
    omitted_raw = example("command-request")
    del omitted_raw["command"]["args"]
    omitted = CommandRequest.model_validate(omitted_raw)
    explicit = example("command-request")
    explicit["command"]["args"] = {}
    assert same_command_payload(omitted, CommandRequest.model_validate(explicit))


@pytest.mark.parametrize(
    "field,value",
    [
        ("target_id", "line-2"),
        ("execution_id", "run-B"),
        ("requested_by", "operator-2"),
        ("expected_control_version", "43"),
        ("expires_at", "2026-10-20T12:00:06Z"),
    ],
)
def test_same_id_different_content_conflicts(field, value):
    raw = example("command-request")
    changed = {**raw, field: value}
    assert not same_command_payload(
        CommandRequest.model_validate(raw), CommandRequest.model_validate(changed)
    )


def test_argument_changes_are_not_idempotent():
    raw = next(raw for raw in example("commands") if raw["command"]["kind"] == "LOAD_JOB")
    changed = deepcopy(raw)
    changed["command"]["args"]["parameters"]["speed_m_per_min"] += 1
    assert not same_command_payload(
        CommandRequest.model_validate(raw), CommandRequest.model_validate(changed)
    )


def test_negative_zero_is_the_same_parameter_value():
    raw = next(raw for raw in example("commands") if raw["command"]["kind"] == "LOAD_JOB")
    raw["command"]["args"]["parameters"]["temperature_c"] = 0
    negative_zero = deepcopy(raw)
    negative_zero["command"]["args"]["parameters"]["temperature_c"] = -0.0
    assert same_command_payload(
        CommandRequest.model_validate(raw), CommandRequest.model_validate(negative_zero)
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("requested_at", "2026-10-20T12:00:00"),
        ("expires_at", "2026-10-20T11:59:59Z"),
        ("execution_id", None),
        ("unexpected", True),
    ],
)
def test_invalid_command_fields_are_rejected(field, value):
    raw = {**example("command-request"), field: value}
    with pytest.raises(ValidationError):
        CommandRequest.model_validate(raw)


def test_unknown_command_and_nonempty_start_args_rejected():
    raw = example("command-request")
    for command in [{"kind": "STOP"}, {"kind": "START", "args": {"force": True}}]:
        with pytest.raises(ValidationError):
            CommandRequest.model_validate({**raw, "command": command})


@pytest.mark.parametrize("value", [0, 2**53 + 1, UINT64_MAX])
def test_uint64_lossless_json(value):
    adapter = TypeAdapter(UInt64)
    wire = adapter.dump_json(adapter.validate_python(value))
    assert wire == f'"{value}"'.encode()
    assert adapter.validate_json(wire) == value
    assert re.fullmatch(adapter.json_schema()["pattern"], str(value))


@pytest.mark.parametrize(
    "value", [-1, UINT64_MAX + 1, True, 1.0, "01", "+1", "1.0", "18446744073709551616"]
)
def test_uint64_invalid_inputs(value):
    with pytest.raises(ValidationError):
        TypeAdapter(UInt64).validate_python(value)


def test_uint64_json_numbers_rejected_for_browser_safety():
    with pytest.raises(ValidationError):
        TypeAdapter(UInt64).validate_json("9007199254740993")
    assert not re.fullmatch(TypeAdapter(UInt64).json_schema()["pattern"], str(UINT64_MAX + 1))


def test_wire_primitives_do_not_coerce_numbers_strings_or_trailing_newlines():
    request = example("command-request")
    for value in [1792497600, "1792497600", "2026-10-20 12:00:00Z"]:
        with pytest.raises(ValidationError):
            CommandRequest.model_validate_json(json.dumps({**request, "requested_at": value}))
    for value in ["1\n", "18446744073709551615\n"]:
        with pytest.raises(ValidationError):
            TypeAdapter(UInt64).validate_json(json.dumps(value))
        assert list(Draft202012Validator(TypeAdapter(UInt64).json_schema()).iter_errors(value))
    with pytest.raises(ValidationError):
        CommandRequest.model_validate({**request, "target_id": "line-1\n"})
    assert list(
        Draft202012Validator(export_schemas()["command-request"]).iter_errors(
            {**request, "target_id": "line-1\n"}
        )
    )
    summary = example("run-summary")
    for value in [1, "true"]:
        with pytest.raises(ValidationError):
            RunSummary.model_validate({**summary, "final": value})
    for value in [True, "180"]:
        with pytest.raises(ValidationError):
            RunSummary.model_validate(
                {**summary, "parameters": {"temperature_c": value, "speed_m_per_min": 10}}
            )


def test_production_requires_original_execution_and_positive_quantity():
    raw = example("event")
    for bad in [
        {**raw, "execution_id": None},
        {key: value for key, value in raw.items() if key != "execution_id"},
        {**raw, "payload": {**raw["payload"], "quantity": "0"}},
    ]:
        with pytest.raises(ValidationError):
            EVENT_ADAPTER.validate_python(bad)


def test_nonproduction_event_can_have_no_execution():
    raw = example("event")
    raw.update(
        kind="STATE_CHANGED",
        execution_id=None,
        payload={"previous_state": "STOPPED", "state": "READY", "control_version": "42"},
    )
    assert EVENT_ADAPTER.validate_python(raw).execution_id is None


@pytest.mark.parametrize(
    "change",
    [
        {"page_last_seq": "0"},
        {"has_more": True},
        {"after_seq": "1"},
        {"retained_from_seq": "3"},
        {"retained_through_seq": "0"},
    ],
)
def test_page_bounds_and_cursor_constraints(change):
    with pytest.raises(ValidationError):
        EventPage.model_validate({**example("event-page"), **change})


def test_page_rejects_gaps_wrong_stream_and_duplicate_ids():
    raw = example("event-page")
    for changes in [{"seq": "2"}, {"stream_id": "another-stream"}, {"source_id": "other-source"}]:
        with pytest.raises(ValidationError):
            EventPage.model_validate({**raw, "events": [{**raw["events"][0], **changes}]})
    duplicate = {**raw["events"][0], "seq": "2"}
    with pytest.raises(ValidationError):
        EventPage.model_validate(
            {
                **raw,
                "events": [*raw["events"], duplicate],
                "page_last_seq": "2",
                "retained_through_seq": "2",
            }
        )


def test_empty_page_and_retained_boundary_are_valid():
    page = example("event-page")
    page.update(after_seq="1", events=[], page_last_seq="1")
    assert EventPage.model_validate(page).events == ()
    page.update(after_seq="0", retained_from_seq="1", retained_through_seq="0", page_last_seq="0")
    assert EventPage.model_validate(page).retained_through_seq == 0


def test_gap_response_must_describe_exact_missing_interval():
    raw = example("event-read-error")
    assert EventReadError.model_validate(raw).code == "RETENTION_GAP"
    for changes in [{"missing_from_seq": "2"}, {"missing_through_seq": "7"}, {"after_seq": "9"}]:
        with pytest.raises(ValidationError):
            EventReadError.model_validate({**raw, **changes})


def test_expiry_and_unknown_outcome_have_distinct_meanings():
    expired = CommandResult.model_validate(example("command-expired"))
    unknown = CommandResult.model_validate(example("command-outcome-unknown"))
    assert expired.delivered_at is None and expired.completed_at is not None
    assert unknown.delivered_at is not None and unknown.completed_at is None
    with pytest.raises(ValidationError):
        CommandResult.model_validate(
            {**example("command-expired"), "delivered_at": "2026-10-20T12:00:01Z"}
        )
    with pytest.raises(ValidationError):
        CommandResult.model_validate(
            {**example("command-outcome-unknown"), "completed_at": "2026-10-20T12:00:10Z"}
        )


def test_rejection_and_success_require_reasons_and_completion_evidence():
    assert CommandSubmission.model_validate(example("command-rejected")).disposition == "REJECTED"
    with pytest.raises(ValidationError):
        CommandSubmission.model_validate({**example("command-rejected"), "reason_code": None})
    with pytest.raises(ValidationError):
        CommandResult.model_validate({**example("command-result"), "evidence": None})
    with pytest.raises(ValidationError):
        CommandResult.model_validate(
            {**example("command-result"), "status": "REQUESTED", "completed_at": None}
        )
    with pytest.raises(ValidationError):
        CommandResult.model_validate(
            {
                **example("command-expired"),
                "evidence": example("command-result")["evidence"],
            }
        )


@pytest.mark.parametrize(
    "change",
    [
        {"wip_quantity": 1},
        {"input_quantity": "3"},
        {"state": "ENDING"},
        {"final": False},
        {"ended_at": None},
        {"last_event_seq": "0"},
    ],
)
def test_final_summary_requires_reconciled_quantity_and_closed_boundary(change):
    with pytest.raises(ValidationError):
        RunSummary.model_validate({**example("run-summary"), **change})


def test_nonfinal_summary_preserves_wip_and_cannot_pretend_to_be_ended():
    raw = example("run-summary")
    raw.update(state="ENDING", final=False, ended_at=None, input_quantity="2", wip_quantity=1)
    assert RunSummary.model_validate(raw).wip_quantity == 1
    with pytest.raises(ValidationError):
        RunSummary.model_validate({**raw, "ended_at": "2026-10-20T12:01:00Z"})
