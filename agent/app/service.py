"""One facade over the whole incident lifecycle, shared by the CLIs, the REST API
and the ADK agent tools, so every path goes through the same safety gates.

Human-only operations (decide, execute, resolve, publish) are exposed to the
REST API and CLI; the ADK agents only get the read/propose/draft operations.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings, load_settings
from .llm import Gemini
from .models import Incident, TriageResult
from .redact import redact, redact_obj
from .tools.approvals import Approvals
from .tools.bq import Warehouse
from .tools.forecast import ForecastConfig, compute_forecasts
from .tools.impact import Impact, summarize
from .tools.incident_store import IncidentStore
from .tools.postmortem import PostmortemDraft, build_facts, draft_postmortem, next_postmortem_id
from .tools.remediation import Proposal, propose
from .tools.retrieval import RetrievalResult, retrieve
from .tools.topology import Topology
from .triage import run_triage


class ServiceError(RuntimeError):
    pass


class SREService:
    def __init__(self, settings: Settings | None = None, use_llm: bool | None = None):
        self.s = settings or load_settings()
        self.wh = Warehouse(self.s)
        if use_llm is None:
            use_llm = os.environ.get("NO_LLM") != "1"
        self.llm = Gemini(self.s.project) if use_llm else None
        self._sandbox = None
        self.t = self.s.table

    @property
    def sandbox(self):
        if self._sandbox is None:
            from executor.sandbox import Sandbox
            self._sandbox = Sandbox()
        return self._sandbox

    # ------------------------------------------------------------------ M1
    def triage(self, lookback: int | None = None) -> TriageResult:
        return run_triage(self.wh, lookback)

    def open_incident(self, incident: Incident) -> tuple[str, bool]:
        return IncidentStore(self.wh).upsert(incident)

    def load_incident(self, incident_id: str) -> Incident:
        rows = self.wh.query(f"SELECT triage_json FROM {self.t('sre_agent_ops.agent_incidents')} "
                             "WHERE incident_id = @id", [self.wh.param("id", incident_id)])
        if not rows:
            raise ServiceError(f"{incident_id} was not opened by the agent")
        return Incident.from_dict(json.loads(rows[0]["triage_json"]))

    # ------------------------------------------------------------------ M2
    def find_runbook(self, incident: Incident, disable: set[str] = frozenset()) -> RetrievalResult:
        embed = self.llm.embed_query if self.llm else None
        return retrieve(incident, self.wh, embed=embed, disable=disable)

    def propose(self, incident: Incident, retrieval: RetrievalResult | None = None) -> tuple[RetrievalResult, Proposal]:
        retrieval = retrieval or self.find_runbook(incident)
        runbooks = {r.runbook_id: r for r in self.wh.fetch_runbooks()}
        return retrieval, propose(incident, retrieval, runbooks, self.sandbox, self.llm)

    def request_approval(self, incident_id: str, proposal: Proposal) -> str:
        return Approvals(self.wh).request(incident_id, proposal)

    def decide(self, approval_id: str, approve: bool, by: str, reason: str = "") -> None:
        Approvals(self.wh).decide(approval_id, approve, by, reason)

    def execute(self, approval_id: str):
        from executor.execute import execute
        return execute(self.wh, approval_id, self.sandbox)

    # ------------------------------------------------------------------ M3
    def forecasts(self, lookback: int = 120, horizon: float = 60.0, explain: bool = True,
                  persist: bool = False) -> list:
        from .predict import enrich
        import uuid

        now = self.wh.now()
        alerts = self.wh.fetch_alerts(now - timedelta(minutes=lookback), now)
        try:
            policy = self.wh.metric_thresholds()
        except Exception:
            policy = {}
        fc = compute_forecasts(alerts, self.wh.fetch_nodes(), now, policy, ForecastConfig(horizon_minutes=horizon))
        if fc and explain:
            enrich(fc, self.wh, self.llm)
        if fc and persist:
            self.wh.insert_forecasts([{**asdict(f), "forecast_id": f"fc-{uuid.uuid4().hex[:12]}"} for f in fc])
        return fc

    # ------------------------------------------------------------------ M4
    def impact(self, incident_id: str) -> Impact:
        rows = self.wh.query(f"SELECT * FROM {self.t('sre_agent_ops.v_incident_impact')} "
                             "WHERE incident_id = @id", [self.wh.param("id", incident_id)])
        return summarize(incident_id, rows)

    def incident_row(self, incident_id: str) -> dict[str, Any]:
        rows = self.wh.query(f"SELECT * FROM {self.t('sre_incident_mart.incidents')} WHERE incident_id = @id",
                             [self.wh.param("id", incident_id)])
        if not rows:
            raise ServiceError(f"unknown incident {incident_id}")  # FK check for the postmortem
        return rows[0]

    def resolve(self, incident_id: str, by: str) -> datetime:
        """Human action. Resolution time = last successful remediation, else now."""
        if not by.strip():
            raise ServiceError("a named resolver is required")
        p = self.wh.param
        rows = self.wh.query(f"""
            UPDATE {self.t('sre_incident_mart.incidents')}
            SET status = 'RESOLVED',
                resolved_at = COALESCE(
                  (SELECT MAX(finished_at) FROM {self.t('sre_incident_mart.remediation_logs')}
                   WHERE incident_id = @id AND status = 'SUCCESS'), CURRENT_TIMESTAMP())
            WHERE incident_id = @id AND status != 'RESOLVED'
              AND incident_id IN (SELECT incident_id FROM {self.t('sre_agent_ops.agent_incidents')});
            SELECT status, resolved_at FROM {self.t('sre_incident_mart.incidents')} WHERE incident_id = @id;""",
            [p("id", incident_id)])
        if not rows or rows[0]["status"] != "RESOLVED":
            raise ServiceError(f"{incident_id} could not be resolved (unknown or not agent-owned)")
        self.wh.query(f"""
            INSERT {self.t('sre_agent_ops.agent_runs')} (run_id, incident_id, stage, status, started_at, finished_at, detail)
            VALUES (GENERATE_UUID(), @id, 'resolve', 'OK', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), @detail)""",
            [p("id", incident_id), p("detail", redact(f"resolved by {by}"))])
        return rows[0]["resolved_at"]

    def write_postmortem(self, incident_id: str, publish: bool = False) -> tuple[PostmortemDraft, bool]:
        incident = self.incident_row(incident_id)
        triage = json.loads(self.wh.query(
            f"SELECT triage_json FROM {self.t('sre_agent_ops.agent_incidents')} WHERE incident_id = @id",
            [self.wh.param("id", incident_id)])[0]["triage_json"]) if self._agent_owned(incident_id) else None
        if triage is None:
            raise ServiceError(f"{incident_id} has no agent triage record to ground a postmortem")
        imp = self.impact(incident_id)
        p = self.wh.param
        remediations = self.wh.query(f"""
            SELECT * FROM {self.t('sre_incident_mart.remediation_logs')}
            WHERE incident_id = @id ORDER BY started_at""", [p("id", incident_id)])
        approvals = self.wh.query(f"""
            SELECT approval_id, status, decided_by, risk_level, requested_at, decided_at
            FROM {self.t('sre_agent_ops.approvals')} WHERE incident_id = @id ORDER BY requested_at""",
            [p("id", incident_id)])
        runbook = None
        if remediations:
            rb = next((r for r in self.wh.fetch_runbooks() if r.runbook_id == remediations[-1]["runbook_id"]), None)
            if rb:
                runbook = {"id": rb.runbook_id, "title": rb.title, "steps": rb.remediation_steps}
        facts = build_facts(incident, triage, imp, remediations, approvals, runbook)

        year = datetime.now(timezone.utc).year
        ids = [r["postmortem_id"] for r in self.wh.query(
            f"SELECT postmortem_id FROM {self.t('sre_incident_mart.incident_postmortems')}")]
        draft = draft_postmortem(next_postmortem_id(ids, year), facts, imp, self.llm)

        row = draft.row()
        publish_now = publish and incident["status"] == "RESOLVED"
        self.wh.query(f"""
            INSERT {self.t('sre_incident_mart.incident_postmortems')}
              (postmortem_id, incident_id, root_cause, impact_summary, downtime_minutes,
               contributing_factors, action_items, published_at)
            SELECT @postmortem_id, @incident_id, @root_cause, @impact_summary, @downtime_minutes,
                   @contributing_factors, @action_items, IF(@publish, CURRENT_TIMESTAMP(), NULL)
            FROM {self.t('sre_incident_mart.incidents')} WHERE incident_id = @incident_id""",
            [p(k, v) for k, v in row.items()] + [p("publish", publish_now)])
        return draft, publish_now

    def publish_postmortem(self, postmortem_id: str) -> None:
        rows = self.wh.query(f"""
            UPDATE {self.t('sre_incident_mart.incident_postmortems')} pm
            SET published_at = CURRENT_TIMESTAMP()
            WHERE pm.postmortem_id = @id AND pm.published_at IS NULL
              AND EXISTS (SELECT 1 FROM {self.t('sre_incident_mart.incidents')} i
                          WHERE i.incident_id = pm.incident_id AND i.status = 'RESOLVED');
            SELECT @@row_count AS n;""", [self.wh.param("id", postmortem_id)])
        if not rows or rows[0]["n"] != 1:
            raise ServiceError(f"{postmortem_id} not published: unknown, already published, "
                               "or its incident is not RESOLVED")

    def _agent_owned(self, incident_id: str) -> bool:
        return bool(self.wh.query(f"SELECT 1 FROM {self.t('sre_agent_ops.agent_incidents')} "
                                  "WHERE incident_id = @id LIMIT 1", [self.wh.param("id", incident_id)]))

    # ------------------------------------------------------------------ console reads
    def list_incidents(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.wh.query(f"""
            SELECT i.*, ai.incident_id IS NOT NULL AS agent_detected, ai.alert_count,
                   ai.root_node_id, ai.root_alert_type, ai.confidence
            FROM {self.t('sre_incident_mart.incidents')} i
            LEFT JOIN {self.t('sre_agent_ops.agent_incidents')} ai USING (incident_id)
            ORDER BY i.started_at DESC LIMIT @n""", [self.wh.param("n", limit)])

    def incident_detail(self, incident_id: str) -> dict[str, Any]:
        p = self.wh.param
        incident = self.incident_row(incident_id)
        triage = None
        if self._agent_owned(incident_id):
            triage = json.loads(self.wh.query(
                f"SELECT triage_json FROM {self.t('sre_agent_ops.agent_incidents')} WHERE incident_id = @id",
                [p("id", incident_id)])[0]["triage_json"])
        approvals = self.wh.query(f"""
            SELECT approval_id, runbook_id, status, risk_level, script, rollback_script, guardrail_report,
                   sandbox_report, plan_json, requested_at, decided_at, decided_by, decision_reason,
                   execution_id, executed_at
            FROM {self.t('sre_agent_ops.approvals')} WHERE incident_id = @id ORDER BY requested_at DESC""",
            [p("id", incident_id)])
        remediations = self.wh.query(f"""
            SELECT * FROM {self.t('sre_incident_mart.remediation_logs')}
            WHERE incident_id = @id ORDER BY started_at DESC""", [p("id", incident_id)])
        postmortems = self.wh.query(f"""
            SELECT * FROM {self.t('sre_incident_mart.incident_postmortems')}
            WHERE incident_id = @id ORDER BY postmortem_id DESC""", [p("id", incident_id)])
        n_alerts = self.wh.query(f"""
            SELECT COUNT(*) AS n FROM {self.t('sre_incident_mart.correlated_alerts')}
            WHERE incident_id = @id""", [p("id", incident_id)])[0]["n"]
        return redact_obj(json.loads(json.dumps({
            "incident": incident, "triage": triage, "correlated_alerts": n_alerts,
            "impact": self.impact(incident_id).to_dict(), "approvals": approvals,
            "remediations": remediations, "postmortems": postmortems,
        }, default=str)))

    def timeline(self, incident_id: str, bucket_s: int = 10) -> list[dict[str, Any]]:
        """Correlated alerts per time bucket, split root vs symptom."""
        return self.wh.query(f"""
            SELECT TIMESTAMP_SECONDS(DIV(UNIX_SECONDS(a.timestamp), @b) * @b) AS bucket,
                   IF(a.node_id = ai.root_node_id AND a.alert_type = ai.root_alert_type, 'root', 'symptom') AS kind,
                   COUNT(*) AS n
            FROM {self.t('sre_incident_mart.correlated_alerts')} c
            JOIN {self.t('sre_agent_ops.v_alerts')} a USING (alert_id)
            LEFT JOIN {self.t('sre_agent_ops.agent_incidents')} ai USING (incident_id)
            WHERE c.incident_id = @id
            GROUP BY bucket, kind ORDER BY bucket""", [self.wh.param("id", incident_id), self.wh.param("b", bucket_s)])

    def topology(self, incident_id: str | None = None) -> dict[str, Any]:
        nodes = self.wh.fetch_nodes()
        highlight: dict[str, str] = {}
        topo = Topology(nodes)
        if incident_id and self._agent_owned(incident_id):
            inc = self.load_incident(incident_id)
            for n in inc.nodes:
                highlight[n] = "symptom"
            highlight[inc.root_cause.node_id] = "root"
            alerts = self.wh.query(f"""
                SELECT a.* EXCEPT(source) FROM {self.t('sre_incident_mart.correlated_alerts')} c
                JOIN {self.t('sre_agent_ops.v_alerts')} a USING (alert_id) WHERE c.incident_id = @id""",
                [self.wh.param("id", incident_id)])
            from .models import Alert
            topo = topo.with_alert_evidence([Alert(**a) for a in alerts])
        return {
            "nodes": [{**asdict(n), "role": highlight.get(n.node_id)} for n in nodes],
            "edges": [{"source": a, "target": b} for a, b in topo.edges()],
        }

    def metrics(self) -> dict[str, Any]:
        t = self.t
        mttr = self.wh.query(f"""
            SELECT severity, COUNT(*) AS resolved,
                   ROUND(AVG(TIMESTAMP_DIFF(resolved_at, started_at, SECOND)) / 60, 1) AS mttr_minutes
            FROM {t('sre_incident_mart.incidents')} WHERE status = 'RESOLVED' AND resolved_at IS NOT NULL
            GROUP BY severity ORDER BY severity""")
        overall = self.wh.query(f"""
            SELECT ROUND(AVG(TIMESTAMP_DIFF(resolved_at, started_at, SECOND)) / 60, 1) AS mttr_minutes,
                   COUNTIF(status != 'RESOLVED') AS open_incidents
            FROM {t('sre_incident_mart.incidents')}""")[0]
        agent = self.wh.query(f"""
            SELECT COUNT(*) AS agent_incidents, SUM(ai.alert_count) AS alerts_clustered,
                   ROUND(AVG(TIMESTAMP_DIFF(ai.created_at, i.started_at, SECOND)) / 60, 1) AS detect_minutes,
                   ROUND(AVG(IF(i.status = 'RESOLVED', TIMESTAMP_DIFF(i.resolved_at, i.started_at, SECOND), NULL)) / 60, 1)
                     AS agent_mttr_minutes
            FROM {t('sre_agent_ops.agent_incidents')} ai JOIN {t('sre_incident_mart.incidents')} i USING (incident_id)""")[0]
        approvals = self.wh.query(f"""
            SELECT COUNTIF(status = 'PENDING') AS pending,
                   ROUND(AVG(TIMESTAMP_DIFF(decided_at, requested_at, SECOND)) / 60, 1) AS approve_minutes
            FROM {t('sre_agent_ops.approvals')}""")[0]
        rate = self.wh.query(f"""
            SELECT COUNT(*) AS n FROM {t('sre_agent_ops.v_alerts')}
            WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 5 MINUTE)""")[0]["n"]
        return json.loads(json.dumps({
            "mttr_by_severity": mttr, "mttr_minutes": overall["mttr_minutes"],
            "open_incidents": overall["open_incidents"], "agent": agent, "approvals": approvals,
            "alerts_per_minute_5m": round(rate / 5, 1),
        }, default=str))
