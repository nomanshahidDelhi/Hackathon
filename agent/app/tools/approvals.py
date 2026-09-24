"""Human-in-the-loop approval records (sre_agent_ops.approvals).

Approval is data, not a prompt instruction: the executor reads this table and
refuses to run anything without an APPROVED row whose script hash matches.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from typing import Any

from ..redact import redact, redact_obj
from .bq import Warehouse
from .remediation import Proposal


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ApprovalError(RuntimeError):
    pass


class Approvals:
    def __init__(self, wh: Warehouse):
        self.wh = wh
        self.table = wh.s.table("sre_agent_ops.approvals")

    def request(self, incident_id: str, proposal: Proposal) -> str:
        if proposal.status != "READY" or proposal.draft is None or proposal.guardrails is None:
            raise ApprovalError(f"proposal is {proposal.status}: {proposal.reason}")
        if proposal.guardrails.blocked:
            raise ApprovalError("guardrails BLOCK this script; it cannot be sent for approval")
        d = proposal.draft
        script, rollback = redact(d.script), redact(d.rollback)
        approval_id = f"apr-{uuid.uuid4().hex[:12]}"
        plan = redact_obj({
            "rationale": d.rationale, "preconditions": d.preconditions, "verification": d.verification,
            "source": d.source, "selection": proposal.selection, "retrieval_method": proposal.retrieval_method,
            "attempts": [asdict(a) for a in proposal.attempts],
        })
        p = self.wh.param
        self.wh.query(f"""
            INSERT {self.table}
              (approval_id, incident_id, runbook_id, script, rollback_script, script_sha256, risk_level,
               guardrail_report, sandbox_report, status, requested_at, plan_json)
            VALUES (@id, @incident_id, @runbook_id, @script, @rollback, @sha, @risk,
                    @guardrails, @sandbox, 'PENDING', CURRENT_TIMESTAMP(), @plan)""", [
            p("id", approval_id), p("incident_id", incident_id), p("runbook_id", proposal.runbook_id),
            p("script", script), p("rollback", rollback), p("sha", sha256(script)),
            p("risk", proposal.guardrails.risk),
            p("guardrails", json.dumps(asdict(proposal.guardrails), default=str)),
            p("sandbox", json.dumps(asdict(proposal.sandbox), default=str) if proposal.sandbox else None, "STRING"),
            p("plan", json.dumps(plan, default=str)),
        ])
        return approval_id

    def decide(self, approval_id: str, approve: bool, by: str, reason: str = "") -> None:
        if not by.strip():
            raise ApprovalError("a named approver is required")
        rows = self.wh.query(f"""
            UPDATE {self.table}
            SET status = @status, decided_at = CURRENT_TIMESTAMP(), decided_by = @by, decision_reason = @reason
            WHERE approval_id = @id AND status = 'PENDING';
            SELECT @@row_count AS n;""", [
            self.wh.param("status", "APPROVED" if approve else "REJECTED"),
            self.wh.param("by", by), self.wh.param("reason", redact(reason) or ""),
            self.wh.param("id", approval_id),
        ])
        if not rows or rows[0]["n"] != 1:
            raise ApprovalError(f"{approval_id} is not PENDING (already decided, or unknown)")

    def get(self, approval_id: str) -> dict[str, Any]:
        rows = self.wh.query(f"SELECT * FROM {self.table} WHERE approval_id = @id",
                             [self.wh.param("id", approval_id)])
        if not rows:
            raise ApprovalError(f"unknown approval {approval_id}")
        return rows[0]

    def pending(self) -> list[dict[str, Any]]:
        return self.wh.query(f"""
            SELECT approval_id, incident_id, runbook_id, risk_level, requested_at
            FROM {self.table} WHERE status = 'PENDING' ORDER BY requested_at DESC""")
