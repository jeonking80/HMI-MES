"""Passive exact-execution SPC API over retained, clock-proven raw captures."""

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from apps.api.execution_read import ExecutionLookup, ExecutionNotFound
from apps.api.minute_aggregation import ANALOG_UNITS, RawObservation, aggregate_minutes
from apps.api.reports import (
    _clock_reversal_cutoffs,
    canonical_clock_available,
    clock_binding_sql,
    execution_state_bounds,
)
from apps.api.spc import SPCSlot, calculate_spc


class SPCQuery(ExecutionLookup):
    tag: Literal["Sealer.TemperaturePV", "Sealer.AirPressure"]


def historical_spec(parameters, tag, configs):
    """No current-head fallback. Ambiguous, absent and one-sided specs stay null."""
    if tag != "Sealer.TemperaturePV" or not configs or any(c is None for c in configs):
        return None
    target = parameters.get("temperature_c")
    bands = {
        (c.get("setpoint_c"), c.get("activation_delta_c"))
        for c in configs
        if c.get("kind") == "TEMPERATURE_DEVIATION"
    }
    if len(bands) != 1 or any(c.get("kind") != "TEMPERATURE_DEVIATION" for c in configs):
        return None
    setpoint, delta = bands.pop()
    if setpoint != target or not isinstance(delta, (float, int)) or delta <= 0:
        return None
    return dict(usl=target + delta, lsl=target - delta, target=target, source="recipe+alarm_rule")


def read_spc(connection, query: SPCQuery, *, now=None):
    now = now or datetime.now(UTC)
    values = query.model_dump()
    # After END_RUN the PLC can keep the ended execution ID active while idle. Samples are
    # attributed only up to the confirmed STOPPING->STOPPED end, never through idle time.
    _, ended = execution_state_bounds(connection, query.execution_id)
    until = min(now, ended) if ended is not None else now

    canonical = canonical_clock_available(connection)
    raw_at = "COALESCE(r.canonical_utc,r.acquired_at)" if canonical else "r.acquired_at"
    original_at = "(sample->>'acquired_at')::timestamptz"
    original_join = ""
    proof_join = ""
    proof_fields = "NULL::jsonb AS clock_evidence,NULL::timestamptz AS canonical_utc"
    if canonical:
        original_join = (
            "LEFT JOIN capture_clock_evidence original_clock ON "
            "original_clock.gateway_id=original.gateway_id "
            "AND original_clock.batch_id=original.batch_id"
        )
        original_at = (
            "CASE WHEN "
            + clock_binding_sql("original", "original_clock", "s.line_id")
            + " AND original_clock.status='GOOD' AND "
            + original_at
            + "=(original.body->>'captured_at')::timestamptz "
            "THEN original_clock.canonical_api_utc ELSE " + original_at + " END"
        )
        proof_join = (
            "LEFT JOIN capture_clock_evidence clock ON "
            "clock.gateway_id=b.gateway_id AND clock.batch_id=b.batch_id"
        )
        proof_fields = "to_jsonb(clock) AS clock_evidence,r.canonical_utc"

    # One statement snapshot supplies the execution identity, original batch attribution,
    # raw clock proof and capture-assigned historical rule (never channel current_version).
    rows = (
        connection.execute(
            text(f"""
        WITH certified_execution AS MATERIALIZED (
          SELECT e.*,
            (SELECT min({original_at})
             FROM ingest_batches original {original_join}
             CROSS JOIN LATERAL jsonb_array_elements(original.body->'samples') sample
             WHERE original.source_id=e.source_id AND original.stream_id=e.stream_id
               AND sample->>'tag_id'=:tag

               AND ({original_at})>=e.registered_at
               AND ({original_at})<:until

               AND EXISTS (
                 SELECT 1 FROM jsonb_array_elements(original.body->'samples') identity
                 WHERE identity->>'tag_id'='Line.ActiveExecutionId'
                   AND ((identity->>'status_code')::bigint >> 30)=0
                   AND identity->'reading'->>'value'=e.execution_id
                   AND (identity->>'acquired_at')::timestamptz=
                       (sample->>'acquired_at')::timestamptz)
               AND (({original_at})<:cutoff OR NOT EXISTS (
                 SELECT 1 FROM analog_history_raw retained
                 WHERE retained.line_id=:line_id
                   AND retained.source_id=original.source_id
                   AND retained.stream_id=original.stream_id
                   AND retained.gateway_id=original.gateway_id
                   AND retained.batch_id=original.batch_id
                   AND retained.capture_generation=original.capture_generation
                   AND retained.capture_seq=original.capture_seq
                   AND retained.tag_id=:tag AND retained.sample_id=sample->>'sample_id'))
            ) AS missing_prefix_at
          FROM production_executions e
          JOIN source_streams s ON s.source_id=e.source_id AND s.stream_id=e.stream_id
          WHERE e.execution_id=:execution_id AND e.source_id=:source_id
            AND e.stream_id=:stream_id AND s.line_id=:line_id
        )
        SELECT e.parameters,e.registered_at,e.missing_prefix_at,r.observation,
          {proof_fields},b.body AS original_body,b.content_sha256,
          (SELECT jsonb_agg(sample) FROM jsonb_array_elements(b.body->'samples') sample
           WHERE sample->>'tag_id'='Line.ActiveExecutionId') AS identities,
          (SELECT jsonb_agg(v.config) FROM alarm_capture_decisions d
           JOIN alarm_rule_versions v ON v.channel_id=d.channel_id AND v.version=d.rule_version
           WHERE d.gateway_id=r.gateway_id AND d.batch_id=r.batch_id
             AND d.source_id=r.source_id AND d.stream_id=r.stream_id
             AND d.capture_generation=r.capture_generation AND d.capture_seq=r.capture_seq
             AND v.config->>'kind'=:kind) AS configs
        FROM certified_execution e
        JOIN source_streams s ON s.source_id=e.source_id AND s.stream_id=e.stream_id
        LEFT JOIN analog_history_raw r ON r.source_id=e.source_id AND r.stream_id=e.stream_id
          AND r.line_id=s.line_id AND r.tag_id=:tag

          AND {raw_at}>=greatest(e.registered_at,:cutoff) AND {raw_at}<:until
          AND (floor(extract(second FROM {raw_at})) IN (0,12,24,36,48)
               OR {raw_at}>=:recent)

        LEFT JOIN ingest_batches b ON b.gateway_id=r.gateway_id AND b.batch_id=r.batch_id
        {proof_join}
        WHERE e.execution_id=:execution_id AND e.source_id=:source_id
          AND e.stream_id=:stream_id AND s.line_id=:line_id
        ORDER BY {raw_at},r.capture_seq
    """),
            dict(
                values,
                until=until,
                cutoff=now - timedelta(days=7),
                recent=until - timedelta(minutes=15),
                kind="TEMPERATURE_DEVIATION"
                if query.tag == "Sealer.TemperaturePV"
                else "PRESSURE_LOW",
            ),
        )
        .mappings()
        .all()
    )
    if not rows:
        raise ExecutionNotFound()
    start = max(rows[0]["registered_at"], now - timedelta(days=7))
    # Scope for aggregate_minutes must never span more than a day. Bucket separately,
    # preserving selected latest captures and treating cross-epoch minutes as unknown.
    buckets = {}
    configs = []
    clock_cutoffs = _clock_reversal_cutoffs(connection, query.line_id, values)
    for row in rows:
        if row["observation"] is None:
            continue
        obs = RawObservation.model_validate(row["observation"])
        original_acquired = obs.sample.acquired_at
        identity = row["identities"] or []
        attributed = (
            len(identity) == 1
            and identity[0]["status_code"] >> 30 == 0
            and identity[0]["reading"]["value"] == query.execution_id
            and datetime.fromisoformat(identity[0]["acquired_at"]) == original_acquired
        )
        if canonical and row["clock_evidence"] is not None:
            from apps.api.capture_clock import effective_capture_clock

            original = row["original_body"]
            key = (
                obs.scope.source_id,
                obs.scope.stream_id,
                obs.scope.gateway_id,
                obs.scope.capture_generation,
                obs.scope.clock_epoch,
            )
            decision = effective_capture_clock(
                row["clock_evidence"],
                body=original,
                batch_sha256=row["content_sha256"],
                reversal_seq=clock_cutoffs.get(key),
            )
            valid = (
                decision.status == "GOOD"
                and row["clock_evidence"]["line_id"] == query.line_id
                and row["canonical_utc"] is not None
                and row["canonical_utc"] == decision.canonical_api_utc
                and original_acquired == datetime.fromisoformat(original["captured_at"])
            )
            coordinate = row["canonical_utc"] if valid else original_acquired
            if not valid and decision.reason_code == "CLOCK_REVERSAL":
                accepted = effective_capture_clock(
                    row["clock_evidence"], body=original, batch_sha256=row["content_sha256"]
                )
                if (
                    accepted.status == "GOOD"
                    and row["clock_evidence"]["line_id"] == query.line_id
                    and row["canonical_utc"] == accepted.canonical_api_utc
                    and original_acquired == datetime.fromisoformat(original["captured_at"])
                ):
                    # Keep the invalidated latest capture in its accepted slot
                    # as UNKNOWN so an older GOOD sample cannot reappear there.
                    coordinate = row["canonical_utc"]
            obs = obs.model_copy(
                update={
                    "sample": obs.sample.model_copy(update={"acquired_at": coordinate}),
                    "time_quality": "GOOD" if valid else "UNKNOWN",
                }
            )
        elif canonical and row["canonical_utc"] is not None:
            # A naked derived coordinate without its immutable proof is not evidence.
            obs = obs.model_copy(update={"time_quality": "UNKNOWN"})
        key = (
            obs.scope.source_id,
            obs.scope.stream_id,
            obs.scope.gateway_id,
            obs.scope.capture_generation,
            obs.scope.clock_epoch,
        )
        if obs.capture_seq >= clock_cutoffs.get(key, float("inf")):
            obs = obs.model_copy(update={"time_quality": "UNKNOWN"})
        minute = obs.sample.acquired_at.replace(second=0, microsecond=0)
        buckets.setdefault(minute, []).append((obs, attributed))
        if attributed:
            configs.extend(row["configs"] or [None])
    # Detect reversals across minute boundaries too, before per-minute selection.
    epoch_observations = {}
    for observations in buckets.values():
        for obs, _ in observations:
            epoch_observations.setdefault(obs.scope.model_dump_json(), []).append(obs)
    reversals = {}
    for epoch, observations in epoch_observations.items():
        previous = None
        for obs in sorted(observations, key=lambda o: o.capture_seq):
            if previous is not None and obs.sample.acquired_at < previous:
                reversals[epoch] = obs.capture_seq
                break
            previous = obs.sample.acquired_at
    slots = []
    for minute, observations in sorted(buckets.items()):
        epochs = {obs.scope.model_dump_json() for obs, _ in observations}
        if len(epochs) != 1:
            slots.extend(
                SPCSlot(minute + timedelta(seconds=s), None, "TIME_UNKNOWN") for s in range(60)
            )
            continue
        raw = tuple(obs for obs, _ in observations)
        attributed = {obs.sample.sample_id: ok for obs, ok in observations}
        aggregate = aggregate_minutes(
            raw,
            scope=raw[0].scope,
            tag_ids=(query.tag,),
            start=minute,
            end=minute + timedelta(minutes=1),
        )[0]
        for slot in aggregate.slots:
            quality = "GOOD"
            if slot.selected is None:
                quality = "MISSING"
            elif not attributed[slot.selected.sample.sample_id]:
                quality = "MISSING"
            elif (
                raw[0].scope.model_dump_json() in reversals
                and slot.selected.capture_seq >= reversals[raw[0].scope.model_dump_json()]
            ):
                quality = "TIME_UNKNOWN"
            elif slot.invalid_reason in {"CLOCK_REVERSAL", "TIME_UNKNOWN", "TIME_BAD"}:
                quality = "TIME_UNKNOWN"
            elif not slot.valid:
                quality = "UNCERTAIN" if slot.selected.sample.status_code >> 30 == 1 else "BAD"
            slots.append(
                SPCSlot(
                    slot.start,
                    float(slot.selected.sample.reading.value) if quality == "GOOD" else None,
                    quality,
                    raw[0].scope.model_dump_json(),
                )
            )
    # An execution that ended before the 7-day raw window has no retained window left.
    # Report that limitation instead of handing calculate_spc an inverted interval.
    expired = until <= start
    result = calculate_spc(
        slots=slots,
        start=start,
        end=max(start, until),
        now=now,
        tag_id=query.tag,
        unit="°C" if query.tag == "Sealer.TemperaturePV" else ANALOG_UNITS[query.tag],
        spec=historical_spec(rows[0]["parameters"], query.tag, configs),
    )
    result["baseline_basis"] = "first-20-complete-execution-subgroups-prefix-certified"
    result["baseline_limitation"] = "EXECUTION_OUTSIDE_RAW_RETENTION" if expired else None
    missing_prefix = rows[0]["missing_prefix_at"]
    if (
        result["baseline"]
        and missing_prefix is not None
        and missing_prefix < datetime.fromisoformat(result["baseline"]["to"])
    ):
        # Lost originals might contain earlier complete groups. Retained groups cannot
        # establish that they are the execution's first 20, even if 25 remain today.
        result.update(status="INSUFFICIENT_DATA", baseline=None, limits=None, capability=None)
        result["baseline_limitation"] = "ORIGINAL_PREFIX_RAW_UNAVAILABLE"
        for group in result["subgroups"]:
            group["violations"] = []
    result["history_limitation"] = "RAW_RETENTION_LIMITED; LATE_CAPTURES_MAY_REVISE_BASELINE"
    result["execution_id"] = query.execution_id
    result["scope"] = {k: values[k] for k in ("line_id", "source_id", "stream_id")}
    return result


def register_spc_routes(app: FastAPI, auth, *, session_cookie="hmi_session"):
    @app.get("/api/v1/spc/xbar-r")
    async def spc(request: Request):
        required = set(SPCQuery.model_fields)
        if set(request.query_params) != required or any(
            len(request.query_params.getlist(key)) != 1 for key in required
        ):
            raise HTTPException(422, "exact scope, execution and one supported tag required")
        try:
            query = SPCQuery.model_validate(dict(request.query_params))
        except ValidationError:
            raise HTTPException(422, "invalid SPC query") from None

        def perform():
            with auth.authorized_snapshot(
                request.cookies.get(session_cookie), permission="read"
            ) as tx:
                return read_spc(tx.connection, query)

        try:
            payload = await run_in_threadpool(perform)
        except ExecutionNotFound:
            raise HTTPException(404, "execution not found in exact scope") from None
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})
