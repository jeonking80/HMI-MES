"""PostgreSQL connection and migration configuration; never initialize on import."""

from __future__ import annotations

import os
from pathlib import Path

from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine
from sqlalchemy.engine import URL, make_url

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
INITIAL_REVISION = "0001_reference_data"
SUPPORTED_AUTH_REVISIONS = frozenset(
    {
        "0003_user_auth",
        "0004_commands",
        "0005_command_dispatch",
        "0006_settlement",
        "0007_execution_plan",
        "0008_work_orders",
        "0009_work_order_binding",
        "0010_alarm_store",
        "0011_alarm_capture_context",
        "0012_alarm_line_fault",
        "0013_work_order_cancel",
        "0014_mixed_alarm_runtime",
        "0015_analog_history",
        "0016_downtime_reasons",
        "0017_guest_viewer",
        "0018_clock_evidence",
    }
)
SUPPORTED_INGESTION_REVISIONS = SUPPORTED_AUTH_REVISIONS | {"0002_ingestion"}
SUPPORTED_REFERENCE_REVISIONS = SUPPORTED_INGESTION_REVISIONS | {INITIAL_REVISION}
SUPPORTED_COMMAND_REVISIONS = frozenset(
    {
        "0005_command_dispatch",
        "0006_settlement",
        "0007_execution_plan",
        "0008_work_orders",
        "0009_work_order_binding",
        "0010_alarm_store",
        "0011_alarm_capture_context",
        "0012_alarm_line_fault",
        "0013_work_order_cancel",
        "0014_mixed_alarm_runtime",
        "0015_analog_history",
        "0016_downtime_reasons",
        "0017_guest_viewer",
        "0018_clock_evidence",
    }
)
SUPPORTED_SETTLEMENT_REVISIONS = frozenset(
    {
        "0006_settlement",
        "0007_execution_plan",
        "0008_work_orders",
        "0009_work_order_binding",
        "0010_alarm_store",
        "0011_alarm_capture_context",
        "0012_alarm_line_fault",
        "0013_work_order_cancel",
        "0014_mixed_alarm_runtime",
        "0015_analog_history",
        "0016_downtime_reasons",
        "0017_guest_viewer",
        "0018_clock_evidence",
    }
)
SUPPORTED_WORKFLOW_REVISIONS = frozenset(
    {
        "0009_work_order_binding",
        "0010_alarm_store",
        "0011_alarm_capture_context",
        "0012_alarm_line_fault",
        "0013_work_order_cancel",
        "0014_mixed_alarm_runtime",
        "0015_analog_history",
        "0016_downtime_reasons",
        "0017_guest_viewer",
        "0018_clock_evidence",
    }
)
SUPPORTED_ALARM_REVISIONS = frozenset(
    {
        "0010_alarm_store",
        "0011_alarm_capture_context",
        "0012_alarm_line_fault",
        "0013_work_order_cancel",
        "0014_mixed_alarm_runtime",
        "0015_analog_history",
        "0016_downtime_reasons",
        "0017_guest_viewer",
        "0018_clock_evidence",
    }
)
SUPPORTED_ALARM_CAPTURE_REVISIONS = frozenset(
    {
        "0011_alarm_capture_context",
        "0012_alarm_line_fault",
        "0013_work_order_cancel",
        "0014_mixed_alarm_runtime",
        "0015_analog_history",
        "0016_downtime_reasons",
        "0017_guest_viewer",
        "0018_clock_evidence",
    }
)
SUPPORTED_REGISTRATION_REVISIONS = SUPPORTED_WORKFLOW_REVISIONS | {"0008_work_orders"}
SUPPORTED_CANCELLATION_REVISIONS = frozenset(
    {
        "0013_work_order_cancel",
        "0014_mixed_alarm_runtime",
        "0015_analog_history",
        "0016_downtime_reasons",
        "0017_guest_viewer",
        "0018_clock_evidence",
    }
)
# Every new mixed-coordinator mutation requires this explicit feature barrier.
SUPPORTED_MIXED_ALARM_REVISIONS = frozenset(
    {
        "0014_mixed_alarm_runtime",
        "0015_analog_history",
        "0016_downtime_reasons",
        "0017_guest_viewer",
        "0018_clock_evidence",
    }
)
SCHEMA_LOCK_ID = 843001


def database_url_from_env() -> URL:
    """Read a required DSN without including its contents in validation errors."""
    value = os.environ.get("HMI_DATABASE_DSN")
    if not value:
        raise ValueError("HMI_DATABASE_DSN is required; no default database is selected")
    try:
        url = make_url(value)
    except Exception:
        raise ValueError("HMI_DATABASE_DSN must be a valid PostgreSQL URL") from None
    if url.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise ValueError("HMI_DATABASE_DSN must use PostgreSQL with the psycopg driver")
    if not url.database or not url.username or not (url.host or url.query.get("host")):
        raise ValueError("HMI_DATABASE_DSN must explicitly select host, user, and database")
    return url.set(drivername="postgresql+psycopg")


def create_database_engine(url: URL | None = None) -> Engine:
    selected = database_url_from_env() if url is None else url
    if selected.drivername != "postgresql+psycopg":
        raise ValueError("Only PostgreSQL with the psycopg driver is supported")
    return create_engine(selected, echo=False, hide_parameters=True, pool_pre_ping=True)


def migration_config(connection: Connection | None = None) -> Config:
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    script_path = str(REPOSITORY_ROOT / "apps/api/migrations")
    config.set_main_option("script_location", script_path.replace("%", "%%"))
    if connection is not None:
        config.attributes["connection"] = connection
    return config
