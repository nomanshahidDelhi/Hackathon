-- 05_approvals_execution.sql
-- Our own table only: link an approval to the single execution it authorised,
-- so an approval can never be replayed.
ALTER TABLE `__PROJECT_ID__.sre_agent_ops.approvals`
  ADD COLUMN IF NOT EXISTS plan_json STRING OPTIONS(description="Redacted proposal: rationale, preconditions, verification, runbook selection."),
  ADD COLUMN IF NOT EXISTS execution_id STRING OPTIONS(description="FK to remediation_logs.execution_id once executed."),
  ADD COLUMN IF NOT EXISTS executed_at TIMESTAMP;
