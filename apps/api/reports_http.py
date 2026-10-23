"""Authenticated report and spreadsheet exports with strict query contracts."""

import csv
import io
from datetime import date, timedelta

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool

from apps.api.reports import (
    ReportNotFound,
    read_daily_report,
    read_execution_reports,
    read_execution_timeline,
)
from contracts.models import Identifier, UTCTimestamp

ID = TypeAdapter(Identifier)
TIME = TypeAdapter(UTCTimestamp)


def csv_cell(value):
    value = "" if value is None else str(value)
    return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value


def report_csv(report, *, daily=False):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    if daily:
        writer.writerow(
            ["날짜", "양품", "불량", "폐기", "가동초", "정지초", "불명초", "잠정", "잠정사유"]
        )
        rows = [
            [
                report["date"],
                *report["totals"].values(),
                *[report["durations_seconds"][k] for k in ("running", "stopped", "unknown")],
                report["provisional"],
                ",".join(report["provisional_reasons"]),
            ]
        ]
    else:
        writer.writerow(
            [
                "실행ID",
                "작업지시",
                "물품",
                "레시피",
                "등록시각",
                "양품",
                "불량",
                "폐기",
                "잠정",
                "잠정사유",
            ]
        )
        rows = [
            [
                r["execution_id"],
                r["work_order_id"],
                r["item_id"],
                r["recipe_version"],
                r["registered_at"],
                *r["totals"].values(),
                r["provisional"],
                ",".join(r["provisional_reasons"]),
            ]
            for r in report["items"]
        ]
    writer.writerows([[csv_cell(v) for v in row] for row in rows])
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")


def register_report_routes(app, auth):
    async def handle(request, kind, execution_id=None, export=False):
        required = (
            {"line_id", "date"}
            if kind == "daily"
            else {"line_id", "source_id", "stream_id"}
            if kind == "timeline"
            else {"line_id", "from", "to"}
        )
        optional = {"limit", "cursor"} if kind == "executions" else set()
        query = request.query_params
        if (
            not required <= set(query)
            or set(query) - required - optional
            or any(len(query.getlist(k)) != 1 for k in query)
        ):
            raise HTTPException(422, "invalid report query")
        try:
            values = {k: ID.validate_python(query[k]) for k in required if k.endswith("_id")}
            if execution_id is not None:
                ID.validate_python(execution_id)
            if kind == "daily":
                day = date.fromisoformat(query["date"])
                if day.isoformat() != query["date"]:
                    raise ValueError()
            elif kind == "executions":
                start, end = TIME.validate_python(query["from"]), TIME.validate_python(query["to"])
                limit = int(query.get("limit", "50"))
                if (
                    str(limit) != query.get("limit", "50")
                    or not 1 <= limit <= 100
                    or not start < end
                    or end - start > timedelta(days=93)
                ):
                    raise ValueError()
                cursor = ID.validate_python(query["cursor"]) if "cursor" in query else None
        except (ValueError, ValidationError):
            raise HTTPException(422, "invalid report query values") from None

        def perform():
            with auth.authorized_transaction(
                request.cookies.get("hmi_session"), permission="read", refresh_idle=False
            ) as tx:
                now = auth._now()
                annotate = getattr(app.state, "report_segment_annotator", None)
                if kind == "daily":
                    return read_daily_report(
                        tx.connection, values["line_id"], day, now=now, annotate=annotate
                    )
                if kind == "timeline":
                    return read_execution_timeline(
                        tx.connection, execution_id, **values, now=now, annotate=annotate
                    )
                return read_execution_reports(
                    tx.connection,
                    values["line_id"],
                    start,
                    end,
                    now=now,
                    limit=limit,
                    cursor=cursor,
                    annotate=annotate,
                )

        try:
            report = await run_in_threadpool(perform)
        except ReportNotFound:
            raise HTTPException(404, "report execution not found") from None
        except ValueError:
            raise HTTPException(422, "invalid report cursor") from None
        headers = {"Cache-Control": "no-store"}
        if export:
            headers["Content-Disposition"] = 'attachment; filename="' + kind + '.csv"'
            return Response(
                report_csv(report, daily=kind == "daily"),
                media_type="text/csv; charset=utf-8",
                headers=headers,
            )
        return JSONResponse(report, headers=headers)

    @app.get("/api/v1/reports/executions.csv")
    async def executions_csv(request: Request):
        return await handle(request, "executions", export=True)

    @app.get("/api/v1/reports/executions")
    async def executions(request: Request):
        return await handle(request, "executions")

    @app.get("/api/v1/reports/executions/{execution_id}/timeline")
    async def timeline(execution_id: str, request: Request):
        return await handle(request, "timeline", execution_id)

    @app.get("/api/v1/reports/daily.csv")
    async def daily_csv(request: Request):
        return await handle(request, "daily", export=True)

    @app.get("/api/v1/reports/daily")
    async def daily(request: Request):
        return await handle(request, "daily")
