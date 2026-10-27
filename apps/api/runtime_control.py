"""Operator on/off request for the plant runtime host, and the host agent's report.

The state lives in the operational table ``runtime_ops.control`` (see
``deploy/runtime-control.sql``), outside the versioned product schema. Without that
table the feature is unavailable and nothing else changes.

A start request is bounded: it expires at ``run_until`` and the agent then stops the
runtime by itself. The agent only reports what it observes; it never decides on its
own to start equipment.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.exc import ProgrammingError

RUN_LIMIT = timedelta(minutes=60)
AGENT_STALE = timedelta(seconds=30)
AGENT_STATES = frozenset({"OFF", "STARTING", "RUNNING", "STOPPING", "ERROR"})
DETAIL_LIMIT = 200


class RuntimeControlUnavailable(RuntimeError):
    """The operational control table is not installed in this database."""


def _row(db: Connection, *, lock: bool = False):
    try:
        return (
            db.execute(
                sa.text(
                    "SELECT desired,requested_by,requested_at,run_until,reported_state,"
                    "reported_detail,reported_at FROM runtime_ops.control WHERE singleton"
                    + (" FOR UPDATE" if lock else "")
                )
            )
            .mappings()
            .one()
        )
    except ProgrammingError as error:
        raise RuntimeControlUnavailable from error


def _effective(row, now: datetime) -> str:
    return "ON" if row["desired"] == "ON" and row["run_until"] and now < row["run_until"] else "OFF"


def _iso(value):
    return value.isoformat() if value is not None else None


def view(db: Connection, now: datetime) -> dict:
    row = _row(db)
    online = row["reported_at"] is not None and now - row["reported_at"] < AGENT_STALE
    return {
        "desired": _effective(row, now),
        "run_until": _iso(row["run_until"]) if _effective(row, now) == "ON" else None,
        "requested_by": row["requested_by"],
        "requested_at": _iso(row["requested_at"]),
        "agent_online": online,
        # A silent agent's last report is history, not the current state.
        "reported_state": row["reported_state"] if online else "UNKNOWN",
        "reported_detail": row["reported_detail"] if online else None,
        "reported_at": _iso(row["reported_at"]),
    }


def request(db: Connection, action: str, *, actor: str, now: datetime) -> dict:
    if action not in {"start", "stop"}:
        raise ValueError("unknown runtime action")
    _row(db, lock=True)
    db.execute(
        sa.text(
            "UPDATE runtime_ops.control SET desired=:desired,requested_by=:actor,"
            "requested_at=:now,run_until=:until WHERE singleton"
        ),
        {
            "desired": "ON" if action == "start" else "OFF",
            "actor": actor,
            "now": now,
            "until": now + RUN_LIMIT if action == "start" else None,
        },
    )
    return view(db, now)


def agent_exchange(db: Connection, *, state: str, detail: str | None, now: datetime) -> dict:
    if state not in AGENT_STATES:
        raise ValueError("unknown agent state")
    if detail is not None and (not isinstance(detail, str) or len(detail) > DETAIL_LIMIT):
        raise ValueError("invalid agent detail")
    row = _row(db, lock=True)
    db.execute(
        sa.text(
            "UPDATE runtime_ops.control SET reported_state=:state,reported_detail=:detail,"
            "reported_at=:now WHERE singleton"
        ),
        {"state": state, "detail": detail, "now": now},
    )
    desired = _effective(row, now)
    return {"desired": desired, "run_until": _iso(row["run_until"]) if desired == "ON" else None}
