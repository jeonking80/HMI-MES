"""Real authenticated rule provisioning and opt-in CLI internal HTTP startup."""

import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from alembic import command
from pydantic import ValidationError
from sqlalchemy import event, text

from apps.api.alarm_consumer import AlarmConsumerConfig, AlarmConsumerUnavailable, _digest
from apps.api.alarm_coordinator import AlarmCoordinator, MixedAlarmChannel, MixedAlarmConsumerConfig
from apps.api.alarm_lifecycle import AlarmConflict, AlarmRule, AlarmScope
from apps.api.alarm_qualification import PressureRuleConfig, TemperatureRuleConfig
from apps.api.alarm_rules import PLCFaultRuleConfig
from apps.api.alarm_runtime import (
    ALARM_RULES_FILE,
    MixedRuleOperation,
    MixedRuleProvisioningConfig,
    RuleOperation,
    RuleProvisioningConfig,
    configure_alarm_rules,
    start_alarm_runtime,
)
from apps.api.auth import UserAuth
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.database import migration_config
from apps.api.ingestion import IngestionService, SourceBindingError
from contracts.tags.validate import load_contract
from contracts.transport import BatchBody, GatewayBatch, batch_digest

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://alarm-admin.example.test"
SCOPE = AlarmScope(line_id=LINE_ID, source_id="source", stream_id="stream", asset_id="Sealer")


def rule(version="p1"):
    return PressureRuleConfig(rule=AlarmRule(rule_id="pressure", version=version, severity="HIGH"))


def provisioning(*, version="p1", expected=None, operation="install-pressure", **changes):
    return RuleProvisioningConfig(
        scope=SCOPE,
        gateway_id="gateway",
        capture_generation=1,
        operations=(
            RuleOperation(operation_id=operation, expected_version=expected, config=rule(version)),
        ),
        **changes,
    )


def account(auth, name, roles, *, initial=False):
    password = secrets.token_urlsafe(32)
    auth.bootstrap_user(name, password, roles=roles)
    if not initial:
        challenge = auth.begin_login()
        session = auth.login(
            name,
            password,
            challenge_token=challenge.token,
            csrf_token=challenge.csrf_token,
            origin=ORIGIN,
        )
        password = secrets.token_urlsafe(32)
        session = auth.change_password(
            session.token, password, csrf_token=session.csrf_token, origin=ORIGIN
        )
        auth.revoke(session.token, csrf_token=session.csrf_token, origin=ORIGIN)
    return password


@pytest.fixture
def state(migrated, tmp_path):
    bootstrap_reference_data(migrated)
    IngestionService(migrated).configure_source(
        source_id="source",
        stream_id="stream",
        gateway_id="gateway",
        line_id=LINE_ID,
        capture_generation=1,
        now=datetime.now(UTC),
    )
    auth = UserAuth(migrated, allowed_origins={ORIGIN})
    password = account(auth, "admin", {"equipment_admin"})
    secret = tmp_path / "password"
    secret.write_text(password)
    secret.chmod(0o600)
    config = tmp_path / "rules.json"
    config.write_text(provisioning().model_dump_json())
    env = os.environ.copy()
    env["HMI_DATABASE_DSN"] = migrated.url.render_as_string(hide_password=False)
    return SimpleNamespace(
        engine=migrated,
        auth=auth,
        password=password,
        secret=secret,
        config=config,
        env=env,
        tmp=tmp_path,
    )


def cli(state, *args):
    return subprocess.run(
        [sys.executable, "-m", "apps.api", *args],
        cwd=ROOT,
        env=state.env,
        text=True,
        capture_output=True,
        timeout=20,
    )


def configure_cli(state, *, login="admin"):
    return cli(
        state,
        "configure-alarm-rules",
        "--login",
        login,
        "--password-file",
        str(state.secret),
        "--origin",
        ORIGIN,
        "--config-file",
        str(state.config),
    )


def scalar(state, sql):
    with state.engine.connect() as db:
        return db.execute(text(sql)).scalar_one()


def configure(state, config=None, **extra):
    return configure_alarm_rules(
        state.auth,
        login="admin",
        password=state.password,
        origin=ORIGIN,
        config=config or provisioning(),
        **extra,
    )


def test_subprocess_real_admin_provisions_then_replays_without_new_rule_time(state):
    result = configure_cli(state)
    assert result.returncode == 0, result.stderr
    assert state.password not in result.stdout + result.stderr
    with state.engine.connect() as db:
        original = db.execute(text("SELECT * FROM alarm_rule_versions")).mappings().one()

    def forbidden():
        raise AssertionError("Original rule operation cannot consume a new timestamp")

    configure(state, clock=forbidden)
    with state.engine.connect() as db:
        assert db.execute(text("SELECT * FROM alarm_rule_versions")).mappings().one() == original
    assert scalar(state, "SELECT count(*) FROM auth_sessions WHERE revoked_at IS NULL") == 0
    assert scalar(state, "SELECT count(*) FROM alarm_capture_frontiers") == 0


@pytest.mark.parametrize(
    "role,initial",
    [
        ("viewer", False),
        ("operator", False),
        ("production_manager", False),
        ("equipment_admin", True),
    ],
)
def test_provisioning_requires_live_equipment_permission_and_changed_password(state, role, initial):
    password = account(state.auth, "candidate", {role}, initial=initial)
    state.secret.write_text(password)
    result = configure_cli(state, login="candidate")
    assert result.returncode == 1
    assert password not in result.stdout + result.stderr and "Traceback" not in result.stderr
    assert scalar(state, "SELECT count(*) FROM alarm_channels") == 0
    assert scalar(state, "SELECT count(*) FROM auth_sessions WHERE revoked_at IS NULL") == 0


@pytest.mark.parametrize("failure", ["password-public", "password-symlink", "malformed", "binding"])
def test_bad_protected_files_config_and_source_fail_without_installation(state, failure):
    if failure == "password-public":
        state.secret.chmod(0o644)
    elif failure == "password-symlink":
        link = state.tmp / "password-link"
        link.symlink_to(state.secret)
        state.secret = link
    elif failure == "malformed":
        state.config.write_text('{"operations":[]}')
    else:
        config = provisioning().model_dump(mode="json")
        config["gateway_id"] = "other-gateway"
        state.config.write_text(RuleProvisioningConfig.model_validate(config).model_dump_json())
    result = configure_cli(state)
    assert result.returncode == 1
    assert state.password not in result.stdout + result.stderr and "Traceback" not in result.stderr
    assert scalar(state, "SELECT count(*) FROM alarm_channels") == 0


def test_replace_rule_exact_replay_preserves_head_and_conflicting_actor_is_refused(state):
    configure(state)
    replacement = provisioning(version="p2", expected="p1", operation="replace-pressure")
    configure(state, replacement)
    configure(state)  # Original INSTALL receipt must not reset the p2 head.
    configure(state, replacement)
    assert scalar(state, "SELECT current_version FROM alarm_channels") == "p2"
    assert scalar(state, "SELECT count(*) FROM alarm_rule_versions") == 2
    with pytest.raises(AlarmConflict):
        configure(state, provisioning(version="p3", expected="p2", operation="replace-pressure"))
    password = account(state.auth, "other-admin", {"equipment_admin"})
    with pytest.raises(AlarmConflict):
        configure_alarm_rules(
            state.auth, login="other-admin", password=password, origin=ORIGIN, config=replacement
        )
    assert scalar(state, "SELECT count(*) FROM auth_sessions WHERE revoked_at IS NULL") == 0


def test_multi_rule_configuration_failure_rolls_back_the_entire_authorized_transaction(state):
    temperature = TemperatureRuleConfig(
        rule=AlarmRule(rule_id="temperature", version="t1", severity="WARNING"), setpoint_c=150.0
    )
    original = provisioning().model_copy(
        update={
            "operations": (
                RuleOperation(
                    operation_id="install-temperature", expected_version=None, config=temperature
                ),
            )
        }
    )
    configure(state, original)
    conflict = provisioning().model_copy(
        update={
            "operations": (
                provisioning().operations[0],
                RuleOperation(
                    operation_id="different-temperature-install",
                    expected_version=None,
                    config=temperature,
                ),
            )
        }
    )
    with pytest.raises(AlarmConflict):
        configure(state, conflict)
    assert scalar(state, "SELECT count(*) FROM alarm_channels") == 1
    assert scalar(state, "SELECT count(*) FROM alarm_rule_versions") == 1
    assert (
        scalar(state, "SELECT count(*) FROM alarm_receipts WHERE input_id='install-pressure'") == 0
    )
    assert scalar(state, "SELECT count(*) FROM auth_sessions WHERE revoked_at IS NULL") == 0


def capture(seq):
    now = datetime.now(UTC)
    samples = []
    for tag in load_contract()["tags"]:
        name, kind = tag["logical_name"], tag["data_type"]
        value = (
            False
            if kind == "Boolean"
            else ""
            if kind == "String"
            else 0.0
            if kind == "Double"
            else 0
        )
        samples.append(
            {
                "sample_id": f"{seq}-{name}",
                "tag_id": name,
                "source_timestamp": now,
                "server_timestamp": now,
                "acquired_at": now,
                "status_code": 0,
                "reading": {"data_type": kind, "value": value},
            }
        )
    body = BatchBody(
        schema_version="1.2",
        batch_id=f"capture-{seq}",
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        capture_generation=1,
        capture_seq=seq,
        captured_at=now,
        samples=samples,
        capture_time={
            "clock_epoch": "gateway-epoch",
            "monotonic_seconds": float(seq),
            "utc_read_span_seconds": 0.01,
        },
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
    )
    return GatewayBatch(body=body, content_sha256=batch_digest(body)).model_dump(mode="json")


@pytest.mark.parametrize("mixed_runtime", [False, True])
def test_actual_cli_starts_consumer_before_http_and_changed_rule_rejects_atomic_ingest(
    state, mixed_runtime
):
    configure(state, mixed_provisioning() if mixed_runtime else None)
    runtime = state.tmp / "runtime.json"
    config = AlarmConsumerConfig(
        scope=SCOPE, gateway_id="gateway", capture_generation=1, rules=(rule(),), clock_policy=None
    )
    if mixed_runtime:
        config = mixed_runtime_config()
    runtime.write_text(config.model_dump_json())
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    token = secrets.token_urlsafe(32)
    secret = state.tmp / "service-token"
    secret.write_text(token)
    secret.chmod(0o600)
    args = [
        "serve",
        "--source-id",
        "source",
        "--stream-id",
        "stream",
        "--gateway-id",
        "gateway",
        "--capture-generation",
        "1",
        "--key-id",
        "service",
        "--token-file",
        str(secret),
        "--expires-at",
        (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "--port",
        str(port),
        "--alarm-config-file",
        str(runtime),
    ]
    process = subprocess.Popen(
        [sys.executable, "-m", "apps.api", *args],
        cwd=ROOT,
        env=state.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    headers = {"Authorization": "Bearer " + token}
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2) as client:
            for _ in range(100):
                assert process.poll() is None
                try:
                    ready = client.get("/internal/v1/alarm-clock-context", headers=headers)
                    if ready.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.05)
            else:
                pytest.fail("Explicit alarm service did not start")
            assert scalar(state, "SELECT count(*) FROM alarm_capture_frontiers") == (
                2 if mixed_runtime else 1
            )
            assert (
                client.post("/internal/v1/batches", headers=headers, json=capture(1)).status_code
                == 200
            )
            assert scalar(state, "SELECT count(*) FROM alarm_capture_decisions") == (
                2 if mixed_runtime else 1
            )
            configure(
                state, provisioning(version="p2", expected="p1", operation="replace-pressure")
            )
            rejected = client.post("/internal/v1/batches", headers=headers, json=capture(2))
            assert rejected.status_code == 503
            assert scalar(state, "SELECT count(*) FROM ingest_batches") == 1
    finally:
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0
    assert token not in stdout + stderr and state.password not in stdout + stderr
    # A stale runtime cannot silently install/replace a rule during restart.
    stale = cli(state, *args)
    assert stale.returncode == 1 and "Traceback" not in stale.stderr


FAULT = PLCFaultRuleConfig(rule=AlarmRule(rule_id="fault", version="f1", severity="HIGH"))
LINE = SCOPE.model_copy(update={"asset_id": "Line"})


def mixed_provisioning():
    return MixedRuleProvisioningConfig(
        kind="MIXED_ALARM_RULES",
        line_id=SCOPE.line_id,
        source_id=SCOPE.source_id,
        stream_id=SCOPE.stream_id,
        gateway_id="gateway",
        capture_generation=1,
        operations=(
            MixedRuleOperation(
                asset_id="Line", operation_id="install-fault", expected_version=None, config=FAULT
            ),
            MixedRuleOperation(
                asset_id="Sealer",
                operation_id="install-pressure",
                expected_version=None,
                config=rule(),
            ),
        ),
    )


def mixed_runtime_config():
    return MixedAlarmConsumerConfig(
        kind="MIXED_ALARM_RUNTIME",
        line_id=SCOPE.line_id,
        source_id=SCOPE.source_id,
        stream_id=SCOPE.stream_id,
        gateway_id="gateway",
        capture_generation=1,
        channels=(
            MixedAlarmChannel(asset_id="Line", config=FAULT),
            MixedAlarmChannel(asset_id="Sealer", config=rule()),
        ),
        clock_policy=None,
    )


def binding(**updates):
    return SimpleNamespace(
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        capture_generation=1,
        **updates,
    )


def test_mixed_subprocess_real_admin_and_original_actor_timestamp_replay(state):
    config = mixed_provisioning()
    temperature = TemperatureRuleConfig(
        rule=AlarmRule(rule_id="temperature", version="t1", severity="WARNING"), setpoint_c=150.0
    )
    config = config.model_copy(
        update={
            "operations": (
                *config.operations,
                MixedRuleOperation(
                    asset_id="Sealer",
                    operation_id="install-temperature",
                    expected_version=None,
                    config=temperature,
                ),
            )
        }
    )
    state.config.write_text(config.model_dump_json())
    result = configure_cli(state)
    assert result.returncode == 0, result.stderr
    assert state.password not in result.stdout + result.stderr
    with state.engine.connect() as db:
        before = list(
            db.execute(text("SELECT * FROM alarm_rule_versions ORDER BY channel_id")).mappings()
        )

    def forbidden():
        raise AssertionError("Exact mixed retry must reuse original timestamps")

    configure(state, config, clock=forbidden)
    with state.engine.connect() as db:
        assert (
            list(
                db.execute(text("SELECT * FROM alarm_rule_versions ORDER BY channel_id")).mappings()
            )
            == before
        )
    password = account(state.auth, "other-mixed-admin", {"equipment_admin"})
    with pytest.raises(AlarmConflict):
        configure_alarm_rules(
            state.auth,
            login="other-mixed-admin",
            password=password,
            origin=ORIGIN,
            config=config,
            clock=forbidden,
        )
    assert scalar(state, "SELECT count(*) FROM auth_sessions WHERE revoked_at IS NULL") == 0
    assert scalar(state, "SELECT count(*) FROM alarm_capture_frontiers") == 0


@pytest.mark.parametrize("role,initial", [("operator", False), ("equipment_admin", True)])
def test_mixed_cli_requires_equipment_authorization_and_password_change(state, role, initial):
    state.config.write_text(mixed_provisioning().model_dump_json())
    password = account(state.auth, "candidate", {role}, initial=initial)
    state.secret.write_text(password)
    result = configure_cli(state, login="candidate")
    assert result.returncode == 1 and "Traceback" not in result.stderr
    assert password not in result.stdout + result.stderr
    assert scalar(state, "SELECT count(*) FROM alarm_channels") == 0
    assert scalar(state, "SELECT count(*) FROM auth_sessions WHERE revoked_at IS NULL") == 0


def test_mixed_provisioning_last_conflict_rolls_back_both_targets(state):
    configure(state)  # Existing pressure INSTALL receipt has a different operation ID below.
    config = mixed_provisioning()
    changed = config.model_dump(mode="json")
    changed["operations"][1]["operation_id"] = "different-pressure-install"
    with pytest.raises(AlarmConflict):
        configure(state, MixedRuleProvisioningConfig.model_validate(changed))
    assert scalar(state, "SELECT count(*) FROM alarm_channels") == 1
    assert scalar(state, "SELECT count(*) FROM alarm_receipts WHERE input_id='install-fault'") == 0
    assert scalar(state, "SELECT count(*) FROM auth_sessions WHERE revoked_at IS NULL") == 0


@pytest.mark.parametrize("failure", ["target", "duplicate-kind", "proof", "missing-expected"])
def test_mixed_file_rejects_invalid_targets_duplicates_and_user_processing_proof(failure):
    raw = mixed_provisioning().model_dump(mode="json")
    if failure == "target":
        raw["operations"][0]["asset_id"] = "Sealer"
    elif failure == "duplicate-kind":
        extra = dict(raw["operations"][0], operation_id="other-fault")
        raw["operations"].append(extra)
    elif failure == "proof":
        raw["operations"][0]["context"] = {"classification": "LIVE"}
    else:
        del raw["operations"][0]["expected_version"]
    with pytest.raises(ValidationError):
        ALARM_RULES_FILE.validate_python(raw)


def test_public_legacy_and_mixed_runtime_use_exact_coordinator_hash_and_omission_guard(state):
    configure(state, mixed_provisioning())
    legacy = AlarmConsumerConfig(
        scope=SCOPE, gateway_id="gateway", capture_generation=1, rules=(rule(),), clock_policy=None
    )
    consumer = start_alarm_runtime(state.engine, legacy, args=binding())
    assert isinstance(consumer, AlarmCoordinator) and consumer.config_sha256 == _digest(legacy)
    mixed = start_alarm_runtime(state.engine, mixed_runtime_config(), args=binding())
    assert isinstance(mixed, AlarmCoordinator)
    assert mixed.config_sha256 == _digest(mixed_runtime_config())
    with state.engine.connect() as db:
        before = list(
            db.execute(text("SELECT * FROM alarm_capture_frontiers ORDER BY channel_id")).mappings()
        )
    with pytest.raises(AlarmConsumerUnavailable):
        start_alarm_runtime(state.engine, legacy, args=binding())
    with state.engine.connect() as db:
        assert (
            list(
                db.execute(
                    text("SELECT * FROM alarm_capture_frontiers ORDER BY channel_id")
                ).mappings()
            )
            == before
        )


def test_runtime_source_mismatch_and_old_head_fail_without_activation(state):
    configure(state, mixed_provisioning())
    wrong = mixed_runtime_config().model_copy(update={"gateway_id": "other"})
    with pytest.raises(SourceBindingError):
        start_alarm_runtime(state.engine, wrong, args=binding())
    with state.engine.connect() as db:
        command.downgrade(migration_config(db), "0013_work_order_cancel")
    legacy = AlarmConsumerConfig(
        scope=SCOPE, gateway_id="gateway", capture_generation=1, rules=(rule(),), clock_policy=None
    )
    with pytest.raises(AlarmConsumerUnavailable):
        start_alarm_runtime(state.engine, legacy, args=binding())
    with pytest.raises(AlarmConflict):
        configure(state, mixed_provisioning())
    assert scalar(state, "SELECT count(*) FROM alarm_capture_frontiers") == 0


def test_provisioning_locks_source_and_complete_current_roster_before_any_inputs(state):
    configure(state, mixed_provisioning())
    start_alarm_runtime(state.engine, mixed_runtime_config(), args=binding())
    order = []

    def trace(db, cursor, sql, params, context, many):
        if "FOR UPDATE" in sql and "source_streams" in sql:
            order.append("source")
        if "pg_advisory_xact_lock" in sql and isinstance(params, dict):
            key = params.get("key", "")
            if not isinstance(key, str):
                return
            if key.startswith("alarm-channel:"):
                order.append(key)
            elif key.startswith("alarm-input:"):
                order.append(key)

    event.listen(state.engine, "before_cursor_execute", trace)
    try:
        configure(state)  # Single legacy operation still locks both durable targets.
    finally:
        event.remove(state.engine, "before_cursor_execute", trace)
    first_input = next(i for i, key in enumerate(order) if key.startswith("alarm-input:"))
    channels = [key for key in order[:first_input] if key.startswith("alarm-channel:")]
    assert "source" in order[:first_input]
    assert len(channels) == 2 and channels == sorted(channels)
