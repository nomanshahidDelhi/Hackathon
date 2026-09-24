-- 04_agent_incidents.sql
-- Registry of incidents the agent opened, so reruns update the same incident
-- instead of opening a new one, and the console can tell agent-detected
-- incidents from historical ones.
CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_agent_ops.agent_incidents`
(
  incident_id STRING OPTIONS(description="FK to sre_incident_mart.incidents."),
  fingerprint STRING OPTIONS(description="root node | root signature | onset minute."),
  root_node_id STRING,
  root_alert_type STRING,
  root_service STRING,
  alert_count INT64,
  confidence FLOAT64,
  triage_json STRING OPTIONS(description="Redacted triage summary (candidates, scores, signatures)."),
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);
