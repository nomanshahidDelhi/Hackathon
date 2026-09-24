from datetime import datetime, timezone

import pytest

from agent.app.llm import LLMUnavailable
from agent.app.tools.impact import impact_sentence, summarize
from agent.app.tools.postmortem import (build_facts, draft_postmortem, next_postmortem_id,
                                        root_cause_is_causal)

T = datetime(2026, 9, 25, 11, 46, tzinfo=timezone.utc)


def row(cid, name, svc, tier, mrr, rate, target, downtime=45.0, mim=43200):
    return {"incident_id": "inc-5019", "title": "t", "status": "INVESTIGATING", "severity": "P1",
            "started_at": T, "impact_end": T, "ongoing": False, "downtime_minutes": downtime,
            "minutes_in_month": mim, "customer_id": cid, "customer_name": name, "service_name": svc,
            "region": "ca-central-1", "tier": tier, "mrr_cad": mrr, "credit_rate": rate,
            "restoration_target_minutes": target,
            "prorated_revenue_cad": mrr * downtime / mim,
            "sla_credit_cad": mrr * rate * downtime / mim, "sla_breached": downtime > target}


ROWS = [
    row("CUST-1001", "Loblaw", "billing-service", "GOLD", 248500.0, 10.0, 30),
    row("CUST-1002", "BMO", "customer-billing-db", "GOLD", 212750.0, 10.0, 30),
    row("CUST-2001", "Metro", "payment-gateway", "SILVER", 78200.0, 5.0, 240),
    row("CUST-3007", "Laurentian", "billing-service", "BRONZE", 12400.0, 2.0, 480),
]


def test_impact_summary_groups_warehouse_rows():
    imp = summarize("inc-5019", ROWS)
    assert imp.revenue_at_risk_cad == pytest.approx(248500 + 212750 + 78200 + 12400)
    # GOLD: (248500 + 212750) * 10 * 45 / 43200
    gold = next(t for t in imp.tiers if t.tier == "GOLD")
    assert gold.sla_credit_cad == pytest.approx(461250 * 10 * 45 / 43200, abs=0.01)
    assert [t.tier for t in imp.tiers] == ["GOLD", "SILVER", "BRONZE"]
    assert imp.breached_tiers == ["GOLD"]
    assert imp.customers[0]["customer_name"] == "Loblaw"
    s = impact_sentence(imp)
    assert "4 accounts" not in s and "Accounts affected: 4" in s and "GOLD" in s and "CAD" in s


def test_empty_impact():
    imp = summarize("inc-1", [])
    assert imp.revenue_at_risk_cad == 0 and "No customer accounts" in impact_sentence(imp)


def test_postmortem_ids():
    assert next_postmortem_id([], 2026) == "pm-2026-0001"
    assert next_postmortem_id(["pm-2026-0041", "pm-2026-0007", "pm-2025-0100", "junk", None], 2026) == "pm-2026-0042"


TRIAGE = {
    "alert_count": 450,
    "symptom_services": ["billing-service", "edge-gateway", "payment-gateway"],
    "root_cause": {"node_id": "node-db-01", "node_name": "customer-billing-db-primary",
                   "service_name": "customer-billing-db", "region": "ca-central-1",
                   "alert_type": "connection_pool_exhausted", "first_at": T.isoformat(),
                   "sample_message": "Connection pool exhausted: 200/200 active connections"},
    "signatures": [{"node_id": "node-db-01", "alert_type": "connection_pool_exhausted"},
                   {"node_id": "node-gateway-01", "alert_type": "gateway_5xx_surge"},
                   {"node_id": "node-vm-01", "alert_type": "latency_spike"}],
}


@pytest.fixture
def facts():
    return build_facts({"incident_id": "inc-5019", "title": "t", "status": "RESOLVED", "severity": "P1",
                        "affected_region": "ca-central-1", "started_at": T, "resolved_at": T},
                       TRIAGE, summarize("inc-5019", ROWS),
                       [{"execution_id": "exec-9019", "runbook_id": "sop-102", "status": "SUCCESS",
                         "hitl_approved": True}],
                       [{"approval_id": "apr-1", "status": "APPROVED", "decided_by": "Jane"}],
                       {"id": "sop-102", "title": "Recover pool",
                        "steps": "8. Verify.\n9. Follow up permanently: enforce idle_in_transaction_session_timeout."})


@pytest.mark.parametrize("text,ok", [
    ("Connection pool exhaustion on customer-billing-db-primary starved billing-service; the edge 5xx surge followed.", True),
    ("A gateway 5xx surge at edge-gateway caused the outage.", False),                  # symptom as cause
    ("Latency spike on billing caused customer-billing-db connection pool exhausted.", False),  # leads with symptom
    ("The database had problems.", False),                                              # vague
])
def test_root_cause_validation(facts, text, ok):
    assert root_cause_is_causal(text, facts)[0] is ok


class LLM:
    def __init__(self, out=None, fail=False):
        self.out, self.fail = out, fail

    def generate_json(self, prompt, schema, temperature=0.2):
        if self.fail:
            raise LLMUnavailable("down")
        return self.out


def test_postmortem_uses_gemini_when_valid(facts):
    llm = LLM({"root_cause": "Connection pool exhaustion on customer-billing-db-primary blocked billing clients.",
               "contributing_factors": ["No idle-in-transaction timeout", "Pool ceiling not alerted"],
               "action_items": [{"action": "Enforce idle_in_transaction_session_timeout", "owner_team": "Billing DB"}]})
    d = draft_postmortem("pm-2026-0001", facts, summarize("inc-5019", ROWS), llm)
    assert d.source == "gemini"
    assert d.action_items == ["Enforce idle_in_transaction_session_timeout (owner: Billing DB)"]
    row = d.row()
    assert row["downtime_minutes"] == 45.0          # from the warehouse, not the LLM
    assert row["contributing_factors"].count("\n") == 1
    assert "Revenue at risk" in row["impact_summary"]


def test_postmortem_rejects_symptom_root_cause(facts):
    llm = LLM({"root_cause": "The edge-gateway 5xx surge took down billing.",
               "contributing_factors": ["x"], "action_items": [{"action": "y", "owner_team": "z"}]})
    d = draft_postmortem("pm-2026-0001", facts, summarize("inc-5019", ROWS), llm)
    assert d.source == "deterministic" and "customer-billing-db-primary" in d.root_cause
    assert any("rejected" in n for n in d.notes)


def test_postmortem_without_gemini(facts):
    d = draft_postmortem("pm-2026-0001", facts, summarize("inc-5019", ROWS), LLM(fail=True))
    assert d.source == "deterministic"
    assert "cascade symptoms" in d.root_cause
    assert any("idle_in_transaction_session_timeout" in a for a in d.action_items)
