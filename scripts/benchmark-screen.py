#!/usr/bin/env python3
"""One disposable PG17 run: retained screen reads over authenticated localhost HTTP.

The SQL-cloned fixture measures reads, not gateway intake/protocol throughput.
No DSN is accepted or inherited. Only this process's new container is touched.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/integration"))

import httpx
import psycopg
import sqlalchemy as sa
import uvicorn
from alembic import command
from sqlalchemy.engine import URL
from test_screen_http import SCOPE, capture

import apps.api.user_http as user_http
from apps.api.auth import UserAuth
from apps.api.bootstrap import bootstrap_reference_data
from apps.api.database import create_database_engine, migration_config
from apps.api.ingestion import IngestionService, ingest_batches
from apps.api.screen_frames import APP_SCREEN_ADAPTER, APP_SCREEN_MAX_BYTES, ScreenDataFrame
from apps.api.user_http import UserHTTPConfig, create_user_app
from contracts.transport import BatchBody, batch_digest

ROOT = Path(__file__).resolve().parents[1]

IMAGE = (
    "postgres:17-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826"
)
RARE = "Sealer.AirPressure"
RESERVE_BYTES = 1024**3
LEGACY_SQL = """
SELECT DISTINCT ON (sample->>'tag_id') sample, b.batch_id,
       b.capture_generation, b.capture_seq,
       b.body->>'captured_at' AS captured_at, b.stored_at
FROM ingest_batches b
CROSS JOIN LATERAL jsonb_array_elements(b.body->'samples') AS sample
WHERE b.source_id=:source_id AND b.stream_id=:stream_id
  AND b.gateway_id=:gateway_id AND b.capture_generation=:capture_generation
ORDER BY sample->>'tag_id', b.capture_seq DESC
"""


def docker(*arguments):
    environment = os.environ.copy()
    for key in (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    ):
        environment.pop(key, None)
    result = subprocess.run(
        ["docker", "--host=unix:///var/run/docker.sock", *arguments],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("local Docker command failed")
    return result.stdout.strip()


@contextmanager
def disposable_engine(report):
    container = None
    engine = None
    with tempfile.TemporaryDirectory(prefix="hmi-screen-secret-") as directory:
        secret_path = Path(directory) / "postgres.env"
        password = secrets.token_urlsafe(32)
        try:
            descriptor = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(f"POSTGRES_PASSWORD={password}\nPOSTGRES_DB=hmi_screen_benchmark\n")
            docker("info", "--format", "{{.ServerVersion}}")
            container = docker(
                "run",
                "--detach",
                "--rm",
                "--pull=never",
                "--label",
                "hmi.task=v10-screen-benchmark",
                "--publish",
                "127.0.0.1::5432",
                "--env-file",
                str(secret_path),
                IMAGE,
            )
            port = int(docker("port", container, "5432/tcp").rsplit(":", 1)[1])
            deadline = time.monotonic() + 45
            while True:
                try:
                    with psycopg.connect(
                        host="127.0.0.1",
                        port=port,
                        user="postgres",
                        password=password,
                        dbname="hmi_screen_benchmark",
                        connect_timeout=2,
                    ) as db:
                        assert db.execute("SHOW server_version_num").fetchone()[0].startswith("17")
                    break
                except psycopg.OperationalError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("owned PostgreSQL readiness deadline") from None
                    time.sleep(0.25)
            free = (
                int(
                    docker(
                        "exec",
                        container,
                        "sh",
                        "-c",
                        "df -Pk /var/lib/postgresql/data | awk 'NR==2 {print $4}'",
                    )
                )
                * 1024
            )
            report["database_disk_preflight"] = {
                "free_bytes": free,
                "required_reserve_bytes": RESERVE_BYTES,
                "scope": "new owned container PostgreSQL data filesystem",
            }
            if free < RESERVE_BYTES:
                raise RuntimeError("insufficient owned PostgreSQL filesystem capacity")
            engine = create_database_engine(
                URL.create(
                    "postgresql+psycopg",
                    username="postgres",
                    password=password,
                    host="127.0.0.1",
                    port=port,
                    database="hmi_screen_benchmark",
                )
            )
            yield engine
        finally:
            if engine is not None:
                engine.dispose()
            try:
                if container:
                    docker("rm", "--force", "--volumes", container)
                    report["owned_container_removed"] = True
            finally:
                secret_path.unlink(missing_ok=True)


def clone(template, sequence, *, partial):
    body = copy.deepcopy(template)
    body["batch_id"] = str(uuid4())
    body["capture_seq"] = str(sequence)
    body["samples"] = [
        sample for sample in body["samples"] if not partial or sample["tag_id"] != RARE
    ]
    for sample in body["samples"]:
        sample["sample_id"] = str(uuid4())
        if sample["tag_id"] == RARE:
            sample["status_code"] = 0
            sample["reading"]["value"] = 12.5
    validated = BatchBody.model_validate(body)
    assert validated.capture_seq == sequence
    assert validated.source_id == SCOPE["source_id"]
    assert validated.stream_id == SCOPE["stream_id"] and validated.gateway_id == "gateway"
    assert len(validated.samples) == (47 if partial else 48)
    assert len({sample.sample_id for sample in validated.samples}) == len(validated.samples)
    return validated


def insert_clones(engine, template, first, last, *, partial):
    final = None
    for left in range(first, last + 1, 250):
        rows = []
        for sequence in range(left, min(left + 250, last + 1)):
            final = clone(template, sequence, partial=partial)
            rows.append(
                dict(
                    gateway_id=final.gateway_id,
                    batch_id=final.batch_id,
                    source_id=final.source_id,
                    stream_id=final.stream_id,
                    capture_generation=final.capture_generation,
                    capture_seq=final.capture_seq,
                    content_sha256=batch_digest(final),
                    body=final.model_dump(mode="json"),
                    ack={"fixture_only": True},
                    stored_at=final.captured_at,
                )
            )
        with engine.begin() as db:
            db.execute(sa.insert(ingest_batches), rows)
    assert final is not None
    with engine.begin() as db:
        db.execute(sa.text("ANALYZE ingest_batches"))
    return final


def inventory(engine, expected_count, latest_sequence, *, partial):
    with engine.connect() as db:
        count, sequences, minimum, maximum, invalid = db.execute(
            sa.text("""
            SELECT count(*), count(DISTINCT capture_seq), min(capture_seq), max(capture_seq),
              count(*) FILTER (WHERE body->>'batch_id' IS DISTINCT FROM batch_id
                OR body->>'gateway_id' IS DISTINCT FROM gateway_id
                OR body->>'source_id' IS DISTINCT FROM source_id
                OR body->>'stream_id' IS DISTINCT FROM stream_id
                OR (body->>'capture_seq')::numeric IS DISTINCT FROM capture_seq
                OR (body->>'capture_generation')::numeric IS DISTINCT FROM capture_generation
                OR body->>'schema_version' IS DISTINCT FROM '1.1')
            FROM ingest_batches
        """)
        ).one()
        assert count == sequences == expected_count and minimum == 1
        assert maximum == latest_sequence and invalid == 0
        shapes = dict(
            db.execute(
                sa.text("""
            SELECT jsonb_array_length(body->'samples'),count(*) FROM ingest_batches GROUP BY 1
        """)
            ).all()
        )
        expected_shapes = {48: 1, 47: expected_count - 1} if partial else {48: 2, 47: count - 2}
        assert shapes == expected_shapes
        unique_ids, total_ids = db.execute(
            sa.text("""
            SELECT count(DISTINCT sample->>'sample_id'),count(*) FROM ingest_batches
            CROSS JOIN LATERAL jsonb_array_elements(body->'samples') sample
        """)
        ).one()
        assert unique_ids == total_ids
        if partial:
            assert (
                db.execute(
                    sa.text("""
                SELECT count(*) FROM ingest_batches WHERE capture_seq>1 AND
                body @> '{"samples":[{"tag_id":"Sealer.AirPressure"}]}'::jsonb
            """)
                ).scalar_one()
                == 0
            )
        return dict(
            batches=count,
            unique_capture_sequences=sequences,
            invalid_identity_rows=invalid,
            sample_counts=shapes,
            unique_sample_ids=unique_ids,
            relation_bytes=db.execute(
                sa.text("SELECT pg_total_relation_size('ingest_batches')")
            ).scalar_one(),
        )


def validate(response, expected, old, *, partial):
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers and len(response.content) <= APP_SCREEN_MAX_BYTES
    frame = APP_SCREEN_ADAPTER.validate_json(response.content)
    assert isinstance(frame, ScreenDataFrame) and frame.scope.model_dump() == SCOPE
    assert frame.screen.kind == "SNAPSHOT" and frame.screen.revision == 0
    assert frame.missing_tag_ids == ()
    wanted = {sample.tag_id: sample for sample in expected.samples}
    if partial:
        wanted[RARE] = next(sample for sample in old.samples if sample.tag_id == RARE)
    actual = {sample.tag_id: sample for sample in frame.screen.view.samples}
    assert actual == wanted and len(actual) == len(frame.evidence) == 48
    for evidence in frame.evidence:
        source = old if partial and evidence.tag_id == RARE else expected
        assert evidence.batch_id == source.batch_id
        assert evidence.capture_seq == source.capture_seq and evidence.capture_generation == 1
        assert evidence.captured_at == evidence.stored_at == source.captured_at
    if partial:
        assert actual[RARE].status_code == 0x80000000
        assert actual[RARE].reading.value is None
        raw = next(s for s in response.json()["screen"]["view"]["samples"] if s["tag_id"] == RARE)
        assert raw["status_code"] == 0x80000000 and raw["reading"]["value"] is None
    else:
        assert actual[RARE].status_code == 0 and actual[RARE].reading.value == 12.5


def explain(engine, captured):
    result = {}
    with engine.connect() as db:
        result["legacy_distinct_on"] = db.execute(
            sa.text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + LEGACY_SQL),
            dict(SCOPE, gateway_id="gateway", capture_generation=1),
        ).scalar_one()
        for index, (statement, parameters) in enumerate(captured):
            result[f"snapshot_query_{index + 1}"] = {
                "sql": statement,
                "plan": db.exec_driver_sql(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters
                ).scalar_one(),
            }
    assert captured
    return result


def distribution(samples, threshold):
    ordered = sorted(samples)
    return dict(
        samples=len(samples),
        durations_seconds=samples,
        p50_seconds=ordered[math.ceil(len(samples) * 0.5) - 1],
        p95_seconds=ordered[math.ceil(len(samples) * 0.95) - 1],
        max_seconds=max(samples),
        threshold_seconds=threshold,
        passed=ordered[math.ceil(len(samples) * 0.95) - 1] <= threshold,
    )


def benchmark(engine, report, template, old, partial_latest, batches, threshold):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    auth = UserAuth(engine, allowed_origins={origin})
    password = secrets.token_urlsafe(24)
    auth.bootstrap_user("screen-benchmark", password, roles={"viewer"})
    app = create_user_app(auth, UserHTTPConfig(origin=origin, allow_insecure_loopback=True))
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    local = threading.local()
    captured = []
    original = user_http.read_snapshot

    def observing(*args, **kwargs):
        local.active = True
        try:
            return original(*args, **kwargs)
        finally:
            local.active = False

    def collect(_connection, _cursor, statement, parameters, _context, _many):
        if getattr(local, "active", False) and "ingest_batches" in statement:
            if not any(sql == statement for sql, _ in captured):
                captured.append((statement, copy.deepcopy(parameters)))

    user_http.read_snapshot = observing
    sa.event.listen(engine, "before_cursor_execute", collect)
    try:
        thread.start()
        deadline = time.monotonic() + 15
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("HTTP server readiness deadline")
            time.sleep(0.05)
        with httpx.Client(base_url=origin, timeout=120, trust_env=False) as client:
            assert client.get("/api/v1/screen", params=SCOPE).status_code == 401
            challenge = client.post(
                "/auth/challenge", headers={"Origin": origin, "X-HMI-Request": "1"}
            )
            assert challenge.status_code == 200
            login = client.post(
                "/auth/login",
                json={"login": "screen-benchmark", "password": password},
                headers={"Origin": origin, "X-CSRF-Token": challenge.json()["csrf_token"]},
            )
            assert login.status_code == 200
            rotated = client.post(
                "/auth/password",
                json={"new_password": secrets.token_urlsafe(24)},
                headers={"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]},
            )
            assert rotated.status_code == 200
            for shape in ("partial", "full"):
                partial = shape == "partial"
                if partial:
                    latest = partial_latest
                else:
                    latest = insert_clones(
                        engine, template, batches + 2, batches + 2, partial=False
                    )
                    report["full_inventory"] = inventory(
                        engine, batches + 2, batches + 2, partial=False
                    )
                captured.clear()
                durations, sizes = [], []
                report[shape] = {"durations_seconds": durations}
                for _ in range(30):
                    began = time.perf_counter()
                    response = client.get("/api/v1/screen", params=SCOPE)
                    durations.append(time.perf_counter() - began)
                    validate(response, latest, old, partial=partial)
                    sizes.append(len(response.content))
                report[shape] = dict(distribution(durations, threshold), payload_bytes=sizes)
                report[f"{shape}_plans"] = explain(engine, captured)
    finally:
        sa.event.remove(engine, "before_cursor_execute", collect)
        user_http.read_snapshot = original
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batches", type=int, default=5000, help="partial captures, minimum 5000")
    parser.add_argument("--p95-seconds", type=float, default=0.5)
    parser.add_argument("--output", type=Path, default=Path("/tmp/hmi-screen-v10.json"))
    args = parser.parse_args()
    report = dict(
        status="running",
        postgres_image=IMAGE,
        fixture="one real intake template; validated SQL bulk clones bypass intake",
        limitation="read performance only; no protocol/intake throughput or clock proof",
        query_bound="bounded JSON expansion, not bounded history containment probes",
        measurement="30 serial TCP HTTP requests per shape; login excluded; no retries/warmup",
        cache_condition="DB/OS caches not evicted; fixture inventory precedes reads",
        started_utc=datetime.now(UTC).isoformat(),
    )
    code = 1
    try:
        if (
            args.batches < 5000
            or not math.isfinite(args.p95_seconds)
            or not 0 < args.p95_seconds <= 0.5
        ):
            raise ValueError("invalid benchmark options")
        report["host_disk_free_bytes"] = shutil.disk_usage("/tmp").free
        if report["host_disk_free_bytes"] < RESERVE_BYTES:
            raise RuntimeError("insufficient host filesystem capacity")
        with disposable_engine(report) as engine:
            with engine.connect() as db:
                command.upgrade(migration_config(db), "head")
            with engine.connect() as db:
                report["migration_revision"] = db.execute(
                    sa.text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                report["postgresql_version"] = db.execute(sa.text("SELECT version()")).scalar_one()
            report["cpu_count"] = os.cpu_count()
            bootstrap_reference_data(engine)
            at = datetime.now(UTC)
            service = IngestionService(engine)
            service.configure_source(**SCOPE, gateway_id="gateway", capture_generation=1, now=at)
            batch = capture(SimpleNamespace(at=at))
            assert len(batch.body.samples) == 48
            service.ingest(
                batch,
                authenticated_gateway_id="gateway",
                authenticated_source_id=SCOPE["source_id"],
                authenticated_stream_id=SCOPE["stream_id"],
                now=at,
            )
            template = batch.body.model_dump(mode="json")
            latest = insert_clones(engine, template, 2, args.batches + 1, partial=True)
            report["partial_inventory"] = inventory(
                engine, args.batches + 1, args.batches + 1, partial=True
            )
            benchmark(engine, report, template, batch.body, latest, args.batches, args.p95_seconds)
            report["status"] = (
                "passed"
                if report["partial"]["passed"] and report["full"]["passed"]
                else "target_failed"
            )
            code = 0 if report["status"] == "passed" else 1
    except Exception as error:
        # Neither exception messages nor HTTP/auth bodies may disclose secrets.
        report["status"] = "failed"
        report["error_type"] = type(error).__name__
        report["failure_locations"] = [
            dict(file=Path(frame.filename).name, line=frame.lineno, function=frame.name)
            for frame in traceback.extract_tb(error.__traceback__)
        ]
    finally:
        report["ended_utc"] = datetime.now(UTC).isoformat()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
        print(json.dumps({"status": report["status"], "output": str(args.output)}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
