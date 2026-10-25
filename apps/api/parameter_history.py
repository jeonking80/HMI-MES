"""Original parameter bounds and durable evidence for execution adjustment history."""

import hashlib
import json

from contracts.models import Parameters
from contracts.parameter_history import validate_parameter_history, validate_speed_adjustment


def validate_parameter_change(event, original_parameters, original_recipe_version):
    """Validate one immutable baseline without assuming delivery order."""
    original = Parameters.model_validate(original_parameters)
    payload = event.payload
    if payload.baseline_parameters != original or payload.recipe_version != original_recipe_version:
        raise ValueError("Parameter change differs from original baseline")
    for parameters in (payload.previous_parameters, payload.parameters):
        validate_speed_adjustment(parameters, original)


def validate_parameter_evidence(summary, original_parameters, original_recipe_version, rows):
    """Require exactly the applied original stream events named by the summary."""
    # Preserve the original exact configuration comparison for legacy summaries.
    original = Parameters.model_validate(original_parameters)
    if not summary.parameter_changes and summary.parameters.model_dump(
        mode="json"
    ) != original.model_dump(mode="json"):
        raise ValueError("Legacy summary differs from original parameters")
    validate_parameter_history(summary, original, original_recipe_version)
    changes = summary.parameter_changes
    if len(rows) != len(changes):
        raise ValueError("Parameter history lacks complete durable evidence")
    for row, event in zip(rows, changes, strict=True):
        body = event.model_dump(mode="json")
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if (
            not row["applied"]
            or row["event_id"] != event.event_id
            or row["source_id"] != summary.source_id
            or row["stream_id"] != summary.stream_id
            or row["execution_id"] != summary.execution_id
            or row["kind"] != "PARAMETERS_CHANGED"
            or row["seq"] != event.seq
            or row["body"] != body
            or row["content_sha256"] != hashlib.sha256(canonical.encode()).hexdigest()
        ):
            raise ValueError("Parameter history differs from durable applied event")
