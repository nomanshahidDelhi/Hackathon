"""Write agent-detected incidents back to the warehouse.

Only ever adds rows to incidents / correlated_alerts (and our own
agent_incidents registry); seeded rows and schemas are never touched. All
writes are DML in one transaction: streaming inserts would lock the rows out of
later UPDATEs (status changes) for up to 90 minutes.
"""
from __future__ import annotations

import json
from dataclasses import asdict

from ..redact import redact_obj
from ..models import SEVERITY_RANK, TIER_RANK, Incident
from .bq import Warehouse


def priority(incident: Incident, highest_tier: str | None) -> str:
    critical = SEVERITY_RANK.get(incident.max_severity, 0) >= SEVERITY_RANK["CRITICAL"]
    tier = TIER_RANK.get(highest_tier or "", 0)
    if critical and tier >= TIER_RANK["GOLD"]:
        return "P1"
    if critical or tier >= TIER_RANK["SILVER"]:
        return "P2"
    return "P3"


def title(incident: Incident) -> str:
    rc = incident.root_cause
    symptom = f", cascading to {len(incident.symptom_services)} services" if incident.symptom_services else ""
    return f"{rc.alert_type.replace('_', ' ')} on {rc.node_name} ({rc.service_name}){symptom}"


def summary_json(incident: Incident) -> str:
    d = asdict(incident)
    d.pop("alert_ids")  # stored in correlated_alerts
    return json.dumps(redact_obj(json.loads(json.dumps(d, default=str))))


class IncidentStore:
    def __init__(self, wh: Warehouse):
        self.wh = wh
        self.t = wh.s.table

    def find_open(self, alert_ids: list[str]) -> str | None:
        """An open agent incident that already owns any of these alerts."""
        rows = self.wh.query(f"""
            SELECT ai.incident_id
            FROM {self.t('sre_agent_ops.agent_incidents')} ai
            JOIN {self.t('sre_incident_mart.correlated_alerts')} c USING (incident_id)
            JOIN {self.t('sre_incident_mart.incidents')} i USING (incident_id)
            WHERE c.alert_id IN UNNEST(@ids) AND i.status != 'RESOLVED'
            ORDER BY ai.created_at DESC
            LIMIT 1""", [self.wh.param("ids", alert_ids)])
        return rows[0]["incident_id"] if rows else None

    def next_id(self) -> str:
        rows = self.wh.query(f"""
            SELECT COALESCE(MAX(SAFE_CAST(REGEXP_EXTRACT(incident_id, r'^inc-(\\d+)$') AS INT64)), 5000) + 1 AS n
            FROM {self.t('sre_incident_mart.incidents')}""")
        return f"inc-{rows[0]['n']}"

    def upsert(self, incident: Incident) -> tuple[str, bool]:
        """Returns (incident_id, created). Reruns attach new alerts to the open incident."""
        existing = self.find_open(incident.alert_ids)
        tier = self.wh.highest_tier(incident.services, incident.regions)
        p = self.wh.param
        common = [
            p("ids", incident.alert_ids),
            p("summary", summary_json(incident)),
            p("alert_count", incident.alert_count),
            p("confidence", float(incident.confidence)),
        ]

        if existing:
            self.wh.query(f"""
                BEGIN TRANSACTION;
                MERGE {self.t('sre_incident_mart.correlated_alerts')} T
                USING (SELECT @incident_id AS incident_id, id AS alert_id FROM UNNEST(@ids) id) S
                ON T.incident_id = S.incident_id AND T.alert_id = S.alert_id
                WHEN NOT MATCHED THEN INSERT (incident_id, alert_id) VALUES (S.incident_id, S.alert_id);
                UPDATE {self.t('sre_agent_ops.agent_incidents')}
                SET alert_count = @alert_count, confidence = @confidence,
                    triage_json = @summary, updated_at = CURRENT_TIMESTAMP()
                WHERE incident_id = @incident_id;
                COMMIT TRANSACTION;""", [p("incident_id", existing), *common])
            return existing, False

        incident_id = self.next_id()
        rc = incident.root_cause
        self.wh.query(f"""
            BEGIN TRANSACTION;
            INSERT {self.t('sre_incident_mart.incidents')}
              (incident_id, title, status, severity, affected_region, started_at, resolved_at, customer_tier_impacted)
            VALUES (@incident_id, @title, 'INVESTIGATING', @severity, @region, @started_at, NULL, @tier);
            INSERT {self.t('sre_incident_mart.correlated_alerts')} (incident_id, alert_id)
            SELECT @incident_id, id FROM UNNEST(@ids) id;
            INSERT {self.t('sre_agent_ops.agent_incidents')}
              (incident_id, fingerprint, root_node_id, root_alert_type, root_service,
               alert_count, confidence, triage_json, created_at, updated_at)
            VALUES (@incident_id, @fingerprint, @root_node, @root_type, @root_service,
                    @alert_count, @confidence, @summary, CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP());
            COMMIT TRANSACTION;""", [
            p("incident_id", incident_id),
            p("title", title(incident)),
            p("severity", priority(incident, tier)),
            p("region", rc.region),
            p("started_at", incident.started_at),
            p("tier", tier, "STRING"),
            p("fingerprint", incident.fingerprint()),
            p("root_node", rc.node_id),
            p("root_type", rc.alert_type),
            p("root_service", rc.service_name),
            *common,
        ])
        return incident_id, True
