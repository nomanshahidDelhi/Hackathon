"""Execute an approved remediation and write the audit row to remediation_logs.

Gate, checked here and not trusted from the caller:
  1. approval row exists and is APPROVED by a named human
  2. it has not been executed before (no replay)
  3. sha256 of the script equals the hash recorded at request time (no tampering)
  4. guardrails re-run now and do not BLOCK
Target: there is no real production estate in the hackathon, so execution runs
in the isolated sandbox (EXECUTION_TARGET=sandbox). On failure the rollback runs
and the row is ROLLED_BACK (or FAILED if the rollback fails too).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from agent.app.redact import redact
from agent.app.tools.approvals import ApprovalError, Approvals, sha256
from agent.app.tools.bq import Warehouse
from agent.app.tools.guardrails import check_script
from executor.sandbox import Sandbox


@dataclass
class ExecutionResult:
    execution_id: str
    status: str
    error_output: str | None
    target: str


def gate(row: dict) -> None:
    if row["status"] != "APPROVED" or not row.get("decided_by"):
        raise ApprovalError(f"{row['approval_id']} is {row['status']}; human approval required")
    if row.get("execution_id"):
        raise ApprovalError(f"{row['approval_id']} already executed as {row['execution_id']}")
    if sha256(row["script"]) != row["script_sha256"]:
        raise ApprovalError("script changed after approval (hash mismatch)")
    g = check_script(row["script"], rollback=row["rollback_script"])
    if g.blocked:
        raise ApprovalError(f"guardrails now BLOCK this script: {g.summary()}")


def next_execution_id(wh: Warehouse) -> str:
    rows = wh.query(f"""
        SELECT COALESCE(MAX(SAFE_CAST(REGEXP_EXTRACT(execution_id, r'^exec-(\\d+)$') AS INT64)), 9000) + 1 AS n
        FROM {wh.s.table('sre_incident_mart.remediation_logs')}""")
    return f"exec-{rows[0]['n']}"


def execute(wh: Warehouse, approval_id: str, sandbox: Sandbox | None = None) -> ExecutionResult:
    target = os.environ.get("EXECUTION_TARGET", "sandbox")
    if target != "sandbox":
        raise ApprovalError(f"execution target {target!r} is not configured in this build")

    approvals = Approvals(wh)
    row = approvals.get(approval_id)
    gate(row)

    sandbox = sandbox or Sandbox()
    started = datetime.now(timezone.utc)
    run = sandbox.dry_run(row["script"], None, runs=1)
    ok = run.syntax_ok and run.runs and run.runs[0].exit_code == 0
    if ok:
        status, error = "SUCCESS", None
    else:
        err = run.syntax_error or (run.runs[0].stderr if run.runs else "no output")
        rb = sandbox.dry_run(row["rollback_script"], None, runs=1)
        rb_ok = rb.syntax_ok and rb.runs and rb.runs[0].exit_code == 0
        status = "ROLLED_BACK" if rb_ok else "FAILED"
        error = redact(f"{err.strip()[-800:]}" + ("" if rb_ok else " | rollback also failed"))
    finished = datetime.now(timezone.utc)

    execution_id = next_execution_id(wh)
    p = wh.param
    wh.query(f"""
        BEGIN TRANSACTION;
        INSERT {wh.s.table('sre_incident_mart.remediation_logs')}
          (execution_id, incident_id, runbook_id, executed_script, executed_by, hitl_approved,
           status, error_output, started_at, finished_at)
        VALUES (@exec, @incident, @runbook, @script, 'sre-agent', @approved, @status, @error, @started, @finished);
        UPDATE {wh.s.table('sre_agent_ops.approvals')}
          SET execution_id = @exec, executed_at = CURRENT_TIMESTAMP()
          WHERE approval_id = @approval AND execution_id IS NULL;
        UPDATE {wh.s.table('sre_incident_mart.incidents')}
          SET status = 'MONITORING'
          WHERE incident_id = @incident AND status = 'INVESTIGATING' AND @status = 'SUCCESS'
            AND incident_id IN (SELECT incident_id FROM {wh.s.table('sre_agent_ops.agent_incidents')});
        COMMIT TRANSACTION;""", [
        p("exec", execution_id), p("incident", row["incident_id"]), p("runbook", row["runbook_id"]),
        p("script", redact(row["script"])), p("approved", row["status"] == "APPROVED"),
        p("status", status), p("error", error, "STRING"), p("started", started), p("finished", finished),
        p("approval", approval_id),
    ])
    return ExecutionResult(execution_id, status, error, target)
