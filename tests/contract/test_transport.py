"""Transport contract checks, independent schema examples and meaningful boundaries."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from contracts.models import UINT64_MAX
from contracts.transport import (
    SCREEN_ADAPTER,
    BatchBody,
    DurableAck,
    GatewayBatch,
    GatewayCommunication,
    ScreenSnapshot,
    ScreenUpdate,
    TagSample,
    batch_digest,
    canonical_batch_body,
    export_transport_schemas,
    validate_ack,
    validate_screen_update,
)

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"


def example(name):
    return json.loads((CONTRACTS / "examples" / f"transport-{name}.json").read_text())


HEARTBEATS = (
    "Line.Heartbeat",
    "Infeed.LinkHeartbeat",
    "Sealer.LinkHeartbeat",
    "Outfeed.LinkHeartbeat",
)


def communication(*, state="GOOD", connected=True, read_age=0.0, heartbeat_age=0.0):
    return {
        "state": state,
        "connected": connected,
        "read_age_seconds": read_age,
        "heartbeat_ages_seconds": dict.fromkeys(HEARTBEATS, heartbeat_age),
    }


@pytest.mark.parametrize("name", ["batch", "batch-gap"])
def test_legacy_canonical_bytes_and_fixture_hashes_are_unchanged(name):
    raw = example(name)
    parsed = GatewayBatch.model_validate_json(json.dumps(raw))
    assert canonical_batch_body(parsed.body) == json.dumps(
        raw["body"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert batch_digest(parsed.body) == raw["content_sha256"]
    assert "communication" not in parsed.body.model_dump(mode="json")
    assert json.loads(parsed.model_dump_json()) == raw
    # Defaulted legacy fields remain defaulted without injecting the new field.
    body = BatchBody.model_validate(
        {key: value for key, value in raw["body"].items() if key != "schema_version"}
    )
    assert batch_digest(body) == raw["content_sha256"]


def test_version_11_sample_health_round_trip_schema_and_hash_binding():
    raw = {**example("batch")["body"], "schema_version": "1.1", "communication": communication()}
    raw["samples"][0]["acquired_at"] = raw["captured_at"]
    body = BatchBody.model_validate(raw)
    batch = GatewayBatch(body=body, content_sha256=batch_digest(body))
    encoded = json.loads(batch.model_dump_json())
    assert encoded["body"]["communication"] == communication()
    assert GatewayBatch.model_validate_json(batch.model_dump_json()) == batch
    schema = export_transport_schemas()["transport-batch"]
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(encoded)
    changed = deepcopy(encoded)
    changed["body"]["communication"]["read_age_seconds"] = 0.1
    with pytest.raises(ValidationError, match="digest mismatch"):
        GatewayBatch.model_validate(changed)


@pytest.mark.parametrize("acquired_at", ["2026-10-20T12:00:30Z", "2026-10-20T12:00:32Z"])
def test_version_11_rejects_stale_or_future_sample_capture_evidence(acquired_at):
    raw = {**example("batch")["body"], "schema_version": "1.1", "communication": communication()}
    raw["samples"][0]["acquired_at"] = acquired_at
    with pytest.raises(ValidationError, match="acquired_at must equal batch captured_at"):
        BatchBody.model_validate(raw)
    # Legacy batches retain their historical capture/acquisition semantics.
    raw.pop("communication")
    assert BatchBody.model_validate({**raw, "schema_version": "1.0"}).samples


def test_version_11_capture_equality_preserves_original_source_and_server_timestamps():
    raw = {**example("batch")["body"], "schema_version": "1.1", "communication": communication()}
    sample = raw["samples"][0]
    sample.update(
        acquired_at=raw["captured_at"],
        source_timestamp="2026-10-19T00:00:00Z",
        server_timestamp="2026-10-21T00:00:00Z",
    )
    parsed = BatchBody.model_validate(raw)
    assert parsed.model_dump(mode="json")["samples"][0] == sample


def test_health_version_requirements_are_enforced_by_model_and_exported_schema():
    schema = export_transport_schemas()["transport-batch"]
    for version, health in [("1.0", communication()), ("1.0", None), ("1.1", None)]:
        raw = deepcopy(example("batch"))
        raw["body"].update(schema_version=version, communication=health)
        with pytest.raises(ValidationError):
            BatchBody.model_validate(raw["body"])
        assert list(Draft202012Validator(schema).iter_errors(raw))
    raw = deepcopy(example("batch"))
    raw["body"]["schema_version"] = "1.1"
    with pytest.raises(ValidationError, match="require communication"):
        BatchBody.model_validate(raw["body"])
    assert list(Draft202012Validator(schema).iter_errors(raw))
    for name in ("batch", "batch-gap"):
        event_or_gap = {**example(name)["body"], "schema_version": "1.1", "samples": []}
        body = BatchBody.model_validate(event_or_gap)
        assert body.communication is None
        batch = GatewayBatch(body=body, content_sha256=batch_digest(body))
        Draft202012Validator(schema).validate(json.loads(batch.model_dump_json()))


@pytest.mark.parametrize(
    "state,connected,read_age,heartbeat_age",
    [
        ("GOOD", True, 0, 2.999),
        ("DELAYED", True, 3.0, 0),
        ("DELAYED", False, 0, 4.999),
        ("ALARM", True, 0, 5.0),
        ("ALARM", False, 123.0, None),
        ("UNKNOWN", False, None, None),
        ("UNKNOWN", True, 0.0, None),
        # OPC startup reports finite fallback ages while observations are still unseen.
        ("UNKNOWN", True, 0.0, 0.0),
    ],
)
def test_communication_thresholds_and_unknown_evidence(state, connected, read_age, heartbeat_age):
    raw = communication(
        state=state, connected=connected, read_age=read_age, heartbeat_age=heartbeat_age
    )
    parsed = GatewayCommunication.model_validate(raw)
    assert GatewayCommunication.model_validate_json(parsed.model_dump_json()) == parsed


@pytest.mark.parametrize(
    "state,connected,read_age,heartbeat_age",
    [
        ("GOOD", False, 0, 0),
        ("GOOD", True, None, 0),
        ("GOOD", True, 0, None),
        ("GOOD", True, 3, 0),
        ("DELAYED", True, 2.999, 0),
        ("DELAYED", True, 0, 5),
        ("ALARM", True, 0, 4.999),
        ("UNKNOWN", False, 3, None),
        ("UNKNOWN", True, 0, 5),
    ],
)
def test_contradictory_communication_state_is_rejected(state, connected, read_age, heartbeat_age):
    with pytest.raises(ValidationError, match="contradicts"):
        GatewayCommunication.model_validate(
            communication(
                state=state, connected=connected, read_age=read_age, heartbeat_age=heartbeat_age
            )
        )


@pytest.mark.parametrize("value", [-0.001, float("nan"), float("inf"), -float("inf"), True, "1"])
@pytest.mark.parametrize("target", ["read", "heartbeat"])
def test_communication_ages_are_exact_finite_nonnegative_numbers(value, target):
    raw = communication(state="UNKNOWN")
    if target == "read":
        raw["read_age_seconds"] = value
    else:
        raw["heartbeat_ages_seconds"][HEARTBEATS[0]] = value
    with pytest.raises(ValidationError):
        GatewayCommunication.model_validate(raw)


@pytest.mark.parametrize("change", ["missing", "extra", "short_name", "coerced_connected"])
def test_communication_heartbeat_names_and_connection_flag_are_strict(change):
    raw = communication()
    if change == "missing":
        del raw["heartbeat_ages_seconds"][HEARTBEATS[0]]
    elif change == "extra":
        raw["heartbeat_ages_seconds"]["Other.Heartbeat"] = 0.0
    elif change == "short_name":
        raw["heartbeat_ages_seconds"]["Line"] = raw["heartbeat_ages_seconds"].pop(HEARTBEATS[0])
    else:
        raw["connected"] = 1
    with pytest.raises(ValidationError):
        GatewayCommunication.model_validate(raw)


@pytest.mark.parametrize("name", ["batch", "ack", "screen"])
def test_exported_schema_is_current_and_valid(name):
    schema = json.loads((CONTRACTS / "schemas" / f"transport-{name}.schema.json").read_text())
    assert schema == export_transport_schemas()[f"transport-{name}"]
    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize(
    "name,schema_name,model",
    [
        ("batch", "batch", GatewayBatch),
        ("batch-gap", "batch", GatewayBatch),
        ("batch-capture-time", "batch", GatewayBatch),
        ("ack", "ack", DurableAck),
        ("ack-quarantined", "ack", DurableAck),
        ("ack-gap", "ack", DurableAck),
        ("screen", "screen", SCREEN_ADAPTER),
        ("screen-update", "screen", SCREEN_ADAPTER),
        ("screen-resync", "screen", SCREEN_ADAPTER),
    ],
)
def test_examples_validate_in_schema_and_model(name, schema_name, model):
    raw = example(name)
    schema = export_transport_schemas()[f"transport-{schema_name}"]
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(raw)
    if model is SCREEN_ADAPTER:
        parsed = model.validate_json(json.dumps(raw))
        assert model.validate_json(model.dump_json(parsed)) == parsed
    else:
        parsed = model.model_validate_json(json.dumps(raw))
        assert model.model_validate_json(parsed.model_dump_json()) == parsed


@pytest.mark.parametrize("value", [2**53 + 1, 2**63, UINT64_MAX])
def test_uint64_tag_values_round_trip_without_float_conversion(value):
    raw = example("batch")["body"]["samples"][0]
    raw["reading"]["value"] = str(value)
    sample = TagSample.model_validate_json(json.dumps(raw))
    assert sample.reading.value == value
    assert json.loads(sample.model_dump_json())["reading"]["value"] == str(value)
    with pytest.raises(ValidationError):
        TagSample.model_validate_json(
            json.dumps({**raw, "reading": {**raw["reading"], "value": value}})
        )


@pytest.mark.parametrize(
    "tag,reading",
    [
        ("Line.State", {"data_type": "Int32", "value": 2}),
        ("Line.EStop", {"data_type": "Boolean", "value": False}),
        ("Line.WipCount", {"data_type": "UInt32", "value": 1}),
        ("Line.ActiveExecutionId", {"data_type": "String", "value": ""}),
        ("Sealer.TemperaturePV", {"data_type": "Double", "value": 180}),
    ],
)
def test_known_dictionary_types_accept_exact_values(tag, reading):
    raw = example("batch")["body"]["samples"][0]
    sample = TagSample.model_validate({**raw, "tag_id": tag, "reading": reading})
    assert sample.reading.value == reading["value"]


@pytest.mark.parametrize(
    "change,match",
    [
        ({"tag_id": "Missing.Tag"}, "tag must exist"),
        ({"reading": {"data_type": "Double", "value": 1.0}}, "declared OPC data type"),
        ({"reading": {"data_type": "UInt64", "value": None}}, "Good status"),
        ({"status_code": -1}, "greater than or equal"),
        ({"acquired_at": "2026-10-20T12:00:00"}, "explicit UTC offset"),
        ({"token": "unexpected"}, "Extra inputs"),
    ],
)
def test_malformed_samples_rejected(change, match):
    raw = {**example("batch")["body"]["samples"][0], **change}
    with pytest.raises(ValidationError, match=match):
        TagSample.model_validate(raw)


@pytest.mark.parametrize("status", [0x40000000, 0x80000000])
def test_bad_uncertain_values_preserve_raw_status_and_missing_timestamps(status):
    raw = example("batch")["body"]["samples"][0]
    raw.update(status_code=status, source_timestamp=None, server_timestamp=None)
    raw["reading"]["value"] = None
    sample = TagSample.model_validate(raw)
    assert sample.status_code == status and sample.reading.value is None


def test_unknown_enum_and_coerced_boolean_numeric_values_rejected():
    raw = example("batch")["body"]["samples"][0]
    for tag, reading in [
        ("Line.State", {"data_type": "Int32", "value": 99}),
        ("Line.EStop", {"data_type": "Boolean", "value": "false"}),
        ("Line.WipCount", {"data_type": "UInt32", "value": True}),
        ("Sealer.TemperaturePV", {"data_type": "Double", "value": "180"}),
        ("Sealer.TemperaturePV", {"data_type": "Double", "value": float("inf")}),
    ]:
        with pytest.raises(ValidationError):
            TagSample.model_validate({**raw, "tag_id": tag, "reading": reading})


def test_digest_normalizes_object_order_offsets_and_numeric_types_but_binds_all_ids():
    raw = example("batch")["body"]
    equivalent = dict(reversed(list(raw.items())))
    equivalent.update(captured_at="2026-10-20T21:00:31+09:00", capture_generation=1)
    left = BatchBody.model_validate(raw)
    assert batch_digest(left) == batch_digest(BatchBody.model_validate(equivalent))
    for field, value in [
        ("batch_id", "another-batch"),
        ("gateway_id", "another-gateway"),
        ("capture_generation", "2"),
        ("capture_seq", "2"),
    ]:
        assert batch_digest(left) != batch_digest(BatchBody.model_validate({**raw, field: value}))
    changed = deepcopy(example("batch"))
    changed["body"]["samples"][0]["reading"]["value"] = "9007199254740994"
    with pytest.raises(ValidationError, match="digest mismatch"):
        GatewayBatch.model_validate(changed)


@pytest.mark.parametrize("field", ["batch_id", "gateway_id", "source_id", "stream_id"])
@pytest.mark.parametrize("value", ["", "contains space", "newline\n"])
def test_invalid_batch_identifiers(field, value):
    with pytest.raises(ValidationError):
        BatchBody.model_validate({**example("batch")["body"], field: value})


def test_batch_rejects_empty_duplicate_mixed_source_and_overlapping_gap_content():
    raw = example("batch")["body"]
    event = raw["events"][0]
    sample = raw["samples"][0]
    for change in [
        {"samples": [], "events": []},
        {"samples": [sample, sample]},
        {"samples": [sample, {**sample, "sample_id": "sample-002"}]},
        {"events": [event, event]},
        {"events": [{**event, "source_id": "other-source"}]},
        {"events": [{**event, "stream_id": "other-stream"}]},
        {"gaps": [{**example("batch-gap")["body"]["gaps"][0], "from_seq": "1"}]},
    ]:
        with pytest.raises(ValidationError):
            BatchBody.model_validate({**raw, **change})
    with pytest.raises(ValidationError):
        BatchBody.model_validate({**raw, "samples": [sample] * 49})


def test_ack_binding_accepts_persistence_and_quarantine_without_claiming_application():
    batch = GatewayBatch.model_validate(example("batch"))
    applied = DurableAck.model_validate(example("ack"))
    quarantined = DurableAck.model_validate(example("ack-quarantined"))
    validate_ack(batch, applied)
    validate_ack(batch, quarantined)
    assert quarantined.progress.verified_through_seq == 0
    assert quarantined.progress.latest_ingested_seq == 1
    validate_ack(
        GatewayBatch.model_validate(example("batch-gap")),
        DurableAck.model_validate(example("ack-gap")),
    )


def test_ack_cannot_claim_uncommitted_or_verify_quarantined_history():
    raw = example("ack-quarantined")
    for change in [
        {"durability": "PENDING"},
        {"committed_at": None},
        {"progress": {**raw["progress"], "verified_through_seq": "1"}},
        {"events": [{**raw["events"][0], "reason": None}]},
    ]:
        with pytest.raises(ValidationError):
            DurableAck.model_validate({**raw, **change})
    gap_ack = example("ack-gap")
    with pytest.raises(ValidationError):
        DurableAck.model_validate(
            {**gap_ack, "progress": {**gap_ack["progress"], "verified_through_seq": "9"}}
        )


@pytest.mark.parametrize(
    "field,value",
    [("batch_id", "other-batch"), ("gateway_id", "other-gateway"), ("content_sha256", "0" * 64)],
)
def test_wrong_ack_identity_or_hash_cannot_release_buffer(field, value):
    ack = DurableAck.model_validate({**example("ack"), field: value})
    with pytest.raises(ValueError, match="identity/hash"):
        validate_ack(GatewayBatch.model_validate(example("batch")), ack)


def test_incomplete_ack_and_missing_durable_gap_are_rejected():
    ack = DurableAck.model_validate({**example("ack"), "events": []})
    with pytest.raises(ValueError, match="every batch event"):
        validate_ack(GatewayBatch.model_validate(example("batch")), ack)
    gap_ack = example("ack-gap")
    gap_ack["recorded_gaps"] = []
    with pytest.raises(ValueError, match="missing interval"):
        validate_ack(
            GatewayBatch.model_validate(example("batch-gap")), DurableAck.model_validate(gap_ack)
        )


def test_stale_gap_receipt_does_not_reopen_history_that_was_already_backfilled():
    body = example("batch-gap")["body"]
    body["gaps"][0]["irrecoverable"] = False
    parsed_body = BatchBody.model_validate(body)
    batch = GatewayBatch(body=parsed_body, content_sha256=batch_digest(parsed_body))
    ack = example("ack-gap")
    ack.update(content_sha256=batch.content_sha256, recorded_gaps=body["gaps"])
    ack["progress"].update(verified_through_seq="9", unresolved_gaps=[])
    validate_ack(batch, DurableAck.model_validate(ack))


def test_screen_updates_are_contiguous_and_same_sync_and_line():
    snapshot = ScreenSnapshot.model_validate(example("screen"))
    update = ScreenUpdate.model_validate(example("screen-update"))
    validate_screen_update(snapshot, update)
    for change in [
        {"sync_id": "reconnected-sync"},
        {"base_revision": "43", "revision": "44"},
        {"view": {**example("screen-update")["view"], "line_id": "other-line"}},
    ]:
        parsed = ScreenUpdate.model_validate({**example("screen-update"), **change})
        with pytest.raises(ValueError, match="fresh snapshot"):
            validate_screen_update(snapshot, parsed)
    with pytest.raises(ValidationError, match="immediately follow"):
        ScreenUpdate.model_validate({**example("screen-update"), "revision": "44"})


def test_unknown_state_requires_restricted_control_and_screen_size_is_bounded():
    raw = example("screen")
    with pytest.raises(ValidationError, match="restricted control"):
        ScreenSnapshot.model_validate({**raw, "view": {**raw["view"], "state": "UNKNOWN"}})
    with pytest.raises(ValidationError):
        ScreenSnapshot.model_validate(
            {**raw, "view": {**raw["view"], "samples": raw["view"]["samples"] * 49}}
        )
    with pytest.raises(ValidationError):
        ScreenSnapshot.model_validate({**raw, "sync_id": ""})


def test_version_12_capture_evidence_round_trip_and_legacy_hashes():
    raw = {**example("batch")["body"], "schema_version": "1.1", "communication": communication()}
    raw["samples"][0]["acquired_at"] = raw["captured_at"]
    legacy = BatchBody.model_validate(raw)
    assert canonical_batch_body(legacy) == json.dumps(
        raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert "capture_time" not in legacy.model_dump(mode="json")
    for version in ("1.0", "1.1"):
        old = {**raw, "schema_version": version, "capture_time": None}
        if version == "1.0":
            old.pop("communication")
        with pytest.raises(ValidationError):
            BatchBody.model_validate(old)
        assert list(
            Draft202012Validator(export_transport_schemas()["transport-batch"]).iter_errors(
                {"body": old, "content_sha256": "0" * 64}
            )
        )
    raw["schema_version"] = "1.2"
    for missing in ("capture_time", "communication"):
        candidate = {
            **raw,
            "capture_time": {
                "clock_epoch": "collector-1",
                "monotonic_seconds": 10.0,
                "utc_read_span_seconds": 0.01,
            },
        }
        candidate.pop(missing)
        with pytest.raises(ValidationError):
            BatchBody.model_validate(candidate)
    raw["capture_time"] = {
        "clock_epoch": "collector-1",
        "monotonic_seconds": 10.0,
        "utc_read_span_seconds": 0.01,
    }
    body = BatchBody.model_validate(raw)
    batch = GatewayBatch(body=body, content_sha256=batch_digest(body))
    assert GatewayBatch.model_validate_json(batch.model_dump_json()) == batch
    Draft202012Validator(export_transport_schemas()["transport-batch"]).validate(
        json.loads(batch.model_dump_json())
    )
    changed = json.loads(batch.model_dump_json())
    changed["body"]["capture_time"]["clock_epoch"] = "collector-2"
    with pytest.raises(ValidationError, match="digest"):
        GatewayBatch.model_validate(changed)


@pytest.mark.parametrize(
    "field,value",
    [
        ("monotonic_seconds", -1.0),
        ("monotonic_seconds", float("inf")),
        ("utc_read_span_seconds", -0.01),
        ("utc_read_span_seconds", float("nan")),
        ("utc_read_span_seconds", 11.0),
    ],
)
def test_capture_bracket_rejects_invalid_coordinates(field, value):
    from contracts.transport import CaptureTime

    with pytest.raises(ValidationError):
        CaptureTime.model_validate(
            {
                "clock_epoch": "collector-1",
                "monotonic_seconds": 10.0,
                "utc_read_span_seconds": 0.01,
                field: value,
            }
        )
