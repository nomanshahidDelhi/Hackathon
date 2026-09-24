-- 03_views.sql
-- Single read path for the agent: seeded telemetry plus anything streamed in.
-- Needs sre_telemetry.alert_stream, so it runs after the kit's step 06.
CREATE OR REPLACE VIEW `__PROJECT_ID__.sre_agent_ops.v_alerts` AS
SELECT
  alert_id, node_id, service_name, severity, alert_type, message,
  measured_value, timestamp, 'seed' AS source
FROM `__PROJECT_ID__.sre_telemetry.alert_stream`
UNION ALL
SELECT
  alert_id, node_id, service_name, severity, alert_type, message,
  measured_value, timestamp, source
FROM `__PROJECT_ID__.sre_agent_ops.alert_stream_live`;
