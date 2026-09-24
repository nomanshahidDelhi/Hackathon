"""M3 forecasting CLI: flag likely breaches ahead of impact, and explain why.

    python -m agent.app.predict                     # analyse the last 2 h, print
    python -m agent.app.predict --persist           # also write sre_agent_ops.forecasts
    python -m agent.app.predict --horizon 30 --no-llm
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from collections import defaultdict
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from .config import load_settings
from .llm import Gemini, LLMUnavailable
from .logging_setup import setup_logging
from .redact import redact
from .tools.bq import Warehouse
from .tools.forecast import Forecast, ForecastConfig, compute_forecasts, describe
from .tools.retrieval import retrieve_query, signal_query

EXPLAIN_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "explanation": {"type": "STRING"},
        "recommended_action": {"type": "STRING"},
    },
    "required": ["explanation", "recommended_action"],
}


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 2}


def similar_incidents(f: Forecast, past: list[dict[str, Any]], k: int = 2) -> list[dict[str, Any]]:
    """Past incidents whose title shares the most vocabulary with this signal."""
    want = _tokens(f"{f.alert_type} {f.service_name} {f.node_type}")
    scored = []
    for inc in past:
        overlap = len(want & _tokens(inc["title"]))
        if overlap:
            scored.append((overlap, inc))
    scored.sort(key=lambda s: -s[0])
    return [{
        "incident_id": i["incident_id"], "title": i["title"], "status": i["status"],
        "remediations": [f"{r['runbook_id']}:{r['status']}" for r in (i.get("remediations") or [])],
    } for _, i in scored[:k]]


def enrich(forecasts: list[Forecast], wh: Warehouse, llm: Gemini | None) -> None:
    """One retrieval + explanation per alert_type (the fleet shares a cause), applied to each node."""
    try:
        past = wh.past_incidents()
    except Exception:
        past = []
    by_type: dict[str, list[Forecast]] = defaultdict(list)
    for f in forecasts:
        by_type[f.alert_type].append(f)

    for alert_type, group in by_type.items():
        lead = group[0]
        q = signal_query(alert_type, lead.service_name, lead.node_type, lead.sample_message)
        retrieval = retrieve_query(q, alert_type, set(), wh, embed=llm.embed_query if llm else None)
        best = retrieval.best
        runbook = None
        if best:
            runbook = next((r for r in wh.fetch_runbooks() if r.runbook_id == best.runbook_id), None)
        similar = similar_incidents(lead, past)

        text, action = describe(lead), (f"Follow {best.runbook_id}: {best.title}" if best else "Investigate the trend")
        if llm is not None and lead.status in ("WARN", "BREACHED", "STALE"):
            facts = {
                "signal": alert_type, "status": lead.status, "affected_nodes": len(group),
                "nodes": [{"node": f.node_name, "region": f.region, "current": f.current_value,
                           "slope_per_min": f.slope_per_min, "eta_minutes": f.eta_minutes,
                           "band": [f.eta_low_minutes, f.eta_high_minutes]} for f in group[:6]],
                "threshold": lead.threshold, "threshold_source": lead.threshold_source,
                "r_squared": lead.r_squared, "data_age_minutes": lead.data_age_minutes,
                "evidence": redact(lead.sample_message),
                "runbook": {"id": runbook.runbook_id, "title": runbook.title,
                            "steps": runbook.remediation_steps[:900]} if runbook else None,
                "similar_past_incidents": similar,
            }
            prompt = (
                "You are an SRE on call. Using ONLY these facts, explain in 2-3 sentences why this is "
                "likely to become an outage and when, citing the numbers (slope, threshold, ETA and its "
                "band). Then give one concrete preventive action from the runbook steps. If status is "
                "STALE, say the projected breach time passed with no fresh data and what to verify.\n\n"
                f"{json.dumps(facts, indent=2, default=str)}"
            )
            try:
                out = llm.generate_json(prompt, EXPLAIN_SCHEMA)
                text = f"{describe(lead)}\n{out['explanation']}"
                action = out["recommended_action"]
            except LLMUnavailable:
                pass

        for f in group:
            f.runbook_id = best.runbook_id if best else None
            f.similar_incidents = similar
            f.explanation = redact(describe(f) if f is not lead else text)
            f.recommended_action = redact(action)


def render(forecasts: list[Forecast], window_min: int) -> str:
    if not forecasts:
        return f"no rising trends toward a threshold in the last {window_min} min"
    out = [f"{len(forecasts)} forecast(s) over the last {window_min} min"]
    for f in forecasts:
        hi = f"{f.eta_high_minutes:.1f}" if f.eta_high_minutes is not None else "?"
        out.append(f"  {f.status:8} {f.node_id:<18} {f.alert_type:<26} now={f.current_value:7.2f} "
                   f"thr={f.threshold:g} slope={f.slope_per_min:+.3f}/min r2={f.r_squared:.3f} "
                   f"eta={f.eta_minutes:6.1f} min [{f.eta_low_minutes:.1f}, {hi}] age={f.data_age_minutes:.0f}m")
    lead = forecasts[0]
    out += ["", "why:", lead.explanation, "", f"action: {lead.recommended_action}"]
    if lead.similar_incidents:
        out.append("similar: " + "; ".join(f"{s['incident_id']} {s['title']} {s['remediations']}"
                                            for s in lead.similar_incidents))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lookback", type=int, default=120, help="minutes of history to fit")
    ap.add_argument("--horizon", type=float, default=60.0, help="WARN if breach is within this many minutes")
    ap.add_argument("--persist", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    setup_logging()

    wh = Warehouse(load_settings())
    now = wh.now()
    alerts = wh.fetch_alerts(now - timedelta(minutes=args.lookback), now)
    try:
        policy = wh.metric_thresholds()
    except Exception:
        policy = {}
    forecasts = compute_forecasts(alerts, wh.fetch_nodes(), now, policy,
                                  ForecastConfig(horizon_minutes=args.horizon))
    llm = None if args.no_llm else Gemini(wh.s.project)
    if forecasts:
        enrich(forecasts, wh, llm)

    if args.json:
        print(json.dumps([asdict(f) for f in forecasts], indent=2, default=str))
    else:
        print(render(forecasts, args.lookback))

    if args.persist and forecasts:
        wh.insert_forecasts([{**asdict(f), "forecast_id": f"fc-{uuid.uuid4().hex[:12]}"} for f in forecasts])
        print(f"\nwrote {len(forecasts)} rows to sre_agent_ops.forecasts", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
