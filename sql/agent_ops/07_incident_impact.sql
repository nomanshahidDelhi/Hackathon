-- 07_incident_impact.sql
-- Blast radius and SLA credits per incident x affected customer, computed in
-- the warehouse (never by the LLM).
--
--   scope     services and regions actually alerting in the incident
--             (correlated_alerts -> alert -> node region)
--   window    started_at -> resolved_at; while open, up to the last correlated
--             alert, or now if alerts are still arriving (last 5 min)
--   customers customer_accounts on the same service AND region (region is the
--             geographic spine shared by nodes, customers and incidents)
--   credit    mrr_cad * tier_credit_rate * downtime_minutes / minutes_in_month
--             (formula from customer_accounts.mrr_cad; rates from sla_policy)
CREATE OR REPLACE VIEW `__PROJECT_ID__.sre_agent_ops.v_incident_impact` AS
WITH scope AS (
  SELECT c.incident_id, a.service_name, n.region, MAX(a.timestamp) AS last_alert_at
  FROM `__PROJECT_ID__.sre_incident_mart.correlated_alerts` c
  JOIN `__PROJECT_ID__.sre_agent_ops.v_alerts` a USING (alert_id)
  JOIN `__PROJECT_ID__.sre_topology.network_nodes` n USING (node_id)
  GROUP BY 1, 2, 3
),
win AS (
  SELECT i.incident_id, i.title, i.status, i.severity, i.started_at, i.resolved_at,
         MAX(s.last_alert_at) AS last_alert_at
  FROM `__PROJECT_ID__.sre_incident_mart.incidents` i
  JOIN scope s USING (incident_id)
  GROUP BY 1, 2, 3, 4, 5, 6
),
dt AS (
  SELECT *,
    resolved_at IS NULL
      AND last_alert_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 5 MINUTE) AS ongoing,
    COALESCE(resolved_at,
      IF(last_alert_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 5 MINUTE),
         CURRENT_TIMESTAMP(), GREATEST(last_alert_at, started_at))) AS impact_end
  FROM win
),
dm AS (
  SELECT *,
    GREATEST(TIMESTAMP_DIFF(impact_end, started_at, SECOND), 0) / 60.0 AS downtime_minutes,
    EXTRACT(DAY FROM LAST_DAY(DATE(started_at))) * 1440 AS minutes_in_month
  FROM dt
)
SELECT
  d.incident_id, d.title, d.status, d.severity, d.started_at, d.impact_end, d.ongoing,
  d.downtime_minutes, d.minutes_in_month,
  ca.customer_id, ca.customer_name, ca.service_name, ca.region, ca.tier, ca.mrr_cad,
  p.credit_rate, p.restoration_target_minutes,
  ca.mrr_cad * d.downtime_minutes / d.minutes_in_month AS prorated_revenue_cad,
  ca.mrr_cad * p.credit_rate * d.downtime_minutes / d.minutes_in_month AS sla_credit_cad,
  d.downtime_minutes > p.restoration_target_minutes AS sla_breached
FROM dm d
JOIN (SELECT DISTINCT incident_id, service_name, region FROM scope) s USING (incident_id)
JOIN `__PROJECT_ID__.sre_incident_mart.customer_accounts` ca
  ON ca.service_name = s.service_name AND ca.region = s.region
JOIN `__PROJECT_ID__.sre_agent_ops.sla_policy` p ON p.tier = ca.tier;
