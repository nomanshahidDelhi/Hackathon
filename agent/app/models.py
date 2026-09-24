"""Plain data types shared by the triage tools. No GCP imports here."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any

SEVERITY_RANK = {"INFO": 0, "WARNING": 1, "ERROR": 2, "CRITICAL": 3}
TIER_RANK = {"BRONZE": 1, "SILVER": 2, "GOLD": 3}


@dataclass(frozen=True)
class Node:
    node_id: str
    node_name: str
    node_type: str
    region: str
    ip_address: str = ""
    status: str = "healthy"


@dataclass(frozen=True)
class Alert:
    alert_id: str
    node_id: str
    service_name: str
    severity: str
    alert_type: str
    message: str
    measured_value: float | None
    timestamp: datetime


@dataclass
class SignatureStats:
    """All window alerts sharing (node_id, alert_type)."""
    node_id: str
    alert_type: str
    region: str
    services: list[str]
    alert_ids: list[str]
    count: int
    first_at: datetime
    last_at: datetime
    max_severity: str
    baseline_count: int
    expected: float
    surprise_p: float
    slope_per_min: float
    r_squared: float
    last_value: float | None
    pattern: str  # routine | burst | trend

    @property
    def duration_s(self) -> float:
        return (self.last_at - self.first_at).total_seconds()


@dataclass
class RootCandidate:
    node_id: str
    node_name: str
    node_type: str
    region: str
    service_name: str
    alert_type: str
    first_at: datetime
    sample_message: str
    score: float
    components: dict[str, float]


@dataclass
class Incident:
    cluster_id: str
    alert_ids: list[str]
    alert_count: int
    started_at: datetime
    last_seen_at: datetime
    regions: list[str]
    services: list[str]
    nodes: list[str]
    signatures: list[dict[str, Any]]
    root_cause: RootCandidate
    candidates: list[RootCandidate]
    confidence: float
    symptom_services: list[str]
    max_severity: str

    def fingerprint(self) -> str:
        """Stable across reruns of the same storm: root node + signature + onset minute."""
        rc = self.root_cause
        return f"{rc.node_id}|{rc.alert_type}|{self.started_at.strftime('%Y-%m-%dT%H:%M')}"


@dataclass
class TriageResult:
    window_start: datetime
    window_end: datetime
    total_alerts: int
    routine_alerts: int
    incidents: list[Incident]
    precursors: list[SignatureStats] = field(default_factory=list)  # slow trends -> forecasting (M3)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj
