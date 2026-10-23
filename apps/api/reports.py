"""Passive report calculations over original scoped capture and execution evidence.

Coverage is bounded by the existing three-second freshness threshold. Neither an
old state event nor a late receipt establishes continuous communication.
"""

from __future__ import annotations

import heapq
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text

from apps.api.minute_aggregation import seoul_day_bounds
from contracts.tags.validate import load_contract

STATES = load_contract()["enums"]["EquipmentState"]
REASONS = ("MATERIAL_WAIT", "EQUIPMENT", "CHANGEOVER", "PLANNED", "UNCLASSIFIED")
FRESHNESS_SECONDS = 3


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


def build_timeline(start, end, evidence):
    """Partition coverage; conflicting owners/states remain UNKNOWN, never double count.

    Evidence entries contain from/to, class/state/fault, reason and provenance.
    Adjacent pieces merge only when their entire evidence identity agrees.
    """
    end = max(start, end)
    boundaries = {start, end}
    relevant = [e for e in evidence if e["from"] < end and e["to"] > start]
    for row in relevant:
        boundaries.update((max(start, row["from"]), min(end, row["to"])))
    points, result = sorted(boundaries), []
    additions = sorted(enumerate(relevant), key=lambda pair: pair[1]["from"])
    pointer = 0
    heaps = {}
    gap_rows = []
    for left, right in zip(points, points[1:], strict=False):
        while pointer < len(additions) and additions[pointer][1]["from"] <= left:
            index, entry = additions[pointer]
            if entry.get("sequence_gap"):
                gap_rows.append(entry)
            else:
                key = (entry.get("source_id"), entry.get("stream_id"))
                observed = entry.get("observed_at", entry["from"]).timestamp()
                generation, sequence = entry.get("capture_order", (0, observed))
                heapq.heappush(heaps.setdefault(key, []), (-generation, -sequence, -index, index))
            pointer += 1
        covering = []
        for heap in heaps.values():
            while heap and relevant[heap[0][3]]["to"] <= left:
                heapq.heappop(heap)
            if heap:
                covering.append(relevant[heap[0][3]])
        gap_rows = [entry for entry in gap_rows if entry["to"] > left]
        covering += gap_rows
        known = [e for e in covering if e["class"] != "UNKNOWN"]
        unknown = [e for e in covering if e["class"] == "UNKNOWN"]
        identities = {
            (
                e.get("source_id"),
                e.get("stream_id"),
                e.get("execution_id"),
                e["state"],
                e.get("fault_code"),
            )
            for e in known
        }
        chosen = None
        if unknown:
            chosen = next(
                (e for e in unknown if e.get("unknown_reason") == "COMMUNICATION_LOST"), unknown[0]
            )
        elif len(identities) == 1:
            chosen = known[-1]
        segment = {
            "from": iso(left),
            "to": iso(right),
            "class": "UNKNOWN",
            "state": None,
            "fault_code": None,
            "stop_reason": None,
            "unknown_reason": "NO_EVIDENCE",
        }
        if not chosen and covering:
            segment["evidence_scopes"] = [
                {key: entry.get(key) for key in ("source_id", "stream_id", "execution_id")}
                for entry in covering
            ]
        if chosen:
            segment.update(
                {
                    k: v
                    for k, v in chosen.items()
                    if k not in {"from", "to", "sequence_gap", "observed_at", "capture_order"}
                }
            )
        if segment["class"] != "UNKNOWN":
            segment.pop("unknown_reason", None)
        if result and {k: v for k, v in result[-1].items() if k not in {"from", "to"}} == {
            k: v for k, v in segment.items() if k not in {"from", "to"}
        }:
            result[-1]["to"] = segment["to"]
        else:
            result.append(segment)
    return result


def _clock_known(row):
    body = row["body"]
    raw = body.get("capture_time") or {}
    for context in row["contexts"] or []:
        assessment = (context or {}).get("clock_assessment") or {}
        if (
            assessment.get("batch_sha256") == row["content_sha256"]
            and assessment.get("gateway_id") == row["gateway_id"]
            and assessment.get("clock_epoch") == raw.get("clock_epoch")
            and -2
            <= assessment.get("offset_lower_seconds", -999)
            <= assessment.get("offset_upper_seconds", 999)
            <= 2
        ):
            return True
    return False


def _clock_reversal_cutoffs(db, line_id, scope):
    # Original ingest_batches are retained independently of analog raw retention.
    # Scan sequence history before clipping a report day: a later timestamp or
    # receipt cannot repair a clock epoch once its chronology has reversed.
    params = {"line": line_id}
    where = "s.line_id=:line"
    if scope:
        params.update(source=scope["source_id"], stream=scope["stream_id"])
        where += " AND b.source_id=:source AND b.stream_id=:stream"
    rows = db.execute(
        text(
            """
        WITH captures AS (
          SELECT b.source_id,b.stream_id,b.gateway_id,b.capture_generation,b.capture_seq,
            b.body->'capture_time'->>'clock_epoch' AS epoch,
            (b.body->>'captured_at')::timestamptz AS at,
            (b.body->'capture_time'->>'monotonic_seconds')::double precision AS mono
          FROM ingest_batches b JOIN source_streams s USING(source_id,stream_id)
          WHERE """
            + where
            + """ AND b.body->'capture_time'->>'clock_epoch' IS NOT NULL
        ), ordered AS (
          SELECT *,max(at) OVER history AS previous_at,
                   max(mono) OVER history AS previous_mono
          FROM captures WINDOW history AS (
            PARTITION BY source_id,stream_id,gateway_id,capture_generation,epoch
            ORDER BY capture_seq ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
        ) SELECT source_id,stream_id,gateway_id,capture_generation,epoch,
                 min(capture_seq) AS cutoff
          FROM ordered WHERE at<previous_at OR mono<=previous_mono
          GROUP BY source_id,stream_id,gateway_id,capture_generation,epoch
    """
        ),
        params,
    ).mappings()
    return {
        (
            r["source_id"],
            r["stream_id"],
            r["gateway_id"],
            int(r["capture_generation"]),
            r["epoch"],
        ): int(r["cutoff"])
        for r in rows
    }


def load_report_evidence(db, line_id, start, end, *, scope=None, execution_id=None):
    """Read retained capture evidence without changing ingestion or alarm decisions."""
    params = {"line": line_id, "start": start - timedelta(seconds=3), "end": end}
    where = (
        "s.line_id=:line AND (b.body->>'captured_at')::timestamptz>=:start "
        "AND (b.body->>'captured_at')::timestamptz<:end"
    )
    if scope:
        params.update(source=scope["source_id"], stream=scope["stream_id"])
        where += " AND s.source_id=:source AND s.stream_id=:stream"
    rows = (
        db.execute(
            text(
                """SELECT b.*,s.conflicted,s.reconciliation_required,
        ARRAY(SELECT d.context FROM alarm_capture_decisions d
          WHERE d.gateway_id=b.gateway_id AND d.batch_id=b.batch_id) AS contexts
        FROM ingest_batches b JOIN source_streams s USING(source_id,stream_id)
        WHERE """
                + where
                + " ORDER BY b.capture_generation,b.capture_seq"
            ),
            params,
        )
        .mappings()
        .all()
    )
    reversals = _clock_reversal_cutoffs(db, line_id, scope)
    evidence = []
    for row in rows:
        body = row["body"]
        at = timestamp(body["captured_at"])
        key = (
            row["source_id"],
            row["stream_id"],
            row["gateway_id"],
            int(row["capture_generation"]),
            (body.get("capture_time") or {}).get("clock_epoch"),
        )
        reversed_clock = int(row["capture_seq"]) >= reversals.get(key, float("inf"))
        if not body.get("samples"):
            if reversed_clock:
                evidence.append(
                    {
                        "from": at,
                        "to": at + timedelta(seconds=FRESHNESS_SECONDS),
                        "observed_at": at,
                        "capture_order": (int(row["capture_generation"]), int(row["capture_seq"])),
                        "class": "UNKNOWN",
                        "state": None,
                        "fault_code": None,
                        "stop_reason": None,
                        "unknown_reason": "TIME_UNKNOWN",
                        "source_id": row["source_id"],
                        "stream_id": row["stream_id"],
                        "execution_id": execution_id,
                    }
                )
            continue
        samples = {s["tag_id"]: s for s in body.get("samples", [])}
        active = samples.get("Line.ActiveExecutionId", {}).get("reading", {}).get("value")
        if execution_id and active != execution_id:
            # A capture for another execution (or absent ownership tag) cuts off
            # earlier ownership evidence. Clock continuity is stream-wide, even
            # when a digital-only capture omits ActiveExecutionId.
            evidence.append(
                {
                    "from": at,
                    "to": at + timedelta(seconds=FRESHNESS_SECONDS),
                    "observed_at": at,
                    "capture_order": (int(row["capture_generation"]), int(row["capture_seq"])),
                    "class": "UNKNOWN",
                    "state": None,
                    "fault_code": None,
                    "stop_reason": None,
                    "unknown_reason": "TIME_UNKNOWN" if reversed_clock else "NO_EVIDENCE",
                    "source_id": row["source_id"],
                    "stream_id": row["stream_id"],
                    "execution_id": None,
                    "observed_execution_id": active,
                }
            )
            continue
        state_sample = samples.get("Line.State", {})
        state = STATES.get(str(state_sample.get("reading", {}).get("value")))
        reason = None
        communication = body.get("communication") or {}
        if communication.get("state") != "GOOD" or not communication.get("connected"):
            reason = "COMMUNICATION_LOST"
        elif not _clock_known(row) or reversed_clock:
            reason = "TIME_UNKNOWN"
        elif row["conflicted"] or not state or state_sample.get("status_code", 2**31) >> 30:
            reason = "NO_EVIDENCE"
        fault_sample = samples.get("Line.FaultCode", {})
        evidence.append(
            {
                "from": at,
                "observed_at": at,
                "capture_order": (int(row["capture_generation"]), int(row["capture_seq"])),
                "to": at + timedelta(seconds=FRESHNESS_SECONDS),
                "class": "UNKNOWN" if reason else "RUNNING" if state == "RUNNING" else "STOPPED",
                "state": None if reason else state,
                "fault_code": None
                if reason
                else fault_sample.get("reading", {}).get("value") or None,
                "stop_reason": None,
                **({"unknown_reason": reason} if reason else {}),
                "source_id": row["source_id"],
                "stream_id": row["stream_id"],
                "execution_id": active or None,
            }
        )
    for entry in list(evidence):
        if entry["to"] < end:
            evidence.append(
                {
                    **entry,
                    "from": entry["to"],
                    "to": end,
                    "class": "UNKNOWN",
                    "state": None,
                    "fault_code": None,
                    "unknown_reason": "COMMUNICATION_LOST",
                }
            )
    # A permanent sequence gap has no wall-time bounds. Conservatively cover the
    # interval from preceding event until the next known event after the gap.
    gaps = db.execute(
        text("""SELECT g.*,s.line_id FROM stream_gaps g
        JOIN source_streams s USING(source_id,stream_id) WHERE s.line_id=:line"""),
        {"line": line_id},
    ).mappings()
    for gap in gaps:
        if scope and (gap["source_id"], gap["stream_id"]) != (
            scope["source_id"],
            scope["stream_id"],
        ):
            continue
        bounds = (
            db.execute(
                text("""SELECT
          max((body->>'occurred_at')::timestamptz) FILTER (WHERE seq<:lo) AS before,
          min((body->>'occurred_at')::timestamptz) FILTER (WHERE seq>:hi) AS after
          FROM event_inbox WHERE source_id=:source AND stream_id=:stream"""),
                {
                    "lo": gap["from_seq"],
                    "hi": gap["through_seq"],
                    "source": gap["source_id"],
                    "stream": gap["stream_id"],
                },
            )
            .mappings()
            .one()
        )
        evidence.append(
            {
                "from": bounds["before"] or start,
                "to": bounds["after"] or end,
                "class": "UNKNOWN",
                "state": None,
                "fault_code": None,
                "stop_reason": None,
                "unknown_reason": "COMMUNICATION_LOST",
                "sequence_gap": True,
                "source_id": gap["source_id"],
                "stream_id": gap["stream_id"],
            }
        )
    return evidence


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


def read_execution_timeline(db, execution_id, line_id, source_id, stream_id, *, now, annotate=None):
    scope = dict(line_id=line_id, source_id=source_id, stream_id=stream_id)
    row = _execution(db, execution_id, line_id, scope)
    _, ended = execution_state_bounds(db, execution_id)
    start, end = row["registered_at"], min(ended or now, now)
    segments = build_timeline(
        start,
        end,
        load_report_evidence(db, line_id, start, end, scope=scope, execution_id=execution_id),
    )
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
    segments = build_timeline(
        start, end, load_report_evidence(db, line_id, start, end, scope=scope)
    )
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
