"""Passive, scoped dashboard composed from the existing report and snapshot rules."""

from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import select, text

from apps.api.ingestion import ingest_batches, source_streams
from apps.api.models import Line
from apps.api.reports import iso, read_daily_report
from apps.api.snapshot import SnapshotNotFound, read_snapshot

MAX_RESPONSE_BYTES = 64 * 1024


class DashboardUnavailable(RuntimeError):
    pass


def read_dashboard(db, *, line_id: str, source_id: str, stream_id: str, day: date, now: datetime):
    # Intake holds the line exclusively. This shared lock keeps report quantities,
    # current capture and alarm state consistent during this passive projection.
    if (
        db.execute(select(Line.id).where(Line.id == line_id).with_for_update(read=True)).first()
        is None
    ):
        raise SnapshotNotFound("Dashboard line is not registered")
    scope = {"line_id": line_id, "source_id": source_id, "stream_id": stream_id}
    snapshot = read_snapshot(db, **scope, now=now, prefer_complete_capture=True)
    daily = read_daily_report(db, line_id, day, now=now, scope=scope)
    source = (
        db.execute(
            select(source_streams).where(
                source_streams.c.line_id == line_id,
                source_streams.c.source_id == source_id,
                source_streams.c.stream_id == stream_id,
            )
        )
        .mappings()
        .one()
    )
    last_receipt = db.execute(
        select(ingest_batches.c.stored_at)
        .where(
            ingest_batches.c.source_id == source_id,
            ingest_batches.c.stream_id == stream_id,
            ingest_batches.c.gateway_id == source["gateway_id"],
            ingest_batches.c.capture_generation == source["capture_generation"],
        )
        .order_by(ingest_batches.c.stored_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    freshness = "STALE"
    if snapshot.health.state == "NEVER_OBSERVED":
        freshness = "NO_DATA"
    elif snapshot.health.state == "CAPTURE_FRESH" and snapshot.health.heartbeat == "GOOD":
        freshness = "LIVE"
    # Only fresh evidence may name the running execution; stale or absent capture
    # must not keep showing the last observed one as if it were still active.
    active = _active_execution(db, scope, snapshot) if freshness == "LIVE" else None
    alarms = _alarms(db, scope)
    good, reject = int(daily["totals"]["good"]), int(daily["totals"]["reject"])
    quality = good / (good + reject) if good + reject else None
    durations = daily["durations_seconds"]
    result = {
        "date": day.isoformat(),
        "timezone": "Asia/Seoul",
        "generated_at": iso(now),
        "freshness": {
            "last_receipt_at": iso(last_receipt) if last_receipt else None,
            "status": freshness,
        },
        "production": {
            **daily["totals"],
            "yield_ratio": quality,
            "basis": "RECEIVED_EVENTS",
            "provisional": daily["provisional"],
        },
        "active_execution": active,
        "time": {
            **{
                name + "_seconds": durations[name]
                for name in ("running", "stopped", "unknown", "total")
            },
            "running_ratio": daily["running_ratio"],
            "unknown_ratio": daily["unknown_ratio"],
        },
        "oee_partial": {
            "availability": daily["running_ratio"],
            "quality": quality,
            "performance": None,
            "note": "PERFORMANCE_NOT_DEFINED",
        },
        "alarms": alarms,
        "hourly": daily["hourly"],
    }
    if (
        len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        > MAX_RESPONSE_BYTES
    ):
        raise DashboardUnavailable("Dashboard projection exceeds response bound")
    return result


def _active_execution(db, scope, snapshot):
    execution_id = snapshot.screen.view.execution_id
    if execution_id is None:
        return None
    row = (
        db.execute(
            text("""
        SELECT e.execution_id,e.work_order_id,w.item_id,p.target_quantity,t.good_quantity,
               r.status AS settlement_status
        FROM production_executions e
        JOIN source_streams s ON (s.source_id,s.stream_id)=(e.source_id,e.stream_id)
        LEFT JOIN work_order_execution_links l ON l.execution_id=e.execution_id
          AND l.work_order_id=e.work_order_id AND l.line_id=s.line_id
          AND l.source_id=e.source_id AND l.stream_id=e.stream_id
        LEFT JOIN work_orders w ON w.id=l.work_order_id AND w.line_id=l.line_id
        LEFT JOIN execution_plans p ON p.execution_id=e.execution_id
        LEFT JOIN execution_totals t ON t.execution_id=e.execution_id
        LEFT JOIN run_reconciliations r ON r.execution_id=e.execution_id
        WHERE s.line_id=:line_id AND e.source_id=:source_id AND e.stream_id=:stream_id
          AND e.execution_id=:execution_id
    """),
            {**scope, "execution_id": execution_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    quantity = int(row["good_quantity"]) if row["good_quantity"] is not None else None
    target = int(row["target_quantity"]) if row["target_quantity"] is not None else None
    return {
        "execution_id": row["execution_id"],
        "work_order_id": row["work_order_id"],
        "item_id": row["item_id"],
        "target_quantity": target,
        "good": str(quantity) if quantity is not None else None,
        "progress_ratio": quantity / target if quantity is not None and target else None,
        "line_state": str(snapshot.screen.view.state),
        "settlement_status": row["settlement_status"] or "PENDING",
    }


def _alarms(db, scope):
    rows = (
        db.execute(
            text("""
        SELECT i.payload->'rule'->>'severity' AS severity,
               count(*) FILTER (WHERE i.returned_at IS NULL) AS active,
               count(*) FILTER (WHERE i.acknowledged_at IS NULL) AS unacknowledged
        FROM alarm_instances i JOIN alarm_channels c USING(channel_id)
        WHERE c.line_id=:line_id AND c.source_id=:source_id AND c.stream_id=:stream_id
        GROUP BY i.payload->'rule'->>'severity'
    """),
            scope,
        )
        .mappings()
        .all()
    )
    return {
        "active_total": sum(row["active"] for row in rows),
        "active_by_severity": {row["severity"]: row["active"] for row in rows if row["active"]},
        "unacknowledged": sum(row["unacknowledged"] for row in rows),
    }
