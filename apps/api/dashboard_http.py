"""Strict authenticated HTTP entry point for the passive dashboard."""

import re
from datetime import date

from fastapi import HTTPException, Request
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from apps.api.dashboard import DashboardUnavailable, read_dashboard
from apps.api.snapshot import SnapshotNotFound
from contracts.models import Identifier

_ID = TypeAdapter(Identifier)


def register_dashboard_routes(app, auth):
    @app.get("/api/v1/dashboard")
    async def dashboard(request: Request):
        required = {"line_id", "source_id", "stream_id", "date"}
        if set(request.query_params) != required or any(
            len(request.query_params.getlist(key)) != 1 for key in required
        ):
            raise HTTPException(422, "exact dashboard scope and date required")
        try:
            values = {
                key: _ID.validate_python(request.query_params[key]) for key in required - {"date"}
            }
            raw_day = request.query_params["date"]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_day) is None:
                raise ValueError
            day = date.fromisoformat(raw_day)
        except (ValueError, ValidationError):
            raise HTTPException(422, "invalid dashboard scope or date") from None

        def read():
            with auth.authorized_transaction(
                request.cookies.get("hmi_session"), permission="read", refresh_idle=False
            ) as tx:
                return read_dashboard(tx.connection, **values, day=day, now=auth._now())

        try:
            return await run_in_threadpool(read)
        except SnapshotNotFound:
            raise HTTPException(404, "dashboard source not found") from None
        except (DashboardUnavailable, SQLAlchemyError):
            raise HTTPException(503, "dashboard unavailable") from None
        except ValueError:
            raise HTTPException(422, "invalid dashboard request") from None
