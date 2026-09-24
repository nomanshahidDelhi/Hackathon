"""Draft a re-runnable fix + rollback from the retrieved runbook, then prove it safe (M2).

Loop (max 3 drafts): Gemini adapts the runbook -> guardrails (code) -> sandbox
dry-run x2 -> if anything fails, the findings go back to Gemini for a revision.
If Gemini is unavailable the runbook itself is templated into a script. A plan
that never passes both gates is escalated to a human; it is never executable.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from ..llm import LLMUnavailable
from ..models import Incident
from ..redact import redact
from .guardrails import GuardrailReport, check_script
from .retrieval import RetrievalResult, Runbook

log = logging.getLogger(__name__)
MAX_DRAFTS = 3


class LLM(Protocol):
    def generate_json(self, prompt: str, schema: dict[str, Any], temperature: float = 0.2) -> dict[str, Any]: ...


class DryRunner(Protocol):
    def dry_run(self, script: str, rollback: str | None, runs: int = 2): ...


@dataclass
class Draft:
    script: str
    rollback: str
    rationale: str
    preconditions: list[str]
    verification: list[str]
    source: str  # gemini | template


@dataclass
class Attempt:
    n: int
    source: str
    guardrails: str
    sandbox: str | None
    accepted: bool


@dataclass
class Proposal:
    status: str                      # READY | ESCALATE
    runbook_id: str | None
    runbook_title: str | None
    retrieval_method: str
    draft: Draft | None
    guardrails: GuardrailReport | None
    sandbox: Any | None              # executor.sandbox.SandboxReport
    selection: dict[str, Any] = field(default_factory=dict)
    attempts: list[Attempt] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(asdict(self), default=str))


DRAFT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "script": {"type": "STRING"},
        "rollback_script": {"type": "STRING"},
        "rationale": {"type": "STRING"},
        "preconditions": {"type": "ARRAY", "items": {"type": "STRING"}},
        "verification": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["script", "rollback_script", "rationale", "preconditions", "verification"],
}

SELECTION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "chosen_runbook_id": {"type": "STRING"},
        "agrees_with_ranking": {"type": "BOOLEAN"},
        "explanation": {"type": "STRING"},
        "rejected": {"type": "ARRAY", "items": {
            "type": "OBJECT",
            "properties": {"runbook_id": {"type": "STRING"}, "reason": {"type": "STRING"}},
            "required": ["runbook_id", "reason"]}},
    },
    "required": ["chosen_runbook_id", "agrees_with_ranking", "explanation", "rejected"],
}


def incident_facts(incident: Incident) -> dict[str, Any]:
    rc = incident.root_cause
    return {
        "root_cause": {"node": rc.node_name, "node_id": rc.node_id, "node_type": rc.node_type,
                       "region": rc.region, "service": rc.service_name, "alert_type": rc.alert_type,
                       "evidence": redact(rc.sample_message)},
        "symptom_services": incident.symptom_services,
        "regions": incident.regions,
        "alert_count": incident.alert_count,
        "signatures": [{"node_id": s["node_id"], "alert_type": s["alert_type"], "count": s["count"]}
                       for s in incident.signatures[:12]],
    }


# ---------------------------------------------------------------------------
# selection second opinion
# ---------------------------------------------------------------------------
def review_selection(llm: LLM | None, incident: Incident, retrieval: RetrievalResult,
                     runbooks: dict[str, Runbook]) -> dict[str, Any]:
    """Gemini explains the choice and why near-misses lose. The deterministic ranking
    stands; a disagreement is surfaced to the approver, not silently applied."""
    top = retrieval.hits[:3]
    if llm is None or not top:
        return {"source": "ranking", "chosen_runbook_id": top[0].runbook_id if top else None}
    candidates = [{"runbook_id": h.runbook_id, "title": h.title, "failure_signature": h.failure_signature,
                   "rank_score": h.score, "evidence": h.components,
                   "steps_excerpt": runbooks[h.runbook_id].remediation_steps[:700]} for h in top]
    prompt = (
        "You are an SRE incident commander. Choose the runbook that fixes the ROOT CAUSE of this "
        "incident, not one that treats a downstream symptom. Explain briefly, and say why each other "
        "candidate is wrong for this incident.\n\n"
        f"INCIDENT:\n{json.dumps(incident_facts(incident), indent=2)}\n\n"
        f"CANDIDATES (retrieved by vector search, ranked):\n{json.dumps(candidates, indent=2)}"
    )
    try:
        out = llm.generate_json(prompt, SELECTION_SCHEMA)
        out["source"] = "gemini"
        out["agrees_with_ranking"] = out.get("chosen_runbook_id") == top[0].runbook_id
        return out
    except LLMUnavailable as exc:
        return {"source": "ranking", "chosen_runbook_id": top[0].runbook_id, "llm_error": str(exc)[:200]}


# ---------------------------------------------------------------------------
# drafting
# ---------------------------------------------------------------------------
SQL_START = re.compile(r"(?i)^\s*(SELECT|ALTER|UPDATE|INSERT|DELETE|CREATE|DROP|ANALYZE|VACUUM|SET|REINDEX)\b")
ASSIGN = re.compile(r"^\s*([A-Z_][A-Z0-9_]*)=(.+)$")


def _strip_header(script: str) -> str:
    lines = [l for l in script.splitlines()
             if not l.startswith("#!") and not re.match(r"^\s*set\s+-[euxo ]+pipefail\s*$", l)]
    return "\n".join(lines).strip()


def rollback_to_bash(rollback: str, script: str) -> str:
    """Runbook rollbacks mix bash with raw SQL; wrap SQL in psql using the script's own variables."""
    assigns = [m.group(0).strip() for m in map(ASSIGN.match, script.splitlines()) if m]
    body, sql = [], []
    for raw in rollback.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("--"):
            body.append("# " + line[2:].strip())
        elif SQL_START.match(line) or sql:
            sql.append(line)
            if line.rstrip().endswith(";"):
                stmt = " ".join(sql).replace('"', '\\"')
                body.append(f'psql -h "$DB_HOST" -d "$DB" -v ON_ERROR_STOP=1 -c "{stmt}"')
                sql = []
        else:
            body.append(line)
    uses_db = any("psql" in b for b in body)
    header = [a for a in assigns if uses_db or a.split("=")[0] in " ".join(body)]
    if uses_db and not any(a.startswith("DB_HOST=") for a in header):
        header.append(': "${DB_HOST:?DB_HOST must be set}" "${DB:?DB must be set}"')
    return "\n".join(["#!/bin/bash", "set -euo pipefail", *header, *body]) + "\n"


def template_draft(incident: Incident, runbook: Runbook) -> Draft:
    rc = incident.root_cause
    script = "\n".join([
        "#!/bin/bash",
        "set -euo pipefail",
        f"# Runbook {runbook.runbook_id}: {runbook.title}",
        f"# Root cause: {rc.alert_type} on {rc.node_name} ({rc.region})",
        _strip_header(runbook.remediation_script),
    ]) + "\n"
    return Draft(
        script=script,
        rollback=rollback_to_bash(runbook.rollback_commands, runbook.remediation_script),
        rationale=f"Runbook {runbook.runbook_id} applied as written (Gemini unavailable).",
        preconditions=[f"Root cause confirmed as {rc.alert_type} on {rc.node_name}"],
        verification=[l.strip() for l in runbook.remediation_steps.splitlines() if "verif" in l.lower()][:3],
        source="template",
    )


def llm_draft(llm: LLM, incident: Incident, runbook: Runbook, feedback: str | None) -> Draft:
    prompt = (
        "You are a senior SRE. Turn the runbook below into ONE bash remediation script and ONE bash "
        "rollback script for this specific incident.\n"
        "Requirements:\n"
        "- Start both with `#!/bin/bash` and `set -euo pipefail`.\n"
        "- Idempotent: safe to run twice. Set desired absolute values; never derive a new value "
        "from the current one (no doubling), check state before changing it, use IF NOT EXISTS / apply.\n"
        "- Scope every destructive action (WHERE clauses, one namespace/deployment). No deletes of "
        "namespaces/nodes/volumes, no DROP/TRUNCATE, no rm -rf, no sudo, no hard BGP resets.\n"
        "- Only use: kubectl psql redis-cli rndc unbound-control dig openssl etcdctl vtysh netconf-cli "
        "gcloud crictl systemctl logrotate journalctl curl ssh certbot jq and coreutils "
        "(df du find sort head tail grep awk cut wc date sleep cat tr truncate).\n"
        "- The rollback must be executable bash (wrap SQL in psql -c).\n"
        "- Never include credentials; rely on the environment.\n"
        "- Echo what each step does. Treat downstream symptoms as recovering on their own; fix the cause.\n\n"
        f"INCIDENT:\n{json.dumps(incident_facts(incident), indent=2)}\n\n"
        f"RUNBOOK {runbook.runbook_id}: {runbook.title}\n"
        f"Failure signature: {runbook.failure_signature}\n"
        f"Steps:\n{runbook.remediation_steps}\n\n"
        f"Reference script:\n{runbook.remediation_script}\n\n"
        f"Reference rollback:\n{runbook.rollback_commands}\n"
    )
    if feedback:
        prompt += f"\nYOUR PREVIOUS DRAFT WAS REJECTED. Fix every issue:\n{feedback}\n"
    out = llm.generate_json(prompt, DRAFT_SCHEMA)
    return Draft(
        script=out["script"].strip() + "\n",
        rollback=out["rollback_script"].strip() + "\n",
        rationale=out.get("rationale", ""),
        preconditions=list(out.get("preconditions", [])),
        verification=list(out.get("verification", [])),
        source="gemini",
    )


def _feedback(g: GuardrailReport, sandbox) -> str:
    lines = [f"- [{f.severity}] {f.rule}: {f.message} :: {f.snippet}"
             for f in g.findings if f.severity in ("BLOCK", "HIGH", "WARN")]
    if sandbox is not None and not sandbox.passed:
        lines.append(f"- sandbox: {sandbox.summary()}")
        for r in sandbox.runs:
            if r.stderr:
                lines.append(f"  stderr: {r.stderr[-400:]}")
        if not sandbox.idempotent:
            lines.append("  the two runs issued different commands: the script is not idempotent")
    return "\n".join(lines)


def propose(
    incident: Incident,
    retrieval: RetrievalResult,
    runbooks: dict[str, Runbook],
    sandbox: DryRunner,
    llm: LLM | None = None,
) -> Proposal:
    best = retrieval.best
    if best is None:
        return Proposal("ESCALATE", None, None, retrieval.method, None, None, None,
                        reason="no runbook found for this root cause")
    runbook = runbooks[best.runbook_id]
    selection = review_selection(llm, incident, retrieval, runbooks)
    attempts: list[Attempt] = []
    feedback: str | None = None
    tried_template = False
    last: tuple[Draft, GuardrailReport, Any] | None = None

    llm_ok = llm is not None
    # Up to MAX_DRAFTS Gemini drafts, then the runbook template as a last resort.
    for n in range(1, MAX_DRAFTS + 2):
        draft = None
        if llm_ok and n <= MAX_DRAFTS:
            try:
                draft = llm_draft(llm, incident, runbook, feedback)
            except LLMUnavailable as exc:
                log.warning("drafting falls back to runbook template: %s", exc)
                llm_ok = False
        if draft is None:
            if tried_template:
                break
            draft, tried_template = template_draft(incident, runbook), True

        g = check_script(draft.script, rollback=draft.rollback)
        sb = None if g.blocked else sandbox.dry_run(draft.script, draft.rollback)
        ok = not g.blocked and sb is not None and sb.passed
        attempts.append(Attempt(n, draft.source, g.summary(), sb.summary() if sb else None, ok))
        last = (draft, g, sb)
        if ok:
            return Proposal("READY", runbook.runbook_id, runbook.title, retrieval.method,
                            draft, g, sb, selection, attempts)
        feedback = _feedback(g, sb)

    draft, g, sb = last if last else (None, None, None)
    return Proposal("ESCALATE", runbook.runbook_id, runbook.title, retrieval.method, draft, g, sb,
                    selection, attempts, reason="no draft passed guardrails and sandbox; human action required")
