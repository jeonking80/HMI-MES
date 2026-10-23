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
    # One statement snapshot supplies the execution identity, original batch attribution,
    # raw clock proof and capture-assigned historical rule (never channel current_version).
    rows = (
        connection.execute(
            text("""
        WITH certified_execution AS MATERIALIZED (
          SELECT e.*,
            (SELECT min((sample->>'acquired_at')::timestamptz)
             FROM ingest_batches original
             CROSS JOIN LATERAL jsonb_array_elements(original.body->'samples') sample
             WHERE original.source_id=e.source_id AND original.stream_id=e.stream_id
               AND sample->>'tag_id'=:tag
               AND (sample->>'acquired_at')::timestamptz>=e.registered_at
               AND (sample->>'acquired_at')::timestamptz<:now
               AND EXISTS (
                 SELECT 1 FROM jsonb_array_elements(original.body->'samples') identity
                 WHERE identity->>'tag_id'='Line.ActiveExecutionId'
                   AND ((identity->>'status_code')::bigint >> 30)=0
                   AND identity->'reading'->>'value'=e.execution_id
                   AND (identity->>'acquired_at')::timestamptz=
                       (sample->>'acquired_at')::timestamptz)
               AND ((sample->>'acquired_at')::timestamptz<:cutoff OR NOT EXISTS (
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
          AND r.acquired_at>=greatest(e.registered_at,:cutoff) AND r.acquired_at<:now
          AND (floor(extract(second FROM r.acquired_at)) IN (0,12,24,36,48)
               OR r.acquired_at>=:recent)
        LEFT JOIN ingest_batches b ON b.gateway_id=r.gateway_id AND b.batch_id=r.batch_id
        WHERE e.execution_id=:execution_id AND e.source_id=:source_id
          AND e.stream_id=:stream_id AND s.line_id=:line_id
        ORDER BY r.acquired_at,r.capture_seq
    """),
            dict(
                values,
                now=now,
                cutoff=now - timedelta(days=7),
                recent=now - timedelta(minutes=15),
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
    for row in rows:
        if row["observation"] is None:
            continue
        obs = RawObservation.model_validate(row["observation"])
        identity = row["identities"] or []
        attributed = (
            len(identity) == 1
            and identity[0]["status_code"] >> 30 == 0
            and identity[0]["reading"]["value"] == query.execution_id
            and datetime.fromisoformat(identity[0]["acquired_at"]) == obs.sample.acquired_at
        )
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
    result = calculate_spc(
        slots=slots,
        start=start,
        end=now,
        now=now,
        tag_id=query.tag,
        unit="°C" if query.tag == "Sealer.TemperaturePV" else ANALOG_UNITS[query.tag],
        spec=historical_spec(rows[0]["parameters"], query.tag, configs),
    )
    result["baseline_basis"] = "first-20-complete-execution-subgroups-prefix-certified"
    result["baseline_limitation"] = None
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
            with auth.authorized_transaction(
                request.cookies.get(session_cookie), permission="read", refresh_idle=False
            ) as tx:
                return read_spc(tx.connection, query)

        try:
            payload = await run_in_threadpool(perform)
        except ExecutionNotFound:
            raise HTTPException(404, "execution not found in exact scope") from None
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})
