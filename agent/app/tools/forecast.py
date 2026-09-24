"""Warn before customers feel it (M3): forecast threshold breaches from slow trends.

For every (node, alert_type) with enough recent numeric samples, fit a line
(slope, not snapshot). If it is rising steadily toward a threshold, project
when it crosses, with an uncertainty band from the slope's standard error.
Thresholds come from the alert text itself ("projected to breach 95% ...") and
fall back to the policy table sre_agent_ops.metric_thresholds.

Status:
  BREACHED  fitted value already at/over the threshold
  WARN      projected breach within the horizon (the actionable window)
  WATCH     rising, but breach beyond the horizon
  STALE     projected breach time has passed and no fresh samples arrived --
            either it breached silently or the collector stopped; both need eyes
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from ..models import Alert, Node

THRESHOLD_RES = [
    re.compile(r"breach(?:es|ing)?\s+(\d+(?:\.\d+)?)\s*%", re.I),
    re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:capacity\s+)?(?:threshold|limit|ceiling)", re.I),
    re.compile(r"(?:threshold|limit|ceiling)\s*(?:of|at|=)?\s*(\d+(?:\.\d+)?)", re.I),
]


@dataclass(frozen=True)
class ForecastConfig:
    min_points: int = 6
    min_span_minutes: float = 10.0
    min_r2: float = 0.8
    horizon_minutes: float = 60.0
    stale_after_minutes: float = 15.0


@dataclass
class Fit:
    slope: float
    intercept: float
    r2: float
    slope_se: float
    residual_std: float


@dataclass
class Forecast:
    node_id: str
    node_name: str
    node_type: str
    region: str
    service_name: str
    alert_type: str
    samples: int
    first_at: datetime
    last_at: datetime
    current_value: float
    threshold: float
    threshold_source: str
    slope_per_min: float
    r_squared: float
    eta_minutes: float              # from `now` to projected crossing
    eta_low_minutes: float          # fast edge of the band (slope + 2 SE)
    eta_high_minutes: float | None  # slow edge (None if slope - 2 SE <= 0)
    predicted_breach_at: datetime
    data_age_minutes: float
    status: str
    sample_message: str
    explanation: str = ""
    recommended_action: str = ""
    runbook_id: str | None = None
    similar_incidents: list[dict] = field(default_factory=list)


def ols(points: list[tuple[float, float]]) -> Fit | None:
    n = len(points)
    if n < 3:
        return None
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points)
    syy = sum((y - my) ** 2 for _, y in points)
    sxy = sum((x - mx) * (y - my) for x, y in points)
    if sxx == 0 or syy == 0:
        return None
    slope = sxy / sxx
    intercept = my - slope * mx
    sse = sum((y - (intercept + slope * x)) ** 2 for x, y in points)
    resid = math.sqrt(sse / (n - 2))
    return Fit(slope, intercept, (sxy * sxy) / (sxx * syy), resid / math.sqrt(sxx), resid)


def parse_threshold(message: str) -> float | None:
    for rx in THRESHOLD_RES:
        m = rx.search(message or "")
        if m:
            return float(m.group(1))
    return None


def policy_threshold(alert_type: str, policy: dict[str, float]) -> float | None:
    """policy maps a lowercase pattern (substring of alert_type) to a threshold."""
    hits = [(len(p), t) for p, t in policy.items() if p in alert_type.lower()]
    return max(hits)[1] if hits else None


def compute_forecasts(
    alerts: Iterable[Alert],
    nodes: Iterable[Node],
    now: datetime,
    policy: dict[str, float] | None = None,
    cfg: ForecastConfig = ForecastConfig(),
) -> list[Forecast]:
    node_map = {n.node_id: n for n in nodes}
    groups: dict[tuple[str, str], list[Alert]] = defaultdict(list)
    for a in alerts:
        if a.measured_value is not None and a.timestamp <= now:
            groups[(a.node_id, a.alert_type)].append(a)

    out: list[Forecast] = []
    for (node_id, alert_type), group in groups.items():
        if len(group) < cfg.min_points:
            continue
        group.sort(key=lambda a: a.timestamp)
        first, last = group[0].timestamp, group[-1].timestamp
        span = (last - first).total_seconds() / 60.0
        if span < cfg.min_span_minutes:
            continue

        pts = [((a.timestamp - first).total_seconds() / 60.0, float(a.measured_value)) for a in group]
        fit = ols(pts)
        if fit is None or fit.slope <= 0 or fit.r2 < cfg.min_r2:
            continue

        threshold, source = parse_threshold(group[-1].message), "alert message"
        if threshold is None:
            threshold, source = policy_threshold(alert_type, policy or {}), "policy"
        if threshold is None:
            continue

        current = fit.intercept + fit.slope * span  # fitted, not the noisy last reading
        to_cross = (threshold - current) / fit.slope
        predicted = last + timedelta(minutes=to_cross)
        eta_now = (predicted - now).total_seconds() / 60.0
        since_last = (now - last).total_seconds() / 60.0
        fast = fit.slope + 2 * fit.slope_se
        slow = fit.slope - 2 * fit.slope_se

        if current >= threshold:
            status = "BREACHED"
        elif eta_now < 0 or since_last > cfg.stale_after_minutes and eta_now < since_last:
            status = "STALE"
        elif eta_now <= cfg.horizon_minutes:
            status = "WARN"
        else:
            status = "WATCH"

        node = node_map.get(node_id)
        out.append(Forecast(
            node_id=node_id,
            node_name=node.node_name if node else node_id,
            node_type=node.node_type if node else "unknown",
            region=node.region if node else "unknown",
            service_name=group[-1].service_name,
            alert_type=alert_type,
            samples=len(group),
            first_at=first,
            last_at=last,
            current_value=round(current, 3),
            threshold=threshold,
            threshold_source=source,
            slope_per_min=round(fit.slope, 4),
            r_squared=round(fit.r2, 4),
            eta_minutes=round(eta_now, 1),
            eta_low_minutes=round(max(threshold - current, 0) / fast - since_last, 1),
            eta_high_minutes=round(max(threshold - current, 0) / slow - since_last, 1) if slow > 0 else None,
            predicted_breach_at=predicted,
            data_age_minutes=round(since_last, 1),
            status=status,
            sample_message=group[-1].message,
        ))

    order = {"BREACHED": 0, "WARN": 1, "STALE": 2, "WATCH": 3}
    out.sort(key=lambda f: (order[f.status], f.eta_minutes))
    return out


def describe(f: Forecast) -> str:
    """Deterministic explanation; Gemini may add operator-facing prose on top."""
    band = (f"{f.eta_low_minutes:.1f}-{f.eta_high_minutes:.1f}" if f.eta_high_minutes is not None
            else f">{f.eta_low_minutes:.1f}")
    base = (f"{f.alert_type} on {f.node_name} ({f.region}) is at {f.current_value:.1f} and rising "
            f"{f.slope_per_min:+.3f}/min (R2={f.r_squared:.2f}, {f.samples} samples over "
            f"{(f.last_at - f.first_at).total_seconds() / 60:.0f} min). Threshold {f.threshold:g} "
            f"(from {f.threshold_source}) is crossed at {f.predicted_breach_at:%H:%M} UTC")
    if f.status == "STALE":
        return base + f", which has already passed with no samples for {f.data_age_minutes:.0f} min: verify the node and its collector."
    if f.status == "BREACHED":
        return base + ": already at or over the threshold."
    return base + f", {f.eta_minutes:.0f} min from now (band {band} min)."
