-- 01_agent_ops_schema.sql
-- Our own dataset. The seven seeded tables are never altered; everything the
-- agent needs beyond them lives here. Idempotent: CREATE ... IF NOT EXISTS.

CREATE SCHEMA IF NOT EXISTS `__PROJECT_ID__.sre_agent_ops`
  OPTIONS(location = '__LOCATION__', description = 'Agent-owned state: SLA policy, live alert ingest, approvals, runs, forecasts');

-- Contractual SLA policy per tier. The kit gives the credit formula
-- (credit = mrr_cad * tier_credit_rate * downtime_minutes / minutes_in_month)
-- but not the rates, so they are policy config seeded by script.
CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_agent_ops.sla_policy`
(
  tier STRING OPTIONS(description="GOLD, SILVER or BRONZE; joins customer_accounts.tier."),
  restoration_target_minutes INT64 OPTIONS(description="Contractual time-to-restore. GOLD is the only sub-hour commitment."),
  credit_rate FLOAT64 OPTIONS(description="tier_credit_rate in the SLA-credit formula."),
  notes STRING
)
OPTIONS(description="SLA targets and credit multipliers per customer tier.");

-- Alerts arriving through Pub/Sub (M3). Same columns as sre_telemetry.alert_stream
-- plus ingest metadata, so v_alerts can union the two.
CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_agent_ops.alert_stream_live`
(
  alert_id STRING,
  node_id STRING,
  service_name STRING,
  severity STRING,
  alert_type STRING,
  message STRING OPTIONS(description="Redacted before write."),
  measured_value FLOAT64,
  timestamp TIMESTAMP,
  ingested_at TIMESTAMP,
  source STRING OPTIONS(description="Producer id, e.g. loadgen or a tool name.")
)
PARTITION BY DATE(timestamp)
CLUSTER BY service_name, node_id
OPTIONS(description="Streamed alerts written by the ingest service.");

-- Per-batch reconciliation so "no alert dropped" is provable.
CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_agent_ops.ingest_audit`
(
  batch_id STRING,
  received INT64,
  written INT64,
  duplicates INT64,
  dead_lettered INT64,
  first_event_at TIMESTAMP,
  last_event_at TIMESTAMP,
  written_at TIMESTAMP
)
PARTITION BY DATE(written_at);

-- Human-in-the-loop approvals. The executor refuses to run without an
-- APPROVED row whose script_sha256 matches what it is asked to execute.
CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_agent_ops.approvals`
(
  approval_id STRING,
  incident_id STRING,
  runbook_id STRING,
  script STRING,
  rollback_script STRING,
  script_sha256 STRING,
  risk_level STRING OPTIONS(description="LOW, MEDIUM, HIGH. BLOCKED scripts never reach this table."),
  guardrail_report STRING OPTIONS(description="JSON verdicts from guardrails.py."),
  sandbox_report STRING OPTIONS(description="JSON result of the dry-run(s)."),
  status STRING OPTIONS(description="PENDING, APPROVED, REJECTED, EXPIRED."),
  requested_at TIMESTAMP,
  decided_at TIMESTAMP,
  decided_by STRING,
  decision_reason STRING
);

-- One row per agent stage per run: timings for MTTR/agent metrics and an
-- audit trail. detail is redacted before write.
CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_agent_ops.agent_runs`
(
  run_id STRING,
  incident_id STRING,
  stage STRING,
  status STRING,
  started_at TIMESTAMP,
  finished_at TIMESTAMP,
  detail STRING
)
PARTITION BY DATE(started_at);

-- Breach forecasts (M3).
CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_agent_ops.forecasts`
(
  forecast_id STRING,
  node_id STRING,
  service_name STRING,
  alert_type STRING,
  samples INT64,
  current_value FLOAT64,
  threshold FLOAT64,
  slope_per_min FLOAT64,
  r_squared FLOAT64,
  eta_minutes FLOAT64,
  predicted_breach_at TIMESTAMP,
  generated_at TIMESTAMP,
  explanation STRING
)
PARTITION BY DATE(generated_at);
