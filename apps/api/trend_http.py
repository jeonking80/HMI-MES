"""Strict viewer-authorized trend HTTP contract."""

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from fastapi import HTTPException, Request
from pydantic import TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool

from apps.api.minute_aggregation import ANALOG_UNITS
from apps.api.trend_history import TREND_REVISIONS, floor_minute, read_trends
from contracts.models import Identifier, UTCTimestamp

_ID = TypeAdapter(Identifier)
_TIME = TypeAdapter(UTCTimestamp)


def parse_query(query):
    required = {"line_id", "source_id", "stream_id", "tag", "from", "to"}
    if not required <= set(query) or set(query) - required - {"resolution"}:
        raise ValueError("invalid trend parameters")
    if any(len(query.getlist(key)) != 1 for key in set(query) - {"tag"}):
        raise ValueError("duplicate trend parameter")
    tags = query.getlist("tag")
    if not 1 <= len(tags) <= 4 or len(set(tags)) != len(tags):
        raise ValueError("one to four distinct analog tags required")
    if any(tag not in ANALOG_UNITS for tag in tags):
        raise ValueError("unsupported analog tag")
    start, end = _TIME.validate_python(query["from"]), _TIME.validate_python(query["to"])
    resolution = query.get("resolution", "auto")
    if resolution not in {"auto", "second", "minute"} or start >= end:
        raise ValueError("invalid trend interval")
    if resolution == "auto":
        resolution = "second" if end - start <= timedelta(hours=1) else "minute"
    limit = timedelta(hours=1) if resolution == "second" else timedelta(days=7)
    step = timedelta(seconds=1) if resolution == "second" else timedelta(minutes=1)
    bucket = start.replace(microsecond=0) if resolution == "second" else floor_minute(start)
    if end - start > limit or (end - bucket) / step > (3600 if resolution == "second" else 10080):
        raise ValueError("trend range exceeds resolution bound")
    scope = {key: _ID.validate_python(query[key]) for key in ("line_id", "source_id", "stream_id")}
    return dict(scope=scope, tags=tags, start=start, end=end, resolution=resolution)


def register_trend_routes(app, auth):
    @app.get("/api/v1/trends")
    async def trends(request: Request):
        try:
            parameters = parse_query(request.query_params)
        except (ValueError, ValidationError):
            raise HTTPException(422, "invalid trend query") from None

        def read():
            with auth.authorized_transaction(
                request.cookies.get("hmi_session"), permission="read", refresh_idle=False
            ) as authorized:
                connection = authorized.connection
                revision = connection.execute(
                    sa.text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                if revision not in TREND_REVISIONS:
                    raise HTTPException(503, "trend history unavailable")
                exists = connection.execute(
                    sa.text("""SELECT 1 FROM source_streams
                    WHERE line_id=:line_id AND source_id=:source_id AND stream_id=:stream_id"""),
                    parameters["scope"],
                ).first()
                if exists is None:
                    raise HTTPException(404, "source is not configured for this line")
                return read_trends(connection, **parameters, now=datetime.now(UTC))

        return await run_in_threadpool(read)
