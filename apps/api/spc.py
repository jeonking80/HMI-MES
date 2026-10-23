"""Deterministic Xbar-R statistics. This module performs no I/O or ML inference."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import fsum, isfinite

A2, D3, D4, D2 = 0.577, 0.0, 2.114, 2.326
OFFSETS = (0, 12, 24, 36, 48)
RULES_VERSION = "western-electric-1-4-v1"


def z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class SPCSlot:
    at: datetime
    value: float | None
    quality: str = "GOOD"
    # Includes gateway, generation and clock epoch. Never combine independent clocks.
    epoch: str = ""


def western_electric(means: list[float], center: float, sigma: float) -> list[list[str]]:
    """Mark the final point of each WE window; equality is inside a zone."""
    result = []
    for index, value in enumerate(means):
        rules = []
        if abs(value - center) > 3 * sigma:
            rules.append("WE1")
        for length, threshold, needed, rule in (
            (3, 2, 2, "WE2"),
            (5, 1, 4, "WE3"),
            (8, 0, 8, "WE4"),
        ):
            window = means[max(0, index - length + 1) : index + 1]
            if len(window) == length and any(
                sum(sign * (point - center) > threshold * sigma for point in window) >= needed
                for sign in (-1, 1)
            ):
                rules.append(rule)
        result.append(rules)
    return result


def regression_warning(points, *, now, spec):
    """Recent UTC minute means, with a future crossing measured from the fitted line."""
    usable = [
        (at, value)
        for at, value in points
        if now - timedelta(minutes=15) <= at < now and isfinite(value)
    ]
    empty = dict(
        method="linear-regression-15min", slope_per_minute=None, r2=None, projected_cross=None
    )
    if len(usable) < 2:
        return empty
    origin = usable[0][0]
    xs = [(at - origin).total_seconds() / 60 for at, _ in usable]
    scale = max(abs(value) for _, value in usable) or 1.0
    ys = [value / scale for _, value in usable]
    mx, my = fsum(xs) / len(xs), fsum(ys) / len(ys)
    xx = fsum((x - mx) ** 2 for x in xs)
    yy = fsum((y - my) ** 2 for y in ys)
    if xx == 0:
        return empty
    xy = fsum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    normalized_slope = xy / xx
    slope = normalized_slope * scale
    if not isfinite(slope):
        return empty
    r2 = min(1.0, max(0.0, xy * xy / (xx * yy))) if yy else 0.0
    result = dict(empty, slope_per_minute=slope, r2=r2)
    if len(usable) < 10 or r2 < 0.6 or not spec or slope == 0:
        return result
    limit = "usl" if slope > 0 else "lsl"
    crossing = (spec[limit] / scale - my) / normalized_slope + mx
    current = (now - origin).total_seconds() / 60
    if isfinite(crossing) and crossing > current:
        try:
            result["projected_cross"] = dict(
                limit=limit.upper(), at=z(origin + timedelta(minutes=crossing))
            )
        except OverflowError:
            pass
    return result


def calculate_spc(*, slots, start, end, now, tag_id, unit, spec=None):
    """Use complete UTC minutes in [start,end); first 20 eligible groups are Phase I.

    Inputs are already selected 1-second slots. Missing slots remain missing. One
    canonical epoch is selected by the earliest otherwise complete subgroup; other
    epochs are excluded rather than pooled into a process distribution.
    """
    if any(at.tzinfo is None for at in (start, end, now)) or start > end:
        raise ValueError("SPC boundaries must be ordered and timezone aware")
    selected = {}
    by_minute = defaultdict(list)
    for slot in slots:
        if slot.at in selected:
            raise ValueError("SPC requires unique selected second slots")
        selected[slot.at] = slot
        by_minute[slot.at.replace(second=0, microsecond=0)].append(slot)
    reasons = Counter()
    groups, trend = [], []
    epoch = None
    minute = start.astimezone(UTC).replace(second=0, microsecond=0)
    if minute < start:
        minute += timedelta(minutes=1)
    while minute + timedelta(minutes=1) <= end:
        chosen = [selected.get(minute + timedelta(seconds=s)) for s in OFFSETS]
        invalid = set()
        for slot in chosen:
            if slot is None:
                invalid.add("MISSING")
            elif slot.quality != "GOOD":
                invalid.add(slot.quality)
            elif slot.value is None or not isfinite(slot.value):
                invalid.add("BAD")
        epochs = {slot.epoch for slot in chosen if slot is not None}
        if len(epochs) > 1 or (epoch is not None and epochs and epochs != {epoch}):
            invalid.add("TIME_UNKNOWN")
        if not invalid and not isfinite(
            max(s.value for s in chosen) - min(s.value for s in chosen)
        ):
            invalid.add("BAD")
        if invalid:
            # One primary reason per excluded minute, stable precedence.
            reason = next(
                (r for r in ("TIME_UNKNOWN", "BAD", "UNCERTAIN", "MISSING") if r in invalid),
                sorted(invalid)[0],
            )
            reasons[reason] += 1
        else:
            if epoch is None:
                epoch = chosen[0].epoch
            values = [slot.value for slot in chosen]
            groups.append(
                dict(
                    t=z(minute),
                    mean=fsum(v / 5 for v in values),
                    range=max(values) - min(values),
                    n=5,
                    violations=[],
                )
            )
        minute += timedelta(minutes=1)
    # Regression does not require complete offset subgroups: use the same epoch
    # as Phase I, or the earliest valid minute's sole epoch when Phase I is absent.
    trend_epoch = epoch
    for minute, minute_slots in sorted(by_minute.items()):
        if not (start <= minute and minute + timedelta(minutes=1) <= end):
            continue
        epochs = {slot.epoch for slot in minute_slots}
        valid = [
            slot
            for slot in minute_slots
            if slot.quality == "GOOD" and slot.value is not None and isfinite(slot.value)
        ]
        if len(epochs) != 1 or not valid:
            continue
        if trend_epoch is None:
            trend_epoch = valid[0].epoch
        values = [slot.value for slot in valid if slot.epoch == trend_epoch]
        if values:
            trend.append((minute, fsum(v / len(values) for v in values)))
    limits = baseline = capability = None
    if len(groups) >= 20:
        first = groups[:20]
        center = fsum(g["mean"] / 20 for g in first)
        rbar = fsum(g["range"] / 20 for g in first)
        sigma = A2 * rbar / 3
        limits = dict(
            xbar=dict(ucl=center + A2 * rbar, cl=center, lcl=center - A2 * rbar),
            range=dict(ucl=D4 * rbar, cl=rbar, lcl=D3 * rbar),
        )
        baseline = {
            "from": first[0]["t"],
            "to": z(datetime.fromisoformat(first[-1]["t"]) + timedelta(minutes=1)),
            "subgroups": 20,
        }
        violations = western_electric([g["mean"] for g in groups], center, sigma)
        for group, rules in zip(groups, violations, strict=True):
            group["violations"] = rules
        if spec and len(groups) >= 25:
            # Capability uses all eligible subgroups, estimating within sigma by Rbar/d2.
            within = fsum(g["range"] / len(groups) for g in groups) / D2
            mean = fsum(g["mean"] / len(groups) for g in groups)
            if within > 0:
                capability = dict(
                    cp=(spec["usl"] - spec["lsl"]) / (6 * within),
                    cpk=min(spec["usl"] - mean, mean - spec["lsl"]) / (3 * within),
                    subgroups=len(groups),
                )
    if limits and any(not isfinite(value) for chart in limits.values() for value in chart.values()):
        limits = baseline = capability = None
    if capability and any(not isfinite(value) for value in capability.values()):
        capability = None
    return dict(
        tag_id=tag_id,
        unit=unit,
        method="statistical",
        subgroup_size=5,
        rules_version=RULES_VERSION,
        status="OK" if limits else "INSUFFICIENT_DATA",
        baseline=baseline,
        limits=limits,
        spec=spec,
        capability=capability,
        subgroups=groups,
        excluded=dict(
            count=sum(reasons.values()),
            reasons={key: reasons[key] for key in ("MISSING", "BAD", "TIME_UNKNOWN", "UNCERTAIN")},
        ),
        trend_warning=regression_warning(trend, now=now, spec=spec),
        generated_at=z(now),
        baseline_basis="first-20-complete-subgroups-in-retained-snapshot",
    )
