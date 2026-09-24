-- 05_seed_topology.sql
CREATE SCHEMA IF NOT EXISTS `__PROJECT_ID__.sre_topology`
  OPTIONS(location = '__LOCATION__', description = 'Network topology and dependency nodes');

CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_topology.network_nodes`
(
  node_id STRING,
  node_name STRING,
  node_type STRING,
  region STRING,
  ip_address STRING,
  status STRING
);

MERGE INTO `__PROJECT_ID__.sre_topology.network_nodes` AS T
USING (
  SELECT
    node_id,
    node_name,
    node_type,
    region,
    ip_address,
    status
  FROM UNNEST(ARRAY<STRUCT<
          node_id STRING,
          node_name STRING,
          node_type STRING,
          region STRING,
          ip_address STRING,
          status STRING>>[

  ('node-cc1-edge-01',  'edge-node-tor-01',               'edge_node',     'ca-central-1', '10.10.1.11', 'degraded'),
  ('node-cc1-edge-02',  'edge-node-mtl-01',               'edge_node',     'ca-central-1', '10.10.1.12', 'healthy'),
  ('node-gateway-01',   'edge-gateway-01',                'gateway',       'ca-central-1', '10.10.0.11', 'healthy'),
  ('node-cc1-gw-02',    'payment-gateway-01',             'gateway',       'ca-central-1', '10.10.0.12', 'healthy'),
  ('node-cc1-gw-03',    'voip-gateway-01',                'gateway',       'ca-central-1', '10.10.0.13', 'healthy'),
  ('node-cc1-lb-01',    'billing-lb-01',                  'load_balancer', 'ca-central-1', '10.10.2.11', 'healthy'),
  ('node-cc1-lb-02',    'payment-lb-01',                  'load_balancer', 'ca-central-1', '10.10.2.12', 'healthy'),
  ('node-vm-01',        'billing-service-vm-01',          'vm',            'ca-central-1', '10.10.3.11', 'healthy'),
  ('node-cc1-vm-02',    'billing-service-vm-02',          'vm',            'ca-central-1', '10.10.3.12', 'healthy'),
  ('node-cc1-vm-03',    'payment-gateway-vm-01',          'vm',            'ca-central-1', '10.10.3.13', 'healthy'),
  ('node-cc1-vm-04',    'auth-service-vm-01',             'vm',            'ca-central-1', '10.10.3.14', 'healthy'),
  ('node-cc1-vm-05',    'crm-api-vm-01',                  'vm',            'ca-central-1', '10.10.3.15', 'healthy'),
  ('node-cc1-vm-06',    'notification-service-vm-01',     'vm',            'ca-central-1', '10.10.3.16', 'healthy'),
  ('node-cc1-cache-01', 'billing-session-cache-01',       'cache',         'ca-central-1', '10.10.4.11', 'healthy'),
  ('node-cc1-cache-02', 'payment-token-cache-01',         'cache',         'ca-central-1', '10.10.4.12', 'degraded'),

  ('node-db-01',        'customer-billing-db-primary',    'database',      'ca-central-1', '10.10.5.11', 'degraded'),
  ('node-cc1-db-02',    'customer-billing-db-replica-01', 'database',      'ca-central-1', '10.10.5.12', 'healthy'),
  ('node-cc1-db-03',    'auth-identity-db-01',            'database',      'ca-central-1', '10.10.5.13', 'healthy'),

  ('node-ce1-edge-01',  'edge-node-hfx-01',               'edge_node',     'ca-east-1',    '10.20.1.11', 'healthy'),
  ('node-ce1-edge-02',  'edge-node-qbc-01',               'edge_node',     'ca-east-1',    '10.20.1.12', 'degraded'),
  ('node-ce1-gw-01',    'edge-gateway-ce1-01',            'gateway',       'ca-east-1',    '10.20.0.11', 'healthy'),
  ('node-ce1-gw-02',    'voip-gateway-ce1-01',            'gateway',       'ca-east-1',    '10.20.0.12', 'healthy'),
  ('node-ce1-lb-01',    'billing-lb-ce1-01',              'load_balancer', 'ca-east-1',    '10.20.2.11', 'healthy'),
  ('node-ce1-vm-01',    'billing-service-vm-ce1-01',      'vm',            'ca-east-1',    '10.20.3.11', 'healthy'),
  ('node-ce1-vm-02',    'crm-api-vm-ce1-01',              'vm',            'ca-east-1',    '10.20.3.12', 'healthy'),
  ('node-ce1-vm-03',    'mobile-backend-vm-ce1-01',       'vm',            'ca-east-1',    '10.20.3.13', 'healthy'),
  ('node-ce1-vm-04',    'provisioning-service-vm-ce1-01', 'vm',            'ca-east-1',    '10.20.3.14', 'healthy'),
  ('node-ce1-cache-01', 'mobile-session-cache-ce1-01',    'cache',         'ca-east-1',    '10.20.4.11', 'healthy'),
  ('node-ce1-db-01',    'customer-billing-db-replica-02', 'database',      'ca-east-1',    '10.20.5.11', 'healthy'),
  ('node-ce1-db-02',    'data-warehouse-ce1-01',          'database',      'ca-east-1',    '10.20.5.12', 'healthy'),

  ('node-cw1-edge-01',  'edge-node-yvr-01',               'edge_node',     'ca-west-1',    '10.30.1.11', 'degraded'),
  ('node-cw1-edge-02',  'edge-node-yyc-01',               'edge_node',     'ca-west-1',    '10.30.1.12', 'healthy'),
  ('node-cw1-gw-01',    'edge-gateway-cw1-01',            'gateway',       'ca-west-1',    '10.30.0.11', 'healthy'),
  ('node-cw1-gw-02',    'payment-gateway-cw1-01',         'gateway',       'ca-west-1',    '10.30.0.12', 'healthy'),
  ('node-cw1-lb-01',    'mobile-lb-cw1-01',               'load_balancer', 'ca-west-1',    '10.30.2.11', 'healthy'),
  ('node-cw1-vm-01',    'mobile-backend-vm-cw1-01',       'vm',            'ca-west-1',    '10.30.3.11', 'healthy'),
  ('node-cw1-vm-02',    'provisioning-service-vm-cw1-01', 'vm',            'ca-west-1',    '10.30.3.12', 'healthy'),
  ('node-cw1-vm-03',    'network-monitor-vm-cw1-01',      'vm',            'ca-west-1',    '10.30.3.13', 'healthy'),
  ('node-cw1-cache-01', 'crm-cache-cw1-01',               'cache',         'ca-west-1',    '10.30.4.11', 'healthy'),
  ('node-cw1-db-01',    'data-warehouse-cw1-01',          'database',      'ca-west-1',    '10.30.5.11', 'healthy'),

  ('node-cc1-5g-01',    '5g-core-amf-cc1-01',             'gateway',       'ca-central-1', '10.10.6.11', 'healthy'),
  ('node-cc1-5g-02',    '5g-core-amf-cc1-02',             'gateway',       'ca-central-1', '10.10.6.12', 'healthy'),
  ('node-ce1-5g-01',    '5g-core-amf-ce1-01',             'gateway',       'ca-east-1',    '10.20.6.11', 'healthy'),
  ('node-cw1-5g-01',    '5g-core-amf-cw1-01',             'gateway',       'ca-west-1',    '10.30.6.11', 'healthy'),

  ('node-cc1-kafka-01', 'kafka-event-broker-cc1-01',      'vm',            'ca-central-1', '10.10.7.11', 'healthy'),
  ('node-cc1-kafka-02', 'kafka-event-broker-cc1-02',      'vm',            'ca-central-1', '10.10.7.12', 'healthy'),
  ('node-ce1-kafka-01', 'kafka-event-broker-ce1-01',      'vm',            'ca-east-1',    '10.20.7.11', 'healthy'),

  ('node-cc1-redis-01', 'redis-session-cluster-cc1-01',   'cache',         'ca-central-1', '10.10.4.21', 'healthy'),
  ('node-cw1-redis-01', 'redis-session-cluster-cw1-01',   'cache',         'ca-west-1',    '10.30.4.21', 'healthy'),

  ('node-cc1-dns-01',   'dns-authoritative-cc1-01',       'edge_node',     'ca-central-1', '10.10.0.53', 'healthy'),
  ('node-ce1-dns-01',   'dns-recursor-ce1-01',            'edge_node',     'ca-east-1',    '10.20.0.53', 'healthy'),
  ('node-cw1-dns-01',   'dns-recursor-cw1-01',            'edge_node',     'ca-west-1',    '10.30.0.53', 'healthy'),

  ('node-cc1-cdn-01',   'cdn-origin-shield-cc1-01',       'edge_node',     'ca-central-1', '10.10.8.11', 'healthy'),
  ('node-ce1-cdn-01',   'cdn-edge-pop-ce1-01',            'edge_node',     'ca-east-1',    '10.20.8.11', 'healthy'),
  ('node-cw1-cdn-01',   'cdn-edge-pop-cw1-01',            'edge_node',     'ca-west-1',    '10.30.8.11', 'healthy'),

  ('node-cc1-olt-01',   'fiber-olt-chassis-tor-01',       'edge_node',     'ca-central-1', '10.10.9.11', 'healthy'),
  ('node-ce1-olt-01',   'fiber-olt-chassis-hfx-01',       'edge_node',     'ca-east-1',    '10.20.9.11', 'healthy'),
  ('node-cw1-olt-01',   'fiber-olt-chassis-yvr-01',       'edge_node',     'ca-west-1',    '10.30.9.11', 'healthy'),

  ('node-cc1-k8s-01',   'kubernetes-ingress-ctrl-cc1-01', 'load_balancer', 'ca-central-1', '10.10.2.21', 'healthy'),
  ('node-ce1-k8s-01',   'kubernetes-ingress-ctrl-ce1-01', 'load_balancer', 'ca-east-1',    '10.20.2.21', 'healthy'),
  ('node-cw1-k8s-01',   'kubernetes-ingress-ctrl-cw1-01', 'load_balancer', 'ca-west-1',    '10.30.2.21', 'healthy'),

  ('node-cc1-iot-01',   'iot-telemetry-hub-cc1-01',       'vm',            'ca-central-1', '10.10.3.25', 'healthy'),
  ('node-ce1-iot-01',   'iot-telemetry-hub-ce1-01',       'vm',            'ca-east-1',    '10.20.3.25', 'healthy'),
  ('node-cw1-iot-01',   'iot-telemetry-hub-cw1-01',       'vm',            'ca-west-1',    '10.30.3.25', 'healthy')
  ])
) AS S
ON T.node_id = S.node_id
WHEN MATCHED THEN
  UPDATE SET
    node_name = S.node_name,
    node_type = S.node_type,
    region = S.region,
    ip_address = S.ip_address,
    status = S.status
WHEN NOT MATCHED THEN
  INSERT (node_id, node_name, node_type, region, ip_address, status)
  VALUES (S.node_id, S.node_name, S.node_type, S.region, S.ip_address, S.status);
