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
            WHERE timestamp BETWEEN @start AND @end""",
            [self.param("start", start), self.param("end", end)])
        return [Alert(**r) for r in rows]

    def fetch_baseline(self, before: datetime, days: int) -> dict[tuple[str, str], int]:
        """Alert counts per (node_id, alert_type) over `days` before the window."""
        rows = self.query(f"""
            SELECT node_id, alert_type, COUNT(*) AS n
            FROM {self.s.table('sre_agent_ops.v_alerts')}
            WHERE timestamp >= TIMESTAMP_SUB(@before, INTERVAL @days DAY)
              AND timestamp < @before
            GROUP BY node_id, alert_type""",
            [self.param("before", before), self.param("days", days)])
        return {(r["node_id"], r["alert_type"]): r["n"] for r in rows}

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
