-- v1_tables.sql
SELECT
  'alert_stream' AS check_name,
  '__LOCATION__' AS expected_location,
  COUNT(*) AS row_count,
  COUNT(DISTINCT alert_id) AS distinct_keys,
  COUNT(*) - COUNT(DISTINCT alert_id) AS duplicate_keys
FROM `__PROJECT_ID__.sre_telemetry.alert_stream`
UNION ALL
SELECT
  'network_nodes',
  '__LOCATION__',
  COUNT(*),
  COUNT(DISTINCT node_id),
  COUNT(*) - COUNT(DISTINCT node_id)
FROM `__PROJECT_ID__.sre_topology.network_nodes`
UNION ALL
SELECT
  'runbooks',
  '__LOCATION__',
  COUNT(*),
  COUNT(DISTINCT runbook_id),
  COUNT(*) - COUNT(DISTINCT runbook_id)
FROM `__PROJECT_ID__.sre_knowledge_base.runbooks`
UNION ALL
SELECT
  'customer_accounts',
  '__LOCATION__',
  COUNT(*),
  COUNT(DISTINCT customer_id),
  COUNT(*) - COUNT(DISTINCT customer_id)
FROM `__PROJECT_ID__.sre_incident_mart.customer_accounts`
UNION ALL
SELECT
  'incidents',
  '__LOCATION__',
  COUNT(*),
  COUNT(DISTINCT incident_id),
  COUNT(*) - COUNT(DISTINCT incident_id)
FROM `__PROJECT_ID__.sre_incident_mart.incidents`
UNION ALL
SELECT
  'correlated_alerts',
  '__LOCATION__',
  COUNT(*),
  COUNT(DISTINCT CONCAT(incident_id, ':', alert_id)),
  COUNT(*) - COUNT(DISTINCT CONCAT(incident_id, ':', alert_id))
FROM `__PROJECT_ID__.sre_incident_mart.correlated_alerts`
UNION ALL
SELECT
  'remediation_logs',
  '__LOCATION__',
  COUNT(*),
  COUNT(DISTINCT execution_id),
  COUNT(*) - COUNT(DISTINCT execution_id)
FROM `__PROJECT_ID__.sre_incident_mart.remediation_logs`
UNION ALL
SELECT
  'incident_postmortems',
  '__LOCATION__',
  COUNT(*),
  COUNT(DISTINCT postmortem_id),
  COUNT(*) - COUNT(DISTINCT postmortem_id)
FROM `__PROJECT_ID__.sre_incident_mart.incident_postmortems`
UNION ALL
SELECT
  'broken_fk_correlated_to_incidents',
  '__LOCATION__',
  COUNTIF(i.incident_id IS NULL),
  0,
  COUNTIF(i.incident_id IS NULL)
FROM `__PROJECT_ID__.sre_incident_mart.correlated_alerts` c
LEFT JOIN `__PROJECT_ID__.sre_incident_mart.incidents` i USING (incident_id)
UNION ALL
SELECT
  'broken_fk_correlated_to_alerts',
  '__LOCATION__',
  COUNTIF(a.alert_id IS NULL),
  0,
  COUNTIF(a.alert_id IS NULL)
FROM `__PROJECT_ID__.sre_incident_mart.correlated_alerts` c
LEFT JOIN `__PROJECT_ID__.sre_telemetry.alert_stream` a USING (alert_id)
ORDER BY check_name;
