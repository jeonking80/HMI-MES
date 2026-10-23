"""Real PostgreSQL, authenticated ASGI and immutable measured intake evidence."""

from datetime import timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.ingestion import IngestionService
from apps.api.internal import InternalConfig, create_internal_app
from tests.integration.test_internal_api import binding

_spec = spec_from_file_location(
    "measured_cases", Path(__file__).parents[1] / "unit/test_measured_clock_math.py"
)
_cases = module_from_spec(_spec)
_spec.loader.exec_module(_cases)
AT = _cases.AT
measured_batch = _cases.measured_batch


@pytest.fixture
async def measured(migrated, tmp_path):
    bootstrap_reference_data(migrated)
    service = IngestionService(migrated, measured_clock_enabled=True)
    service.configure_source(
        source_id="simulator-1",
        stream_id="stream-1",
        gateway_id="gateway-1",
        line_id=LINE_ID,
        capture_generation=1,
        now=AT,
    )
    credential, token = binding(tmp_path, expires_at=AT + timedelta(hours=1))
    clock = [AT + timedelta(seconds=0.1)]
    app = create_internal_app(service, [credential], config=InternalConfig(clock=lambda: clock[0]))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://clock.example.test"
    ) as client:
        yield SimpleNamespace(
            app=app,
            token=token,
            service=service,
            engine=migrated,
            client=client,
            clock=clock,
            headers={"Authorization": "Bearer " + token},
        )


async def probe(env, **changes):
    value = measured_batch().clock_evidence.model_dump(mode="json")
    for key in ("nonce", "api_utc", "t1_utc", "t1_monotonic_seconds", "method"):
        value.pop(key)
    value.update(changes)
    return await env.client.post("/internal/v1/clock-probes", json=value, headers=env.headers)


async def submit(env, value):
    env.clock[0] = AT + timedelta(seconds=1)
    return await env.client.post(
        "/internal/v1/batches", content=value.model_dump_json(), headers=env.headers
    )


def scalar(env, sql):
    with env.engine.connect() as connection:
        return connection.execute(text(sql)).scalar_one()


async def test_skew_delayed_good_retry_and_proof_mutation(measured):
    env = measured
    response = await probe(env)
    assert response.status_code == 200, response.text
    value = measured_batch(nonce=response.json()["nonce"])
    first = await submit(env, value)
    assert first.status_code == 200, first.text
    again = await submit(env, value)
    assert again.status_code == 200 and again.json() == first.json()
    assert scalar(env, "SELECT status FROM capture_clock_evidence") == "GOOD"
    assert scalar(env, "SELECT proof->>'credential_key_id' FROM capture_clock_evidence") == (
        "gateway-key-1"
    )
    assert scalar(env, "SELECT offset_lower_seconds FROM capture_clock_evidence") < -3.7
    changed = value.model_copy(
        update={
            "clock_evidence": value.clock_evidence.model_copy(
                update={"t1_monotonic_seconds": 100.21}
            )
        }
    )
    assert (await submit(env, changed)).status_code == 409
    assert scalar(env, "SELECT count(*) FROM capture_clock_evidence") == 1


@pytest.mark.parametrize("kwargs", [{"rtt": 2.1}, {"age": 61.0}, {"epoch": "new-epoch"}])
async def test_invalid_evidence_persists_unknown(measured, kwargs):
    response = await probe(measured)
    value = measured_batch(nonce=response.json()["nonce"], **kwargs)
    result = await submit(measured, value)
    assert result.status_code == 200, result.text
    assert scalar(measured, "SELECT status FROM capture_clock_evidence") == "UNKNOWN"
    assert scalar(measured, "SELECT canonical_api_utc FROM capture_clock_evidence") is None


async def test_nonce_other_batch_foreign_scope_and_rollback(measured, monkeypatch):
    env = measured
    response = await probe(env)
    nonce = response.json()["nonce"]
    value = measured_batch(nonce=nonce)

    def fail(connection):
        raise RuntimeError("rollback test")

    monkeypatch.setattr(env.service, "_before_commit", fail)
    with pytest.raises(RuntimeError, match="rollback test"):
        await submit(env, value)
    assert scalar(env, "SELECT consumed_batch_id FROM capture_clock_probes") is None
    assert scalar(env, "SELECT count(*) FROM capture_clock_evidence") == 0
    monkeypatch.setattr(env.service, "_before_commit", lambda connection: None)
    assert (await submit(env, value)).status_code == 200
    assert (await submit(env, measured_batch(nonce=nonce, seq=2))).status_code == 409
    foreign = measured_batch(nonce=nonce, seq=2)
    foreign = foreign.model_copy(
        update={
            "clock_evidence": foreign.clock_evidence.model_copy(
                update={"source_id": "foreign-source"}
            )
        }
    )
    assert (await submit(env, foreign)).status_code == 409


async def test_probe_auth_scope_and_default_off(measured):
    env = measured
    assert (await env.client.post("/internal/v1/clock-probes", json={})).status_code == 401
    assert (await probe(env, source_id="foreign-source")).status_code == 403
    assert (await probe(env, capture_generation="2")).status_code == 403
    env.service.measured_clock_enabled = False
    assert (await probe(env)).status_code == 404
    assert scalar(env, "SELECT count(*) FROM capture_clock_probes") == 0


async def test_scope_capacity_and_bounded_cleanup(measured, monkeypatch):
    import apps.api.measured_clock as clock_service

    monkeypatch.setattr(clock_service, "PROBE_SCOPE_CAP", 2)
    env = measured
    assert (await probe(env)).status_code == 200
    assert (await probe(env)).status_code == 200
    assert (await probe(env)).status_code == 429
    env.clock[0] = AT + timedelta(seconds=61)
    assert (await probe(env)).status_code == 200
    assert scalar(env, "SELECT count(*) FROM capture_clock_probes") == 1


async def test_actual_gateway_intake_client_skew_and_injected_network_delay(measured):
    from apps.gateway.http_client import IntakeClient, IntakeConfig
    from contracts.clock_evidence import ClockProbeRequest

    env = measured

    class DelayedASGI(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            # Logical 100 ms outbound delay, independent of database time.
            if request.url.path.endswith("clock-probes"):
                env.clock[0] = AT + timedelta(seconds=0.1)
            response = await httpx.ASGITransport(app=env.app).handle_async_request(request)
            # Logical 100 ms return delay in the same monotonic/UTC coordinates.
            if request.url.path.endswith("clock-probes"):
                env.clock[0] = AT + timedelta(seconds=0.2)
            return response

    client = IntakeClient(
        IntakeConfig(base_url="https://clock.example.test", token=env.token),
        transport=DelayedASGI(),
    )
    try:
        template = measured_batch()
        request = ClockProbeRequest.model_validate(
            {
                key: value
                for key, value in template.clock_evidence.model_dump(mode="json").items()
                if key in ClockProbeRequest.model_fields
            }
        )
        response = await client.clock_probe(request)
        assert response.api_utc == AT + timedelta(seconds=0.1)
        value = measured_batch(nonce=response.nonce)
        env.clock[0] = AT + timedelta(seconds=1)
        ack = await client.deliver(value)
        assert ack.durability == "COMMITTED"
        assert scalar(env, "SELECT status FROM capture_clock_evidence") == "GOOD"
    finally:
        await client.close()


async def test_probe_records_request_entry_before_database_delay(measured, monkeypatch):
    env = measured
    original = env.service._lock_stream

    def delayed(connection, source, stream):
        env.clock[0] += timedelta(seconds=0.5)
        return original(connection, source, stream)

    monkeypatch.setattr(env.service, "_lock_stream", delayed)
    result = await probe(env)
    assert result.status_code == 200
    assert result.json()["api_utc"] == "2026-10-23T12:00:00.100000Z"
    assert scalar(env, "SELECT api_received_at FROM capture_clock_probes") == (
        AT + timedelta(seconds=0.1)
    )


async def test_foreign_credential_nonce_cannot_be_consumed(measured):
    env = measured
    response = await probe(env)
    with env.engine.begin() as connection:
        connection.execute(text("UPDATE capture_clock_probes SET credential_key_id='foreign-key'"))
    assert (await submit(env, measured_batch(nonce=response.json()["nonce"]))).status_code == 409
    assert scalar(env, "SELECT consumed_batch_id FROM capture_clock_probes") is None
    assert scalar(env, "SELECT count(*) FROM ingest_batches") == 0


async def test_fresh_capture_interval_clips_to_request_receipt(measured):
    env = measured
    response = await probe(env)
    value = measured_batch(nonce=response.json()["nonce"], age=0.0)
    env.clock[0] = AT + timedelta(seconds=0.11)
    result = await env.client.post(
        "/internal/v1/batches", content=value.model_dump_json(), headers=env.headers
    )
    assert result.status_code == 200, result.text
    assert scalar(env, "SELECT status FROM capture_clock_evidence") == "GOOD"
    canonical = scalar(env, "SELECT canonical_api_utc FROM capture_clock_evidence")
    assert canonical <= env.clock[0]
    assert scalar(env, "SELECT interval_upper_utc FROM capture_clock_evidence") == env.clock[0]


async def test_expired_full_scope_recovers_before_unrelated_expiry_prefix(measured):
    env = measured
    assert (await probe(env)).status_code == 200
    original_nonce_sha = scalar(env, "SELECT nonce_sha256 FROM capture_clock_probes")
    # 200 earlier expired rows outside this epoch previously monopolized the
    # 100-row cleanup, leaving this full 120-row scope blocked by rollback.
    with env.engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO capture_clock_probes
            (nonce_sha256,credential_key_id,gateway_id,source_id,stream_id,capture_generation,
             clock_epoch,api_received_at,expires_at,gateway_send_utc,gateway_send_monotonic)
            SELECT lpad(to_hex(n),64,'0'),credential_key_id,gateway_id,source_id,stream_id,
                capture_generation,CASE WHEN n<=200 THEN 'unrelated-epoch' ELSE clock_epoch END,
                api_received_at-CASE WHEN n<=200 THEN interval '2 minutes'
                    ELSE interval '1 minute' END,
                expires_at-CASE WHEN n<=200 THEN interval '2 minutes'
                    ELSE interval '1 minute' END,gateway_send_utc,gateway_send_monotonic
            FROM capture_clock_probes CROSS JOIN generate_series(1,320) AS n""")
        )
        connection.execute(
            text("DELETE FROM capture_clock_probes WHERE nonce_sha256=:nonce"),
            {"nonce": original_nonce_sha},
        )
    env.clock[0] = AT + timedelta(seconds=61)
    assert scalar(env, "SELECT count(*) FROM capture_clock_probes") == 320
    result = await probe(env)
    assert result.status_code == 200, result.text
    assert scalar(env, "SELECT count(*) FROM capture_clock_probes") == 221
    assert (
        scalar(env, "SELECT count(*) FROM capture_clock_probes WHERE clock_epoch='clock-1'") == 21
    )
    assert (
        scalar(env, "SELECT count(*) FROM capture_clock_probes WHERE clock_epoch='unrelated-epoch'")
        == 200
    )
