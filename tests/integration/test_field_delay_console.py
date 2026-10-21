"""D3 console HTTP draft; pure publication boundary, no sockets/CLI/browser claim.

Run only in the approved merged D3 environment. The pure adapter uses an explicit
monotonic test clock; GET must never advance it, prepare a plan, or adopt a frame.
Actual UA atomic adoption and CLI/browser tests belong to the later integration.
"""

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from asyncua import ua
from pydantic import ValidationError

from apps.simulator.console import create_app
from apps.simulator.field_channels import FieldChannels, FieldFaultInput
from apps.simulator.field_publication import FieldPublication, get_publication_runtime
from apps.simulator.tags import project_tags
from tests.integration.test_simulator_console import ORIGIN, post, sign_in
from tests.integration.test_simulator_console import environment as environment


@pytest.mark.parametrize("duration", [100, 200, 10_000])
def test_delay_duration_exact_integer_boundaries(duration):
    assert (
        FieldFaultInput(mode="DELAY", expected_revision="0", delay_ms=duration).delay_ms == duration
    )


@pytest.mark.parametrize("duration", [None, True, False, "100", 100.0, 0, 99, 101, 10_001])
def test_delay_duration_rejects_coercion_missing_and_wrong_step(duration):
    with pytest.raises(ValidationError):
        FieldFaultInput(mode="DELAY", expected_revision="0", delay_ms=duration)


@pytest.mark.parametrize(
    "mode", ["NORMAL", "FROZEN", "BAD", "UNCERTAIN", "READ_FAILURE", "LINK_DOWN"]
)
def test_existing_modes_have_no_delay(mode):
    assert FieldFaultInput(mode=mode, expected_revision="0").delay_ms is None
    assert FieldFaultInput(mode=mode, expected_revision="0", delay_ms=None).delay_ms is None
    with pytest.raises(ValidationError):
        FieldFaultInput(mode=mode, expected_revision="0", delay_ms=100)


@pytest.fixture
async def console(environment):
    env = environment
    fields = FieldChannels(env.store)
    mono = [0.0]
    publication = FieldPublication(fields, monotonic_clock=lambda: mono[0])
    app = create_app(env.controller, env.auth, env.config, field_channels=fields)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN
    ) as browser:
        signed = await sign_in(browser)
        yield SimpleNamespace(
            env=env,
            fields=fields,
            mono=mono,
            publication=publication,
            client=browser,
            csrf=signed.json()["csrf_token"],
        )


def publish(case, *, status=ua.StatusCodes.Good):
    at = case.env.now[0] + timedelta(seconds=case.mono[0])
    values = project_tags(
        case.env.controller.state, None, counter_epoch="test", heartbeat=int(case.mono[0]) + 1
    )
    plan = case.publication.prepare(values, acquired_at=at, published_at=at, status_code=status)

    def adopt_model_boundary():
        case.fields._published = plan.frames
        case.fields._field_publication_runtime = plan.runtime_status

    case.publication.publish(plan, adopt_model_boundary)
    return plan


async def sealer(case, route="/field-faults"):
    response = await case.client.get(route)
    assert response.status_code == 200, response.text
    values = response.json()["field_channels"] if route == "/snapshot" else response.json()
    return next(item for item in values if item["configured"]["group_id"] == "Sealer")


async def test_runtime_unknown_then_config_pending_and_waiting_are_distinct(console):
    c = console
    initial = await sealer(c)
    assert initial["published"] is None and initial["publication_runtime"] is None
    publish(c)
    result = await post(
        c.client,
        "/field-faults/Sealer",
        c.csrf,
        {"mode": "DELAY", "expected_revision": "0", "delay_ms": 6000},
    )
    assert result.status_code == 200
    configured_only = await sealer(c)
    assert configured_only["pending"]
    assert configured_only["publication_runtime"]["published_revision"] == "0"
    c.mono[0] = 1.0
    publish(c, status=ua.StatusCodes.BadSensorFailure)
    waiting = await sealer(c)
    assert not waiting["pending"]
    assert waiting["publication_runtime"] == {
        "group_id": "Sealer",
        "published_revision": "1",
        "published_mode": "DELAY",
        "waiting_for_first_completion": True,
        "read_in_flight": True,
    }
    assert (await sealer(c, "/snapshot"))["publication_runtime"] == waiting["publication_runtime"]
    before = (
        c.fields._published,
        get_publication_runtime(c.fields),
        c.publication._engine.runtime,
        c.env.store.connection.execute("SELECT * FROM field_channel_audit").fetchall(),
        c.env.controller.state.model_dump_json(),
    )
    for _ in range(3):
        assert await sealer(c) == waiting
    after = (
        c.fields._published,
        get_publication_runtime(c.fields),
        c.publication._engine.runtime,
        c.env.store.connection.execute("SELECT * FROM field_channel_audit").fetchall(),
        c.env.controller.state.model_dump_json(),
    )
    assert after == before  # Existing authentication idle refresh is separate from field state.
    c.mono[0] = 7.0
    publish(c)
    completed = await sealer(c)
    assert completed["publication_runtime"]["waiting_for_first_completion"] is False
    # A completed read is not a Good-quality guarantee. Original Bad samples survive.
    assert all(
        field["status_code"] == ua.StatusCodes.BadSensorFailure
        for field in completed["published"]["fields"]
    )


async def test_duration_change_requires_normal_actual_publication_and_cas(console):
    c = console
    publish(c)
    first = await post(
        c.client,
        "/field-faults/Sealer",
        c.csrf,
        {"mode": "DELAY", "expected_revision": "0", "delay_ms": 100},
    )
    assert first.status_code == 200
    same = await post(
        c.client,
        "/field-faults/Sealer",
        c.csrf,
        {"mode": "DELAY", "expected_revision": "1", "delay_ms": 100},
    )
    assert same.status_code == 200 and same.json() == first.json()
    assert (
        await post(
            c.client,
            "/field-faults/Sealer",
            c.csrf,
            {"mode": "DELAY", "expected_revision": "1", "delay_ms": 200},
        )
    ).status_code == 409
    assert (
        await post(
            c.client, "/field-faults/Sealer", c.csrf, {"mode": "NORMAL", "expected_revision": "1"}
        )
    ).status_code == 200
    assert (
        await post(
            c.client,
            "/field-faults/Sealer",
            c.csrf,
            {"mode": "DELAY", "expected_revision": "2", "delay_ms": 200},
        )
    ).status_code == 409
    c.mono[0] = 1.0
    publish(c)
    assert (
        await post(
            c.client,
            "/field-faults/Sealer",
            c.csrf,
            {"mode": "DELAY", "expected_revision": "2", "delay_ms": 200},
        )
    ).status_code == 200
    assert (
        await post(
            c.client, "/field-faults/Sealer", c.csrf, {"mode": "NORMAL", "expected_revision": "1"}
        )
    ).status_code == 409


@pytest.mark.parametrize("route", ["/field-faults", "/snapshot"])
async def test_unknown_namespace_safe_503_without_ddl_or_sql_leak(console, route):
    c = console
    # Real unsupported field namespace, not a mocked error response.
    with c.env.store.transaction():
        c.env.store.connection.execute(
            "CREATE TEMP TABLE field_channel_unknown_private_sql(secret TEXT)"
        )
    before = c.env.store.connection.execute(
        "SELECT name,sql FROM temp.sqlite_master ORDER BY name"
    ).fetchall()
    result = await c.client.get(route)
    assert result.status_code == 503
    assert result.json() == {"detail": "field environment unavailable"}
    assert "private_sql" not in result.text and "SELECT" not in result.text
    assert (
        c.env.store.connection.execute(
            "SELECT name,sql FROM temp.sqlite_master ORDER BY name"
        ).fetchall()
        == before
    )
