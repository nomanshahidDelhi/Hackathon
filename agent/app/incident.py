"""M4 CLI: business impact, resolution and the executive postmortem.

    python -m agent.app.incident list
    python -m agent.app.incident impact inc-5019
    python -m agent.app.incident resolve inc-5019 --by "Your Name"
    python -m agent.app.incident postmortem inc-5019 [--publish]    # writes incident_postmortems
    python -m agent.app.incident publish pm-2026-0004
    python -m agent.app.incident metrics
"""
from __future__ import annotations

import argparse
import json
import sys

from .logging_setup import setup_logging
from .service import ServiceError, SREService
from .tools.impact import Impact


def render_impact(imp: Impact) -> str:
    if not imp.customers:
        return f"{imp.incident_id}: no mapped customer accounts in the affected services/regions"
    out = [
        f"{imp.incident_id} [{imp.status}] downtime {imp.downtime_minutes:.1f} min"
        f"{' (ongoing)' if imp.ongoing else ''} | services {', '.join(imp.services)} | {', '.join(imp.regions)}",
        f"revenue at risk (MRR): CAD {imp.revenue_at_risk_cad:>12,.2f}",
        f"prorated revenue     : CAD {imp.prorated_revenue_cad:>12,.2f}",
        f"SLA credits owed     : CAD {imp.sla_credit_cad:>12,.2f}",
        "by tier:",
    ]
    for t in imp.tiers:
        out.append(f"  {t.tier:<7} accounts={t.accounts:<3} mrr={t.mrr_cad:>12,.2f} credits={t.sla_credit_cad:>10,.2f} "
                   f"rate={t.credit_rate:g} target={t.restoration_target_minutes}m breached={t.breached_accounts}")
    out.append("accounts:")
    for c in imp.customers:
        out.append(f"  {c['customer_id']} {c['customer_name']:<34} {c['tier']:<6} {c['service_name']:<20} "
                   f"mrr={c['mrr_cad']:>11,.2f} credit={c['sla_credit_cad']:>9,.2f}{'  BREACHED' if c['sla_breached'] else ''}")
    out.append(f"basis: {imp.basis}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("metrics")
    sub.add_parser("impact").add_argument("incident_id")
    r = sub.add_parser("resolve")
    r.add_argument("incident_id")
    r.add_argument("--by", required=True)
    pm = sub.add_parser("postmortem")
    pm.add_argument("incident_id")
    pm.add_argument("--publish", action="store_true")
    pm.add_argument("--no-llm", action="store_true")
    sub.add_parser("publish").add_argument("postmortem_id")
    args = ap.parse_args(argv)
    setup_logging()

    svc = SREService(use_llm=not getattr(args, "no_llm", False))
    try:
        if args.cmd == "list":
            for i in svc.list_incidents():
                tag = f"agent: {i['alert_count']} alerts, root {i['root_alert_type']}@{i['root_node_id']}" \
                    if i["agent_detected"] else "historical"
                print(f"{i['incident_id']}  {i['status']:<13} {i['severity']}  {i['affected_region']:<13} "
                      f"{str(i['started_at'])[:19]}  {i['title'][:60]}  [{tag}]")
        elif args.cmd == "metrics":
            print(json.dumps(svc.metrics(), indent=2))
        elif args.cmd == "impact":
            print(render_impact(svc.impact(args.incident_id)))
        elif args.cmd == "resolve":
            print(f"{args.incident_id} RESOLVED at {svc.resolve(args.incident_id, args.by)}")
        elif args.cmd == "postmortem":
            d, published = svc.write_postmortem(args.incident_id, publish=args.publish)
            print(f"{d.postmortem_id} for {d.incident_id} [{d.source}] "
                  f"{'PUBLISHED' if published else 'DRAFT (published_at NULL)'}")
            for n in d.notes:
                print(f"  note: {n}")
            print(f"\nroot cause:\n{d.root_cause}\n\nimpact:\n{d.impact_summary}\n\n"
                  f"downtime: {d.downtime_minutes:.2f} min\n\ncontributing factors:\n"
                  + "\n".join(f"  - {f}" for f in d.contributing_factors)
                  + "\n\naction items:\n" + "\n".join(f"  - {a}" for a in d.action_items))
            if args.publish and not published:
                print("\nnot published: the incident is not RESOLVED yet (resolve it, then `publish`)")
        elif args.cmd == "publish":
            svc.publish_postmortem(args.postmortem_id)
            print(f"{args.postmortem_id} published")
    except ServiceError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
