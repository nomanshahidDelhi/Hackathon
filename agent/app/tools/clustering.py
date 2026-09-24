"""Turn a window of alerts into incidents (M1).

Pipeline (all deterministic; the LLM only explains the result):
  1. Group window alerts by signature (node_id, alert_type).
  2. Noise filter: compare each signature's count in the window with its own
     7-day baseline (Poisson tail). Routine chatter drops out regardless of
     severity; novel or bursting signatures stay.
  3. Pattern: a signature whose values climb steadily over tens of minutes is
     a *trend* (a precursor for forecasting, not part of a storm); the rest are
     *bursts*.
  4. Cluster bursts that overlap in time and share a region or a service.
  5. Rank root-cause candidates inside each cluster: earliest onset, deepest
     tier, how many other involved nodes depend on it, saturation evidence,
     and whether its own messages blame something upstream.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from ..models import SEVERITY_RANK, Alert, Incident, Node, RootCandidate, SignatureStats, TriageResult
from .topology import MAX_LAYER, Topology, layer


@dataclass(frozen=True)
class TriageConfig:
    min_count: int = 3             # fewer alerts than this is never a storm signature
    surprise_p: float = 1e-3       # Poisson tail below this = anomalous vs baseline
    min_expected: float = 0.1      # floor on expected count for never-seen signatures
    merge_gap_s: float = 120.0     # bursts closer than this belong together
    trend_min_minutes: float = 15.0
    trend_min_r2: float = 0.7
    trend_min_points: int = 6


# Messages that say "I am full" point at a cause; messages that say "my
# dependency failed" point away from the node that emitted them.
SATURATION_RE = re.compile(
    r"exhaust|saturat|out of memory|\boom|no space|\bfull\b|(\b\d+)/\1\b|100(\.0+)?\s*%",
    re.IGNORECASE,
)
# No trailing \b: "upstream_connect_error" must match (underscore is a word char).
BLAME_RE = re.compile(r"\bupstream|\bdependency\b|downstream of", re.IGNORECASE)

WEIGHTS = {
    "onset": 0.30,
    "coverage": 0.25,
    "depth": 0.15,
    "saturation": 0.20,
    "degraded": 0.05,
    "blames_upstream": -0.20,
}


# ---------------------------------------------------------------------------
# statistics helpers
# ---------------------------------------------------------------------------
def poisson_sf(k: int, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam)."""
    if k <= 0:
        return 1.0
    if k <= lam:  # lower tail is small here, so 1 - cdf is accurate
        term = math.exp(-lam)
        cdf = term
        for i in range(1, k):
            term *= lam / i
            cdf += term
        return max(0.0, 1.0 - cdf)
    # Sum the upper tail directly (avoids 1 - cdf rounding to ~1e-16).
    term = math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1)) if lam > 0 else 0.0
    total, i = 0.0, k
    while term > total * 1e-17 and term > 0:
        total += term
        i += 1
        term *= lam / i
    return min(1.0, total)


def linear_fit(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Least-squares slope and R^2 for (x, y) points. Degenerate input -> (0, 0)."""
    n = len(points)
    if n < 2:
        return 0.0, 0.0
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points)
    syy = sum((y - my) ** 2 for _, y in points)
    sxy = sum((x - mx) * (y - my) for x, y in points)
    if sxx == 0 or syy == 0:
        return 0.0, 0.0
    slope = sxy / sxx
    return slope, (sxy * sxy) / (sxx * syy)


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------
def signature_stats(
    alerts: list[Alert],
    nodes: dict[str, Node],
    baseline_counts: dict[tuple[str, str], int],
    baseline_minutes: float,
    window_minutes: float,
    cfg: TriageConfig,
) -> list[SignatureStats]:
    groups: dict[tuple[str, str], list[Alert]] = defaultdict(list)
    for a in alerts:
        groups[(a.node_id, a.alert_type)].append(a)

    out: list[SignatureStats] = []
    for (node_id, alert_type), group in groups.items():
        group.sort(key=lambda a: a.timestamp)
        count = len(group)
        base = baseline_counts.get((node_id, alert_type), 0)
        expected = max(base / baseline_minutes * window_minutes if baseline_minutes > 0 else 0.0, cfg.min_expected)
        p = poisson_sf(count, expected)

        t0 = group[0].timestamp
        pts = [((a.timestamp - t0).total_seconds() / 60.0, a.measured_value)
               for a in group if a.measured_value is not None]
        slope, r2 = linear_fit(pts)
        duration_min = (group[-1].timestamp - t0).total_seconds() / 60.0

        if count < cfg.min_count or p >= cfg.surprise_p:
            pattern = "routine"
        elif (duration_min >= cfg.trend_min_minutes and len(pts) >= cfg.trend_min_points
              and r2 >= cfg.trend_min_r2 and slope != 0):
            pattern = "trend"
        else:
            pattern = "burst"

        node = nodes.get(node_id)
        out.append(SignatureStats(
            node_id=node_id,
            alert_type=alert_type,
            region=node.region if node else "unknown",
            services=sorted({a.service_name for a in group}),
            alert_ids=[a.alert_id for a in group],
            count=count,
            first_at=group[0].timestamp,
            last_at=group[-1].timestamp,
            max_severity=max((a.severity for a in group), key=lambda s: SEVERITY_RANK.get(s, 0)),
            baseline_count=base,
            expected=round(expected, 4),
            surprise_p=p,
            slope_per_min=slope,
            r_squared=r2,
            last_value=group[-1].measured_value,
            pattern=pattern,
        ))
    return out


def cluster_bursts(sigs: list[SignatureStats], cfg: TriageConfig) -> list[list[SignatureStats]]:
    """Union-find over burst signatures that overlap in time and share a region or service."""
    parent = list(range(len(sigs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(sigs):
        for j in range(i + 1, len(sigs)):
            b = sigs[j]
            gap = max((b.first_at - a.last_at).total_seconds(), (a.first_at - b.last_at).total_seconds())
            if gap > cfg.merge_gap_s:
                continue
            if a.region == b.region or set(a.services) & set(b.services):
                parent[find(i)] = find(j)

    groups: dict[int, list[SignatureStats]] = defaultdict(list)
    for i, s in enumerate(sigs):
        groups[find(i)].append(s)
    return list(groups.values())


def rank_root_causes(
    cluster: list[SignatureStats],
    alerts_by_id: dict[str, Alert],
    topo: Topology,
) -> list[RootCandidate]:
    cluster_alerts = [alerts_by_id[i] for s in cluster for i in s.alert_ids]
    graph = topo.with_alert_evidence(cluster_alerts)

    by_node: dict[str, list[SignatureStats]] = defaultdict(list)
    for s in cluster:
        by_node[s.node_id].append(s)
    involved = set(by_node)

    t_min = min(s.first_at for s in cluster)
    t_max = max(s.first_at for s in cluster)
    span = max((t_max - t_min).total_seconds(), 1.0)

    candidates: list[RootCandidate] = []
    for node_id, node_sigs in by_node.items():
        node = topo.nodes.get(node_id)
        node_alerts = [alerts_by_id[i] for s in node_sigs for i in s.alert_ids]
        first = min(s.first_at for s in node_sigs)
        lead = min(node_sigs, key=lambda s: s.first_at)
        sample = next(a for a in node_alerts if a.alert_id == lead.alert_ids[0])

        others = len(involved) - 1
        comp = {
            "onset": 1.0 - (first - t_min).total_seconds() / span,
            "coverage": len(graph.dependents(node_id, within=involved)) / others if others else 1.0,
            "depth": (layer(node) / MAX_LAYER) if node else 0.5,
            "saturation": sum(bool(SATURATION_RE.search(a.message)) for a in node_alerts) / len(node_alerts),
            "degraded": 1.0 if node and node.status == "degraded" else 0.0,
            "blames_upstream": sum(bool(BLAME_RE.search(a.message)) for a in node_alerts) / len(node_alerts),
        }
        score = sum(WEIGHTS[k] * v for k, v in comp.items())
        candidates.append(RootCandidate(
            node_id=node_id,
            node_name=node.node_name if node else node_id,
            node_type=node.node_type if node else "unknown",
            region=node.region if node else lead.region,
            service_name=sample.service_name,
            alert_type=lead.alert_type,
            first_at=first,
            sample_message=sample.message,
            score=round(score, 4),
            components={k: round(v, 3) for k, v in comp.items()},
        ))
    candidates.sort(key=lambda c: (-c.score, c.first_at))
    return candidates


def build_incident(
    idx: int,
    cluster: list[SignatureStats],
    alerts_by_id: dict[str, Alert],
    topo: Topology,
) -> Incident:
    candidates = rank_root_causes(cluster, alerts_by_id, topo)
    root = candidates[0]
    margin = root.score - candidates[1].score if len(candidates) > 1 else root.score
    alert_ids = sorted(i for s in cluster for i in s.alert_ids)
    services = sorted({svc for s in cluster for svc in s.services})
    return Incident(
        cluster_id=f"cluster-{idx + 1}",
        alert_ids=alert_ids,
        alert_count=len(alert_ids),
        started_at=min(s.first_at for s in cluster),
        last_seen_at=max(s.last_at for s in cluster),
        regions=sorted({s.region for s in cluster}),
        services=services,
        nodes=sorted({s.node_id for s in cluster}),
        signatures=[
            {"node_id": s.node_id, "alert_type": s.alert_type, "count": s.count,
             "first_at": s.first_at, "max_severity": s.max_severity}
            for s in sorted(cluster, key=lambda s: s.first_at)
        ],
        root_cause=root,
        candidates=candidates[:5],
        confidence=round(min(1.0, max(0.0, margin) / 0.3), 3),
        symptom_services=[s for s in services if s != root.service_name],
        max_severity=max((s.max_severity for s in cluster), key=lambda v: SEVERITY_RANK.get(v, 0)),
    )


def triage(
    alerts: Iterable[Alert],
    nodes: Iterable[Node],
    baseline_counts: dict[tuple[str, str], int],
    baseline_minutes: float,
    window_start: datetime,
    window_end: datetime,
    cfg: TriageConfig = TriageConfig(),
) -> TriageResult:
    alerts = [a for a in alerts if window_start <= a.timestamp <= window_end]
    nodes = list(nodes)
    node_map = {n.node_id: n for n in nodes}
    window_minutes = (window_end - window_start).total_seconds() / 60.0

    sigs = signature_stats(alerts, node_map, baseline_counts, baseline_minutes, window_minutes, cfg)
    bursts = [s for s in sigs if s.pattern == "burst"]
    trends = sorted((s for s in sigs if s.pattern == "trend"), key=lambda s: -s.slope_per_min)

    node_services: dict[str, set[str]] = defaultdict(set)
    for a in alerts:
        node_services[a.node_id].add(a.service_name)
    topo = Topology(nodes, node_services)

    alerts_by_id = {a.alert_id: a for a in alerts}
    clusters = cluster_bursts(bursts, cfg)
    incidents = [build_incident(i, c, alerts_by_id, topo) for i, c in enumerate(clusters)]
    incidents.sort(key=lambda inc: (-SEVERITY_RANK.get(inc.max_severity, 0), -inc.alert_count))
    for i, inc in enumerate(incidents):
        inc.cluster_id = f"cluster-{i + 1}"

    return TriageResult(
        window_start=window_start,
        window_end=window_end,
        total_alerts=len(alerts),
        routine_alerts=sum(s.count for s in sigs if s.pattern == "routine"),
        incidents=incidents,
        precursors=trends,
    )
