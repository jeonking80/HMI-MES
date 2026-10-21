import json
from pathlib import Path

import pytest

from apps.simulator.plc import PLCState
from apps.simulator.process import ProcessState
from apps.simulator.tags import project_tags
from contracts.models import UINT64_MAX


def test_projection_matches_exact_48_contract_channels_and_types():
    values = project_tags(PLCState(), None, counter_epoch="epoch", heartbeat=UINT64_MAX)
    contract = json.loads(Path("contracts/tags/tags.v1.json").read_text())
    types = {
        "Int32": int,
        "UInt32": int,
        "UInt64": int,
        "Double": float,
        "Boolean": bool,
        "String": str,
    }
    assert set(values) == {t["logical_name"] for t in contract["tags"]}
    for tag in contract["tags"]:
        assert type(values[tag["logical_name"]]) is types[tag["data_type"]]
    assert values["Line.Heartbeat"] == UINT64_MAX
    assert values["Line.State"] == 7
    assert not values["Sealer.HeaterOn"]
    assert not values["Infeed.RunFeedback"]


def test_projection_refuses_mixed_execution_or_wip_snapshots():
    with pytest.raises(ValueError, match="execution"):
        project_tags(PLCState(), ProcessState("a"), counter_epoch="epoch", heartbeat=0)


@pytest.mark.parametrize("value", [-1, True, UINT64_MAX + 1, 1.0])
def test_heartbeat_is_lossless_and_never_wraps(value):
    with pytest.raises(ValueError):
        project_tags(PLCState(), None, counter_epoch="epoch", heartbeat=value)
