"""Stopped-writer, local-only complete backup sets. No existing database is restored over."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
from contextlib import ExitStack, closing, contextmanager
from dataclasses import asdict, fields
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import make_url

MEMBERS = ("postgres.dump", "settings.json", "simulator.sqlite3", "gateway.sqlite3")
PG_TABLES = (
    "production_executions",
    "execution_totals",
    "production_events",
    "command_requests",
    "alarm_instances",
    "stream_progress",
    "stream_gaps",
)
SIM_TABLES = (
    "simulator_identity",
    "executions",
    "source_events",
    "command_journal",
    "runtime_state",
    "runtime_runs",
    "runtime_settings",
)
GATEWAY_TABLES = ("gateway_identity", "source_progress", "missing_ranges", "outbound_batches")
SETTING_KEYS = {
    "app_version",
    "contract_version",
    "schema_version",
    "line_id",
    "source_id",
    "stream_id",
    "counter_epoch",
    "gateway_id",
    "controller",
}


def controller_settings(runtime):
    return {
        "process_config": asdict(runtime.process_config),
        "plc_config": runtime.plc_config.model_dump(mode="json"),
        "seed": runtime.seed,
    }


def load_controller_settings(settings):
    from apps.simulator.plc import PLCConfig
    from apps.simulator.process import ProcessConfig

    if set(settings) != SETTING_KEYS or any(
        not isinstance(v, str) or not v for k, v in settings.items() if k != "controller"
    ):
        raise ValueError("Invalid nonsecret settings fields")
    controller = settings["controller"]
    if not isinstance(controller, dict) or set(controller) != {
        "process_config",
        "plc_config",
        "seed",
    }:
        raise ValueError("Invalid controller settings")
    process = controller["process_config"]
    if not isinstance(process, dict) or set(process) != {f.name for f in fields(ProcessConfig)}:
        raise ValueError("Invalid process settings")
    process = dict(process)
    process["stage_lengths_mm"] = tuple(process["stage_lengths_mm"])
    process["pattern"] = tuple(process["pattern"])
    if type(controller["seed"]) is not int or not 0 <= controller["seed"] <= 2**64 - 1:
        raise ValueError("Invalid controller seed")
    return dict(
        process_config=ProcessConfig(**process),
        plc_config=PLCConfig.model_validate(controller["plc_config"]),
        seed=controller["seed"],
    )


def local_url(value):
    if any(
        os.environ.get(key) for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS")
    ):
        raise ValueError(
            "Inherited libpq connection overrides are forbidden for local backup/restore"
        )
    url = make_url(value)
    if url.get_backend_name() != "postgresql" or url.host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Only an explicit loopback PostgreSQL database is supported")
    if not url.database or url.query:
        raise ValueError("Explicit database and no connection query options required")
    return url


def pg_tool(tool, args, url, payload=None):
    """Passwords travel through the child environment, never arguments or manifests."""
    url = local_url(url)
    env = os.environ.copy()
    env.update(
        PGHOST=url.host,
        PGPORT=str(url.port or 5432),
        PGUSER=url.username or "",
        PGPASSWORD=url.password or "",
        PGDATABASE=url.database,
        PGCONNECT_TIMEOUT="5",
    )
    result = subprocess.run(
        [tool, *args],
        input=payload,
        capture_output=True,
        env=env,
        timeout=180,
    )
    if result.returncode:
        raise RuntimeError(f"{tool} failed (exit {result.returncode}); diagnostic output withheld")
    return result.stdout


def _regular(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Symlink paths are not supported")
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("Expected a single-link regular file")
    return path


def _new_directory(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Symlink paths are not supported")
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    return path


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sqlite_rows(path, tables):
    with closing(sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True)) as db:
        if (
            db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
            or db.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise ValueError("SQLite integrity or foreign key validation failed")
        return {t: sorted(_json(row) for row in db.execute(f"SELECT * FROM {t}")) for t in tables}


def _pg_rows(db):
    return {
        t: sorted(_json(dict(r)) for r in db.execute(sa.text(f"SELECT * FROM {t}")).mappings())
        for t in PG_TABLES
    }


def inventory(engine, simulator, gateway):
    with engine.connect() as db:
        pg = _pg_rows(db)
    return {
        "postgres": pg,
        "simulator": _sqlite_rows(simulator, SIM_TABLES),
        "gateway": _sqlite_rows(gateway, GATEWAY_TABLES),
    }


@contextmanager
def _stopped(paths):
    with ExitStack() as stack:
        for path in paths:
            _regular(path)
            lock = _regular(str(path) + ".lock")
            handle = stack.enter_context(lock.open("rb"))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Stop simulator and gateway writers before backup") from None
        yield


def create_backup_set(
    *, engine, simulator, gateway, settings, destination, backup_set_id, run_pg=pg_tool
):
    """Caller stops HTTP/bootstrap/maintenance and safely pauses PLC before closing writers.

    Exclusive owner locks plus PostgreSQL SHARE table locks enforce the durable barrier.
    """
    url = local_url(engine.url)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", backup_set_id):
        raise ValueError("Invalid backup_set_id")
    load_controller_settings(settings)
    simulator, gateway = _regular(simulator), _regular(gateway)
    with _stopped((simulator, gateway)), engine.begin() as db:
        # API schema lock precedes table locks, matching ordinary writer lock ordering.
        from apps.api.database import SCHEMA_LOCK_ID

        db.execute(sa.text("SET LOCAL lock_timeout='5s'"))
        db.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCHEMA_LOCK_ID})
        tables = (
            db.execute(
                sa.text(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
                )
            )
            .scalars()
            .all()
        )
        for table in tables:
            quoted = db.dialect.identifier_preparer.quote(table)
            db.execute(sa.text(f"LOCK TABLE public.{quoted} IN SHARE MODE"))
        with closing(sqlite3.connect(f"{simulator.as_uri()}?mode=ro", uri=True)) as probe:
            row = probe.execute(
                "SELECT snapshot_json FROM runtime_state WHERE singleton=1"
            ).fetchone()
            state = json.loads(row[0])
            if state["state"] not in {
                "STOPPED",
                "READY",
                "PAUSED",
                "FAULT",
                "ESTOP",
                "RECOVERY_REQUIRED",
            } or state.get("run_output", True):
                raise ValueError("PLC must be safely stopped before backup")
        with closing(sqlite3.connect(f"{simulator.as_uri()}?mode=ro", uri=True)) as probe:
            actual_identity = probe.execute(
                "SELECT source_id,stream_id,counter_epoch FROM simulator_identity WHERE singleton=1"
            ).fetchone()
        if actual_identity != tuple(
            settings[k] for k in ("source_id", "stream_id", "counter_epoch")
        ):
            raise ValueError("Settings simulator identity differs from stopped snapshot")
        with closing(sqlite3.connect(f"{gateway.as_uri()}?mode=ro", uri=True)) as probe:
            if (
                probe.execute(
                    "SELECT gateway_id FROM gateway_identity WHERE singleton=1"
                ).fetchone()[0]
                != settings["gateway_id"]
            ):
                raise ValueError("Settings gateway identity differs from stopped snapshot")
        binding = db.execute(
            sa.text(
                "SELECT gateway_id,line_id FROM source_streams "
                "WHERE source_id=:source AND stream_id=:stream"
            ),
            {"source": settings["source_id"], "stream": settings["stream_id"]},
        ).first()
        if binding != (settings["gateway_id"], settings["line_id"]):
            raise ValueError("Settings source binding differs from locked database")
        if (
            db.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            != settings["schema_version"]
        ):
            raise ValueError("Settings schema revision differs from locked database")
        target = _new_directory(destination)
        try:
            (target / "postgres.dump").write_bytes(
                run_pg("pg_dump", ["--format=custom", "--no-owner", "--no-acl"], url)
            )
            for name, source in [("simulator.sqlite3", simulator), ("gateway.sqlite3", gateway)]:
                with (
                    closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as original,
                    closing(sqlite3.connect(target / name)) as copy,
                ):
                    original.backup(copy)
                    copy.execute("PRAGMA journal_mode=DELETE")
            (target / "settings.json").write_text(
                _json({"backup_set_id": backup_set_id, "settings": settings})
            )
            values = {
                "postgres": _pg_rows(db),
                "simulator": _sqlite_rows(target / "simulator.sqlite3", SIM_TABLES),
                "gateway": _sqlite_rows(target / "gateway.sqlite3", GATEWAY_TABLES),
            }
            manifest = {
                "format": 1,
                "backup_set_id": backup_set_id,
                "created_at": datetime.now(UTC).isoformat(),
                "source_database": url.database,
                "checksums": {name: _digest(target / name) for name in MEMBERS},
                "inventory": values,
                "service_credentials": "excluded-reinject-new-key",
                "writer_barrier": "exclusive-owner-locks-and-postgresql-share-locks",
            }
            (target / "manifest.json").write_text(_json(manifest))
            for member in target.iterdir():
                member.chmod(0o600)
            return manifest
        except BaseException:
            shutil.rmtree(target)
            raise


def validate_backup_set(directory):
    root = Path(directory).absolute()
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError("Symlink backup directory")
    if {p.name for p in root.iterdir()} != {*MEMBERS, "manifest.json"}:
        raise ValueError("Unexpected or missing backup members")
    manifest = json.loads(_regular(root / "manifest.json").read_text())
    if manifest.get("format") != 1 or set(manifest.get("checksums", {})) != set(MEMBERS):
        raise ValueError("Unsupported backup manifest")
    for name in MEMBERS:
        if _digest(_regular(root / name)) != manifest["checksums"][name]:
            raise ValueError(f"Checksum mismatch: {name}")
    config = json.loads((root / "settings.json").read_text())
    if (
        config["backup_set_id"] != manifest["backup_set_id"]
        or set(config["settings"]) != SETTING_KEYS
    ):
        raise ValueError("Mixed backup set or invalid settings")
    load_controller_settings(config["settings"])
    return manifest


def restore_backup_set(*, directory, admin_engine, database_name, destination, run_pg=pg_tool):
    """Create a fresh local DB; no overwrite/drop/clean behavior, even on failure.

    A failed restore is deliberately left isolated for diagnosis. Never starts services.
    """
    manifest = validate_backup_set(directory)
    url = local_url(admin_engine.url)
    if (
        not re.fullmatch(r"hmi_restore_[a-z0-9_]{8,48}", database_name)
        or database_name == manifest["source_database"]
    ):
        raise ValueError("Restore requires a distinct explicit hmi_restore_<unique> database")
    target = _new_directory(destination)
    with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as db:
        db.execute(sa.text(f'CREATE DATABASE "{database_name}"'))
        from apps.api.recovery import MARKER_PREFIX

        marker = MARKER_PREFIX + manifest["backup_set_id"]
        # SQL literal rendered by the dialect; never interpolate archive strings into SQL.
        literal = sa.String().literal_processor(db.dialect)(marker)
        db.execute(sa.text(f'COMMENT ON DATABASE "{database_name}" IS {literal}'))
    restored_url = url.set(database=database_name)
    run_pg(
        "pg_restore",
        ["--no-owner", "--no-acl", "--exit-on-error", "--dbname", database_name],
        restored_url,
        _regular(Path(directory) / "postgres.dump").read_bytes(),
    )
    for name in ("simulator.sqlite3", "gateway.sqlite3", "settings.json"):
        shutil.copyfile(_regular(Path(directory) / name), target / name)
        (target / name).chmod(0o600)
    restored = sa.create_engine(restored_url, hide_parameters=True)
    try:
        actual = inventory(restored, target / "simulator.sqlite3", target / "gateway.sqlite3")
        if actual != manifest["inventory"]:
            raise ValueError("Restored inventory differs from backup manifest")
        with restored.begin() as db:
            db.execute(
                sa.text("UPDATE auth_sessions SET revoked_at=now() WHERE revoked_at IS NULL")
            )
            db.execute(sa.text("DELETE FROM auth_login_challenges"))
            db.execute(sa.text("UPDATE users SET auth_version=auth_version+1"))
        with closing(sqlite3.connect(target / "simulator.sqlite3")) as db, db:
            db.execute("UPDATE simulator_identity SET recovery_required=1 WHERE singleton=1")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "console_sessions" in tables:
                db.execute(
                    "UPDATE console_sessions SET revoked_at=? WHERE revoked_at IS NULL",
                    (datetime.now(UTC).isoformat(),),
                )
                db.execute("DELETE FROM console_login_challenges")
                db.execute("UPDATE console_users SET auth_version=auth_version+1")
        descriptor = os.open(target / "service-token", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(secrets.token_urlsafe(32))
        result = {
            "backup_set_id": manifest["backup_set_id"],
            "inventory_verified": True,
            "sessions_revoked": True,
            "service_credentials": "excluded-reinject-new-key",
            "automatic_start": False,
            "control_enabled": False,
            "database_name": database_name,
        }
        (target / "restore.json").write_text(_json(result))
        (target / "restore.json").chmod(0o600)
        return result
    finally:
        restored.dispose()


def recovery_control_gate(app):
    """Wrap an explicitly restored API during validation; operator recreates it to resume.

    Collection, existing batch ACKs and reads remain available. Every command mutation
    and dispatch claim is denied before it can lease/submit a restored pending START.
    """
    from starlette.responses import JSONResponse

    class RecoveryGate:
        async def __call__(self, scope, receive, send):
            path = scope.get("path", "")
            if (
                scope["type"] == "http"
                and scope.get("method") not in {"GET", "HEAD", "OPTIONS"}
                and (
                    path.startswith(
                        (
                            "/api/v1/commands",
                            "/internal/v1/commands",
                            "/api/v1/work-orders",
                            "/api/v1/work-order-actions",
                        )
                    )
                )
            ):
                await JSONResponse({"detail": "RECOVERY_REQUIRED"}, status_code=423)(
                    scope, receive, send
                )
                return
            await app(scope, receive, send)

    return RecoveryGate()


def resume_restored_set(*, engine, directory, restored_directory, operator_id, acknowledged):
    """Owner-only explicit release after unchanged evidence and safely stopped restored PLC.

    Does not recover PLC, start processes, deliver commands, or issue START.
    """
    local_url(engine.url)
    manifest = validate_backup_set(directory)
    if not acknowledged or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}", operator_id):
        raise ValueError(
            "Explicit operator identity and verified recovery acknowledgement required"
        )
    root = Path(restored_directory).absolute()
    receipt = json.loads(_regular(root / "restore.json").read_text())
    if (
        receipt["backup_set_id"] != manifest["backup_set_id"]
        or receipt["database_name"] != engine.url.database
    ):
        raise ValueError("Restore receipt/database differs from selected backup set")
    simulator, gateway = _regular(root / "simulator.sqlite3"), _regular(root / "gateway.sqlite3")
    with _stopped((simulator, gateway)), engine.begin() as db:
        from apps.api.database import SCHEMA_LOCK_ID
        from apps.api.recovery import MARKER_PREFIX

        db.execute(sa.text("SET LOCAL lock_timeout='5s'"))
        db.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCHEMA_LOCK_ID})
        marker = db.execute(
            sa.text(
                "SELECT pg_catalog.shobj_description(oid,'pg_database') "
                "FROM pg_catalog.pg_database "
                "WHERE datname=pg_catalog.current_database()"
            )
        ).scalar_one()
        if marker != MARKER_PREFIX + manifest["backup_set_id"]:
            raise ValueError("Selected database is not in the expected recovery restriction")
        if _pg_rows(db) != manifest["inventory"]["postgres"]:
            raise ValueError("Restored database evidence changed; investigate before resume")
        with closing(sqlite3.connect(f"{simulator.as_uri()}?mode=ro", uri=True)) as probe:
            state = json.loads(
                probe.execute(
                    "SELECT snapshot_json FROM runtime_state WHERE singleton=1"
                ).fetchone()[0]
            )
            blocked = probe.execute(
                "SELECT recovery_required FROM simulator_identity WHERE singleton=1"
            ).fetchone()[0]
            if not blocked or state["state"] != "RECOVERY_REQUIRED" or state["run_output"]:
                raise ValueError(
                    "Stopped restored PLC must remain RECOVERY_REQUIRED with outputs OFF"
                )
        actual_sim = _sqlite_rows(simulator, SIM_TABLES)
        for table in (
            "executions",
            "command_journal",
            "runtime_runs",
            "runtime_settings",
        ):
            if actual_sim[table] != manifest["inventory"]["simulator"][table]:
                raise ValueError("Restored simulator evidence changed; investigate before resume")
        old_events = set(manifest["inventory"]["simulator"]["source_events"])
        new_events = set(actual_sim["source_events"])
        if not old_events <= new_events:
            raise ValueError("Original source events changed during recovery")
        for row in new_events - old_events:
            body = json.loads(json.loads(row)[-1])
            if (
                body.get("kind") != "STATE_CHANGED"
                or body.get("payload", {}).get("state") != "RECOVERY_REQUIRED"
            ):
                raise ValueError("Unexpected simulator event during recovery")
        actual_gateway = _sqlite_rows(gateway, GATEWAY_TABLES)
        original_gateway = manifest["inventory"]["gateway"]
        if actual_gateway["gateway_identity"] != original_gateway["gateway_identity"]:
            raise ValueError("Restored gateway identity changed")
        original_progress = {
            tuple(json.loads(row)[:2]): json.loads(row)
            for row in original_gateway["source_progress"]
        }
        current_progress = {
            tuple(json.loads(row)[:2]): json.loads(row) for row in actual_gateway["source_progress"]
        }
        if original_progress.keys() != current_progress.keys():
            raise ValueError("Restored gateway source/stream identity changed")
        from contracts.transport import StreamProgress

        for scope, row in current_progress.items():
            if row[2] != original_progress[scope][2]:
                raise ValueError("Gateway source read cursor changed during validation")
            progress = StreamProgress.model_validate_json(row[3])
            pg = db.execute(
                sa.text(
                    "SELECT verified_through_seq,latest_ingested_seq FROM stream_progress "
                    "WHERE source_id=:source AND stream_id=:stream"
                ),
                {"source": scope[0], "stream": scope[1]},
            ).one()
            if (
                progress.unresolved_gaps
                or progress.verified_through_seq != int(pg[0])
                or progress.latest_ingested_seq != int(pg[1])
            ):
                raise ValueError("Gateway acknowledged continuity differs from database")
        if (
            actual_gateway["outbound_batches"]
            or actual_gateway["missing_ranges"]
            or manifest["inventory"]["postgres"]["stream_gaps"]
        ):
            raise ValueError("Drain and reconcile pending batches/gaps before resume")
        name = db.dialect.identifier_preparer.quote(engine.url.database)
        verified = f"HMI_MES_RECOVERY_VERIFIED:{manifest['backup_set_id']}:{operator_id}"
        literal = sa.String().literal_processor(db.dialect)(verified)
        db.execute(sa.text(f"COMMENT ON DATABASE {name} IS {literal}"))
    return {
        "backup_set_id": manifest["backup_set_id"],
        "operator_id": operator_id,
        "control_restriction_released": True,
        "automatic_start": False,
        "plc_recovery_required": True,
    }
