"""M1 triage: alert storm -> incident with a named root cause.

    python -m agent.app.triage                     # analyse, print, write nothing
    python -m agent.app.triage --persist           # also open/update the incident in BigQuery
    python -m agent.app.triage --lookback 60 --json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import timedelta

from .config import Settings, load_settings
from .models import TriageResult
from .tools.bq import Warehouse
from .tools.clustering import TriageConfig, triage
from .tools.incident_store import IncidentStore

# Widen the window until something is found: seed data is relative to load
# time, so how far back the storm sits depends on when you run this.
LOOKBACK_LADDER_MIN = (60, 180, 360, 720, 1440)
BASELINE_DAYS = 7


def run_triage(
    wh: Warehouse,
    lookback_min: int | None = None,
    cfg: TriageConfig = TriageConfig(),
) -> TriageResult:
    now = wh.now()
    nodes = wh.fetch_nodes()
    ladder = (lookback_min,) if lookback_min else LOOKBACK_LADDER_MIN
    result = None
    for minutes in ladder:
        start = now - timedelta(minutes=minutes)
        alerts = wh.fetch_alerts(start, now)
        baseline = wh.fetch_baseline(start, BASELINE_DAYS)
        result = triage(alerts, nodes, baseline, BASELINE_DAYS * 1440.0, start, now, cfg)
        if result.incidents:
            break
    return result


def render(result: TriageResult) -> str:
    lines = [
        f"window {result.window_start:%Y-%m-%d %H:%M:%S} -> {result.window_end:%H:%M:%S} UTC",
        f"alerts in window: {result.total_alerts}  routine/noise: {result.routine_alerts}  "
        f"incidents: {len(result.incidents)}  precursor trends: {len(result.precursors)}",
    ]
    for inc in result.incidents:
        rc = inc.root_cause
        lines += [
            "",
            f"[{inc.cluster_id}] {inc.alert_count} alerts -> 1 incident  "
            f"({inc.max_severity}, {', '.join(inc.regions)}, {inc.started_at:%H:%M:%S}-{inc.last_seen_at:%H:%M:%S})",
            f"  root cause : {rc.alert_type} on {rc.node_name} [{rc.node_id}, {rc.node_type}] "
            f"service={rc.service_name} score={rc.score} confidence={inc.confidence}",
            f"  evidence   : {rc.sample_message}",
            f"  symptoms   : {', '.join(inc.symptom_services) or '-'}",
            "  candidates :",
        ]
        for c in inc.candidates:
            comps = " ".join(f"{k}={v}" for k, v in c.components.items())
            lines.append(f"    {c.score:>7.3f}  {c.node_id:<18} {c.alert_type:<26} {comps}")
    if result.precursors:
        lines += ["", "precursor trends (-> forecasting):"]
        for s in result.precursors:
            lines.append(
                f"  {s.node_id:<18} {s.alert_type:<26} n={s.count:<4} last={s.last_value} "
                f"slope={s.slope_per_min:+.3f}/min r2={s.r_squared:.2f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lookback", type=int, help="minutes; default widens 60->1440 until an incident is found")
    ap.add_argument("--persist", action="store_true", help="open/update the top incident in BigQuery")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    settings: Settings = load_settings()
    wh = Warehouse(settings)
    result = run_triage(wh, args.lookback)

    print(json.dumps(result.to_dict(), indent=2) if args.json else render(result))

    if args.persist and result.incidents:
        incident_id, created = IncidentStore(wh).upsert(result.incidents[0])
        print(f"\n{'opened' if created else 'updated'} {incident_id}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
