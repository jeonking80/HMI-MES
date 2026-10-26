"""Retirement preserves business evidence and exact compacted capture identities."""

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from apps.api.auth import Principal
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.commands import enqueue_command
from apps.api.ingest_retention import IngestRetentionExpired, maybe_prune_ingest
from apps.api.ingestion import IngestionService
from apps.api.internal import InternalConfig, ServiceCredential, create_internal_app
from apps.api.settlement import record_final_summary
from contracts.models import CommandRequest
from contracts.transport import BatchBody, GatewayBatch, batch_digest
from tests.integration import test_speed_adjustments as speed
from tests.integration import test_target_auto_end as target
from tests.integration.test_ingestion import batch as event_batch
from tests.integration.test_ingestion import event, register
from tests.integration.test_settlement import record, summary
from tests.integration.test_settlement import service as service

OLD = datetime.now(UTC) - timedelta(days=8)
SCOPE = dict(
    authenticated_gateway_id="gateway-1",
    authenticated_source_id="simulator-1",
    authenticated_stream_id="stream-1",
)


def batch(*events, **changes):
    """Build valid observations without changing the established event-only helper."""
    if not events and not changes.get("gaps"):
        at = changes.setdefault("captured_at", OLD)
        seq = changes.get("capture_seq", 1)
        changes.setdefault(
            "samples",
            [
                dict(
                    sample_id=f"retention-heartbeat-{seq}",
                    tag_id="Line.Heartbeat",
                    source_timestamp=at,
                    server_timestamp=None,
                    acquired_at=at,
                    status_code=0,
                    reading=dict(data_type="UInt64", value=str(seq)),
                )
            ],
        )
    return event_batch(*events, **changes)


@pytest.fixture
def observation_service(migrated):
    bootstrap_reference_data(migrated)
    value = IngestionService(migrated)
    value.configure_source(
        gateway_id="gateway-1",
        source_id="simulator-1",
        stream_id="stream-1",
        line_id=LINE_ID,
        capture_generation=1,
        now=OLD,
    )
    return value


def submit(service, value, at=OLD):
    return service.ingest(value, **SCOPE, now=at)


def prune(engine, batches=200, candidates=400):
    # Only this disposable test owner may advance durable scheduling between attempts.
    with engine.begin() as db:
        db.execute(sa.text("UPDATE ingest_retention_state SET last_attempt_at=NULL"))
    return maybe_prune_ingest(engine, max_batches=batches, max_candidates=candidates)


def finish_compaction(engine, first, *, expected_expired=0, attempts=20):
    """Finish eligible receipts deferred by the SQL work budget, with bounded retries."""
    total = dict(first)
    assert first["status"] not in {"failed", "unavailable", "BUSY"}, first
    for attempt in range(attempts + 1):
        with engine.connect() as db:
            remaining = db.execute(
                sa.text("SELECT count(*) FROM ingest_receipts WHERE expired_at IS NOT NULL")
            ).scalar_one()
        if remaining == expected_expired:
            return total
        assert remaining > expected_expired
        if attempt == attempts:
            break
        result = prune(engine)
        assert result["status"] not in {"failed", "unavailable", "BUSY"}, result
        for key in ("deleted_count", "compacted_count", "scanned_count"):
            total[key] += result[key]
    raise AssertionError("Eligible receipts did not compact within bounded maintenance attempts")


def rows(engine, sql):
    with engine.connect() as db:
        return db.execute(sa.text(sql)).mappings().all()


def test_only_old_settled_events_retire_and_original_ack_is_retained_within_period(service):
    old = batch(event(1, execution_id="run-1"), event(2, execution_id="run-1"))
    submit(service, old)
    assert record(service).status == "SETTLED"
    recent = batch(capture_seq=2)
    ack = submit(service, recent, datetime.now(UTC))
    before = service.get_totals("run-1")
    assert prune(service.engine)["deleted_count"] == 1
    assert submit(service, recent) == ack
    with pytest.raises(IngestRetentionExpired):
        submit(service, old)
    assert service.get_totals("run-1") == before
    assert len(rows(service.engine, "SELECT * FROM event_inbox")) == 2
    assert len(rows(service.engine, "SELECT * FROM production_events")) == 2
    assert rows(service.engine, "SELECT status FROM run_reconciliations")[0]["status"] == "SETTLED"


@pytest.mark.parametrize("state", ["PENDING", "MISMATCH", "CONFLICT", "STALE"])
def test_unsettled_or_stale_final_evidence_never_authorizes_deletion(service, state):
    submit(service, batch(event(1, execution_id="run-1"), event(2, execution_id="run-1")))
    if state == "PENDING":
        record(service, summary(last_event_seq=3))
    elif state == "MISMATCH":
        record(service, summary(input_quantity=3, good_quantity=3))
    else:
        assert record(service).status == "SETTLED"
        if state == "CONFLICT":
            record(service, summary(last_event_seq=3))
        else:
            with service.engine.begin() as db:
                db.execute(sa.text("UPDATE execution_totals SET good_quantity=good_quantity+1"))
    assert prune(service.engine)["deleted_count"] == 0
    assert len(rows(service.engine, "SELECT * FROM ingest_batches")) == 1


@pytest.mark.parametrize("later", ["ADVANCED", "GAP", "QUARANTINE"])
def test_later_other_run_evidence_does_not_pin_settled_original_interval(service, later):
    submit(service, batch(event(1, execution_id="run-1"), event(2, execution_id="run-1")))
    assert record(service).status == "SETTLED"
    register(service, "run-B")
    seq = 4 if later == "GAP" else 3
    execution = "unknown-execution" if later == "QUARANTINE" else "run-B"
    submit(service, batch(event(seq, execution_id=execution), capture_seq=2), datetime.now(UTC))
    assert prune(service.engine)["deleted_count"] == 1
    assert rows(service.engine, "SELECT batch_id FROM ingest_batches") == [{"batch_id": "batch-2"}]
    assert service.get_totals("run-1")["good_quantity"] == 2


@pytest.mark.parametrize("kind", ["AUTO_END", "SPEED_ADJUSTED"])
def test_real_plc_final_histories_retire_without_losing_execution_totals(migrated, tmp_path, kind):
    from apps.simulator.storage import SimulatorStorage

    with SimulatorStorage(tmp_path / "retention-plc.sqlite3") as store:
        if kind == "SPEED_ADJUSTED":
            controller = speed.make_controller(store)
            service, scope = speed.api_service(migrated, controller)
            speed.start(controller)
            speed.adjust(controller)
            final = speed.finish(controller)
            source = speed.events(controller)
            assert final.parameter_changes
        else:
            controller = target.make_controller(store)
            service, scope = target.api_service(migrated, controller)
            target.register(service, controller)
            target.load(controller)
            target.apply(controller, "START", "start")
            final = target.finish(controller)
            source = target.events(controller)
        retained_controls = [item for item in source if item.execution_id != "run-A"]
        run_events = [item for item in source if item.execution_id == "run-A"]
        if retained_controls:
            controls = BatchBody(
                batch_id="recent-controls",
                gateway_id="gateway",
                source_id=scope["authenticated_source_id"],
                stream_id=scope["authenticated_stream_id"],
                capture_generation=1,
                capture_seq=1,
                captured_at=datetime.now(UTC),
                events=retained_controls,
            )
            service.ingest(
                GatewayBatch(body=controls, content_sha256=batch_digest(controls)),
                **scope,
                now=datetime.now(UTC),
            )
        body = BatchBody(
            batch_id="old-plc-events",
            gateway_id="gateway",
            source_id=scope["authenticated_source_id"],
            stream_id=scope["authenticated_stream_id"],
            capture_generation=1,
            capture_seq=2,
            captured_at=OLD,
            events=run_events,
        )
        service.ingest(GatewayBatch(body=body, content_sha256=batch_digest(body)), **scope, now=OLD)
        with migrated.begin() as db:
            assert (
                record_final_summary(db, final, **scope, now=datetime.now(UTC)).status == "SETTLED"
            )
        before = service.get_totals("run-A")
        assert prune(migrated)["deleted_count"] == 1
        assert service.get_totals("run-A") == before
        assert len(rows(migrated, "SELECT * FROM event_inbox")) == len(source)


def test_generation_advance_does_not_pin_historical_observations(observation_service):
    service = observation_service
    submit(service, batch())
    # An operator-approved generation advance is represented by this test-owner
    # update; configure_source itself intentionally forbids replacing registration.
    with service.engine.begin() as db:
        db.execute(sa.text("UPDATE source_streams SET capture_generation=2"))
    assert prune(service.engine)["deleted_count"] == 1
    assert rows(service.engine, "SELECT capture_generation FROM ingest_retention_ranges") == [
        {"capture_generation": 1},
    ]
    with pytest.raises(IngestRetentionExpired):
        submit(service, batch())


def test_explicit_one_day_retention_and_within_period_ack(observation_service):
    service = observation_service
    now = datetime.now(UTC)
    old = batch()
    fresh = batch(capture_seq=2)
    submit(service, old, now - timedelta(days=2))
    original_ack = submit(service, fresh, now - timedelta(hours=12))
    assert maybe_prune_ingest(service.engine, retention_hours=24)["deleted_count"] == 1
    assert submit(service, fresh) == original_ack
    with pytest.raises(IngestRetentionExpired):
        submit(service, old)


def test_null_execution_control_event_is_not_an_event_free_observation(observation_service):
    service = observation_service
    value = batch(
        event(
            kind="STATE_CHANGED",
            execution_id=None,
            payload={"previous_state": "STOPPED", "state": "STOPPED", "control_version": "1"},
        )
    )
    submit(service, value)
    assert prune(service.engine)["deleted_count"] == 0
    assert len(rows(service.engine, "SELECT * FROM ingest_batches")) == 1


@pytest.mark.parametrize("blocker", ["GAP", "QUARANTINE", "COMMAND"])
def test_observation_retirement_preserves_relevant_source_holds(service, blocker):
    submit(service, batch())
    if blocker == "COMMAND":
        now = datetime.now(UTC)
        with service.engine.begin() as db:
            enqueue_command(
                db,
                CommandRequest(
                    command_id="retention-hold",
                    target_id="Line",
                    execution_id="run-1",
                    requested_by="production",
                    requested_at=now,
                    expires_at=now + timedelta(seconds=5),
                    expected_control_version=1,
                    command={"kind": "START", "args": {}},
                ),
                principal=Principal("production", frozenset({"operator"}), False),
                line_id=LINE_ID,
                source_id="simulator-1",
                stream_id="stream-1",
                now=now,
            )
    else:
        seq = 3 if blocker == "GAP" else 1
        execution = "run-1" if blocker == "GAP" else "unknown-run"
        submit(service, batch(event(seq, execution_id=execution), capture_seq=2), datetime.now(UTC))
    assert prune(service.engine)["deleted_count"] == 0
    assert rows(service.engine, "SELECT expired_at FROM ingest_receipts WHERE capture_seq=1") == [
        {"expired_at": None},
    ]


def test_candidate_scan_skips_ineligible_and_locked_sources_without_starving_others(service):
    service.configure_source(
        gateway_id="gateway-1",
        source_id="source-B",
        stream_id="stream-B",
        line_id=LINE_ID,
        capture_generation=1,
        now=OLD,
    )
    for capture_seq in (1, 3):
        submit(
            service,
            batch(
                event((capture_seq + 1) // 2, execution_id="run-1"),
                capture_seq=capture_seq,
            ),
        )
        observation = batch(
            capture_seq=capture_seq + 1,
            source_id="source-B",
            stream_id="stream-B",
        )
        service.ingest(
            observation,
            **(
                SCOPE
                | {
                    "authenticated_source_id": "source-B",
                    "authenticated_stream_id": "stream-B",
                }
            ),
            now=OLD,
        )
    with service.engine.begin() as locked:
        locked.execute(
            sa.text(
                "SELECT 1 FROM source_streams WHERE source_id='source-B' FOR UPDATE",
            )
        )
        with service.engine.begin() as maintenance:
            maintenance.execute(sa.text("SET LOCAL statement_timeout='500ms'"))
            result = maintenance.execute(
                sa.text(
                    "SELECT prune_ingest_batches(7,200,400)",
                )
            ).scalar_one()
        assert result["deleted_count"] == 0
    assert prune(service.engine, candidates=4)["deleted_count"] == 2
    assert rows(service.engine, "SELECT DISTINCT source_id FROM ingest_batches") == [
        {"source_id": "simulator-1"},
    ]


def test_expired_global_capture_identity_cannot_move_to_another_source(observation_service):
    service = observation_service
    submit(service, batch())
    assert prune(service.engine)["deleted_count"] == 1
    service.configure_source(
        gateway_id="gateway-1",
        source_id="other-source",
        stream_id="other-stream",
        line_id=LINE_ID,
        capture_generation=1,
        now=datetime.now(UTC),
    )
    value = batch(
        batch_id="changed-source-batch", source_id="other-source", stream_id="other-stream"
    )
    with pytest.raises(IngestRetentionExpired):
        service.ingest(
            value,
            **(
                SCOPE
                | {
                    "authenticated_source_id": "other-source",
                    "authenticated_stream_id": "other-stream",
                }
            ),
            now=datetime.now(UTC),
        )
    assert rows(service.engine, "SELECT * FROM ingest_conflicts") == []


async def test_authenticated_http_expiry_returns_410_without_ack(observation_service):
    service = observation_service
    value = batch(
        schema_version="1.2",
        communication={
            "state": "GOOD",
            "connected": True,
            "read_age_seconds": 0.0,
            "heartbeat_ages_seconds": {
                name: 0.0
                for name in (
                    "Line.Heartbeat",
                    "Infeed.LinkHeartbeat",
                    "Sealer.LinkHeartbeat",
                    "Outfeed.LinkHeartbeat",
                )
            },
        },
        capture_time={
            "clock_epoch": "retention-context-epoch",
            "monotonic_seconds": 123.5,
            "utc_read_span_seconds": 0.125,
        },
    )
    submit(service, value)
    assert prune(service.engine)["deleted_count"] == 1
    token = secrets.token_urlsafe(32)
    credential = ServiceCredential(
        "retention-key",
        "gateway-1",
        "simulator-1",
        "stream-1",
        hashlib.sha256(token.encode()).digest(),
        datetime.now(UTC) + timedelta(days=1),
    )
    app = create_internal_app(
        service,
        credential_bindings=[credential],
        config=InternalConfig(allow_insecure_loopback=True),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            "/internal/v1/batches",
            json=value.model_dump(mode="json"),
            headers={"Authorization": f"Bearer {token}"},
        )
        context = await client.get(
            "/internal/v1/alarm-clock-context",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 410
    assert response.json()["reason_code"] == "RETENTION_EXPIRED"
    assert "ack" not in response.json()
    assert context.status_code == 200
    latest = context.json()["latest_capture"]
    assert latest["batch_id"] == value.body.batch_id
    assert latest["capture_seq"] == str(value.body.capture_seq)
    assert latest["content_sha256"] == value.content_sha256
    assert latest["capture_time"] == value.body.capture_time.model_dump(mode="json")


@pytest.mark.parametrize("reuse", ["batch", "capture", "both"])
def test_expired_identity_reuse_rejects_before_conflict_or_quantity_effects(
    observation_service, reuse
):
    service = observation_service
    submit(service, batch())
    assert prune(service.engine)["deleted_count"] == 1
    value = batch(
        capture_seq=1 if reuse in {"capture", "both"} else 2,
        batch_id="batch-1" if reuse in {"batch", "both"} else "different-batch",
    )
    with pytest.raises(IngestRetentionExpired):
        submit(service, value)
    assert rows(service.engine, "SELECT * FROM ingest_conflicts") == []
    assert rows(service.engine, "SELECT * FROM production_events") == []
    assert rows(service.engine, "SELECT * FROM ingest_batches") == []


def test_failure_rolls_back_range_receipt_clock_and_raw_atomically(observation_service):
    engine = observation_service.engine
    submit(observation_service, batch())
    with engine.begin() as db:
        # Link the consumed nonce to this exact persisted capture input, so the
        # rollback checks cover related probes rather than an unrelated nonce.
        input_digest = db.execute(
            sa.text("SELECT input_evidence_sha256 FROM capture_clock_evidence")
        ).scalar_one()
        db.execute(
            sa.text("""INSERT INTO capture_clock_probes
            (nonce_sha256,credential_key_id,gateway_id,source_id,stream_id,capture_generation,
             clock_epoch,api_received_at,expires_at,gateway_send_utc,gateway_send_monotonic,
             consumed_batch_id,consumed_input_sha256,consumed_at)
            VALUES (:nonce,'rollback-key','gateway-1','simulator-1','stream-1',1,'clock-1',
                :at,:until,:at,1,'batch-1',:digest,:at)"""),
            {
                "nonce": "a" * 64,
                "digest": input_digest,
                "at": OLD,
                "until": OLD + timedelta(seconds=30),
            },
        )
        db.execute(
            sa.text("""CREATE FUNCTION fail_retention_delete() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected retirement failure'; END $$""")
        )
        db.execute(
            sa.text("""CREATE TRIGGER fail_retention_delete BEFORE DELETE ON ingest_batches
            FOR EACH ROW EXECUTE FUNCTION fail_retention_delete()""")
        )
    assert prune(engine)["status"] == "failed"
    assert len(rows(engine, "SELECT * FROM ingest_batches")) == 1
    assert len(rows(engine, "SELECT * FROM capture_clock_evidence")) == 1
    assert len(rows(engine, "SELECT * FROM capture_clock_probes")) == 1
    assert rows(engine, "SELECT * FROM ingest_retention_ranges") == []
    assert rows(engine, "SELECT expired_at FROM ingest_receipts")[0]["expired_at"] is None
    with engine.begin() as db:
        db.execute(sa.text("DROP TRIGGER fail_retention_delete ON ingest_batches"))
    assert prune(engine)["deleted_count"] == 1
    assert rows(engine, "SELECT * FROM capture_clock_probes") == []


def test_adjacent_ranges_compact_with_independent_modular_hash_oracle(observation_service):
    service = observation_service
    values = [batch(capture_seq=n) for n in (1, 2, 3)]
    for value in values:
        submit(service, value)
    expected = 0
    with service.engine.connect() as db:
        for value in values:
            body = value.body
            # PostgreSQL jsonb serialization is part of the specified leaf encoding.
            encoded = db.execute(
                sa.text("SELECT CAST(CAST(:leaf AS jsonb) AS text)"),
                {
                    "leaf": json.dumps(
                        [
                            "HMI_RETENTION_LEAF_V1",
                            body.gateway_id,
                            body.source_id,
                            body.stream_id,
                            str(body.capture_generation),
                            str(body.capture_seq),
                            body.batch_id,
                            value.content_sha256,
                        ]
                    ),
                },
            ).scalar_one()
            expected += int.from_bytes(hashlib.sha256(encoded.encode()).digest(), "big")
    for _ in values:
        assert prune(service.engine, batches=1)["deleted_count"] == 1
    ranges = rows(service.engine, "SELECT * FROM ingest_retention_ranges")
    assert len(ranges) == 1
    assert (ranges[0]["from_capture_seq"], ranges[0]["through_capture_seq"]) == (1, 3)
    assert ranges[0]["deleted_count"] == 3
    assert ranges[0]["hash_accumulator"] == f"{expected % (1 << 256):064x}"
    assert maybe_prune_ingest(service.engine)["status"] == "THROTTLED"


def test_direct_owner_delete_and_guc_cannot_forge_expiry(observation_service):
    submit(observation_service, batch())
    for sql in (
        "DELETE FROM ingest_receipts",
        "DELETE FROM capture_clock_evidence",
        "UPDATE ingest_receipts SET expired_at=clock_timestamp()",
    ):
        with pytest.raises(DBAPIError), observation_service.engine.begin() as db:
            db.execute(sa.text("SET LOCAL hmi.ingest_retention='on'"))
            db.execute(sa.text(sql))


@pytest.mark.parametrize("day_count", [8, 9])
def test_accelerated_days_keep_seven_raw_sets_and_quantify_permanent_proofs(
    observation_service,
    record_property,
    day_count,
):
    from apps.api.ingestion import ingest_batches

    service = observation_service
    anchor = datetime.now(UTC)
    first = batch()
    original_ack = submit(service, first, anchor - timedelta(days=day_count - 1, hours=12))
    per_day = 1000
    statistics = []
    for day in range(day_count):
        captures = []
        for index in range(per_day):
            seq = day * per_day + index + 1
            if seq == 1:
                continue
            value = batch(capture_seq=seq)
            ack = original_ack.model_dump(mode="json")
            ack.update(
                batch_id=value.body.batch_id,
                capture_seq=str(seq),
                content_sha256=value.content_sha256,
            )
            captures.append(
                dict(
                    gateway_id=value.body.gateway_id,
                    batch_id=value.body.batch_id,
                    source_id=value.body.source_id,
                    stream_id=value.body.stream_id,
                    capture_generation=1,
                    capture_seq=seq,
                    content_sha256=value.content_sha256,
                    body=value.body.model_dump(mode="json"),
                    ack=ack,
                    stored_at=anchor - timedelta(days=day_count - 1 - day, hours=12),
                )
            )
        with service.engine.begin() as db:
            db.execute(sa.insert(ingest_batches), captures)
    result = prune(service.engine, batches=200, candidates=400)
    while result["deleted_count"]:
        result = prune(service.engine, batches=200, candidates=400)
    finish_compaction(service.engine, result)
    with service.engine.connect() as db:
        counts = (
            db.execute(
                sa.text("""SELECT
            (SELECT count(*) FROM ingest_batches) AS raw,
            (SELECT count(*) FROM ingest_receipts) AS receipts,
            (SELECT count(*) FROM ingest_receipts WHERE expired_at IS NOT NULL) AS expired,
            pg_total_relation_size('ingest_batches') AS raw_bytes,
            pg_total_relation_size('ingest_receipts') AS receipt_bytes,
            pg_total_relation_size('ingest_retention_ranges') AS range_bytes""")
            )
            .mappings()
            .one()
        )
        statistics.append(dict(counts))
    assert counts["raw"] == 7 * per_day
    assert counts["receipts"] == 7 * per_day
    assert counts["expired"] == 0
    assert (
        sum(
            r["deleted_count"]
            for r in rows(
                service.engine,
                "SELECT deleted_count FROM ingest_retention_ranges",
            )
        )
        == (day_count - 7) * per_day
    )
    assert counts["receipt_bytes"] > 0
    record_property("accelerated_1000_captures_per_day_storage", json.dumps(statistics))
