-- 06_seed_telemetry.sql
CREATE SCHEMA IF NOT EXISTS `__PROJECT_ID__.sre_telemetry`
  OPTIONS(location = '__LOCATION__', description = 'Live and historical SRE alert telemetry');

CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_telemetry.alert_stream`
(
  alert_id STRING,
  node_id STRING,
  service_name STRING,
  severity STRING,
  alert_type STRING,
  message STRING,
  measured_value FLOAT64,
  timestamp TIMESTAMP
)
PARTITION BY DATE(timestamp);

MERGE INTO `__PROJECT_ID__.sre_telemetry.alert_stream` AS T
USING (
WITH cfg AS (
  SELECT

    ARRAY<STRUCT<service_name STRING, node_id STRING>>[

      ('billing-service',      'node-vm-01'),
      ('billing-service',      'node-cc1-vm-02'),
      ('customer-billing-db',  'node-db-01'),
      ('customer-billing-db',  'node-cc1-db-02'),
      ('edge-gateway',         'node-gateway-01'),
      ('edge-gateway',         'node-cc1-edge-01'),
      ('auth-service',         'node-cc1-vm-04'),
      ('auth-service',         'node-cc1-db-03'),
      ('payment-gateway',      'node-cc1-gw-02'),
      ('payment-gateway',      'node-cc1-vm-03'),
      ('crm-api',              'node-cc1-vm-05'),
      ('voip-gateway',         'node-cc1-gw-03'),

      ('billing-service',      'node-ce1-vm-01'),
      ('billing-service',      'node-ce1-lb-01'),
      ('customer-billing-db',  'node-ce1-db-01'),
      ('edge-gateway',         'node-ce1-gw-01'),
      ('crm-api',              'node-ce1-vm-02'),
      ('mobile-backend',       'node-ce1-vm-03'),
      ('mobile-backend',       'node-ce1-cache-01'),
      ('data-warehouse',       'node-ce1-db-02'),
      ('provisioning-service', 'node-ce1-vm-04'),
      ('voip-gateway',         'node-ce1-gw-02'),
      ('network-monitor',      'node-ce1-edge-01'),
      ('notification-service', 'node-ce1-edge-02'),

      ('edge-gateway',         'node-cw1-gw-01'),
      ('edge-gateway',         'node-cw1-edge-01'),
      ('payment-gateway',      'node-cw1-gw-02'),
      ('mobile-backend',       'node-cw1-vm-01'),
      ('mobile-backend',       'node-cw1-lb-01'),
      ('provisioning-service', 'node-cw1-vm-02'),
      ('provisioning-service', 'node-cw1-edge-02'),
      ('network-monitor',      'node-cw1-vm-03'),
      ('data-warehouse',       'node-cw1-db-01'),
      ('crm-api',              'node-cw1-cache-01'),

      ('notification-service', 'node-cw1-vm-01'),
      ('auth-service',         'node-cw1-gw-01')
    ] AS placements,

    ARRAY<STRUCT<alert_type STRING, severity STRING, vmin FLOAT64, vspan FLOAT64>>[
      ('cpu_utilization_high',   'WARNING',  70.0,   22.0),
      ('memory_pressure',        'WARNING',  72.0,   18.0),
      ('slow_query_warning',     'WARNING', 800.0, 3200.0),
      ('tls_cert_expiry_warning','INFO',      7.0,   38.0),
      ('packet_loss_minor',      'WARNING',   0.5,    3.0),
      ('cache_miss_rate_high',   'WARNING',  20.0,   35.0),
      ('backup_job_delayed',     'INFO',     10.0,  170.0),
      ('config_drift_detected',  'INFO',      1.0,    5.0),
      ('session_pool_warning',   'WARNING',  55.0,   25.0),
      ('api_rate_limit_warning', 'INFO',     60.0,   30.0),
      ('ntp_offset_drift',       'INFO',     50.0,  350.0),
      ('log_ingest_lag',         'WARNING',  30.0,  570.0)
    ] AS noise_types,

    ARRAY<STRUCT<service_name STRING, node_id STRING, alert_type STRING>>[
      ('billing-service', 'node-vm-01',        'gateway_5xx_surge'),
      ('billing-service', 'node-vm-01',        'latency_spike'),
      ('billing-service', 'node-cc1-vm-02',    'gateway_5xx_surge'),
      ('billing-service', 'node-cc1-vm-02',    'latency_spike'),
      ('billing-service', 'node-cc1-lb-01',    'gateway_5xx_surge'),
      ('billing-service', 'node-cc1-lb-01',    'latency_spike'),
      ('payment-gateway', 'node-cc1-gw-02',    'gateway_5xx_surge'),
      ('payment-gateway', 'node-cc1-gw-02',    'latency_spike'),
      ('payment-gateway', 'node-cc1-vm-03',    'gateway_5xx_surge'),
      ('payment-gateway', 'node-cc1-lb-02',    'latency_spike'),
      ('payment-gateway', 'node-cc1-cache-02', 'latency_spike'),
      ('edge-gateway',    'node-gateway-01',   'gateway_5xx_surge'),
      ('edge-gateway',    'node-gateway-01',   'latency_spike'),
      ('edge-gateway',    'node-cc1-edge-01',  'gateway_5xx_surge'),
      ('edge-gateway',    'node-cc1-edge-02',  'latency_spike')
    ] AS cascade_targets,

    ARRAY<STRUCT<node_id STRING>>[
      STRUCT('node-cc1-edge-01'),
      STRUCT('node-cc1-edge-02'),
      STRUCT('node-ce1-edge-01'),
      STRUCT('node-ce1-edge-02'),
      STRUCT('node-cw1-edge-01'),
      STRUCT('node-cw1-edge-02')
    ] AS edge_nodes,

    TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 MINUTE) AS storm_t0
),

telemetry_band_d AS (
  SELECT
    FORMAT('ALT-NOISE-%06d', i) AS alert_id,
    p.node_id,
    p.service_name,
    t.severity,
    t.alert_type,

    CASE t.alert_type
      WHEN 'cpu_utilization_high'    THEN FORMAT('CPU utilization sustained at %.1f%% on %s over a 5m window (warn 70%%)', v, p.node_id)
      WHEN 'memory_pressure'         THEN FORMAT('Memory working set at %.1f%% on %s; kswapd reclaiming (warn 75%%)', v, p.node_id)
      WHEN 'slow_query_warning'      THEN FORMAT('Slow query detected: %.1f ms execution time on %s (warn 800 ms)', v, p.node_id)
      WHEN 'tls_cert_expiry_warning' THEN FORMAT('TLS leaf certificate expires in %.1f days on %s -- schedule renewal', v, p.node_id)
      WHEN 'packet_loss_minor'       THEN FORMAT('Upstream packet loss %.2f%% observed on %s (warn 0.5%%)', v, p.node_id)
      WHEN 'cache_miss_rate_high'    THEN FORMAT('Cache miss rate %.1f%% on %s over 15m (warn 20%%)', v, p.node_id)
      WHEN 'backup_job_delayed'      THEN FORMAT('Nightly backup job running %.1f minutes behind schedule on %s', v, p.node_id)
      WHEN 'config_drift_detected'   THEN FORMAT('%.0f configuration items drifted from Terraform baseline on %s', v, p.node_id)
      WHEN 'session_pool_warning'    THEN FORMAT('Session pool utilisation at %.1f%% on %s (warn 55%%)', v, p.node_id)
      WHEN 'api_rate_limit_warning'  THEN FORMAT('Client API quota consumption at %.1f%% of hourly ceiling on %s', v, p.node_id)
      WHEN 'ntp_offset_drift'        THEN FORMAT('NTP offset drift of %.1f ms on %s exceeds 50 ms advisory', v, p.node_id)
      ELSE                                FORMAT('Log shipping pipeline lagging %.1f s on %s (warn 30 s)', v, p.node_id)
    END AS message,
    ROUND(v, 2) AS measured_value,
    TIMESTAMP_SUB(
      CURRENT_TIMESTAMP(),

      INTERVAL ABS(MOD(FARM_FINGERPRINT(FORMAT('bce26-telemetry_band_d-ts-%d', i)), 604800)) SECOND
    ) AS timestamp
  FROM cfg,
    UNNEST(GENERATE_ARRAY(1, 1400)) AS i,

    UNNEST([cfg.placements[OFFSET(
      ABS(MOD(FARM_FINGERPRINT(FORMAT('bce26-telemetry_band_d-place-%d', i)), ARRAY_LENGTH(cfg.placements))))]]) AS p,
    UNNEST([cfg.noise_types[OFFSET(
      ABS(MOD(FARM_FINGERPRINT(FORMAT('bce26-telemetry_band_d-type-%d', i)), ARRAY_LENGTH(cfg.noise_types))))]]) AS t,
    UNNEST([t.vmin + t.vspan *
      (ABS(MOD(FARM_FINGERPRINT(FORMAT('bce26-telemetry_band_d-val-%d', i)), 1000)) / 1000.0)]) AS v
),

telemetry_band_a AS (
  SELECT
    FORMAT('ALT-STORM-ROOT-%03d', r) AS alert_id,
    'node-db-01' AS node_id,
    'customer-billing-db' AS service_name,
    'CRITICAL' AS severity,
    'connection_pool_exhausted' AS alert_type,
    FORMAT(
      'Connection pool exhausted on customer-billing-db-primary (node-db-01): 200/200 active connections, %d clients queued, acquire timeout 5000 ms exceeded',
      40 + ABS(MOD(FARM_FINGERPRINT(FORMAT('bce26-root-q-%d', r)), 260))
    ) AS message,
    100.0 AS measured_value,
    TIMESTAMP_ADD(
      cfg.storm_t0,
      INTERVAL (CAST(ROUND(28000.0 * (r - 1) / 19.0) AS INT64)
                + ABS(MOD(FARM_FINGERPRINT(FORMAT('bce26-root-j-%d', r)), 800))) MILLISECOND
    ) AS timestamp
  FROM cfg, UNNEST(GENERATE_ARRAY(1, 20)) AS r
),

telemetry_band_b AS (
  SELECT
    FORMAT('ALT-STORM-CASC-%04d', j) AS alert_id,
    c.node_id,
    c.service_name,
    'CRITICAL' AS severity,
    c.alert_type,
    CASE c.alert_type
      WHEN 'gateway_5xx_surge' THEN FORMAT(
        'HTTP 5xx surge on %s via %s: %.1f%% of requests returning 503 upstream_connect_error (SLO 0.5%%)',
        c.service_name, c.node_id, v)
      ELSE FORMAT(
        'p99 latency spike on %s via %s: %.0f ms, upstream db acquire blocking (SLO 400 ms)',
        c.service_name, c.node_id, v)
    END AS message,
    ROUND(v, 2) AS measured_value,
    TIMESTAMP_ADD(
      cfg.storm_t0,
      INTERVAL (CAST(ROUND(1000.0 * (52.0 + 187.0 * POW(j / 430.0, 1.6))) AS INT64)
                + ABS(MOD(FARM_FINGERPRINT(FORMAT('bce26-casc-j-%d', j)), 1000))) MILLISECOND
    ) AS timestamp
  FROM cfg,
    UNNEST(GENERATE_ARRAY(1, 430)) AS j,
    UNNEST([cfg.cascade_targets[OFFSET(
      ABS(MOD(FARM_FINGERPRINT(FORMAT('bce26-casc-tgt-%d', j)), ARRAY_LENGTH(cfg.cascade_targets))))]]) AS c,
    UNNEST([
      CASE c.alert_type

        WHEN 'gateway_5xx_surge'
          THEN 18.0 + 78.0 * (ABS(MOD(FARM_FINGERPRINT(FORMAT('bce26-casc-v-%d', j)), 1000)) / 1000.0)

        ELSE 1200.0 + 7800.0 * (ABS(MOD(FARM_FINGERPRINT(FORMAT('bce26-casc-v-%d', j)), 1000)) / 1000.0)
      END
    ]) AS v
),

telemetry_band_c AS (
  SELECT
    FORMAT('ALT-RAMP-%04d', i) AS alert_id,
    e.node_id,
    'edge-gateway' AS service_name,

    IF(v >= 80.0, 'CRITICAL', 'WARNING') AS severity,
    'disk_pressure_edge_node' AS alert_type,
    FORMAT(
      'Disk usage on %s at %.2f%% -- /var/log and session spool growing ~0.31 pp/min, projected to breach 95%% capacity threshold',
      e.node_id, v
    ) AS message,
    ROUND(v, 2) AS measured_value,
    TIMESTAMP_SUB(
      CURRENT_TIMESTAMP(),
      INTERVAL CAST(ROUND((90.0 - 90.0 * i / 149.0) * 60000.0) AS INT64) MILLISECOND
    ) AS timestamp
  FROM cfg,
    UNNEST(GENERATE_ARRAY(0, 149)) AS i,

    UNNEST([cfg.edge_nodes[OFFSET(MOD(i, ARRAY_LENGTH(cfg.edge_nodes)))]]) AS e,
    UNNEST([
      60.0 + 28.0 * i / 149.0
           + (ABS(MOD(FARM_FINGERPRINT(FORMAT('bce26-telemetry_band_c-n-%d', i)), 71)) - 35) / 100.0
    ]) AS v
),

telemetry_band_e AS (
  SELECT
    FORMAT('ALT-DOM-%04d', i) AS alert_id,
    dom.node_id,
    dom.service_name,
    IF(MOD(h1, 5) = 0, 'ERROR', 'WARNING') AS severity,
    dom.alert_type,
    FORMAT('%s on %s (service=%s, measured=%.2f)', dom.alert_type, dom.node_id, dom.service_name, val) AS message,
    ROUND(val, 2) AS measured_value,
    TIMESTAMP_SUB(
      CURRENT_TIMESTAMP(),
      INTERVAL (2700 + MOD(h2, 602100)) SECOND
    ) AS timestamp
  FROM UNNEST(GENERATE_ARRAY(0, 999)) AS i,
    UNNEST([ABS(MOD(FARM_FINGERPRINT(FORMAT('bce26-dom-1-%d', i)), 1000000))]) AS h1,
    UNNEST([ABS(MOD(FARM_FINGERPRINT(FORMAT('bce26-dom-2-%d', i)), 1000000))]) AS h2,
    UNNEST([
      ARRAY<STRUCT<node_id STRING, service_name STRING, alert_type STRING, low FLOAT64, span FLOAT64>>[
        ('node-cc1-5g-01',    '5g-core-amf',          'sctp_retransmit_elevated',  1.2,  8.5),
        ('node-ce1-5g-01',    '5g-core-amf',          'ngap_handover_delay',      18.0, 65.0),
        ('node-cw1-5g-01',    '5g-core-amf',          'sctp_retransmit_elevated',  1.5,  7.0),
        ('node-cc1-kafka-01', 'kafka-event-bus',      'kafka_consumer_lag_warn', 350.0, 2400.0),
        ('node-ce1-kafka-01', 'kafka-event-bus',      'kafka_isr_shrink_transient',1.0,  3.0),
        ('node-cc1-redis-01', 'redis-session-store',  'redis_eviction_rate_warn', 45.0, 320.0),
        ('node-cw1-redis-01', 'redis-session-store',  'redis_fragmentation_ratio', 1.4,  0.9),
        ('node-cc1-dns-01',   'dns-resolver',         'dns_recursion_latency_ms', 22.0, 75.0),
        ('node-ce1-dns-01',   'dns-resolver',         'dns_nxdomain_rate_elevated',8.0, 40.0),
        ('node-cc1-cdn-01',   'cdn-media-origin',     'cdn_cache_hit_ratio_dip',  52.0, 25.0),
        ('node-cw1-cdn-01',   'cdn-media-origin',     'cdn_origin_ttfb_elevated',140.0, 310.0),
        ('node-cc1-olt-01',   'fiber-olt-controller', 'olt_pon_rx_power_marginal',-26.5, 2.2),
        ('node-ce1-olt-01',   'fiber-olt-controller', 'olt_bip8_fec_corrected',   12.0, 95.0),
        ('node-cc1-k8s-01',   'kubernetes-ingress',   'k8s_ingress_4xx_elevated',  3.5, 11.0),
        ('node-cw1-k8s-01',   'kubernetes-ingress',   'k8s_pod_cpu_throttled_pct',15.0, 45.0),
        ('node-cc1-iot-01',   'iot-telemetry-hub',    'mqtt_session_churn_warn',  85.0, 420.0)
      ][OFFSET(MOD(i, 16))]
    ]) AS dom,
    UNNEST([dom.low + (MOD(h1, 1000) / 1000.0) * dom.span]) AS val
)

SELECT alert_id, node_id, service_name, severity, alert_type, message, measured_value, timestamp FROM telemetry_band_d
UNION ALL
SELECT alert_id, node_id, service_name, severity, alert_type, message, measured_value, timestamp FROM telemetry_band_a
UNION ALL
SELECT alert_id, node_id, service_name, severity, alert_type, message, measured_value, timestamp FROM telemetry_band_b
UNION ALL
SELECT alert_id, node_id, service_name, severity, alert_type, message, measured_value, timestamp FROM telemetry_band_c
UNION ALL
SELECT alert_id, node_id, service_name, severity, alert_type, message, measured_value, timestamp FROM telemetry_band_e
) AS S
ON T.alert_id = S.alert_id
WHEN MATCHED THEN
  UPDATE SET
    node_id = S.node_id,
    service_name = S.service_name,
    severity = S.severity,
    alert_type = S.alert_type,
    message = S.message,
    measured_value = S.measured_value,
    timestamp = S.timestamp
WHEN NOT MATCHED THEN
  INSERT (alert_id, node_id, service_name, severity, alert_type, message, measured_value, timestamp)
  VALUES (S.alert_id, S.node_id, S.service_name, S.severity, S.alert_type, S.message, S.measured_value, S.timestamp);
