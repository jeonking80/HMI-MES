"""Report calculations from the PLC's own records.

Line state comes from the PLC's STATE_CHANGED events and quantities from its
production events. A time with no capture received for more than GAP_SECONDS
(plant off, link down) is UNKNOWN rather than any state.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text

from apps.api.minute_aggregation import seoul_day_bounds
from contracts.tags.validate import load_contract

STATES = load_contract()["enums"]["EquipmentState"]
REASONS = ("MATERIAL_WAIT", "EQUIPMENT", "CHANGEOVER", "PLANNED", "UNCLASSIFIED")
# Captures arrive about once a second; a longer silence means nothing was observed.
GAP_SECONDS = 10
HOLD_SECONDS = 3


class ReportNotFound(LookupError):
    pass


def iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def evaluate_segments(segments):
    totals = dict(running=0.0, stopped=0.0, unknown=0.0, total=0.0)
    stopped = {}
    for segment in segments:
        seconds = (timestamp(segment["to"]) - timestamp(segment["from"])).total_seconds()
        totals[segment["class"].lower()] += seconds
        totals["total"] += seconds
        if segment["class"] == "STOPPED":
            stopped[segment["state"]] = stopped.get(segment["state"], 0) + seconds
    return totals, stopped


def _scope_sql(alias, scope, params):
    where = "s.line_id=:line"
    if scope:
        params.update(source=scope["source_id"], stream=scope["stream_id"])
        where += f" AND {alias}.source_id=:source AND {alias}.stream_id=:stream"
    return where


def _coverage(db, line_id, start, end, scope):
    """Intervals with captures; a silence over GAP_SECONDS splits them."""
    params = {"line": line_id, "start": start - timedelta(seconds=HOLD_SECONDS), "end": end}
    where = _scope_sql("r", scope, params)
    rows = db.execute(
        text(
            "WITH r AS (SELECT r.captured_at AS at FROM ingest_receipts r "
            "JOIN source_streams s USING(source_id,stream_id) WHERE "
            + where
            # Sample captures carry capture time (schema 1.2); event-only batches observe
            # nothing about the line between events.
            + " AND r.clock_epoch IS NOT NULL AND r.captured_at>=:start AND r.captured_at<:end), "
            "o AS (SELECT at, CASE WHEN lag(at) OVER (ORDER BY at) IS NULL "
            f"OR at-lag(at) OVER (ORDER BY at)>interval '{GAP_SECONDS} seconds' "
            "THEN 1 ELSE 0 END AS starts FROM r), "
            "g AS (SELECT at, sum(starts) OVER (ORDER BY at) AS island FROM o) "
            "SELECT min(at) AS first, max(at) AS last FROM g GROUP BY island ORDER BY 1"
        ),
        params,
    ).all()
    hold = timedelta(seconds=HOLD_SECONDS)
    return [
        (max(start, first), min(end, last + hold)) for first, last in rows if last + hold > start
    ]


def _state_changes(db, line_id, start, end, scope):
    """The Line state in force at ``start`` and every PLC state change until ``end``."""
    params = {"line": line_id, "start": start, "end": end}
    where = _scope_sql("p", scope, params)
    select = (
        "SELECT (e.body->>'occurred_at')::timestamptz AS at,e.body->'payload'->>'state' AS state,"
        "e.execution_id,p.source_id,p.stream_id FROM equipment_state_events e "
        "JOIN production_executions p USING(execution_id) "
        "JOIN source_streams s ON (s.source_id,s.stream_id)=(p.source_id,p.stream_id) "
        "WHERE " + where + " AND e.kind='STATE_CHANGED' AND e.body->>'equipment_id'='Line' "
    )
    order = " ORDER BY (e.body->>'occurred_at')::timestamptz,(e.body->>'seq')::numeric"
    before = (
        db.execute(
            text(
                select
                + "AND (e.body->>'occurred_at')::timestamptz<:start"
                + order
                + " DESC LIMIT 1"
            ),
            params,
        )
        .mappings()
        .first()
    )
    during = (
        db.execute(
            text(
                select
                + "AND (e.body->>'occurred_at')::timestamptz>=:start "
                + "AND (e.body->>'occurred_at')::timestamptz<:end"
                + order
            ),
            params,
        )
        .mappings()
        .all()
    )
    return ([dict(before)] if before else []) + [dict(row) for row in during]


def state_timeline(db, line_id, start, end, *, scope=None):
    """Segments of the Line state between ``start`` and ``end`` from PLC events."""
    end = max(start, end)
    covered = _coverage(db, line_id, start, end, scope)
    changes = _state_changes(db, line_id, start, end, scope)
    points = {start, end}
    points.update(c["at"] for c in changes if start < c["at"] < end)
    for left, right in covered:
        points.update((left, right))
    points = sorted(point for point in points if start <= point <= end)
    result = []
    index, current = 0, None
    for left, right in zip(points, points[1:], strict=False):
        while index < len(changes) and changes[index]["at"] <= left:
            current = changes[index]
            index += 1
        seen = any(a <= left and right <= b for a, b in covered)
        segment = {"from": iso(left), "to": iso(right), "fault_code": None, "stop_reason": None}
        if not seen:
            segment.update({"class": "UNKNOWN", "state": None, "unknown_reason": "NO_DATA"})
        else:
            state = current["state"] if current else "STOPPED"
            segment.update(
                {"class": "RUNNING" if state == "RUNNING" else "STOPPED", "state": state}
            )
            if current:
                segment.update({k: current[k] for k in ("execution_id", "source_id", "stream_id")})
        if result and {k: v for k, v in result[-1].items() if k not in {"from", "to"}} == {
            k: v for k, v in segment.items() if k not in {"from", "to"}
        }:
            result[-1]["to"] = segment["to"]
        else:
            result.append(segment)
    return result


def _execution(db, execution_id, line_id, scope=None):
    params = {"id": execution_id, "line": line_id}
    where = "e.execution_id=:id AND s.line_id=:line"
    if scope:
        params.update(source=scope["source_id"], stream=scope["stream_id"])
        where += " AND e.source_id=:source AND e.stream_id=:stream"
    row = (
        db.execute(
            text(
                """SELECT e.*,p.target_quantity,w.item_id,
      r.status AS settlement_status,r.final_summary,
      t.good_quantity,t.reject_quantity,t.scrap_quantity
      FROM production_executions e JOIN source_streams s USING(source_id,stream_id)
      LEFT JOIN execution_plans p USING(execution_id)
      LEFT JOIN work_order_execution_links l USING(execution_id)
      LEFT JOIN work_orders w ON w.id=l.work_order_id
      LEFT JOIN run_reconciliations r USING(execution_id)
      LEFT JOIN execution_totals t USING(execution_id) WHERE """
                + where
            ),
            params,
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ReportNotFound("execution not found in exact scope")
    return row


def execution_state_bounds(db, execution_id):
    events = (
        db.execute(
            text("""SELECT e.body FROM equipment_state_events e
       WHERE execution_id=:id AND e.body->>'equipment_id'='Line' AND kind='STATE_CHANGED'
       ORDER BY (body->>'occurred_at')::timestamptz"""),
            {"id": execution_id},
        )
        .scalars()
        .all()
    )
    started = next(
        (timestamp(e["occurred_at"]) for e in events if e["payload"]["state"] == "RUNNING"), None
    )
    ended = next(
        (
            timestamp(e["occurred_at"])
            for e in events
            if e["payload"]["state"] == "STOPPED" and e["payload"]["previous_state"] == "STOPPING"
        ),
        None,
    )
    return started, ended


def _execution_window(row, ended, now):
    return row["registered_at"], min(ended or now, now)


def read_execution_timeline(
    db, execution_id, line_id, source_id, stream_id, *, now, annotate=None, prefetched=None
):
    """``prefetched`` is ``(execution row, ended)`` already read in this transaction."""
    scope = dict(line_id=line_id, source_id=source_id, stream_id=stream_id)
    if prefetched is None:
        row = _execution(db, execution_id, line_id, scope)
        _, ended = execution_state_bounds(db, execution_id)
    else:
        row, ended = prefetched
    start, end = _execution_window(row, ended, now)
    segments = state_timeline(db, line_id, start, end, scope=scope)
    if annotate:
        segments = annotate(db, segments, scope, now=now)
    durations, _ = evaluate_segments(segments)
    return {
        "execution_id": execution_id,
        "from": iso(start),
        "to": iso(max(start, end)),
        "segments": segments,
        "durations_seconds": durations,
        "check_ok": abs(durations["total"] - (max(start, end) - start).total_seconds()) < 1e-6,
    }


def read_execution_reports(db, line_id, start, end, *, now, limit=50, cursor=None, annotate=None):
    params = dict(line=line_id, start=start, end=end, limit=limit + 1)
    where = "s.line_id=:line AND e.registered_at>=:start AND e.registered_at<:end"
    if cursor:
        # Cursor identity is constrained to this exact query, avoiding cross-line reuse.
        anchor = db.execute(
            text(
                "SELECT e.registered_at FROM production_executions e "
                "JOIN source_streams s USING(source_id,stream_id) WHERE "
                + where
                + " AND e.execution_id=:cursor"
            ),
            params | {"cursor": cursor},
        ).scalar_one_or_none()
        if anchor is None:
            raise ValueError("cursor outside report scope")
        where += " AND (e.registered_at,e.execution_id)<(:anchor,:cursor)"
        params.update(anchor=anchor, cursor=cursor)
    ids = (
        db.execute(
            text(
                "SELECT e.execution_id FROM production_executions e "
                "JOIN source_streams s USING(source_id,stream_id) WHERE "
                + where
                + " ORDER BY e.registered_at DESC,e.execution_id DESC LIMIT :limit"
            ),
            params,
        )
        .scalars()
        .all()
    )
    items = []
    for execution_id in ids[:limit]:
        row = _execution(db, execution_id, line_id)
        started, ended = execution_state_bounds(db, execution_id)
        timeline = read_execution_timeline(
            db,
            execution_id,
            line_id,
            row["source_id"],
            row["stream_id"],
            now=now,
            annotate=annotate,
            prefetched=(row, ended),
        )
        status = row["settlement_status"] or "PENDING"
        totals = {k: str(int(row[k + "_quantity"] or 0)) for k in ("good", "reject", "scrap")}
        if status == "SETTLED" and row["final_summary"]:
            totals = {k: str(row["final_summary"][k + "_quantity"]) for k in totals}
        reasons = [] if status == "SETTLED" else ["EXECUTION_NOT_SETTLED"]
        if timeline["durations_seconds"]["unknown"]:
            reasons.append("UNKNOWN_TIME")
        good, reject = int(totals["good"]), int(totals["reject"])
        items.append(
            {
                "execution_id": execution_id,
                "source_id": row["source_id"],
                "stream_id": row["stream_id"],
                "work_order_id": row["work_order_id"],
                "item_id": row["item_id"],
                "recipe_version": row["recipe_version"],
                "target_quantity": row["target_quantity"],
                "registered_at": iso(row["registered_at"]),
                "started_at": iso(started),
                "ended_at": iso(ended),
                "totals": totals,
                "yield_ratio": good / (good + reject) if good + reject else None,
                "settlement_status": status,
                "provisional": bool(reasons),
                "provisional_reasons": reasons,
                "durations_seconds": timeline["durations_seconds"],
                "stopped_by_state": evaluate_segments(timeline["segments"])[1],
            }
        )
    return {"items": items, "next_cursor": ids[limit - 1] if len(ids) > limit else None}


def read_daily_report(db, line_id, day: date, *, now, scope=None, annotate=None):
    start, day_end = seoul_day_bounds(day)
    end = max(start, min(day_end, now))
    segments = state_timeline(db, line_id, start, end, scope=scope)
    if annotate:
        segments = annotate(db, segments, {"line_id": line_id, **(scope or {})}, now=now)
    durations, _ = evaluate_segments(segments)
    params = dict(line=line_id, start=start, end=end)
    where = "s.line_id=:line AND p.occurred_at>=:start AND p.occurred_at<:end"
    if scope:
        params.update(source=scope["source_id"], stream=scope["stream_id"])
        where += " AND e.source_id=:source AND e.stream_id=:stream"
    events = (
        db.execute(
            text(
                """SELECT p.*,r.status AS settlement_status FROM production_events p
       JOIN production_executions e USING(execution_id)
       JOIN source_streams s USING(source_id,stream_id)
       LEFT JOIN run_reconciliations r USING(execution_id) WHERE """
                + where
            ),
            params,
        )
        .mappings()
        .all()
    )
    totals = dict(good=0, reject=0, scrap=0)
    hourly = [
        {
            "hour_start": iso(start + timedelta(hours=h)),
            "good": 0,
            "reject": 0,
            "running_seconds": 0.0,
            "unknown_seconds": 0.0,
        }
        for h in range(24)
    ]
    executions = set(e["execution_id"] for e in events)
    for segment in segments:
        if segment.get("execution_id"):
            executions.add(segment["execution_id"])
        for hour in hourly:
            a = timestamp(hour["hour_start"])
            overlap = max(
                0,
                (
                    min(timestamp(segment["to"]), a + timedelta(hours=1))
                    - max(timestamp(segment["from"]), a)
                ).total_seconds(),
            )
            key = {"RUNNING": "running_seconds", "UNKNOWN": "unknown_seconds"}.get(segment["class"])
            if key:
                hour[key] += overlap
    for event in events:
        key = {"PRODUCTION_GOOD": "good", "PRODUCTION_REJECT": "reject", "WIP_SCRAP": "scrap"}[
            event["kind"]
        ]
        totals[key] += int(event["quantity"])
        if key != "scrap":
            hourly[int((event["occurred_at"] - start).total_seconds() // 3600)][key] += int(
                event["quantity"]
            )
    for hour in hourly:
        hour.update(good=str(hour["good"]), reject=str(hour["reject"]))
    reasons = []
    if now < day_end:
        reasons.append("DAY_NOT_FINISHED")
    if durations["unknown"]:
        reasons.append("UNKNOWN_TIME")
    if executions:
        statuses = db.execute(
            text(
                "SELECT execution_id,status FROM run_reconciliations WHERE execution_id=ANY(:ids)"
            ),
            {"ids": list(executions)},
        ).all()
        if set(e for e, s in statuses if s == "SETTLED") != executions:
            reasons.append("EXECUTION_NOT_SETTLED")
    by_reason = dict.fromkeys(REASONS, 0.0)
    for segment in segments:
        if segment["class"] == "STOPPED":
            reason = (segment.get("stop_reason") or {}).get("code", "UNCLASSIFIED")
            by_reason[reason] += (
                timestamp(segment["to"]) - timestamp(segment["from"])
            ).total_seconds()
    alarms = db.execute(
        text(
            "SELECT count(*) FROM alarm_instances i JOIN alarm_channels c USING(channel_id) "
            "WHERE c.line_id=:line AND i.acknowledged_at IS NULL"
        ),
        {"line": line_id},
    ).scalar_one()
    total = durations["total"]
    return {
        "date": day.isoformat(),
        "timezone": "Asia/Seoul",
        "from": iso(start),
        "to": iso(end),
        "durations_seconds": durations,
        "running_ratio": durations["running"] / total if total else 0.0,
        "unknown_ratio": durations["unknown"] / total if total else 0.0,
        "stopped_by_reason": by_reason,
        "totals": {k: str(v) for k, v in totals.items()},
        "hourly": hourly,
        "unacknowledged_alarms": alarms,
        "executions": sorted(executions),
        "provisional": bool(reasons),
        "provisional_reasons": reasons,
        "check_ok": abs(total - (end - start).total_seconds()) < 1e-6,
    }
