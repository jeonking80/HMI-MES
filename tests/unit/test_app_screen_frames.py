"""Application envelope contract and actual serialized resource bounds."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from apps.api.screen_frames import (
    APP_SCREEN_ADAPTER,
    APP_SCREEN_MAX_BYTES,
    ScreenScope,
    app_screen_schema,
    make_screen_frame,
)
from apps.api.screen_ws import ScreenWSConfig, _Outbound, _Pressure, _Sequence
from apps.api.snapshot import SampleEvidence, SnapshotHealth, SnapshotRead
from contracts.models import UINT64_MAX
from contracts.tags.validate import load_contract
from contracts.transport import LineView, ScreenResyncRequired, TagSample

AT = datetime(2026, 10, 21, 0, 0, 0, 999999, tzinfo=UTC)
SCOPE = ScreenScope(line_id="line", source_id="source", stream_id="stream")


def projection(*, full=False, scope=SCOPE):
    samples, evidence = [], []
    tags = load_contract()["tags"] if full else load_contract()["tags"][:1]
    for tag in tags:
        kind = tag["data_type"]
        # Six JSON bytes per character for controls, two for quotes/backslashes;
        # four UTF-8 bytes per astral codepoint for IDs (whitespace forbidden).
        value = {
            "Boolean": True,
            "Int32": 2**31 - 1,
            "UInt32": 2**32 - 1,
            "UInt64": UINT64_MAX,
            "Double": -1.7976931348623157e308,
            "String": "\x00" * 128,
        }[kind]
        if tag["enum"]:
            value = int(next(iter(load_contract()["enums"][tag["enum"]])))
        samples.append(
            TagSample(
                sample_id="😀" * 128,
                tag_id=tag["logical_name"],
                source_timestamp=AT,
                server_timestamp=AT,
                acquired_at=AT,
                status_code=2**32 - 1,
                reading={"data_type": kind, "value": value},
            )
        )
        evidence.append(
            SampleEvidence(
                tag_id=tag["logical_name"],
                batch_id="😀" * 128,
                capture_generation=UINT64_MAX,
                capture_seq=UINT64_MAX,
                captured_at=AT,
                stored_at=AT,
                fresh=False,
            )
        )
    view = LineView(
        line_id=scope.line_id,
        state="UNKNOWN",
        execution_id="😀" * 128,
        control_restricted=True,
        samples=tuple(samples),
    )
    return SnapshotRead(
        source_id=scope.source_id,
        stream_id=scope.stream_id,
        screen=_Sequence(scope.line_id).initial(view, AT),
        evidence=tuple(evidence),
        missing_tag_ids=tuple(t["logical_name"] for t in load_contract()["tags"] if t not in tags),
        health=SnapshotHealth(
            state="STALE",
            source_conflicted=True,
            reconciliation_required=True,
            heartbeat="ALARM",
            communication={
                "state": "ALARM",
                "connected": True,
                "read_age_seconds": 1.7976931348623157e308,
                "heartbeat_ages_seconds": {
                    name: 1.7976931348623157e308
                    for name in (
                        "Line.Heartbeat",
                        "Infeed.LinkHeartbeat",
                        "Sealer.LinkHeartbeat",
                        "Outfeed.LinkHeartbeat",
                    )
                },
            },
            communication_captured_at=AT,
            communication_stored_at=AT,
        ),
    )


def test_export_schema_and_uint64_json_strings():
    schema = app_screen_schema()
    assert json.loads(Path("contracts/schemas/app-screen.schema.json").read_text()) == schema
    data = projection()
    frame = make_screen_frame(SCOPE, data.screen, data)
    wire = frame.model_dump_json()
    assert APP_SCREEN_ADAPTER.validate_json(wire) == frame
    Draft202012Validator(schema).validate(json.loads(wire))
    for path in ("revision",):
        malformed = json.loads(wire)
        malformed["screen"][path] = 0
        assert not Draft202012Validator(schema).is_valid(malformed)
        with pytest.raises(ValidationError):
            APP_SCREEN_ADAPTER.validate_json(json.dumps(malformed))
    malformed = json.loads(wire)
    malformed["evidence"][0]["capture_seq"] = UINT64_MAX
    assert not Draft202012Validator(schema).is_valid(malformed)


@pytest.mark.parametrize("change", ["line", "source", "stream", "evidence", "missing", "unknown"])
def test_scope_evidence_and_missing_tag_consistency(change):
    data = projection()
    scope = (
        SCOPE.model_copy(update={change + "_id": "foreign"})
        if change in {"line", "source", "stream"}
        else SCOPE
    )
    if change in {"line", "source", "stream"}:
        with pytest.raises(ValueError):
            make_screen_frame(scope, data.screen, data)
        return
    fields = make_screen_frame(SCOPE, data.screen, data).model_dump()
    if change == "evidence":
        fields["evidence"] = ()
    elif change == "missing":
        fields["missing_tag_ids"] = (data.evidence[0].tag_id,)
    else:
        fields["missing_tag_ids"] = ("not.a.tag",)
    with pytest.raises(ValidationError):
        APP_SCREEN_ADAPTER.validate_python(fields)


def test_reset_has_only_version_scope_and_resync_without_metadata():
    reset = ScreenResyncRequired(
        kind="RESYNC_REQUIRED", sync_id="sync", emitted_at=AT, reason="BACKPRESSURE"
    )
    frame = make_screen_frame(SCOPE, reset)
    assert set(frame.model_dump()) == {"schema_version", "scope", "screen"}
    with pytest.raises(ValueError):
        make_screen_frame(SCOPE, reset, projection())
    wire = json.loads(frame.model_dump_json()) | {"health": projection().health.model_dump()}
    with pytest.raises(ValidationError):
        APP_SCREEN_ADAPTER.validate_python(wire)
    assert not Draft202012Validator(app_screen_schema()).is_valid(wire)


def test_full_48_tag_worst_escaped_payload_and_strict_utf8_queue_bounds():
    scope = ScreenScope(line_id="😀" * 128, source_id="😀" * 128, stream_id="😀" * 128)
    data = projection(full=True, scope=scope)
    assert len(data.screen.view.samples) == 48
    frame = make_screen_frame(scope, data.screen, data)
    size = len(frame.model_dump_json().encode("utf-8"))
    assert size < APP_SCREEN_MAX_BYTES
    queue = _Outbound(ScreenWSConfig(), enveloped=True)
    queue.put(frame)
    assert len(queue.queue.get_nowait().encode("utf-8")) == size
    exact = _Outbound(ScreenWSConfig(max_envelope_bytes=size), enveloped=True)
    exact.put(frame)
    too_small = _Outbound(ScreenWSConfig(max_envelope_bytes=size - 1), enveloped=True)
    with pytest.raises(_Pressure):
        too_small.put(frame)
    assert too_small.queue.empty()
    for _ in range(32):
        queue.put(frame)
    with pytest.raises(_Pressure) as caught:
        queue.put(frame)
    assert caught.value.can_notify and queue.queue.qsize() == 32
    legacy = _Outbound(ScreenWSConfig(max_outbound_bytes=131072))
    assert legacy.byte_limit == 65536
