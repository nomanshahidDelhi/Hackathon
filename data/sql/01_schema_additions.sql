-- 01_schema_additions.sql
CREATE SCHEMA IF NOT EXISTS `__PROJECT_ID__.sre_telemetry`
  OPTIONS(location = '__LOCATION__', description = 'Live and historical SRE alert telemetry');

CREATE SCHEMA IF NOT EXISTS `__PROJECT_ID__.sre_topology`
  OPTIONS(location = '__LOCATION__', description = 'Network topology and dependency nodes');

CREATE SCHEMA IF NOT EXISTS `__PROJECT_ID__.sre_knowledge_base`
  OPTIONS(location = '__LOCATION__', description = 'SRE runbooks and vector embeddings');

CREATE SCHEMA IF NOT EXISTS `__PROJECT_ID__.sre_incident_mart`
  OPTIONS(location = '__LOCATION__', description = 'Incidents, customer accounts, remediation logs, and postmortems');

CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_incident_mart.incident_postmortems`
(
  postmortem_id STRING
    OPTIONS(description="Stable surrogate key for the postmortem document, e.g. 'pm-2026-0042'. One postmortem per incident is the norm, but the key is separate from incident_id so a re-issued/corrected postmortem can be stored without mutating history."),
  incident_id STRING
    OPTIONS(description="Foreign key to sre_incident_mart.incidents.incident_id. Not enforced by BigQuery — the agent is expected to validate the join itself before publishing."),
  root_cause STRING
    OPTIONS(description="The single verified root cause in prose. Deliberately NOT the alert signature: graders check that the agent distinguishes the causal failure from the cascade symptom it triggered."),
  impact_summary STRING
    OPTIONS(description="Customer-facing blast radius: services degraded, regions affected, and which customer tiers breached SLA. Should be derivable by joining customer_accounts on the affected service."),
  downtime_minutes FLOAT64
    OPTIONS(description="Customer-impacting minutes, FLOAT64 so partial-minute and partial-impact (e.g. 30%-degraded) windows can be recorded. This is the multiplier in the SLA-credit formula."),
  contributing_factors STRING
    OPTIONS(description="Newline-separated list of latent conditions that made the incident possible or worse (missing alerting, stale runbook, absent connection-pool ceiling). One factor per line — blameless phrasing expected."),
  action_items STRING
    OPTIONS(description="Newline-separated list of follow-up actions, one per line, ideally with an owning team. These are the preventive commitments, distinct from the remediation that ended the outage."),
  published_at TIMESTAMP
    OPTIONS(description="When the postmortem was published (UTC). NULL means draft — the Guide asks teams to publish only after the incident is RESOLVED.")
)
OPTIONS(
  description="Human-readable postmortems, one per resolved incident. Populated by the agent at the end of an incident-response run, not by the seed scripts — seeding it would give teams the answer key. Intentionally left empty by 01_schema_additions.sql."
);

CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_incident_mart.customer_accounts`
(
  customer_id STRING
    OPTIONS(description="Bell Business Markets account identifier, e.g. 'CUST-1001'. Primary key."),
  customer_name STRING
    OPTIONS(description="Legal/trading name of the enterprise account, used verbatim in customer-facing impact summaries."),
  service_name STRING
    OPTIONS(description="The Bell-operated service this account consumes. Values are drawn from the same vocabulary as sre_telemetry.alert_stream.service_name so an alert can be joined straight through to revenue at risk."),
  tier STRING
    OPTIONS(description="Contractual support tier: GOLD, SILVER or BRONZE. Drives SLA target and credit percentage — GOLD is the only tier with a sub-hour restoration commitment."),
  mrr_cad FLOAT64
    OPTIONS(description="Monthly recurring revenue in Canadian dollars. The SLA-credit base: credit = mrr_cad * tier_credit_rate * (downtime_minutes / minutes_in_month). Present so the agent never has to guess a revenue figure."),
  region STRING
    OPTIONS(description="Primary serving region for the account: ca-central-1, ca-east-1 or ca-west-1. Matches sre_topology.network_nodes.region so blast radius can be scoped geographically.")
)
OPTIONS(
  description="Enterprise customer book of record for the hackathon: which account sits on which service, at what tier, for how much money. Joined to alert_stream/incidents to turn a technical blast radius into a dollar figure. Seeded by 02_seed_customers.sql."
);

CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_knowledge_base.runbooks`
(
  runbook_id STRING
    OPTIONS(description="Primary key for the Standard Operating Procedure (SOP), e.g. 'sop-101'."),
  title STRING
    OPTIONS(description="Human-readable runbook title."),
  failure_signature STRING
    OPTIONS(description="Alert type / failure signature matched by this SOP."),
  remediation_steps STRING
    OPTIONS(description="Step-by-step triage and remediation guide."),
  rollback_commands STRING
    OPTIONS(description="Commands to revert changes if remediation causes regression."),
  remediation_script STRING
    OPTIONS(description="Automated bash remediation script."),
  embedding ARRAY<FLOAT64>
    OPTIONS(description="768-dimensional vector embedding generated by text-embedding-005.")
)
OPTIONS(
  description="SRE Standard Operating Procedures (SOPs) and vector embeddings for semantic search."
);

CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_topology.network_nodes`
(
  node_id STRING
    OPTIONS(description="Unique identifier for the infrastructure node."),
  node_name STRING
    OPTIONS(description="Hostname / logical name of the node."),
  node_type STRING
    OPTIONS(description="Infrastructure category: edge_node, gateway, load_balancer, vm, cache, database."),
  region STRING
    OPTIONS(description="Serving region: ca-central-1, ca-east-1, or ca-west-1."),
  ip_address STRING
    OPTIONS(description="Internal IPv4 address."),
  status STRING
    OPTIONS(description="Current operational status: healthy or degraded.")
)
OPTIONS(
  description="Network topology and dependency nodes across regional data centers."
);

CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_telemetry.alert_stream`
(
  alert_id STRING
    OPTIONS(description="Unique identifier for the telemetry alert event."),
  node_id STRING
    OPTIONS(description="Foreign key to sre_topology.network_nodes.node_id."),
  service_name STRING
    OPTIONS(description="Logical service emitting the alert."),
  severity STRING
    OPTIONS(description="Alert severity: INFO, WARNING, ERROR, or CRITICAL."),
  alert_type STRING
    OPTIONS(description="Failure signature / alert classification."),
  message STRING
    OPTIONS(description="Detailed telemetry message with metric values and thresholds."),
  measured_value FLOAT64
    OPTIONS(description="Numeric metric measurement that triggered the alert."),
  timestamp TIMESTAMP
    OPTIONS(description="UTC timestamp when the alert fired.")
)
PARTITION BY DATE(timestamp)
OPTIONS(
  description="Live and historical SRE alert telemetry stream."
);

CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_incident_mart.incidents`
(
  incident_id STRING
    OPTIONS(description="Unique identifier for the incident, e.g. 'inc-5001'."),
  title STRING
    OPTIONS(description="Short summary of the incident."),
  status STRING
    OPTIONS(description="Lifecycle state: INVESTIGATING, MONITORING, or RESOLVED."),
  severity STRING
    OPTIONS(description="Priority classification: P1, P2, or P3."),
  affected_region STRING
    OPTIONS(description="Primary impacted region: ca-central-1, ca-east-1, or ca-west-1."),
  started_at TIMESTAMP
    OPTIONS(description="UTC timestamp when the incident began."),
  resolved_at TIMESTAMP
    OPTIONS(description="UTC timestamp when the incident was resolved (NULL if active)."),
  customer_tier_impacted STRING
    OPTIONS(description="Highest customer support tier affected: GOLD, SILVER, or BRONZE.")
)
OPTIONS(
  description="Historical and active SRE incidents."
);

CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_incident_mart.correlated_alerts`
(
  incident_id STRING
    OPTIONS(description="Foreign key to sre_incident_mart.incidents.incident_id."),
  alert_id STRING
    OPTIONS(description="Foreign key to sre_telemetry.alert_stream.alert_id.")
)
OPTIONS(
  description="Mapping table associating incidents with their correlated alert events."
);

CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_incident_mart.remediation_logs`
(
  execution_id STRING
    OPTIONS(description="Unique identifier for the remediation execution, e.g. 'exec-9001'."),
  incident_id STRING
    OPTIONS(description="Foreign key to sre_incident_mart.incidents.incident_id."),
  runbook_id STRING
    OPTIONS(description="Foreign key to sre_knowledge_base.runbooks.runbook_id."),
  executed_script STRING
    OPTIONS(description="Exact command or script executed."),
  executed_by STRING
    OPTIONS(description="Actor that ran the remediation (e.g. 'sre-agent', 'network-oncall')."),
  hitl_approved BOOL
    OPTIONS(description="Whether Human-in-the-Loop approval was obtained prior to execution."),
  status STRING
    OPTIONS(description="Execution outcome: SUCCESS, FAILED, or ROLLED_BACK."),
  error_output STRING
    OPTIONS(description="Diagnostic error output if FAILED or ROLLED_BACK (NULL on SUCCESS)."),
  started_at TIMESTAMP
    OPTIONS(description="UTC timestamp when execution started."),
  finished_at TIMESTAMP
    OPTIONS(description="UTC timestamp when execution completed.")
)
OPTIONS(
  description="Audit log of automated and manual remediation executions."
);

-- The remote embedding model is created by 04_generate_embeddings.sql, which
-- owns it end-to-end (schema + runbooks table + model + embedding MERGE).
-- Keeping it out of this file means step 01 is pure BigQuery DDL with no
-- dependency on Vertex AI, so it cannot fail while the vertex_conn IAM grant
-- issued moments earlier is still propagating. Step 04 retries for that.
