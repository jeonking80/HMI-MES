"""Performance harness rejects corrupt results; no database/load is started here."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa

PATH = Path(__file__).parents[2] / "scripts" / "benchmark-history.py"
SPEC = importlib.util.spec_from_file_location("history_benchmark", PATH)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def payload():
    start = datetime(2026, 10, 16, tzinfo=UTC)
    end = start + timedelta(days=1)
    return (
        start,
        end,
        dict(
            resolution="minute",
            policy_version=benchmark.POLICY_VERSION,
            **{"from": benchmark.z(start), "to": benchmark.z(end)},
            series=[
                dict(
                    tag_id=tag,
                    points=[
                        dict(
                            t=benchmark.z(start + timedelta(minutes=i)),
                            value=float(index * 100),
                            min=float(index * 100),
                            max=float(index * 100),
                            quality="GOOD",
                            valid_count=60,
                            expected_count=60,
                        )
                        for i in range(1440)
                    ],
                )
                for index, tag in enumerate(benchmark.TAGS[:4])
            ],
        ),
    )


def test_exact_full_day_result_and_nearest_rank_tail():
    start, end, result = payload()
    benchmark.validate(result, start, end)
    stats = benchmark.distribution(list(range(1, 101)))
    assert stats["p50_seconds"] == 50
    assert stats["p95_seconds"] == 95
    assert stats["max_seconds"] == 100
    assert stats["samples"] == 100


@pytest.mark.parametrize(
    "field,value",
    [
        ("value", 999.0),
        ("quality", "TIME_UNKNOWN"),
        ("valid_count", 59),
        ("t", "2026-10-16T01:00:00Z"),
    ],
)
def test_benchmark_rejects_wrong_values_quality_counts_and_times(field, value):
    start, end, result = payload()
    result["series"][0]["points"][0][field] = value
    with pytest.raises(AssertionError):
        benchmark.validate(result, start, end)


def test_refuses_public_or_wrong_database(monkeypatch):
    for dsn in (
        "postgresql://user@public.example/cx20_test",
        "postgresql://user@localhost/production",
    ):
        monkeypatch.setenv("HMI_DATABASE_DSN", dsn)
        with pytest.raises(ValueError):
            benchmark.local_engine()


@pytest.mark.asyncio
async def test_real_loopback_application_configuration_without_database():
    origin = "http://127.0.0.1:12345"
    engine = sa.create_engine(
        "postgresql+psycopg://unused@127.0.0.1/cx20_smoke", hide_parameters=True
    )

    @sa.event.listens_for(engine, "do_connect")
    def forbid_database_connection(*_args):
        raise AssertionError("configuration smoke must not connect to PostgreSQL")

    try:
        auth = benchmark.UserAuth(engine, allowed_origins={origin})
        app = benchmark.benchmark_app(auth, origin)
        assert not benchmark.UserHTTPConfig(
            origin=origin, allow_insecure_loopback=True
        ).secure_cookies
        assert any(route.path == "/api/v1/trends" for route in app.routes)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=origin
        ) as client:
            # Parsing and the production host boundary precede database authorization.
            assert (await client.get("/api/v1/trends")).status_code == 422
            assert (
                await client.get("/api/v1/trends", headers={"host": "unexpected.example"})
            ).status_code == 400
    finally:
        engine.dispose()


def private_container():
    return dict(
        id="a" * 64,
        labels={"hmi.task": "cx20"},
        ports={"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "15432"}]},
        mounts=[dict(Type="bind", Source="/tmp/cx20-pg", Destination="/tmp/cx20-pg", RW=True)],
    )


def test_only_private_task_database_allows_cold_restart():
    details = private_container()
    url = sa.engine.make_url("postgresql+psycopg://unused@127.0.0.1:15432/cx20_smoke")
    benchmark.validate_cold_container(details, url, "/tmp/cx20-pg/data")


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_task",
        "compose",
        "wrong_port",
        "public_bind",
        "wrong_mount",
        "outside_tmp",
    ],
)
def test_cold_restart_guard_rejects_other_resources(mutation):
    details = private_container()
    data = "/tmp/cx20-pg/data"
    if mutation == "wrong_task":
        details["labels"]["hmi.task"] = "preview"
    elif mutation == "compose":
        details["labels"]["com.docker.compose.project"] = "preview"
    elif mutation == "wrong_port":
        details["ports"]["5432/tcp"][0]["HostPort"] = "5432"
    elif mutation == "public_bind":
        details["ports"]["5432/tcp"][0]["HostIp"] = "0.0.0.0"
    elif mutation == "wrong_mount":
        details["mounts"][0]["Destination"] = "/var/lib/postgresql/data"
    elif mutation == "outside_tmp":
        data = "/workspace/preview-pg/data"
    url = sa.engine.make_url("postgresql+psycopg://unused@127.0.0.1:15432/cx20_smoke")
    with pytest.raises(ValueError):
        benchmark.validate_cold_container(details, url, data)


def test_invalid_container_id_never_calls_docker(monkeypatch):
    def forbidden(*_args):
        raise AssertionError("invalid ID must not reach Docker")

    monkeypatch.setattr(benchmark, "docker_local", forbidden)
    with pytest.raises(ValueError):
        benchmark.cold_restart(None, "preview-container-name")


def test_cold_restart_disposes_pool_and_only_probes_readiness_after_restart(monkeypatch):
    import json
    from contextlib import contextmanager
    from types import SimpleNamespace

    events = []

    def docker(*args):
        events.append(args[0])
        return json.dumps(private_container()) if args[0] == "inspect" else "a" * 64

    def execute(statement):
        events.append(str(statement))
        value = "/tmp/cx20-pg/data" if str(statement) == "SHOW data_directory" else 1
        return SimpleNamespace(scalar_one=lambda: value)

    @contextmanager
    def connect():
        yield SimpleNamespace(execute=execute)

    engine = SimpleNamespace(
        url=sa.engine.make_url("postgresql+psycopg://unused@127.0.0.1:15432/cx20_smoke"),
        connect=connect,
        dispose=lambda: events.append("dispose"),
    )
    monkeypatch.setattr(benchmark, "docker_local", docker)
    state = benchmark.cold_restart(engine, "a" * 12)
    assert events == ["inspect", "SHOW data_directory", "dispose", "restart", "SELECT 1"]
    assert state["postgres_restarted"] is True
    assert state["os_cache_evicted"] is False
    assert state["history_read_after_restart_before_measurement"] is False


@pytest.mark.parametrize("selector", ["PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"])
def test_benchmark_rejects_inherited_connection_redirects(monkeypatch, selector):
    monkeypatch.setenv("HMI_DATABASE_DSN", "postgresql://user@127.0.0.1/cx20_smoke")
    monkeypatch.setenv(selector, "untrusted-override")
    with pytest.raises(ValueError, match="Inherited libpq"):
        benchmark.local_engine()


@pytest.mark.parametrize("selector", ["hostaddr", "host", "service", "options"])
def test_benchmark_rejects_dsn_connection_redirects(monkeypatch, selector):
    monkeypatch.setenv(
        "HMI_DATABASE_DSN",
        f"postgresql://user@127.0.0.1/cx20_smoke?{selector}=untrusted-override",
    )
    with pytest.raises(ValueError, match="no connection query options"):
        benchmark.local_engine()


def test_full_fixture_minute_expectation_preserves_production_float_average():
    start = datetime(2026, 10, 16, tzinfo=UTC)
    tag = benchmark.TAGS[5]
    observation = dict(
        scope=dict(
            source_id="cx20-source",
            stream_id="cx20-stream",
            gateway_id="cx20-gateway",
            capture_generation=1,
            clock_epoch="cx20-clock",
        ),
        capture_seq=1,
        time_quality="GOOD",
        sample=dict(
            sample_id="cx20-1",
            tag_id=tag,
            acquired_at=start,
            source_timestamp=start,
            server_timestamp=None,
            status_code=0,
            reading=dict(data_type="Double", value=500.0),
        ),
    )
    point = benchmark.expected_minute(observation, tag, start)
    assert point == dict(
        t=benchmark.z(start),
        value=500.00000000000006,
        min=500.0,
        max=500.0,
        quality="GOOD",
        valid_count=60,
        expected_count=60,
    )


def test_provenance_identifies_runner_and_backend_bytes():
    import hashlib

    hashes = benchmark.source_provenance()
    assert hashes["scripts/benchmark-history.py"] == hashlib.sha256(PATH.read_bytes()).hexdigest()
    root = PATH.parents[1]
    assert (
        hashes["apps/api/trend_history.py"]
        == hashlib.sha256((root / "apps/api/trend_history.py").read_bytes()).hexdigest()
    )
    assert "contracts/transport.py" in hashes
    assert "deploy/backup_set.py" in hashes


def test_seed_does_not_begin_transaction_around_alembic(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    events = []

    @contextmanager
    def connect():
        events.append("connect")
        yield SimpleNamespace(execute=lambda _sql: SimpleNamespace(scalar_one=lambda: 0))

    def begin():
        raise AssertionError("Alembic migration env owns begin/advisory lock")

    def upgrade(_config, _target):
        events.append("upgrade")
        raise LookupError("stop after testing migration boundary")

    monkeypatch.setattr(benchmark.command, "upgrade", upgrade)
    monkeypatch.setattr(benchmark, "migration_config", lambda db: db)
    with pytest.raises(LookupError):
        benchmark.seed(SimpleNamespace(connect=connect, begin=begin), {})
    assert events == ["connect", "connect", "upgrade"]
