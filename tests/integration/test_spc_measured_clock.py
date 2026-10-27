"""SPC never resurrects an old GOOD slot from an invalidated measured clock."""

from datetime import timedelta

from tests.integration.test_analytics_clock_consumers import (
    AT,
    SCOPE,
    measured_capture,
    setup_service,
)


def test_invalidated_measured_latest_spc_slot_cannot_resurrect_old_good(migrated):
    from apps.api.spc import OFFSETS
    from apps.api.spc_http import SPCQuery, read_spc
    from tests.integration.test_analytics_clock_consumers import TAG

    service = setup_service(migrated, measured=True)
    for seq, second in enumerate(OFFSETS, start=1):
        measured_capture(service, seq, AT + timedelta(seconds=second))
    measured_capture(service, 7, AT + timedelta(seconds=48.1))
    measured_capture(service, 6, AT + timedelta(seconds=48.2))
    with migrated.begin() as db:
        value = read_spc(
            db, SPCQuery(**SCOPE, execution_id="run-a", tag=TAG), now=AT + timedelta(minutes=1)
        )
    assert value["subgroups"] == []
    assert value["excluded"]["reasons"]["TIME_UNKNOWN"] == 1
