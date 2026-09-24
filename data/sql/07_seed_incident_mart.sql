-- 07_seed_incident_mart.sql
CREATE SCHEMA IF NOT EXISTS `__PROJECT_ID__.sre_incident_mart`
  OPTIONS(location = '__LOCATION__', description = 'Incidents, customer accounts, remediation logs, and postmortems');

CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_incident_mart.incidents`
(
  incident_id STRING,
  title STRING,
  status STRING,
  severity STRING,
  affected_region STRING,
  started_at TIMESTAMP,
  resolved_at TIMESTAMP,
  customer_tier_impacted STRING
);

CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_incident_mart.correlated_alerts`
(
  incident_id STRING,
  alert_id STRING
);

CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_incident_mart.remediation_logs`
(
  execution_id STRING,
  incident_id STRING,
  runbook_id STRING,
  executed_script STRING,
  executed_by STRING,
  hitl_approved BOOL,
  status STRING,
  error_output STRING,
  started_at TIMESTAMP,
  finished_at TIMESTAMP
);

MERGE INTO `__PROJECT_ID__.sre_incident_mart.incidents` AS T
USING (
  SELECT
    incident_id,
    title,
    status,
    severity,
    affected_region,
    TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL started_hours_ago HOUR) AS started_at,
    IF(status = 'RESOLVED',
       TIMESTAMP_SUB(CURRENT_TIMESTAMP(),
         INTERVAL (started_hours_ago * 60 - duration_minutes) MINUTE),
       NULL) AS resolved_at,
    customer_tier_impacted
  FROM UNNEST(ARRAY<STRUCT<
    incident_id STRING,
    title STRING,
    status STRING,
    severity STRING,
    affected_region STRING,
    started_hours_ago INT64,
    duration_minutes INT64,
    customer_tier_impacted STRING
  >>[

  ('inc-5001', 'Billing service latency degradation',
   'RESOLVED', 'P2', 'ca-central-1', 312, 47, 'GOLD'),

  ('inc-5002', 'Customer billing DB connection pool saturation',
   'RESOLVED', 'P1', 'ca-central-1', 286, 92, 'GOLD'),
  ('inc-5003', 'Edge gateway 5xx surge during peak window',
   'RESOLVED', 'P2', 'ca-east-1', 251, 38, 'SILVER'),
  ('inc-5004', 'Payment gateway TLS certificate expiry',
   'RESOLVED', 'P1', 'ca-central-1', 224, 143, 'GOLD'),
  ('inc-5005', 'CRM API unindexed query full-table scan',
   'RESOLVED', 'P3', 'ca-west-1', 198, 64, 'BRONZE'),
  ('inc-5006', 'Notification service memory leak OOM restarts',
   'RESOLVED', 'P2', 'ca-east-1', 172, 71, 'SILVER'),
  ('inc-5007', 'BGP peering flap on west regional transit',
   'RESOLVED', 'P1', 'ca-west-1', 145, 26, 'SILVER'),
  ('inc-5008', 'Data warehouse replica lag breach',
   'RESOLVED', 'P3', 'ca-central-1', 119, 110, 'BRONZE'),
  ('inc-5009', 'Auth service database lock contention',
   'RESOLVED', 'P2', 'ca-central-1',  93, 55, 'GOLD'),
  ('inc-5010', 'Provisioning service disk pressure on edge nodes',
   'RESOLVED', 'P3', 'ca-east-1',  67, 11, 'BRONZE'),
  ('inc-5011', 'VoIP gateway packet loss intermittent',
   'RESOLVED', 'P2', 'ca-west-1',  41, 33, 'SILVER'),

  ('inc-5012', 'Mobile backend elevated error rate under investigation',
   'MONITORING', 'P3', 'ca-east-1', 6, 0, 'BRONZE'),

  ('inc-5013', 'Kubernetes ingress controller liveness probe CrashLoopBackOff',
   'RESOLVED', 'P2', 'ca-east-1', 160, 44, 'SILVER'),
  ('inc-5014', 'Kafka event bus consumer group rebalance lag breach',
   'RESOLVED', 'P2', 'ca-central-1', 132, 78, 'GOLD'),
  ('inc-5015', 'Redis session cluster single-shard hot-key eviction surge',
   'RESOLVED', 'P2', 'ca-west-1', 108, 52, 'SILVER'),
  ('inc-5016', 'DNS authoritative resolver SERVFAIL spike on stale ZSK RRSIG',
   'RESOLVED', 'P1', 'ca-east-1', 80, 89, 'SILVER'),
  ('inc-5017', '5G Core AMF N2 SCTP multi-homed association drop',
   'RESOLVED', 'P1', 'ca-central-1', 54, 50, 'GOLD'),
  ('inc-5018', 'CDN origin shield cache miss stampede on live manifest',
   'INVESTIGATING', 'P2', 'ca-west-1', 3, 0, 'SILVER')
  ])
) AS S
ON T.incident_id = S.incident_id
WHEN MATCHED THEN
  UPDATE SET
    title = S.title,
    status = S.status,
    severity = S.severity,
    affected_region = S.affected_region,
    started_at = S.started_at,
    resolved_at = S.resolved_at,
    customer_tier_impacted = S.customer_tier_impacted
WHEN NOT MATCHED THEN
  INSERT (incident_id, title, status, severity, affected_region, started_at, resolved_at, customer_tier_impacted)
  VALUES (S.incident_id, S.title, S.status, S.severity, S.affected_region, S.started_at, S.resolved_at, S.customer_tier_impacted);

MERGE INTO `__PROJECT_ID__.sre_incident_mart.correlated_alerts` AS T
USING (
  WITH
  alerts_with_region AS (
    SELECT a.alert_id, a.timestamp, n.region
    FROM `__PROJECT_ID__.sre_telemetry.alert_stream` a
    JOIN `__PROJECT_ID__.sre_topology.network_nodes` n
      USING (node_id)
  ),
  ranked AS (
    SELECT
      i.incident_id,
      ar.alert_id,
      ROW_NUMBER() OVER (
        PARTITION BY i.incident_id ORDER BY ar.timestamp
      ) AS rn
    FROM `__PROJECT_ID__.sre_incident_mart.incidents` i
    JOIN alerts_with_region ar
      ON ar.region = i.affected_region
     AND ar.timestamp BETWEEN i.started_at
                          AND COALESCE(i.resolved_at,
                                       TIMESTAMP_ADD(i.started_at, INTERVAL 60 MINUTE))
  )
  SELECT incident_id, alert_id
  FROM ranked
  WHERE rn <= 8
) AS S
ON T.incident_id = S.incident_id AND T.alert_id = S.alert_id
WHEN NOT MATCHED THEN
  INSERT (incident_id, alert_id)
  VALUES (S.incident_id, S.alert_id);

MERGE INTO `__PROJECT_ID__.sre_incident_mart.remediation_logs` AS T
USING (
  SELECT
    execution_id,
    incident_id,
    runbook_id,
    executed_script,
    executed_by,
    hitl_approved,
    status,
    error_output,
    TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL started_minutes_ago MINUTE) AS started_at,
    TIMESTAMP_ADD(
      TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL started_minutes_ago MINUTE),
      INTERVAL duration_seconds SECOND) AS finished_at
  FROM UNNEST(ARRAY<STRUCT<
    execution_id STRING,
    incident_id STRING,
    runbook_id STRING,
    executed_script STRING,
    executed_by STRING,
    hitl_approved BOOL,
    status STRING,
    error_output STRING,
    started_minutes_ago INT64,
    duration_seconds INT64
  >>[
  ('exec-9001', 'inc-5001', 'sop-101',
   'kubectl rollout restart deployment/billing-service -n prod',
   'sre-agent', TRUE, 'SUCCESS', NULL, 18700, 42),

  ('exec-9002', 'inc-5002', 'sop-102',
   'psql -c "ALTER SYSTEM SET max_connections = 400;" && kubectl rollout restart deployment/billing-api -n prod',
   'sre-agent', TRUE, 'SUCCESS', NULL, 17100, 88),

  ('exec-9003', 'inc-5003', 'sop-106',
   'kubectl scale deployment/edge-gateway --replicas=6 -n prod',
   'sre-agent', FALSE, 'SUCCESS', NULL, 15020, 31),

  ('exec-9004', 'inc-5004', 'sop-108',
   'certbot renew --cert-name payment-gateway.bell.ca --deploy-hook "systemctl reload nginx"',
   'sre-agent', TRUE, 'ROLLED_BACK',
   'Renewal succeeded but nginx reload failed health check; reverted to previous cert bundle.',
   13400, 176),

  ('exec-9005', 'inc-5005', 'sop-105',
   'psql -c "CREATE INDEX CONCURRENTLY idx_crm_account_lookup ON accounts(account_ref);"',
   'sre-agent', TRUE, 'SUCCESS', NULL, 11850, 240),

  ('exec-9006', 'inc-5006', 'sop-103',
   'kubectl rollout restart deployment/notification-service -n prod',
   'sre-agent', FALSE, 'SUCCESS', NULL, 10300, 27),

  ('exec-9007', 'inc-5007', 'sop-109',
   'vtysh -c "clear bgp 10.40.0.1 soft in"',
   'network-oncall', TRUE, 'SUCCESS', NULL, 8680, 19),

  ('exec-9008', 'inc-5008', 'sop-110',
   'psql -c "SELECT pg_wal_replay_resume();"',
   'sre-agent', TRUE, 'FAILED',
   'Replica apply lag exceeded 900s; resume did not converge within timeout.',
   7100, 310),

  ('exec-9009', 'inc-5009', 'sop-104',
   'psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state = \'idle in transaction\' AND xact_start < now() - interval \'10 minutes\';"',
   'sre-agent', TRUE, 'SUCCESS', NULL, 5560, 63),

  ('exec-9010', 'inc-5010', 'sop-107',
   'logrotate --force /etc/logrotate.d/edge && journalctl --vacuum-size=200M',
   'sre-agent', FALSE, 'SUCCESS', NULL, 4000, 24),

  ('exec-9011', 'inc-5011', 'sop-109',
   'vtysh -c "clear bgp 10.60.0.1 soft in"',
   'sre-agent', TRUE, 'FAILED',
   'Session re-flapped within 30s of clear; escalated to carrier.',
   2460, 45),

  ('exec-9012', 'inc-5011', 'sop-109',
   'vtysh -c "clear bgp 10.60.0.1 soft in" && vtysh -c "bgp graceful-restart"',
   'network-oncall', TRUE, 'ROLLED_BACK',
   'Graceful-restart flag caused adjacency reset on peer; configuration reverted.',
   2400, 52),

  ('exec-9013', 'inc-5013', 'sop-111',
   'kubectl -n ingress patch deployment kubernetes-ingress --type=json -p=\'[{"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/initialDelaySeconds","value":60}]\'',
   'sre-agent', TRUE, 'SUCCESS', NULL, 9580, 39),

  ('exec-9014', 'inc-5014', 'sop-112',
   'kubectl -n streaming set env deployment/kafka-telemetry-consumer KAFKA_MAX_POLL_RECORDS=150 && kubectl -n streaming scale deployment/kafka-telemetry-consumer --replicas=12',
   'sre-agent', TRUE, 'SUCCESS', NULL, 7900, 74),

  ('exec-9015', 'inc-5015', 'sop-113',
   'redis-cli -h redis-session-store.internal CONFIG SET maxmemory-policy volatile-lru',
   'sre-agent', FALSE, 'SUCCESS', NULL, 6450, 18),

  ('exec-9016', 'inc-5016', 'sop-114',
   'rndc sign bell.ca.internal && rndc reload bell.ca.internal && unbound-control flush_zone bell.ca.internal',
   'sre-agent', TRUE, 'FAILED',
   'Primary hidden master HSM PKCS#11 token session timed out on first signing attempt.',
   4820, 95),

  ('exec-9017', 'inc-5016', 'sop-114',
   'rndc sign bell.ca.internal && rndc reload bell.ca.internal && unbound-control flush_zone bell.ca.internal',
   'dns-oncall', TRUE, 'SUCCESS', NULL, 4760, 41),

  ('exec-9018', 'inc-5017', 'sop-115',
   'kubectl -n 5g-core annotate amfdeployment amf-set-01 sctp-preferred-path="secondary" --overwrite',
   'sre-agent', TRUE, 'ROLLED_BACK',
   'Secondary SCTP path exhibited asymmetric MTU 1460 drops; drained to amf-set-02 instead.',
   3220, 64)
  ])
) AS S
ON T.execution_id = S.execution_id
WHEN MATCHED THEN
  UPDATE SET
    incident_id = S.incident_id,
    runbook_id = S.runbook_id,
    executed_script = S.executed_script,
    executed_by = S.executed_by,
    hitl_approved = S.hitl_approved,
    status = S.status,
    error_output = S.error_output,
    started_at = S.started_at,
    finished_at = S.finished_at
WHEN NOT MATCHED THEN
  INSERT (execution_id, incident_id, runbook_id, executed_script, executed_by, hitl_approved, status, error_output, started_at, finished_at)
  VALUES (S.execution_id, S.incident_id, S.runbook_id, S.executed_script, S.executed_by, S.hitl_approved, S.status, S.error_output, S.started_at, S.finished_at);
