"""Business impact of an incident (M4): revenue at risk and SLA credits.

All arithmetic happens in sre_agent_ops.v_incident_impact; this module only
groups the warehouse rows for display and for the postmortem.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from ..models import TIER_RANK


@dataclass
class TierImpact:
    tier: str
    accounts: int
    mrr_cad: float
    sla_credit_cad: float
    breached_accounts: int
    credit_rate: float
    restoration_target_minutes: int


@dataclass
class Impact:
    incident_id: str
    status: str | None
    downtime_minutes: float
    ongoing: bool
    started_at: datetime | None
    impact_end: datetime | None
    services: list[str]
    regions: list[str]
    revenue_at_risk_cad: float          # MRR of every affected account
    prorated_revenue_cad: float         # MRR share of the downtime window
    sla_credit_cad: float
    tiers: list[TierImpact]
    customers: list[dict[str, Any]] = field(default_factory=list)
    basis: str = ("credit = mrr_cad x tier credit_rate x downtime_minutes / minutes_in_month; "
                  "credit rates and restoration targets from sre_agent_ops.sla_policy")

    @property
    def breached_tiers(self) -> list[str]:
        return [t.tier for t in self.tiers if t.breached_accounts]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["breached_tiers"] = self.breached_tiers
        return d


def summarize(incident_id: str, rows: list[dict[str, Any]]) -> Impact:
    if not rows:
        return Impact(incident_id, None, 0.0, False, None, None, [], [], 0.0, 0.0, 0.0, [])
    first = rows[0]
    by_tier: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_tier[r["tier"]].append(r)
    tiers = [
        TierImpact(
            tier=t,
            accounts=len(rs),
            mrr_cad=round(sum(r["mrr_cad"] for r in rs), 2),
            sla_credit_cad=round(sum(r["sla_credit_cad"] for r in rs), 2),
            breached_accounts=sum(bool(r["sla_breached"]) for r in rs),
            credit_rate=rs[0]["credit_rate"],
            restoration_target_minutes=rs[0]["restoration_target_minutes"],
        )
        for t, rs in sorted(by_tier.items(), key=lambda kv: -TIER_RANK.get(kv[0], 0))
    ]
    customers = sorted(
        ({k: r[k] for k in ("customer_id", "customer_name", "service_name", "region", "tier",
                            "mrr_cad", "sla_credit_cad", "sla_breached")} for r in rows),
        key=lambda c: (-TIER_RANK.get(c["tier"], 0), -c["mrr_cad"]),
    )
    for c in customers:
        c["sla_credit_cad"] = round(c["sla_credit_cad"], 2)
    return Impact(
        incident_id=incident_id,
        status=first["status"],
        downtime_minutes=round(first["downtime_minutes"], 2),
        ongoing=bool(first["ongoing"]),
        started_at=first["started_at"],
        impact_end=first["impact_end"],
        services=sorted({r["service_name"] for r in rows}),
        regions=sorted({r["region"] for r in rows}),
        revenue_at_risk_cad=round(sum(r["mrr_cad"] for r in rows), 2),
        prorated_revenue_cad=round(sum(r["prorated_revenue_cad"] for r in rows), 2),
        sla_credit_cad=round(sum(r["sla_credit_cad"] for r in rows), 2),
        tiers=tiers,
        customers=customers,
    )


def impact_sentence(imp: Impact) -> str:
    """Deterministic, customer-facing blast radius (the postmortem's impact_summary)."""
    if not imp.customers:
        return "No customer accounts are mapped to the affected services in the affected regions."
    tiers = ", ".join(f"{t.accounts} {t.tier}" for t in imp.tiers)
    breached = ", ".join(imp.breached_tiers) or "none"
    names = "; ".join(f"{c['customer_name']} ({c['tier']}, {c['service_name']})" for c in imp.customers[:8])
    more = f" and {len(imp.customers) - 8} more" if len(imp.customers) > 8 else ""
    return (
        f"Services degraded: {', '.join(imp.services)} in {', '.join(imp.regions)} for "
        f"{imp.downtime_minutes:.1f} customer-impacting minutes{' (ongoing)' if imp.ongoing else ''}. "
        f"Accounts affected: {len(imp.customers)} ({tiers}): {names}{more}. "
        f"SLA restoration target breached for tiers: {breached}. "
        f"Revenue at risk (MRR of affected accounts): CAD {imp.revenue_at_risk_cad:,.2f}; "
        f"SLA credits owed: CAD {imp.sla_credit_cad:,.2f}."
    )
