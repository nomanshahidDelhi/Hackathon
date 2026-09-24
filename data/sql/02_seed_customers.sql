-- 02_seed_customers.sql
CREATE SCHEMA IF NOT EXISTS `__PROJECT_ID__.sre_incident_mart`
  OPTIONS(location = '__LOCATION__', description = 'Incidents, customer accounts, remediation logs, and postmortems');

CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_incident_mart.customer_accounts`
(
  customer_id STRING,
  customer_name STRING,
  service_name STRING,
  tier STRING,
  mrr_cad FLOAT64,
  region STRING
);

MERGE INTO `__PROJECT_ID__.sre_incident_mart.customer_accounts` AS T
USING (
  SELECT
    customer_id,
    customer_name,
    service_name,
    tier,
    mrr_cad,
    region
  FROM UNNEST(ARRAY<STRUCT<
    customer_id STRING,
    customer_name STRING,
    service_name STRING,
    tier STRING,
    mrr_cad FLOAT64,
    region STRING
  >>[

  ('CUST-1001', 'Loblaw Companies Limited',           'billing-service',       'GOLD',   248500.0, 'ca-central-1'),
  ('CUST-1002', 'Bank of Montreal',                   'customer-billing-db',   'GOLD',   212750.0, 'ca-central-1'),
  ('CUST-1003', 'Air Canada',                         'billing-service',       'GOLD',   165300.0, 'ca-central-1'),
  ('CUST-1004', 'Hydro-Quebec',                       'customer-billing-db',   'GOLD',   143900.0, 'ca-central-1'),

  ('CUST-1005', 'Canadian National Railway',          'edge-gateway',          'GOLD',    98400.0, 'ca-east-1'),
  ('CUST-1006', 'Royal Bank of Canada',               '5g-core-amf',           'GOLD',   235000.0, 'ca-central-1'),
  ('CUST-1007', 'Shopify Inc.',                       'cdn-media-origin',      'GOLD',   189200.0, 'ca-central-1'),
  ('CUST-1008', 'Toronto-Dominion Bank',              'kafka-event-bus',       'GOLD',   174600.0, 'ca-east-1'),
  ('CUST-1009', 'Enbridge Pipelines Inc.',            'iot-telemetry-hub',     'GOLD',   118500.0, 'ca-west-1'),

  ('CUST-2001', 'Metro Inc.',                         'payment-gateway',       'SILVER',  78200.0, 'ca-central-1'),
  ('CUST-2002', 'Canada Goose Holdings',              'crm-api',               'SILVER',  66450.0, 'ca-central-1'),
  ('CUST-2003', 'Desjardins Group',                   'billing-service',       'SILVER',  61900.0, 'ca-east-1'),
  ('CUST-2004', 'WestJet Airlines Ltd.',              'mobile-backend',        'SILVER',  57250.0, 'ca-west-1'),
  ('CUST-2005', 'Saputo Inc.',                        'provisioning-service',  'SILVER',  49800.0, 'ca-central-1'),
  ('CUST-2006', 'Alimentation Couche-Tard',           'edge-gateway',          'SILVER',  44300.0, 'ca-east-1'),
  ('CUST-2007', 'McGill University Health Centre',    'voip-gateway',          'SILVER',  38900.0, 'ca-east-1'),
  ('CUST-2008', 'Ontario Teachers Pension Plan',      'auth-service',          'SILVER',  34600.0, 'ca-central-1'),
  ('CUST-2009', 'Suncor Energy Inc.',                 'network-monitor',       'SILVER',  28750.0, 'ca-west-1'),
  ('CUST-2010', 'Bombardier Aerospace',               'kubernetes-ingress',    'SILVER',  74100.0, 'ca-east-1'),
  ('CUST-2011', 'Magna International',                'iot-telemetry-hub',     'SILVER',  68900.0, 'ca-central-1'),
  ('CUST-2012', 'Telus Health Solutions',             'redis-session-store',   'SILVER',  54300.0, 'ca-west-1'),
  ('CUST-2013', 'CBC / Radio-Canada Media',           'cdn-media-origin',      'SILVER',  47600.0, 'ca-east-1'),
  ('CUST-2014', 'Vancouver Airport Authority',        '5g-core-amf',           'SILVER',  41200.0, 'ca-west-1'),
  ('CUST-2015', 'National Bank of Canada',            'dns-resolver',          'SILVER',  36800.0, 'ca-east-1'),
  ('CUST-2016', 'BC Hydro & Power Authority',         'fiber-olt-controller',  'SILVER',  29500.0, 'ca-west-1'),

  ('CUST-3001', 'Moosehead Breweries',                'notification-service',  'BRONZE',  24100.0, 'ca-east-1'),
  ('CUST-3002', 'Vancouver Island Health Authority',  'mobile-backend',        'BRONZE',  21500.0, 'ca-west-1'),
  ('CUST-3003', 'Cirque du Soleil Entertainment',     'crm-api',               'BRONZE',  19800.0, 'ca-central-1'),
  ('CUST-3004', 'Halifax Port Authority',             'voip-gateway',          'BRONZE',  17600.0, 'ca-east-1'),
  ('CUST-3005', 'Boreal Logistics Group',             'data-warehouse',        'BRONZE',  15900.0, 'ca-central-1'),
  ('CUST-3006', 'Prairie Grain Co-operative',         'provisioning-service',  'BRONZE',  14250.0, 'ca-west-1'),

  ('CUST-3007', 'Laurentian Dental Group',            'billing-service',       'BRONZE',  12400.0, 'ca-central-1'),
  ('CUST-3008', 'Yukon Wilderness Tours',             'edge-gateway',          'BRONZE',  10800.0, 'ca-west-1'),
  ('CUST-3009', 'Fundy Seafoods Ltd.',                'payment-gateway',       'BRONZE',   9450.0, 'ca-east-1'),
  ('CUST-3010', 'Northern Lights Media',              'notification-service',  'BRONZE',   7900.0, 'ca-central-1'),
  ('CUST-3011', 'Gaspe Marine Services',              'customer-billing-db',   'BRONZE',   6250.0, 'ca-east-1'),
  ('CUST-3012', 'Okanagan Vineyards Co-op',           'redis-session-store',   'BRONZE',  22800.0, 'ca-west-1'),
  ('CUST-3013', 'Maritime Ferry Operations',          'dns-resolver',          'BRONZE',  20400.0, 'ca-east-1'),
  ('CUST-3014', 'Banff Gondola & Hospitality',        'kubernetes-ingress',    'BRONZE',  18300.0, 'ca-west-1'),
  ('CUST-3015', 'Acadian Forest Products',            'fiber-olt-controller',  'BRONZE',  16700.0, 'ca-east-1'),
  ('CUST-3016', 'Ottawa Valley Transit',              'kafka-event-bus',       'BRONZE',  15100.0, 'ca-central-1'),
  ('CUST-3017', 'Saskatoon Potash Logistics',         'iot-telemetry-hub',     'BRONZE',  13600.0, 'ca-west-1'),
  ('CUST-3018', 'St. Lawrence Seaway Pilots',         '5g-core-amf',           'BRONZE',  11200.0, 'ca-east-1'),
  ('CUST-3019', 'Whistler Alpine Sensors',            'cdn-media-origin',      'BRONZE',   8600.0, 'ca-west-1'),
  ('CUST-3020', 'Newfoundland Offshore Supply',       'data-warehouse',        'BRONZE',   5800.0, 'ca-east-1')

  ])
) AS S
ON T.customer_id = S.customer_id
WHEN MATCHED THEN
  UPDATE SET
    customer_name = S.customer_name,
    service_name = S.service_name,
    tier = S.tier,
    mrr_cad = S.mrr_cad,
    region = S.region
WHEN NOT MATCHED THEN
  INSERT (customer_id, customer_name, service_name, tier, mrr_cad, region)
  VALUES (S.customer_id, S.customer_name, S.service_name, S.tier, S.mrr_cad, S.region);
