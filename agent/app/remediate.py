"""M2 CLI: runbook retrieval -> safe remediation -> human approval -> execution.

    python -m agent.app.remediate propose                    # triage now, retrieve, draft, dry-run (writes nothing)
    python -m agent.app.remediate propose --incident inc-5019 --request-approval
    python -m agent.app.remediate propose --disable vector_search,local_cosine   # prove the keyword fallback
    python -m agent.app.remediate pending
    python -m agent.app.remediate show apr-...
    python -m agent.app.remediate approve apr-... --by "Jane Doe"
    python -m agent.app.remediate reject  apr-... --by "Jane Doe" --reason "wrong window"
    python -m agent.app.remediate execute apr-...
"""
from __future__ import annotations

import argparse
import json
import sys

from .logging_setup import setup_logging
from .config import load_settings
from .llm import Gemini
from .models import Incident
from .tools.approvals import ApprovalError, Approvals
from .tools.bq import Warehouse
from .tools.incident_store import IncidentStore
from .tools.remediation import Proposal, propose
from .tools.retrieval import RetrievalResult, retrieve
from .triage import run_triage


def load_incident(wh: Warehouse, incident_id: str) -> Incident:
    rows = wh.query(f"""
        SELECT triage_json FROM {wh.s.table('sre_agent_ops.agent_incidents')}
        WHERE incident_id = @id""", [wh.param("id", incident_id)])
    if not rows:
        raise SystemExit(f"{incident_id} was not opened by the agent (run triage --persist first)")
    return Incident.from_dict(json.loads(rows[0]["triage_json"]))


def render(incident: Incident, retrieval: RetrievalResult, proposal: Proposal) -> str:
    rc = incident.root_cause
    out = [
        f"root cause : {rc.alert_type} on {rc.node_name} ({rc.service_name}, {rc.region})",
        f"query      : {retrieval.query[:160]}",
        f"retrieval  : {retrieval.method}" + (f"  (degraded: {'; '.join(retrieval.errors)})" if retrieval.errors else ""),
        "candidates :",
    ]
    for h in retrieval.hits:
        comps = " ".join(f"{k}={v}" for k, v in h.components.items())
        out.append(f"  {h.score:>7.3f}  {h.runbook_id}  {h.failure_signature:<32} {comps}")
    sel = proposal.selection
    if sel.get("source") == "gemini":
        out.append(f"gemini     : picks {sel.get('chosen_runbook_id')} "
                   f"({'agrees' if sel.get('agrees_with_ranking') else 'DISAGREES -- review'}): {sel.get('explanation', '')[:300]}")
        for r in sel.get("rejected", []):
            out.append(f"             rejects {r['runbook_id']}: {r['reason'][:160]}")
    out.append(f"proposal   : {proposal.status} runbook={proposal.runbook_id} {proposal.reason}")
    for a in proposal.attempts:
        out.append(f"  draft {a.n} [{a.source}] guardrails={a.guardrails} sandbox={a.sandbox} accepted={a.accepted}")
    if proposal.draft:
        out += ["", "----- script -----", proposal.draft.script.rstrip(),
                "----- rollback -----", proposal.draft.rollback.rstrip()]
    if proposal.guardrails:
        risky = [f for f in proposal.guardrails.findings if f.severity in ("BLOCK", "HIGH", "WARN")]
        if risky:
            out.append("----- guardrail findings -----")
            out += [f"  [{f.severity}] {f.rule}: {f.message}" for f in risky]
    return "\n".join(out)


def cmd_propose(args, wh: Warehouse) -> int:
    from executor.sandbox import Sandbox

    if args.incident:
        incident_id, incident = args.incident, load_incident(wh, args.incident)
    else:
        result = run_triage(wh, args.lookback)
        if not result.incidents:
            print("no incident in the current window")
            return 0
        incident_id, incident = None, result.incidents[0]

    llm = None if args.no_llm else Gemini(wh.s.project)
    disable = set(filter(None, (args.disable or "").split(",")))
    embed = None if llm is None else llm.embed_query
    retrieval = retrieve(incident, wh, embed=embed, disable=disable)
    runbooks = {r.runbook_id: r for r in wh.fetch_runbooks()}
    proposal = propose(incident, retrieval, runbooks, Sandbox(), llm)
    print(render(incident, retrieval, proposal))

    if args.request_approval:
        if incident_id is None:
            incident_id, _ = IncidentStore(wh).upsert(incident)
        try:
            approval_id = Approvals(wh).request(incident_id, proposal)
        except ApprovalError as exc:
            print(f"\nnot sent for approval: {exc}", file=sys.stderr)
            return 1
        print(f"\napproval requested: {approval_id} for {incident_id} (status PENDING)")
        print(f"  approve: python -m agent.app.remediate approve {approval_id} --by \"<your name>\"")
    return 0 if proposal.status == "READY" else 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("propose")
    p.add_argument("--incident")
    p.add_argument("--lookback", type=int)
    p.add_argument("--disable", help="comma list: vector_search,local_cosine")
    p.add_argument("--no-llm", action="store_true", help="skip Gemini (template draft, keyword/BQ retrieval)")
    p.add_argument("--request-approval", action="store_true")
    sub.add_parser("pending")
    for name in ("show", "execute"):
        sub.add_parser(name).add_argument("approval_id")
    for name in ("approve", "reject"):
        d = sub.add_parser(name)
        d.add_argument("approval_id")
        d.add_argument("--by", required=True)
        d.add_argument("--reason", default="")
    args = ap.parse_args(argv)
    setup_logging()

    wh = Warehouse(load_settings())
    try:
        if args.cmd == "propose":
            return cmd_propose(args, wh)
        approvals = Approvals(wh)
        if args.cmd == "pending":
            for r in approvals.pending():
                print(f"{r['approval_id']}  {r['incident_id']}  {r['runbook_id']}  risk={r['risk_level']}  {r['requested_at']}")
        elif args.cmd == "show":
            r = approvals.get(args.approval_id)
            print(f"{r['approval_id']} {r['status']} incident={r['incident_id']} runbook={r['runbook_id']} "
                  f"risk={r['risk_level']} decided_by={r['decided_by']} execution={r.get('execution_id')}")
            print("----- script -----\n" + r["script"] + "----- rollback -----\n" + r["rollback_script"])
        elif args.cmd in ("approve", "reject"):
            approvals.decide(args.approval_id, args.cmd == "approve", args.by, args.reason)
            print(f"{args.approval_id} {'APPROVED' if args.cmd == 'approve' else 'REJECTED'} by {args.by}")
        elif args.cmd == "execute":
            from executor.execute import execute

            res = execute(wh, args.approval_id)
            print(f"{res.execution_id} {res.status} (target={res.target})" + (f": {res.error_output}" if res.error_output else ""))
            return 0 if res.status == "SUCCESS" else 1
    except ApprovalError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
