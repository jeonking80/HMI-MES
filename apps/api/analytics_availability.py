"""Read-only, fail-closed availability for the complete CX-01 through CX-05 UI."""

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from apps.api.database import SUPPORTED_AUTH_REVISIONS
from apps.api.trend_history import TREND_REVISIONS

# Downtime annotations require schema16; guest schema17 is independent. Reuse
# the explicit service revision allowlists so a reviewed schema18 can opt in.
ANALYTICS_REVISIONS = (TREND_REVISIONS & SUPPORTED_AUTH_REVISIONS) - {"0015_analog_history"}
REQUIRED_ROUTES = frozenset(
    {
        ("/api/v1/trends", "GET"),
        ("/api/v1/reports/executions", "GET"),
        ("/api/v1/reports/executions.csv", "GET"),
        ("/api/v1/reports/executions/{execution_id}/timeline", "GET"),
        ("/api/v1/reports/daily", "GET"),
        ("/api/v1/reports/daily.csv", "GET"),
        ("/api/v1/downtime-reasons", "POST"),
        ("/api/v1/downtime-reasons/history", "GET"),
        ("/api/v1/spc/xbar-r", "GET"),
        ("/api/v1/dashboard", "GET"),
    }
)


def analytics_available(app, auth) -> bool:
    """Registration and readable supported schema, without granting user access.

    Each call rechecks schema under the migration shared lock. Missing runtime
    grants, missing objects and DB outages return only False; no exception or
    database details escape into the pre-login response. No row is written.
    """
    if getattr(app.state, "analytics_routes_registered", False) is not True:
        return False
    registered = {
        (getattr(route, "path", None), method)
        for route in app.routes
        for method in (getattr(route, "methods", None) or ())
    }
    if not REQUIRED_ROUTES <= registered:
        return False
    try:
        with auth._transaction() as db:
            revisions = (
                db.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()
            )
            if len(revisions) != 1 or revisions[0] not in ANALYTICS_REVISIONS:
                return False
            # Schema revision alone does not prove post-migration runtime grants.
            # Probe all new analytics tables with SELECT only; no data is exposed.
            for table in (
                "analog_history_raw",
                "analog_history_minutes",
                "analog_history_clock_state",
                "downtime_reason_history",
            ):
                db.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 0"))
        return True
    except SQLAlchemyError:
        return False
