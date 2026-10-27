"""The control screen's single cycle read: loaded job, settlement and open commands."""

from sqlalchemy import text

from apps.api.bootstrap import LINE_ID
from apps.api.line_status import read_line_status
from tests.integration.test_work_order_workflow import ORIGIN, bind, client, release  # noqa: F401
from tests.integration.test_work_order_workflow import state as state  # noqa: F401


def status(state):
    with state.engine.begin() as db:
        return read_line_status(
            db, line_id=LINE_ID, source_id="source", stream_id="stream", now=state.clock[0]
        )


def test_no_job_then_bound_job_with_its_open_load(state):
    empty = status(state)
    assert empty["execution"] is None and empty["unresolved_command"] is None
    release(state)
    receipt = bind(state)
    value = status(state)
    assert value["execution"]["execution_id"] == receipt["execution_id"]
    assert value["execution"]["work_order_id"] == "work-A"
    assert value["execution"]["settlement"] == "PENDING"
    assert value["unresolved_command"]["kind"] == "LOAD_JOB"
    with state.engine.begin() as db:
        db.execute(
            text(
                "UPDATE command_requests SET status='SUCCEEDED',"
                "result=jsonb_set(result,'{status}','\"SUCCEEDED\"')"
            )
        )
    assert status(state)["unresolved_command"] is None


async def test_route_requires_exact_scope_and_login(state, client):  # noqa: F811
    path = f"/api/v1/line-status?line_id={LINE_ID}&source_id=source&stream_id=stream"
    response = await client.get(path)
    assert response.status_code == 200, response.text
    assert response.json()["execution"] is None
    assert (await client.get(f"/api/v1/line-status?line_id={LINE_ID}")).status_code == 422
    client.cookies.clear()
    assert (await client.get(path)).status_code == 401
