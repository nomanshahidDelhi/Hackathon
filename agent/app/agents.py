"""ADK multi-agent graph (Gemini Flash) over the deterministic SRE tools.

    sre_commander (LlmAgent, routes the request)
      +-- incident_response (SequentialAgent)
      |     triage_agent        alert storm -> one incident, root cause vs symptoms
      |     diagnosis_agent     runbook by meaning, why near-misses lose
      |     remediation_agent   fix + rollback, guardrails, sandbox, approval REQUEST
      |     assess (ParallelAgent)
      |        forecast_agent   breaches coming 15-30 min out
      |        impact_agent     revenue at risk, SLA credits
      +-- postmortem_agent      draft postmortem for a given incident

Agents can read, reason, propose and request. They have no tool to approve,
execute, resolve or publish: those are human actions on the REST API / CLI.
Tools do the math and return compact, redacted summaries; heavy objects move
between agents through session state.
"""
from __future__ import annotations

import json
import os
from typing import Any

from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent
from google.adk.tools import ToolContext

from .models import Incident
from .redact import redact_obj

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
_service = None


def service():
    global _service
    if _service is None:
        from .service import SREService
        _service = SREService()
    return _service


def _retry():
    try:
        from google.adk.workflow import RetryConfig
        return RetryConfig(max_attempts=3, initial_delay=1.0, backoff_factor=2.0, max_delay=10.0, jitter=0.2)
    except Exception:  # older ADK without workflow retries
        return None


def _out(d: dict[str, Any]) -> dict[str, Any]:
    return redact_obj(json.loads(json.dumps(d, default=str)))


def _incident(tool_context: ToolContext) -> Incident | None:
    raw = tool_context.state.get("incident")
    return Incident.from_dict(raw) if raw else None


# ---------------------------------------------------------------------------- tools
def triage_alert_storm(tool_context: ToolContext, lookback_minutes: int = 0) -> dict:
    """Cluster recent alerts into incidents and isolate the root cause.

    Args:
        lookback_minutes: window to analyse; 0 widens automatically from 60 min to 24 h.
    """
    res = service().triage(lookback_minutes or None)
    if not res.incidents:
        tool_context.state["incident"] = None
        return _out({"incidents": 0, "alerts_in_window": res.total_alerts, "routine_filtered": res.routine_alerts,
                     "precursor_trends": len(res.precursors)})
    inc = res.incidents[0]
    tool_context.state["incident"] = json.loads(json.dumps(res.to_dict()["incidents"][0]))
    return _out({
        "alerts_in_window": res.total_alerts, "routine_filtered": res.routine_alerts,
        "incidents": len(res.incidents), "precursor_trends": len(res.precursors),
        "top_incident": {
            "alerts_collapsed": inc.alert_count, "regions": inc.regions, "window": [inc.started_at, inc.last_seen_at],
            "root_cause": {"node": inc.root_cause.node_name, "node_type": inc.root_cause.node_type,
                           "service": inc.root_cause.service_name, "alert_type": inc.root_cause.alert_type,
                           "evidence": inc.root_cause.sample_message, "score": inc.root_cause.score},
            "confidence": inc.confidence,
            "symptom_services": inc.symptom_services,
            "runner_up_candidates": [{"node": c.node_name, "alert_type": c.alert_type, "score": c.score,
                                      "components": c.components} for c in inc.candidates[1:4]],
        },
    })


def _ensure_incident_id(tool_context: ToolContext) -> str | None:
    """Downstream steps must not depend on the LLM remembering to open the incident."""
    incident_id = tool_context.state.get("incident_id")
    if incident_id is None and (inc := _incident(tool_context)) is not None:
        incident_id, _ = service().open_incident(inc)
        tool_context.state["incident_id"] = incident_id
    return incident_id


def open_incident(tool_context: ToolContext) -> dict:
    """Record the triaged incident (or attach to the open one) in the incident mart."""
    inc = _incident(tool_context)
    if inc is None:
        return {"skipped": "no incident was triaged"}
    incident_id, created = service().open_incident(inc)
    tool_context.state["incident_id"] = incident_id
    return {"incident_id": incident_id, "created": created, "status": "INVESTIGATING"}


def find_runbook(tool_context: ToolContext) -> dict:
    """Retrieve runbooks by meaning for the ROOT CAUSE and rank them on evidence."""
    inc = _incident(tool_context)
    if inc is None:
        return {"skipped": "no incident"}
    r = service().find_runbook(inc)
    tool_context.state["runbook_id"] = r.best.runbook_id if r.best else None
    return _out({"method": r.method, "degraded": r.errors, "query": r.query,
                 "ranked": [{"runbook_id": h.runbook_id, "title": h.title, "failure_signature": h.failure_signature,
                             "score": h.score, "evidence": h.components} for h in r.hits]})


def propose_and_request_approval(tool_context: ToolContext) -> dict:
    """Draft an idempotent fix + rollback, check guardrails, dry-run it in the sandbox,
    and if it passes open a PENDING approval for a human. Never executes anything."""
    inc = _incident(tool_context)
    if inc is None:
        return {"skipped": "no incident"}
    svc = service()
    retrieval, p = svc.propose(inc)
    out: dict[str, Any] = {
        "status": p.status, "runbook": p.runbook_id, "reason": p.reason,
        "attempts": [{"n": a.n, "source": a.source, "guardrails": a.guardrails, "sandbox": a.sandbox,
                      "accepted": a.accepted} for a in p.attempts],
        "selection_review": p.selection,
    }
    if p.draft:
        out["script"] = p.draft.script
        out["rollback"] = p.draft.rollback
        out["risk"] = p.guardrails.risk if p.guardrails else None
    incident_id = _ensure_incident_id(tool_context)
    if p.status == "READY" and incident_id:
        approval_id = svc.request_approval(incident_id, p)
        tool_context.state["approval_id"] = approval_id
        out["approval"] = {"approval_id": approval_id, "status": "PENDING",
                           "note": "Execution requires a named human to approve in the console."}
    return _out(out)


def forecast_breaches(tool_context: ToolContext, horizon_minutes: int = 60) -> dict:
    """Find steadily rising signals and project when they cross their threshold."""
    fc = service().forecasts(horizon=float(horizon_minutes))
    tool_context.state["forecast_count"] = len(fc)
    return _out({"forecasts": [{
        "status": f.status, "node": f.node_name, "region": f.region, "signal": f.alert_type,
        "current": f.current_value, "threshold": f.threshold, "slope_per_min": f.slope_per_min,
        "r_squared": f.r_squared, "eta_minutes": f.eta_minutes, "band": [f.eta_low_minutes, f.eta_high_minutes],
        "why": f.explanation, "action": f.recommended_action} for f in fc[:8]]})


def business_impact(tool_context: ToolContext) -> dict:
    """Revenue at risk and SLA credits for the open incident, computed in the warehouse."""
    incident_id = _ensure_incident_id(tool_context)
    if not incident_id:
        return {"skipped": "no incident"}
    imp = service().impact(incident_id)
    d = imp.to_dict()
    d["customers"] = d["customers"][:10]
    return _out(d)


def draft_incident_postmortem(tool_context: ToolContext, incident_id: str) -> dict:
    """Write a DRAFT postmortem row for an incident (published only by a human once RESOLVED).

    Args:
        incident_id: e.g. inc-5019
    """
    d, published = service().write_postmortem(incident_id, publish=False)
    return _out({"postmortem_id": d.postmortem_id, "source": d.source, "published": published,
                 "root_cause": d.root_cause, "impact_summary": d.impact_summary,
                 "downtime_minutes": d.downtime_minutes, "contributing_factors": d.contributing_factors,
                 "action_items": d.action_items, "notes": d.notes})


# ---------------------------------------------------------------------------- graph
COMMON = (
    "You are part of an SRE incident-response team for a telecom estate. Use your tools; never invent "
    "numbers, nodes, customers or runbooks. Be brief and concrete: operators read this during an outage. "
    "You cannot approve, execute, resolve or publish anything; say so if asked, and point to the console."
)


def build_agents() -> LlmAgent:
    retry = _retry()
    kw = {"model": MODEL, **({"retry_config": retry} if retry else {})}

    triage = LlmAgent(
        name="triage_agent", **kw,
        description="Turns an alert storm into one incident and names the root cause.",
        instruction=COMMON + " Call triage_alert_storm. If it finds an incident, call open_incident. Report: "
        "how many alerts collapsed into one incident, the root cause (component + failure mode + evidence), "
        "which services are only symptoms and why, and how much routine noise was filtered.",
        tools=[triage_alert_storm, open_incident], output_key="triage_summary",
    )
    diagnosis = LlmAgent(
        name="diagnosis_agent", **kw,
        description="Finds the runbook that fixes the root cause.",
        instruction=COMMON + " If there is no incident, say so and stop. Otherwise call find_runbook and "
        "explain which runbook fits the root cause, citing its evidence scores, and why the runbooks that "
        "match downstream symptoms were ranked lower.",
        tools=[find_runbook], output_key="diagnosis_summary",
    )
    remediation = LlmAgent(
        name="remediation_agent", **kw,
        description="Drafts a safe fix and asks a human to approve it.",
        instruction=COMMON + " If there is no incident, stop. Otherwise call propose_and_request_approval. "
        "Summarise the fix and rollback, the guardrail risk, the sandbox result (idempotent?), and the "
        "approval id. State clearly that nothing runs until a human approves it in the console.",
        tools=[propose_and_request_approval], output_key="remediation_summary",
    )
    forecast = LlmAgent(
        name="forecast_agent", **kw,
        description="Warns about breaches before customers feel them.",
        instruction=COMMON + " Call forecast_breaches. Report WARN items first with ETA and band, the "
        "threshold, the slope, and the preventive action. Mention STALE items as needing verification.",
        tools=[forecast_breaches], output_key="forecast_summary",
    )
    impact = LlmAgent(
        name="impact_agent", **kw,
        description="Quantifies revenue at risk and SLA credits.",
        instruction=COMMON + " Call business_impact. Report downtime, revenue at risk, SLA credits by tier, "
        "tiers breaching their restoration target, and the top affected accounts. Note that credit rates "
        "come from the configured SLA policy.",
        tools=[business_impact], output_key="impact_summary",
    )
    response = SequentialAgent(
        name="incident_response",
        description="Full response: triage, diagnosis, remediation proposal, forecast and impact.",
        sub_agents=[triage, diagnosis, remediation,
                    ParallelAgent(name="assess", sub_agents=[forecast, impact])],
    )
    postmortem = LlmAgent(
        name="postmortem_agent", **kw,
        description="Drafts the executive postmortem for a named incident.",
        instruction=COMMON + " Call draft_incident_postmortem with the incident id the user gave. Present "
        "root cause, impact, downtime, contributing factors and action items. It is saved as a draft; a "
        "human publishes it once the incident is RESOLVED.",
        tools=[draft_incident_postmortem],
    )
    return LlmAgent(
        name="sre_commander", **kw,
        description="Routes SRE requests.",
        instruction=COMMON + " Route the request: investigating alerts, an outage, or 'what is happening' "
        "-> transfer to incident_response. Writing a postmortem for an incident id -> transfer to "
        "postmortem_agent. Otherwise answer briefly.",
        sub_agents=[response, postmortem],
    )


root_agent = None


def get_root_agent() -> LlmAgent:
    global root_agent
    if root_agent is None:
        root_agent = build_agents()
    return root_agent
