"""Read-only, fail-closed availability for the complete CX-01 through CX-05 UI."""

from collections.abc import Callable
from threading import Lock

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from apps.api.capture_clock import CLOCK_EVIDENCE_REVISIONS
from apps.api.database import SUPPORTED_AUTH_REVISIONS
from apps.api.trend_history import TREND_REVISIONS

# Downtime annotations require schema16; guest schema17 is independent. Reuse
# the explicit service revision allowlists so reviewed successor schemas can opt in.
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


CACHE_TTL_SECONDS = 5.0


class AnalyticsAvailabilityCache:
    """One app's bounded schema/grant snapshot, with one in-flight refresh.

    Both success and failure expire after five seconds. Route registration is
    deliberately outside this cache and must still be checked on every request.
    """

    def __init__(self, monotonic: Callable[[], float]):
        self._monotonic = monotonic
        self._lock = Lock()
        self._value: bool | None = None
        self._expires_at = 0.0

    def available(self, auth) -> bool:
        with self._lock:
            if self._value is None or self._monotonic() >= self._expires_at:
                self._value = _database_available(auth)
                self._expires_at = self._monotonic() + CACHE_TTL_SECONDS
            return self._value


def _routes_available(app) -> bool:
    if getattr(app.state, "analytics_routes_registered", False) is not True:
        return False
    registered = {
        (getattr(route, "path", None), method)
        for route in app.routes
        for method in (getattr(route, "methods", None) or ())
    }
    return REQUIRED_ROUTES <= registered


def _database_available(auth) -> bool:
    """Probe supported schema/read grants without rows, writes, or error details."""
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
            if revisions[0] in CLOCK_EVIDENCE_REVISIONS:
                db.execute(sa.text("SELECT 1 FROM capture_clock_evidence LIMIT 0"))
        return True
    except SQLAlchemyError:
        return False


def analytics_available(app, auth, *, cache: AnalyticsAvailabilityCache | None = None) -> bool:
    """Fail closed on current registration and a short schema/grant snapshot.

    This advertises a feature, not authorization. Business endpoints continue to
    check their own current session permissions and database access normally.
    """
    if not _routes_available(app):
        return False
    database_available = cache.available(auth) if cache is not None else _database_available(auth)
    return database_available and _routes_available(app)
