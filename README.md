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
terraform init
# Lab projects often come with vertex_conn already created. If it exists, adopt it
# instead of creating it (otherwise apply fails with 409 Already Exists):
bq show --connection --location="$GCP_REGION" vertex_conn >/dev/null 2>&1 && \
  terraform import google_bigquery_connection.vertex_conn \
    "projects/$GCP_PROJECT_ID/locations/$GCP_REGION/connections/vertex_conn"
terraform apply
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

## Phase 1 — alert storm → one incident (M1)

```bash
pip install -r agent/requirements.txt
python loader/load_warehouse.py --only ops      # adds sre_agent_ops.agent_incidents
python -m agent.app.triage                      # analyse only, writes nothing
python -m agent.app.triage --persist            # open/update the incident in BigQuery
```

How it decides (`agent/app/tools/clustering.py`, `topology.py`), all at runtime from BigQuery:

1. **Noise filter**: each (node, alert_type) signature's count in the window is compared with its
   own 7-day baseline (Poisson tail). Routine chatter drops out whatever its severity.
2. **Burst vs trend**: a signature whose values climb steadily for 15+ minutes (R² ≥ 0.7) is a
   *precursor* handed to forecasting, not part of the storm.
3. **Clustering**: bursts that overlap in time and share a region or service become one incident.
4. **Root cause**: candidates ranked on earliest onset, how many involved nodes depend on them in
   the inferred topology (tiers, name tokens, `-replica`→`-primary`, "upstream …" message evidence),
   tier depth, saturation evidence, and a penalty when a node's own messages blame something upstream.

The window widens automatically (60 min → 24 h) until an incident is found, because seed data is
relative to load time. Incidents are written with DML in one transaction to `incidents` and
`correlated_alerts`, plus `sre_agent_ops.agent_incidents` so reruns update rather than duplicate.

Tests (`pytest tests`) include a simulator of the kit's telemetry generator (`tests/fixtures/`,
never imported by the agent).

## Phase 2 — runbook retrieval & safe remediation (M2)

```bash
pip install -r agent/requirements.txt
python loader/load_warehouse.py --only ops      # adds approval execution columns
export GEMINI_MODEL=gemini-3.6-flash            # model id as named in the lab; GEMINI_LOCATION defaults to global

python -m agent.app.remediate propose                                   # dry: retrieve, draft, guardrails, sandbox
python -m agent.app.remediate propose --disable vector_search,local_cosine   # prove the keyword fallback
python -m agent.app.remediate propose --request-approval                # opens incident if needed, PENDING approval
python -m agent.app.remediate approve apr-XXXX --by "Your Name"
python -m agent.app.remediate execute apr-XXXX                          # writes remediation_logs
```

- **Retrieval** (`tools/retrieval.py`): BigQuery `VECTOR_SEARCH` with the query embedded in-warehouse
  (`RETRIEVAL_QUERY`, 768 dims) → in-process cosine over the stored vectors → keyword match on
  `failure_signature`. The query describes the *root cause*. Re-rank on similarity, signature match,
  runbook success history, and a penalty for runbooks that match a *symptom* alert instead.
  Gemini gives a second opinion; a disagreement is shown to the approver, never silently applied.
- **Drafting** (`tools/remediation.py`): Gemini adapts the retrieved runbook into an idempotent script
  and a bash rollback. Up to 3 drafts, each fed the previous draft's guardrail and sandbox findings;
  the runbook template is the last resort; otherwise ESCALATE.
- **Guardrails** (`tools/guardrails.py`): code, not prompt. BLOCK = destructive/unscoped/secret/unknown
  binary/no rollback (cannot be approved); REVIEW = mutates state (needs a human); ALLOW = read-only.
- **Sandbox** (`executor/sandbox.py`): `bash -n`, then two runs against stateful stubs with PATH holding
  only the allowlisted stubs and no credentials; different calls on the second run = not idempotent.
- **Approval gate** (`tools/approvals.py`, `executor/execute.py`): executes only an APPROVED row with a
  named approver, never executed before, whose script hash matches, and that still passes guardrails.
  `hitl_approved` in `remediation_logs` comes from that row. Execution target is the sandbox (there is
  no real production estate); on failure the rollback runs and the row is ROLLED_BACK.

## Phase 3 — resilience, forecasting, redaction (M3)

```bash
pip install -r agent/requirements.txt -r ingest/requirements.txt
python loader/load_warehouse.py --only ops      # thresholds table + deduplicating v_alerts view

# Forecast breaches (best right after a --seed-only reload; data is relative to seed time)
python -m agent.app.predict                     # WARN / WATCH / STALE / BREACHED per node, with the why
python -m agent.app.predict --persist           # also write sre_agent_ops.forecasts

# 1,000+ alerts/min with nothing dropped (two terminals)
python -m ingest.consumer
python -m ingest.loadgen --rate 1500 --minutes 5 > manifest.json
python -m ingest.reconcile manifest.json        # PASS/FAIL: stored == sent, peak/min, DLQ, no secrets

# Resilience: knock out dependencies on purpose
CHAOS_DISABLE=vector_search,embeddings python -m agent.app.remediate propose   # keyword fallback
CHAOS_DISABLE=gemini python -m agent.app.remediate propose                      # template drafting
```

- **Forecasting** (`tools/forecast.py`, `predict.py`): least-squares slope per (node, alert_type) with
  ≥6 samples over ≥10 min and R² ≥ 0.8; threshold read from the alert text, else from the
  `metric_thresholds` policy table. ETA with a ±2 SE band. STALE when the projected breach time has
  passed with no fresh samples (either it breached silently or the collector stopped). The "why"
  combines the numbers, the runbook retrieved for the signal, and similar past incidents; Gemini
  words it, and the deterministic text stands if Gemini is down.
- **Ingest** (`ingest/`): streaming pull with flow control, batches of 500 or 1 s, ack only after the
  row is written, nack on write failure (Pub/Sub redelivers, then dead-letters), malformed → DLQ topic,
  duplicates absorbed (in memory, insertId, and `QUALIFY` in `v_alerts`), audit row per batch.
  Load-test rows carry `source='loadgen'` and are excluded from triage/forecasting
  (`INCLUDE_SYNTHETIC=1` to include).
- **Redaction** (`redact.py`): credentials (GCP/GitHub/AWS/Slack keys, OAuth/JWT/bearer, private keys,
  SA JSON, passwords in URIs and `*PASSWORD=`), emails, phones, SINs, Luhn-valid card numbers. Applied
  to every log line (including tracebacks), CLI/JSON output, LLM prompts and replies, stored incident
  summaries, approval scripts, `remediation_logs`, forecasts and ingested alert messages.
- **Retry/backoff** on BigQuery and Gemini calls; every retrieval tier and the drafter degrade instead
  of crashing.

## Phase 4 — impact, postmortem, agent graph, console (M4)

```bash
python loader/load_warehouse.py --only ops      # adds v_incident_impact

# CLI
python -m agent.app.incident impact inc-5019               # revenue at risk + SLA credits
python -m agent.app.incident resolve inc-5019 --by "Your Name"
python -m agent.app.incident postmortem inc-5019 --publish # writes incident_postmortems
python -m agent.app.incident metrics

# Console + agent locally (two terminals)
uvicorn agent.app.server:app --port 8080
cd console && npm ci && npm run dev             # http://localhost:5173

# Cloud Run (Cloud Shell)
terraform -chdir=infra/terraform apply          # picks up the ingest publisher role
./deploy/deploy.sh                              # add `ingest` to also run the consumer
gcloud run services proxy sre-console --region "$GCP_REGION" --port 8080
```

- **Impact** (`sql/agent_ops/07_incident_impact.sql`, `tools/impact.py`): affected services × regions
  from the incident's correlated alerts → `customer_accounts` → revenue at risk (MRR of affected
  accounts) and SLA credits (`mrr_cad × credit_rate × downtime / minutes_in_month`), SLA breach per
  tier against its restoration target. All arithmetic is in the warehouse.
- **Postmortem** (`tools/postmortem.py`): downtime, impact summary, id and FK check are computed;
  Gemini writes root cause, contributing factors and owned action items from gathered facts, and its
  root cause is rejected unless it names the causal failure before any symptom. Drafts have
  `published_at` NULL; publishing requires the incident to be RESOLVED. Corrections are new rows.
- **Agent graph** (`agents.py`, ADK + Gemini Flash): `sre_commander` routes to `incident_response`
  (triage → diagnosis → remediation → parallel forecast + impact) or `postmortem_agent`. Every agent
  has retry with exponential backoff. Agents have **no** approve/execute/resolve/publish tools.
- **Backend** (`server.py`): AG-UI stream at `/agent`, REST at `/api/*` (docs at `/api/docs`),
  responses redacted; human actions need the IAP identity or a named user.
- **Console** (`console/`): KPIs, incident list, "N alerts → 1 incident", root-cause ranking,
  alert-storm timeline, inferred topology graph, fix/rollback with guardrails and one-click approval,
  impact and SLA credits, breach forecast chart, postmortem draft/publish, and the streaming agent.
- **Deploy** (`Dockerfile`, `deploy/deploy.sh`): one image; `sre-console` runs as the `sre-agent`
  service account without public access; the same image runs the ingest consumer.

Offline preview of the console against simulated kit data (no GCP needed):
`cd console && npm run build && cd .. && python -m tests.fixtures.preview_server`.

## Assumptions

- **SLA credit rates** are not in the kit (only the formula
  `mrr_cad × tier_credit_rate × downtime_minutes / minutes_in_month`). Defaults live in
  `sql/agent_ops/02_seed_sla_policy.sql` (GOLD 10×/60 min, SILVER 5×/240 min, BRONZE 2×/480 min);
  replace them if official values are published.
- The agent **adds** rows to `incidents`, `correlated_alerts`, `remediation_logs` and
  `incident_postmortems`; it never alters schema or seeded rows.
