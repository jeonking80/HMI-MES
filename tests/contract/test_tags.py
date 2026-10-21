"""Requirements checks for the §18 tag list; runnable with pytest or unittest."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "contracts/tags/tags.v1.json"
SOURCE_PATH = ROOT / "docs/HMI_MES_시스템_기획서_v1_0.md"
SPEC = importlib.util.spec_from_file_location(
    "tag_contract_validator", ROOT / "contracts/tags/validate.py"
)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)

# Independently transcribed from the source table, grouped by the source types.
# Do not derive these expectations from the implementation's generation/validation code.
REQUIRED_TYPES = {
    "Line": {
        "Int32": "State ControlMode OperationMode",
        "Boolean": "EStop GuardClosed Permissive",
        "UInt32": "FaultCode TargetQuantity WipCount",
        "String": "ActiveExecutionId RecipeVersion CounterEpoch",
        "UInt64": "GoodTotal RejectTotal ScrapTotal Heartbeat ControlVersion EventSeq",
    },
    "Infeed": {
        "Int32": "State",
        "Boolean": "RunFeedback EntrySensor",
        "UInt32": "FaultCode BufferCount",
        "Double": "MotorCurrent ActualSpeed",
        "UInt64": "LinkHeartbeat",
    },
    "Sealer": {
        "Int32": "State",
        "Boolean": "RunFeedback HeaterOn GuardClosed Ready",
        "UInt32": "FaultCode",
        "Double": "MotorCurrent ActualSpeed TemperaturePV TemperatureSP AirPressure",
        "UInt64": "LinkHeartbeat",
    },
    "Outfeed": {
        "Int32": "State CheckResult",
        "Boolean": "RunFeedback ExitSensor",
        "UInt32": "FaultCode",
        "Double": "MotorCurrent ActualSpeed",
        "UInt64": "GoodCounter RejectCounter LinkHeartbeat",
    },
}
EXPECTED_ANALOG_UNITS = {
    "Infeed.MotorCurrent": "A",
    "Sealer.MotorCurrent": "A",
    "Outfeed.MotorCurrent": "A",
    "Infeed.ActualSpeed": "m/min",
    "Sealer.ActualSpeed": "m/min",
    "Outfeed.ActualSpeed": "m/min",
    "Sealer.TemperaturePV": "℃",
    "Sealer.TemperatureSP": "℃",
    "Sealer.AirPressure": "bar",
}
QUANTITY_TAGS = {
    "Line.TargetQuantity",
    "Line.WipCount",
    "Line.GoodTotal",
    "Line.RejectTotal",
    "Line.ScrapTotal",
    "Infeed.BufferCount",
    "Outfeed.GoodCounter",
    "Outfeed.RejectCounter",
}


class TagContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        cls.tags = cls.contract["tags"]
        cls.by_name = {tag["logical_name"]: tag for tag in cls.tags}

    def test_original_source_names_and_independent_type_requirements(self) -> None:
        source = SOURCE_PATH.read_text(encoding="utf-8")
        section = source.split("## 18 태그 사전 초안", 1)[1].split("## 19", 1)[0]
        group = None
        source_names = set()
        source_counts = {}
        for line in section.splitlines():
            if not line.startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            match = re.fullmatch(r"(Line|Infeed|Sealer|Outfeed) (\d+)", cells[0])
            if match:
                group, count = match.groups()
                source_counts[group] = int(count)
            if group is not None:
                for name in cells[1].split(","):
                    source_names.add(f"{group}.{name.strip()}")
        expected_types = {
            f"{group}.{name}": data_type
            for group, type_groups in REQUIRED_TYPES.items()
            for data_type, names in type_groups.items()
            for name in names.split()
        }
        self.assertEqual(source_counts, {"Line": 18, "Infeed": 8, "Sealer": 12, "Outfeed": 10})
        self.assertEqual(
            source_names, set(expected_types), "source changed: review the requirements fixture"
        )
        self.assertEqual(set(self.by_name), source_names)
        self.assertEqual(
            {name: tag["data_type"] for name, tag in self.by_name.items()}, expected_types
        )

    def test_exact_counts_and_no_duplicate_names_or_addresses(self) -> None:
        self.assertEqual(len(self.tags), 48)
        self.assertEqual(len(self.by_name), 48)
        self.assertEqual(
            Counter(tag["group"] for tag in self.tags),
            {"Line": 18, "Infeed": 8, "Sealer": 12, "Outfeed": 10},
        )
        identifiers = [tag["node_id"]["identifier"] for tag in self.tags]
        self.assertEqual(len(set(identifiers)), 48)

    def test_addresses_use_uri_and_string_identifier_without_index(self) -> None:
        self.assertEqual(self.contract["namespace_uri"], "urn:hmi-mes:packaging-line:tags:v1")
        self.assertNotIn("namespace_index", self.contract)
        for tag in self.tags:
            with self.subTest(tag=tag["logical_name"]):
                self.assertEqual(tag["logical_name"], f"{tag['group']}.{tag['name']}")
                self.assertEqual(
                    tag["node_id"], {"identifier_type": "String", "identifier": tag["logical_name"]}
                )

    def test_all_feedback_read_only_at_one_second(self) -> None:
        for tag in self.tags:
            with self.subTest(tag=tag["logical_name"]):
                self.assertEqual(tag["access"], "read_only")
                self.assertIs(type(tag["polling_interval_ms"]), int)
                self.assertEqual(tag["polling_interval_ms"], 1000)

    def test_nine_source_analog_signals_have_history_and_units(self) -> None:
        selected = {tag["logical_name"]: tag["unit"] for tag in self.tags if tag["analog_history"]}
        self.assertEqual(selected, EXPECTED_ANALOG_UNITS)
        for name, tag in self.by_name.items():
            with self.subTest(tag=name):
                self.assertIs(type(tag["analog_history"]), bool)
                if name in EXPECTED_ANALOG_UNITS:
                    self.assertEqual(tag["data_type"], "Double")
                elif name in QUANTITY_TAGS:
                    self.assertEqual(tag["unit"], "개")
                else:
                    self.assertIsNone(tag["unit"])

    def test_state_and_mode_mappings_are_explicit_v1_decisions(self) -> None:
        self.assertEqual(
            self.contract["enums"],
            {
                "EquipmentState": {
                    "0": "STOPPED",
                    "1": "READY",
                    "2": "RUNNING",
                    "3": "PAUSED",
                    "4": "STOPPING",
                    "5": "FAULT",
                    "6": "ESTOP",
                    "7": "RECOVERY_REQUIRED",
                },
                "ControlMode": {"0": "LOCAL", "1": "REMOTE"},
                "OperationMode": {"0": "MANUAL", "1": "AUTO"},
                "CheckResult": {"0": "UNDECIDED", "1": "GOOD", "2": "REJECT"},
            },
        )
        expected_refs = {f"{group}.State": "EquipmentState" for group in REQUIRED_TYPES}
        expected_refs.update(
            {
                "Line.ControlMode": "ControlMode",
                "Line.OperationMode": "OperationMode",
                "Outfeed.CheckResult": "CheckResult",
            }
        )
        self.assertEqual(
            {name: tag["enum"] for name, tag in self.by_name.items() if tag["enum"] is not None},
            expected_refs,
        )

    def test_original_quality_metadata_and_freshness_policy(self) -> None:
        quality = self.contract["quality"]
        self.assertEqual(
            set(quality["data_value_fields"]),
            {"value", "status_code", "source_timestamp", "server_timestamp"},
        )
        self.assertEqual(set(quality["observation_fields"]), {"collected_at", "stored_at"})
        self.assertEqual(quality["calculation_usable_statuses"], ["Good"])
        self.assertEqual(quality["stale_after_ms"], 3000)
        self.assertEqual(quality["communication_alarm_after_ms"], 5000)
        self.assertIs(quality["unchanged_source_timestamp_is_stale"], False)

    def test_validator_accepts_current_contract(self) -> None:
        VALIDATOR.validate_contract(self.contract)
        self.assertEqual(VALIDATOR.load_contract(CONTRACT_PATH), self.contract)

    def test_validator_rejects_invalid_contracts(self) -> None:
        mutations = {
            "missing tag": lambda c: c["tags"].pop(),
            "duplicate tag": lambda c: c["tags"].__setitem__(1, copy.deepcopy(c["tags"][0])),
            "wrong group count": lambda c: c["tags"][0].update(
                group="Infeed",
                logical_name="Infeed.Other",
                name="Other",
                node_id={"identifier_type": "String", "identifier": "Infeed.Other"},
            ),
            "namespace index": lambda c: c.update(namespace_index=2),
            "numeric identifier": lambda c: c["tags"][0].update(
                node_id={"identifier_type": "Numeric", "identifier": 1}
            ),
            "writable setpoint": lambda c: next(
                t for t in c["tags"] if t["name"] == "TemperatureSP"
            ).update(access="read_write"),
            "wrong polling": lambda c: c["tags"][0].update(polling_interval_ms=100),
            "counter history": lambda c: next(
                t for t in c["tags"] if t["name"] == "GoodTotal"
            ).update(analog_history=True),
            "missing analog history": lambda c: next(
                t for t in c["tags"] if t["name"] == "TemperatureSP"
            ).update(analog_history=False),
            "missing enum": lambda c: c["tags"][0].update(enum=None),
            "UNKNOWN PLC state": lambda c: c["enums"]["EquipmentState"].update({"8": "UNKNOWN"}),
            "quality normalizes Bad": lambda c: c["quality"].update(
                calculation_usable_statuses=["Good", "Bad"]
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label):
                candidate = copy.deepcopy(self.contract)
                mutate(candidate)
                with self.assertRaises(ValueError):
                    VALIDATOR.validate_contract(candidate)


if __name__ == "__main__":
    unittest.main()
