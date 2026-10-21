"""Validate the static tag contract without importing either application."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

CONTRACT_PATH = Path(__file__).with_name("tags.v1.json")
GROUP_COUNTS = {"Line": 18, "Infeed": 8, "Sealer": 12, "Outfeed": 10}
DATA_TYPES = {"Int32", "UInt32", "UInt64", "Boolean", "String", "Double"}
TAG_FIELDS = {
    "group",
    "name",
    "logical_name",
    "node_id",
    "data_type",
    "unit",
    "enum",
    "access",
    "polling_interval_ms",
    "analog_history",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_contract(contract: Any) -> None:
    """Raise ValueError for malformed or incompatible v1 contract metadata."""
    _require(isinstance(contract, dict), "contract must be an object")
    _require(
        set(contract) == {"schema_version", "namespace_uri", "enums", "quality", "tags"},
        "unexpected contract fields (namespace indices must not be stored)",
    )
    _require(contract["schema_version"] == "1.0.0", "unsupported schema_version")
    _require(
        contract["namespace_uri"] == "urn:hmi-mes:packaging-line:tags:v1",
        "v1 namespace URI must remain stable",
    )
    enums = contract["enums"]
    _require(isinstance(enums, dict) and bool(enums), "enums must be an object")
    for enum_name, values in enums.items():
        _require(isinstance(enum_name, str), "enum name must be a string")
        _require(isinstance(values, dict) and bool(values), "enum values must be an object")
        for number, label in values.items():
            _require(
                isinstance(number, str)
                and number.isdecimal()
                and str(int(number)) == number
                and int(number) <= 2**31 - 1,
                "enum keys must be canonical nonnegative Int32 values",
            )
            _require(isinstance(label, str) and bool(label), "enum label must be a string")
            _require(label != "UNKNOWN", "UNKNOWN is a display state, not a PLC state")
        _require(len(set(values.values())) == len(values), "duplicate enum labels")

    quality = contract["quality"]
    _require(
        quality
        == {
            "data_value_fields": ["value", "status_code", "source_timestamp", "server_timestamp"],
            "observation_fields": ["collected_at", "stored_at"],
            "calculation_usable_statuses": ["Good"],
            "stale_after_ms": 3000,
            "communication_alarm_after_ms": 5000,
            "unchanged_source_timestamp_is_stale": False,
        },
        "quality policy must retain source metadata and freshness rules",
    )
    tags = contract["tags"]
    _require(isinstance(tags, list) and len(tags) == 48, "exactly 48 tags required")
    names: set[str] = set()
    addresses: set[str] = set()
    groups: Counter[str] = Counter()
    analog_count = 0
    for tag in tags:
        _require(isinstance(tag, dict) and set(tag) == TAG_FIELDS, "invalid tag fields")
        group, name = tag["group"], tag["name"]
        _require(isinstance(group, str) and group in GROUP_COUNTS, "unknown tag group")
        _require(isinstance(name, str) and name.isidentifier(), "invalid tag name")
        logical_name = f"{group}.{name}"
        _require(tag["logical_name"] == logical_name, "logical_name must be Group.Name")
        _require(logical_name not in names, "duplicate logical_name")
        names.add(logical_name)
        node_id = tag["node_id"]
        _require(
            isinstance(node_id, dict)
            and set(node_id) == {"identifier_type", "identifier"}
            and node_id["identifier_type"] == "String"
            and isinstance(node_id["identifier"], str)
            and node_id["identifier"] == logical_name,
            "node_id must use stable Group.Name string identifier without namespace index",
        )
        _require(node_id["identifier"] not in addresses, "duplicate node address")
        addresses.add(node_id["identifier"])
        groups[group] += 1
        data_type = tag["data_type"]
        _require(isinstance(data_type, str) and data_type in DATA_TYPES, "unsupported data_type")
        _require(tag["unit"] is None or isinstance(tag["unit"], str), "invalid unit")
        enum_name = tag["enum"]
        _require(
            enum_name is None or (isinstance(enum_name, str) and enum_name in enums),
            "unknown enum reference",
        )
        _require((data_type == "Int32") == (enum_name is not None), "Int32 tags require enum")
        _require(tag["access"] == "read_only", "all tags must be read_only")
        _require(
            type(tag["polling_interval_ms"]) is int and tag["polling_interval_ms"] == 1000,
            "polling_interval_ms must be 1000",
        )
        _require(type(tag["analog_history"]) is bool, "analog_history must be boolean")
        _require(
            tag["analog_history"] == (data_type == "Double"), "only Double tags are analog history"
        )
        analog_count += tag["analog_history"]
    _require(
        dict(groups) == GROUP_COUNTS,
        "group counts must be Line 18 / Infeed 8 / Sealer 12 / Outfeed 10",
    )
    _require(analog_count == 9, "exactly nine analog history tags required")


def load_contract(path: Path = CONTRACT_PATH) -> dict[str, Any]:
    contract = json.loads(path.read_text(encoding="utf-8"))
    validate_contract(contract)
    return contract


if __name__ == "__main__":
    load_contract()
    print("Valid tag contract: 48 read-only tags, 9 analog histories.")
