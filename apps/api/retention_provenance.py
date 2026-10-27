"""Conservative analytical coverage of expired ingest evidence."""

from datetime import datetime, timedelta

from apps.api.ingest_retention import expired_coverage

EXPIRED = "RETENTION_EXPIRED"
RAW_EXPIRED = {
    "availability": EXPIRED,
    "reason_code": EXPIRED,
    "coverage_basis": "CONSERVATIVE_ENVELOPE",
}


def _time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value


def expiry_windows(connection, *, scope, start, end, tail=timedelta(seconds=1)):
    """Known canonical bounds outrank original UTC; unknown time covers the query
    except where surviving GOOD captures on either side in capture order bound it.

    Ranges merge captures, so even canonical bounds describe an envelope rather
    than continuous deleted observations. Include the final observation's slot
    or report freshness horizon to prevent surviving older state bridging it.
    """
    ranges = expired_coverage(
        connection,
        line_id=scope["line_id"],
        source_id=scope.get("source_id"),
        stream_id=scope.get("stream_id"),
        from_time=start - tail,
        to_time=end,
    )
    windows = []
    for entry in ranges:
        left, right = entry.get("analytical_from"), entry.get("analytical_until")
        if entry.get("time_unknown") or left is None or right is None:
            # Unknown time is bounded only by surviving GOOD neighbours in capture order.
            lower, upper = entry.get("sequence_from"), entry.get("sequence_until")
            left = _time(lower) if lower is not None else start
            right = _time(upper) + tail if upper is not None else end
        else:
            left, right = _time(left), _time(right) + tail
        left, right = max(start, left), min(end, right)
        if left < right:
            windows.append((left, right, entry))
    return windows


def provenance(ranges):
    return {"raw_evidence": dict(RAW_EXPIRED), "retention_expired": ranges} if ranges else {}
