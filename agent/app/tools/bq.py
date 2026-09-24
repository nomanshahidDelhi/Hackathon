"""Read-side access to the warehouse. Every window is anchored on the server's
CURRENT_TIMESTAMP(), never on a hard-coded date (data is relative to seed time)."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Callable, TypeVar

from ..config import Settings
from ..models import TIER_RANK, Alert, Node

log = logging.getLogger(__name__)
T = TypeVar("T")


def with_retry(fn: Callable[[], T], attempts: int = 4, base_delay: float = 1.0) -> T:
    """Retry transient BigQuery failures with exponential backoff."""
    from google.api_core import exceptions as gexc

    transient = (gexc.ServiceUnavailable, gexc.InternalServerError, gexc.TooManyRequests,
                 gexc.GatewayTimeout, gexc.BadGateway)
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except transient as exc:
            if attempt == attempts:
                raise
            delay = base_delay * 2 ** (attempt - 1)
            log.warning("bigquery transient error (%s); retry %d/%d in %.1fs",
                        type(exc).__name__, attempt, attempts - 1, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


# Load-test traffic is real ingest but synthetic content: keep it out of triage
# and forecasting unless INCLUDE_SYNTHETIC=1.
SYNTHETIC_SOURCES = ["loadgen"]


def excluded_sources() -> list[str]:
    import os
    return [] if os.environ.get("INCLUDE_SYNTHETIC") == "1" else SYNTHETIC_SOURCES


class Warehouse:
    def __init__(self, settings: Settings):
        from google.cloud import bigquery

        self.bigquery = bigquery
        self.s = settings
        self.client = bigquery.Client(project=settings.project, location=settings.location)

    # -- plumbing ------------------------------------------------------------
    def param(self, name: str, value: Any, type_: str | None = None):
        bq = self.bigquery
        if isinstance(value, list):
            return bq.ArrayQueryParameter(name, type_ or "STRING", value)
        return bq.ScalarQueryParameter(name, type_ or _infer_type(value), value)

    def query(self, sql: str, params: list | None = None) -> list[dict[str, Any]]:
        cfg = self.bigquery.QueryJobConfig(query_parameters=params or [])

        def run():
            return [dict(r.items()) for r in self.client.query(sql, job_config=cfg).result()]

        return with_retry(run)

    # -- reads ---------------------------------------------------------------
    def now(self) -> datetime:
        return self.query("SELECT CURRENT_TIMESTAMP() AS now")[0]["now"]

    def fetch_nodes(self) -> list[Node]:
        rows = self.query(f"""
            SELECT node_id, node_name, node_type, region, ip_address, status
            FROM {self.s.table('sre_topology.network_nodes')}""")
        return [Node(**r) for r in rows]

    def fetch_alerts(self, start: datetime, end: datetime) -> list[Alert]:
        rows = self.query(f"""
            SELECT alert_id, node_id, service_name, severity, alert_type, message,
                   measured_value, timestamp
            FROM {self.s.table('sre_agent_ops.v_alerts')}
            WHERE timestamp BETWEEN @start AND @end
              AND source NOT IN UNNEST(@excluded)""",
            [self.param("start", start), self.param("end", end), self.param("excluded", excluded_sources())])
        return [Alert(**r) for r in rows]

    def fetch_baseline(self, before: datetime, days: int) -> dict[tuple[str, str], int]:
        """Alert counts per (node_id, alert_type) over `days` before the window."""
        rows = self.query(f"""
            SELECT node_id, alert_type, COUNT(*) AS n
            FROM {self.s.table('sre_agent_ops.v_alerts')}
            WHERE timestamp >= TIMESTAMP_SUB(@before, INTERVAL @days DAY)
              AND timestamp < @before
              AND source NOT IN UNNEST(@excluded)
            GROUP BY node_id, alert_type""",
            [self.param("before", before), self.param("days", days), self.param("excluded", excluded_sources())])
        return {(r["node_id"], r["alert_type"]): r["n"] for r in rows}

    # -- runbooks (M2) -----------------------------------------------------------
    def vector_search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        """Rank runbooks in-warehouse. Query side uses RETRIEVAL_QUERY to pair with the
        RETRIEVAL_DOCUMENT vectors the kit stored."""
        rows = self.query(f"""
            SELECT base.runbook_id AS runbook_id, distance
            FROM VECTOR_SEARCH(
              TABLE {self.s.table('sre_knowledge_base.runbooks')}, 'embedding',
              (SELECT embedding
               FROM AI.GENERATE_EMBEDDING(
                 MODEL {self.s.table('sre_knowledge_base.embedding_model')},
                 (SELECT @q AS content),
                 STRUCT('RETRIEVAL_QUERY' AS task_type, 768 AS output_dimensionality))),
              'embedding', top_k => @k, distance_type => 'COSINE')
            ORDER BY distance""", [self.param("q", query), self.param("k", top_k)])
        if not rows:
            raise RuntimeError("vector search returned no rows (embeddings missing?)")
        return [(r["runbook_id"], r["distance"]) for r in rows]

    def fetch_runbooks(self):
        from .retrieval import Runbook

        rows = self.query(f"""
            SELECT runbook_id, title, failure_signature, remediation_steps,
                   rollback_commands, remediation_script, embedding
            FROM {self.s.table('sre_knowledge_base.runbooks')}""")
        return [Runbook(**{**r, "embedding": list(r["embedding"] or [])}) for r in rows]

    def runbook_history(self) -> dict[str, tuple[int, int]]:
        """runbook_id -> (successful executions, all executions)."""
        rows = self.query(f"""
            SELECT runbook_id,
                   COUNT(DISTINCT IF(status = 'SUCCESS', execution_id, NULL)) AS ok,
                   COUNT(DISTINCT execution_id) AS total
            FROM {self.s.table('sre_incident_mart.remediation_logs')}
            GROUP BY runbook_id""")
        return {r["runbook_id"]: (r["ok"], r["total"]) for r in rows}

    # -- forecasting (M3) ------------------------------------------------------
    def metric_thresholds(self) -> dict[str, float]:
        rows = self.query(f"SELECT pattern, threshold FROM {self.s.table('sre_agent_ops.metric_thresholds')}")
        return {r["pattern"].lower(): r["threshold"] for r in rows}

    def past_incidents(self) -> list[dict[str, Any]]:
        """Incidents with how they were remediated (runbook, outcome)."""
        return self.query(f"""
            SELECT i.incident_id, i.title, i.status, i.severity, i.affected_region,
                   i.started_at, i.resolved_at,
                   ARRAY_AGG(STRUCT(r.runbook_id, r.status) IGNORE NULLS ORDER BY r.started_at) AS remediations
            FROM {self.s.table('sre_incident_mart.incidents')} i
            LEFT JOIN {self.s.table('sre_incident_mart.remediation_logs')} r USING (incident_id)
            GROUP BY 1, 2, 3, 4, 5, 6, 7""")

    def insert_forecasts(self, rows: list[dict[str, Any]]) -> None:
        import json

        self.query(f"""
            INSERT {self.s.table('sre_agent_ops.forecasts')}
              (forecast_id, node_id, service_name, alert_type, samples, current_value, threshold,
               slope_per_min, r_squared, eta_minutes, predicted_breach_at, generated_at, explanation)
            SELECT JSON_VALUE(r, '$.forecast_id'), JSON_VALUE(r, '$.node_id'), JSON_VALUE(r, '$.service_name'),
                   JSON_VALUE(r, '$.alert_type'), CAST(JSON_VALUE(r, '$.samples') AS INT64),
                   CAST(JSON_VALUE(r, '$.current_value') AS FLOAT64), CAST(JSON_VALUE(r, '$.threshold') AS FLOAT64),
                   CAST(JSON_VALUE(r, '$.slope_per_min') AS FLOAT64), CAST(JSON_VALUE(r, '$.r_squared') AS FLOAT64),
                   CAST(JSON_VALUE(r, '$.eta_minutes') AS FLOAT64), TIMESTAMP(JSON_VALUE(r, '$.predicted_breach_at')),
                   CURRENT_TIMESTAMP(), JSON_VALUE(r, '$.explanation')
            FROM UNNEST(JSON_QUERY_ARRAY(@rows)) AS r""", [self.param("rows", json.dumps(rows, default=str))])

    def highest_tier(self, services: list[str], regions: list[str]) -> str | None:
        rows = self.query(f"""
            SELECT DISTINCT tier
            FROM {self.s.table('sre_incident_mart.customer_accounts')}
            WHERE service_name IN UNNEST(@services) AND region IN UNNEST(@regions)""",
            [self.param("services", services), self.param("regions", regions)])
        tiers = [r["tier"] for r in rows if r["tier"] in TIER_RANK]
        return max(tiers, key=TIER_RANK.get) if tiers else None


def _infer_type(value: Any) -> str:
    if isinstance(value, bool):
        return "BOOL"
    if isinstance(value, int):
        return "INT64"
    if isinstance(value, float):
        return "FLOAT64"
    if isinstance(value, datetime):
        return "TIMESTAMP"
    return "STRING"
