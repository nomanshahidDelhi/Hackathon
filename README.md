# BCE Hackfest 2026 — Track 2: Agentic SRE

An incident-response agent on Google Cloud + Gemini that clusters alert storms into one incident,
retrieves the right runbook by meaning, proposes a safe fix behind human approval, forecasts
breaches, and writes the postmortem back to BigQuery.

| Path | What |
|---|---|
| `data/` | Organiser warehouse kit, unmodified (input, not our work) |
| `infra/terraform/` | APIs, `vertex_conn` + IAM, service accounts, Pub/Sub + DLQ, Artifact Registry |
| `loader/load_warehouse.py` | Our re-runnable loader: kit steps 01–07 + `sre_agent_ops`, then verification |
| `sql/agent_ops/` | Our own dataset: SLA policy, live alert ingest, approvals, agent runs, forecasts, `v_alerts` |
| `tests/` | Unit tests (`pytest tests`) |

## Phase 0 — bootstrap and load

Run from Cloud Shell (has `gcloud` and `terraform`) or any machine with both installed.

```bash
export GCP_PROJECT_ID="your-project-id"
export GCP_REGION="us-central1"          # one region, per lab guidelines; datasets are bound to it

gcloud config set project "$GCP_PROJECT_ID"
gcloud auth login
gcloud auth application-default login
gcloud auth application-default set-quota-project "$GCP_PROJECT_ID"

# 1. Infrastructure (APIs, vertex_conn + Vertex AI User grant, SAs, Pub/Sub)
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # set project_id / region
terraform init && terraform apply
cd ../..

# 2. Warehouse
python3 -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r loader/requirements.txt
python loader/load_warehouse.py                 # full load + verify
```

Done when the loader prints `VERIFY OK` (3,000 alerts, 64 nodes, 20 runbooks with 0 unembedded,
45 accounts, 18 incidents, 15–55 correlated alerts with 0 broken keys, 18 remediation logs,
0 postmortems, 3 SLA tiers).

Step 04 (embeddings) retries automatically for ~5 minutes while the `vertex_conn` IAM grant
propagates. If it still fails, re-run just that step: `python loader/load_warehouse.py --only 04`.

Other loader modes:

```bash
python loader/load_warehouse.py --seed-only     # re-roll data; do this shortly before a demo
python loader/load_warehouse.py --verify-only
python loader/load_warehouse.py --list          # show plan, run nothing
```

Timestamps in the kit are generated relative to seed time, so everything downstream queries
windows relative to `CURRENT_TIMESTAMP()`.

## Assumptions

- **SLA credit rates** are not in the kit (only the formula
  `mrr_cad × tier_credit_rate × downtime_minutes / minutes_in_month`). Defaults live in
  `sql/agent_ops/02_seed_sla_policy.sql` (GOLD 10×/60 min, SILVER 5×/240 min, BRONZE 2×/480 min);
  replace them if official values are published.
- The agent **adds** rows to `incidents`, `correlated_alerts`, `remediation_logs` and
  `incident_postmortems`; it never alters schema or seeded rows.
