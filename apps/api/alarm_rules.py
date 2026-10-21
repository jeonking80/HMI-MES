"""Stored alarm config variants; the existing analog qualifier stays analog-only."""

from typing import Annotated, Literal

from pydantic import Field

from apps.api.alarm_lifecycle import AlarmRule, AlarmScope
from apps.api.alarm_qualification import PressureRuleConfig, TemperatureRuleConfig
from contracts.models import ContractModel


class PLCFaultRuleConfig(ContractModel):
    kind: Literal["PLC_FAULT"] = "PLC_FAULT"
    rule: AlarmRule


StoredRuleConfig = Annotated[
    TemperatureRuleConfig | PressureRuleConfig | PLCFaultRuleConfig, Field(discriminator="kind")
]


def require_rule_target(scope: AlarmScope, config: StoredRuleConfig) -> None:
    target = "Line" if isinstance(config, PLCFaultRuleConfig) else "Sealer"
    if scope.asset_id != target:
        raise ValueError("Alarm rule kind requires its exact logical/physical target")
