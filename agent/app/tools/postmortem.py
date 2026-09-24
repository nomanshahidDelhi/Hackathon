"""Write the executive postmortem back to sre_incident_mart.incident_postmortems (M4).

Division of labour:
  warehouse/code  postmortem_id, incident FK check, downtime_minutes, impact_summary
                  (from v_incident_impact), timeline, published_at
  Gemini          root_cause prose, contributing factors, action items with owners
                  -- grounded in the facts gathered here and validated before use:
                  the root cause must name the causal failure, not a cascade symptom.
If Gemini is unavailable or fails validation, deterministic text is used.
A corrected postmortem is a new row (new id); history is never rewritten.
Drafts have published_at NULL; publishing requires the incident to be RESOLVED.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..llm import LLMUnavailable
from ..redact import redact, redact_obj
from .impact import Impact, impact_sentence

PM_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "root_cause": {"type": "STRING"},
        "contributing_factors": {"type": "ARRAY", "items": {"type": "STRING"}},
        "action_items": {"type": "ARRAY", "items": {
            "type": "OBJECT",
            "properties": {"action": {"type": "STRING"}, "owner_team": {"type": "STRING"}},
            "required": ["action", "owner_team"]}},
    },
    "required": ["root_cause", "contributing_factors", "action_items"],
}


@dataclass
class PostmortemDraft:
    postmortem_id: str
    incident_id: str
    root_cause: str
    impact_summary: str
    downtime_minutes: float
    contributing_factors: list[str]
    action_items: list[str]
    source: str                         # gemini | deterministic
    notes: list[str] = field(default_factory=list)

    def row(self) -> dict[str, Any]:
        return {
            "postmortem_id": self.postmortem_id,
            "incident_id": self.incident_id,
            "root_cause": redact(self.root_cause),
            "impact_summary": self.impact_summary,
            "downtime_minutes": float(self.downtime_minutes),
            "contributing_factors": redact("\n".join(self.contributing_factors)),
            "action_items": redact("\n".join(self.action_items)),
        }


def next_postmortem_id(existing_ids: list[str], year: int) -> str:
    nums = [int(m.group(1)) for i in existing_ids if (m := re.match(rf"^pm-{year}-(\d+)$", i or ""))]
    return f"pm-{year}-{(max(nums) + 1) if nums else 1:04d}"


def _tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", s.lower()) if len(t) > 2}


def root_cause_is_causal(text: str, facts: dict[str, Any]) -> tuple[bool, str]:
    """The prose must name the root component and failure mode, and must not
    present a symptom as the cause."""
    rc = facts["root_cause"]
    t = _tokens(text)
    names_component = bool(t & (_tokens(rc["node"]) | _tokens(rc["service"])))
    names_failure = len(t & _tokens(rc["alert_type"])) >= min(2, len(_tokens(rc["alert_type"])))
    if not names_component:
        return False, "does not name the root component"
    if not names_failure:
        return False, "does not name the failure mode"
    # In the opening sentence the causal failure must come before any symptom
    # ("latency spike caused pool exhaustion" inverts cause and effect).
    lead = text.lower().split(". ")[0]

    def first_pos(words: set[str]) -> int:
        hits = [m.start() for w in words for m in [re.search(rf"\b{re.escape(w)}", lead)] if m]
        return min(hits) if hits else len(lead) + 1

    root_words = _tokens(rc["alert_type"])
    root_pos = first_pos(root_words)
    for sym in facts.get("symptom_alert_types", []):
        sym_words = _tokens(sym) - root_words
        if sym_words and first_pos(sym_words) < root_pos:
            return False, f"leads with symptom {sym}"
    return True, ""


def deterministic_root_cause(facts: dict[str, Any]) -> str:
    rc = facts["root_cause"]
    sym = ", ".join(facts.get("symptom_services", [])) or "no downstream services"
    return (f"{rc['alert_type'].replace('_', ' ').capitalize()} on {rc['node']} ({rc['service']}, "
            f"{rc['region']}) was the causal failure. Evidence: {rc['evidence']} The alerts on {sym} "
            f"were cascade symptoms of this failure, not independent faults.")


def deterministic_factors(facts: dict[str, Any]) -> tuple[list[str], list[str]]:
    rc = facts["root_cause"]
    factors = [
        f"A single saturated dependency ({rc['node']}) had no guard that failed fast, so the fault "
        f"propagated to {len(facts.get('symptom_services', []))} downstream services.",
        f"{facts['alert_count']} alerts fired for one fault; responders had to separate cause from echoes.",
    ]
    actions = []
    rb = facts.get("runbook") or {}
    for line in (rb.get("steps") or "").splitlines():
        if re.search(r"follow up|permanent|prevent", line, re.I):
            step = re.sub(r"^\s*\d+\.\s*", "", line).strip()
            actions.append(f"{step} (owner: {rc['service']} team)")
    if not actions:
        actions.append(f"Add a saturation alert on {rc['node']} that fires before exhaustion (owner: SRE)")
    return factors, actions


def build_facts(incident: dict[str, Any], triage: dict[str, Any], impact: Impact,
                remediations: list[dict[str, Any]], approvals: list[dict[str, Any]],
                runbook: dict[str, Any] | None) -> dict[str, Any]:
    rc = triage["root_cause"]
    root_type = rc["alert_type"]
    return redact_obj(json.loads(json.dumps({
        "incident": {k: incident.get(k) for k in ("incident_id", "title", "status", "severity",
                                                  "affected_region", "started_at", "resolved_at")},
        "root_cause": {"node": rc["node_name"], "node_id": rc["node_id"], "service": rc["service_name"],
                       "region": rc["region"], "alert_type": root_type, "evidence": rc["sample_message"],
                       "first_at": rc["first_at"]},
        "symptom_services": triage.get("symptom_services", []),
        "symptom_alert_types": sorted({s["alert_type"] for s in triage.get("signatures", [])
                                       if s["alert_type"] != root_type}),
        "alert_count": triage.get("alert_count"),
        "impact": {"downtime_minutes": impact.downtime_minutes, "services": impact.services,
                   "regions": impact.regions, "accounts": len(impact.customers),
                   "breached_tiers": impact.breached_tiers, "sla_credit_cad": impact.sla_credit_cad,
                   "revenue_at_risk_cad": impact.revenue_at_risk_cad},
        "remediations": [{k: r.get(k) for k in ("execution_id", "runbook_id", "status", "hitl_approved",
                                                "executed_by", "error_output", "started_at", "finished_at")}
                         for r in remediations],
        "approvals": [{k: a.get(k) for k in ("approval_id", "status", "decided_by", "risk_level",
                                             "requested_at", "decided_at")} for a in approvals],
        "runbook": runbook,
    }, default=str)))


def draft_postmortem(postmortem_id: str, facts: dict[str, Any], impact: Impact, llm=None) -> PostmortemDraft:
    notes: list[str] = []
    impact_summary = impact_sentence(impact)
    factors, actions = deterministic_factors(facts)
    root_cause, source = deterministic_root_cause(facts), "deterministic"

    if llm is not None:
        prompt = (
            "Write a blameless SRE postmortem from ONLY these facts. Do not invent numbers, times, "
            "customers or systems.\n"
            "- root_cause: 2-4 sentences. Name the causal failure (component + failure mode) first, "
            "then explain how it cascaded. The downstream alerts are symptoms, not causes.\n"
            "- contributing_factors: 3-5 latent conditions that made it possible or worse, blameless "
            "phrasing (systems and processes, never people).\n"
            "- action_items: 3-5 preventive follow-ups distinct from the fix that ended the outage, "
            "each with an owning team.\n\n"
            f"FACTS:\n{json.dumps(facts, indent=2)}"
        )
        try:
            out = llm.generate_json(prompt, PM_SCHEMA)
            ok, why = root_cause_is_causal(out["root_cause"], facts)
            if ok:
                root_cause = out["root_cause"].strip()
                factors = [f.strip() for f in out["contributing_factors"] if f.strip()] or factors
                actions = [f"{a['action'].strip()} (owner: {a['owner_team'].strip()})"
                           for a in out["action_items"] if a.get("action")] or actions
                source = "gemini"
            else:
                notes.append(f"Gemini root cause rejected ({why}); deterministic text used")
        except LLMUnavailable as exc:
            notes.append(f"Gemini unavailable: {str(exc)[:120]}")

    return PostmortemDraft(
        postmortem_id=postmortem_id,
        incident_id=facts["incident"]["incident_id"],
        root_cause=root_cause,
        impact_summary=impact_summary,
        downtime_minutes=impact.downtime_minutes,
        contributing_factors=factors,
        action_items=actions,
        source=source,
        notes=notes,
    )
