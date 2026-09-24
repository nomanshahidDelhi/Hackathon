"""Offline preview of the console: the real FastAPI app, with a service backed by
simulated kit data run through the real triage / forecast / impact code.

    python -m tests.fixtures.preview_server     # then open http://localhost:8080

Test/dev tooling only; the deployed app always reads BigQuery.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("GCP_PROJECT_ID", "offline-preview")
os.environ.setdefault("GCP_REGION", "us-central1")
os.environ.setdefault("DISABLE_AGENT", "1")

from agent.app import server  # noqa: E402
from agent.app.tools.clustering import triage  # noqa: E402
from agent.app.tools.forecast import compute_forecasts, describe  # noqa: E402
from agent.app.tools.impact import summarize  # noqa: E402
from agent.app.tools.topology import Topology  # noqa: E402
from tests.fixtures import kit_sim  # noqa: E402

KIT = Path(__file__).resolve().parents[2] / "data" / "sql"
NOW = datetime.now(timezone.utc)
RATES = {"GOLD": (10.0, 60), "SILVER": (5.0, 240), "BRONZE": (2.0, 480)}


def customers():
    rx = re.compile(r"\('(CUST-\d+)',\s*'([^']*)',\s*'([\w-]+)',\s*'(\w+)',\s*([\d.]+),\s*'([\w-]+)'\)")
    return [dict(zip(("customer_id", "customer_name", "service_name", "tier", "mrr_cad", "region"),
                     (a, b, c, d, float(e), f)))
            for a, b, c, d, e, f in rx.findall((KIT / "02_seed_customers.sql").read_text(encoding="utf-8"))]


class PreviewService:
    def __init__(self):
        self.nodes = kit_sim.load_nodes()
        self.alerts = kit_sim.generate(NOW - timedelta(minutes=2))
        start = NOW - timedelta(minutes=60)
        base = Counter((a.node_id, a.alert_type) for a in self.alerts if start - timedelta(days=7) <= a.timestamp < start)
        self.inc = triage(self.alerts, self.nodes, base, 7 * 1440.0, start, NOW).incidents[0]
        self.by_id = {a.alert_id: a for a in self.alerts}

    def metrics(self):
        return {"mttr_minutes": 70.4, "open_incidents": 3, "alerts_per_minute_5m": 0.0,
                "agent": {"agent_incidents": 1, "alerts_clustered": self.inc.alert_count,
                          "detect_minutes": 1.2, "agent_mttr_minutes": None},
                "approvals": {"pending": 1, "approve_minutes": None}, "mttr_by_severity": []}

    def list_incidents(self, limit=50):
        rc = self.inc.root_cause
        return [
            {"incident_id": "inc-5019", "title": f"{rc.alert_type.replace('_', ' ')} on {rc.node_name}", "status": "INVESTIGATING",
             "severity": "P1", "affected_region": rc.region, "started_at": self.inc.started_at, "agent_detected": True,
             "alert_count": self.inc.alert_count, "root_node_id": rc.node_id, "root_alert_type": rc.alert_type},
            {"incident_id": "inc-5018", "title": "CDN origin shield cache miss stampede on live manifest",
             "status": "INVESTIGATING", "severity": "P2", "affected_region": "ca-west-1", "agent_detected": False},
            {"incident_id": "inc-5002", "title": "Customer billing DB connection pool saturation",
             "status": "RESOLVED", "severity": "P1", "affected_region": "ca-central-1", "agent_detected": False},
        ]

    def impact(self, incident_id):
        started, end = self.inc.started_at, self.inc.last_seen_at
        dt = (end - started).total_seconds() / 60 + 18
        rows = []
        for c in customers():
            if c["service_name"] in self.inc.services and c["region"] in self.inc.regions:
                rate, target = RATES[c["tier"]]
                rows.append({**c, "incident_id": incident_id, "title": "", "status": "INVESTIGATING", "severity": "P1",
                             "started_at": started, "impact_end": end, "ongoing": False, "downtime_minutes": dt,
                             "minutes_in_month": 43200, "credit_rate": rate, "restoration_target_minutes": target,
                             "prorated_revenue_cad": c["mrr_cad"] * dt / 43200,
                             "sla_credit_cad": c["mrr_cad"] * rate * dt / 43200, "sla_breached": dt > target})
        return summarize(incident_id, rows)

    def incident_detail(self, incident_id):
        tr = json.loads(json.dumps(asdict(self.inc), default=str))
        script = ("#!/bin/bash\nset -euo pipefail\nDB_HOST=customer-billing-db.ca-central-1.internal\n"
                  "psql -h \"$DB_HOST\" -d billing -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                  "WHERE state = 'idle in transaction' AND state_change < now() - interval '5 minutes';\"\n"
                  "kubectl -n billing rollout restart deployment/billing-service\n")
        return json.loads(json.dumps({
            "incident": {"incident_id": incident_id, "title": self.list_incidents()[0]["title"], "status": "INVESTIGATING",
                         "severity": "P1", "affected_region": self.inc.root_cause.region, "customer_tier_impacted": "GOLD"},
            "triage": tr if incident_id == "inc-5019" else None, "correlated_alerts": self.inc.alert_count,
            "impact": self.impact(incident_id).to_dict(),
            "approvals": [{"approval_id": "apr-preview01", "runbook_id": "sop-102", "status": "PENDING", "risk_level": "HIGH",
                           "script": script, "rollback_script": "#!/bin/bash\nset -euo pipefail\nkubectl -n billing rollout undo deployment/billing-service\n",
                           "guardrail_report": json.dumps({"findings": [{"severity": "HIGH", "rule": "pg_terminate", "message": "terminates database sessions"}]}),
                           "sandbox_report": json.dumps({"runs": [{}, {}], "idempotent": True, "rollback": {"exit_code": 0}}),
                           "plan_json": json.dumps({"rationale": "Reclaim idle-in-transaction sessions, then recycle the leaking client.",
                                                    "selection": {"explanation": "sop-102 targets connection pool exhaustion; the 5xx and latency runbooks treat symptoms."}})}],
            "remediations": [], "postmortems": [],
        }, default=str))

    def timeline(self, incident_id, bucket_s=10):
        rc = self.inc.root_cause
        c = Counter()
        for aid in self.inc.alert_ids:
            a = self.by_id[aid]
            b = datetime.fromtimestamp(int(a.timestamp.timestamp()) // bucket_s * bucket_s, timezone.utc)
            c[(b, "root" if (a.node_id, a.alert_type) == (rc.node_id, rc.alert_type) else "symptom")] += 1
        return [{"bucket": b, "kind": k, "n": n} for (b, k), n in sorted(c.items())]

    def topology(self, incident_id=None):
        topo = Topology(self.nodes)
        role = {}
        if incident_id == "inc-5019":
            role = {n: "symptom" for n in self.inc.nodes}
            role[self.inc.root_cause.node_id] = "root"
            topo = topo.with_alert_evidence([self.by_id[a] for a in self.inc.alert_ids])
        return {"nodes": [{**asdict(n), "role": role.get(n.node_id)} for n in self.nodes],
                "edges": [{"source": a, "target": b} for a, b in topo.edges()]}

    def forecasts(self, horizon=60.0, explain=False, lookback=120, persist=False):
        win = [a for a in self.alerts if NOW - timedelta(minutes=lookback) <= a.timestamp <= NOW]
        fc = compute_forecasts(win, self.nodes, NOW)
        for f in fc:
            f.explanation = describe(f)
        return fc

    def series(self, node_id, alert_type, minutes=120):
        return [{"t": a.timestamp, "v": a.measured_value} for a in sorted(self.alerts, key=lambda a: a.timestamp)
                if a.node_id == node_id and a.alert_type == alert_type and a.timestamp >= NOW - timedelta(minutes=minutes)]


server._svc = PreviewService()
app = server.app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8080")))
