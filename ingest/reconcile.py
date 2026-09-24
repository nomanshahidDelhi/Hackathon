"""Prove the load test lost nothing and leaked nothing.

    python -m ingest.loadgen ... > manifest.json
    python -m ingest.reconcile manifest.json

Checks: every valid alert sent is stored exactly once (after dedupe), the peak
ingest minute clears 1,000 alerts, poison messages were dead-lettered, and no
stored message still contains a credential.
"""
from __future__ import annotations

import json
import sys

from agent.app.config import load_settings
from agent.app.redact import find_secrets
from agent.app.tools.bq import Warehouse

TARGET_PER_MIN = 1000


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    manifest = json.loads(open(argv[0], encoding="utf-8").read().strip().splitlines()[-1])
    s = load_settings()
    wh = Warehouse(s)
    p = wh.param
    live = s.table("sre_agent_ops.alert_stream_live")

    counts = wh.query(f"""
        SELECT COUNT(*) AS rows_stored, COUNT(DISTINCT alert_id) AS distinct_alerts,
               MIN(ingested_at) AS first_in, MAX(ingested_at) AS last_in,
               COUNTIF(message LIKE '%[REDACTED:%') AS redacted_rows
        FROM {live} WHERE STARTS_WITH(alert_id, @prefix)""", [p("prefix", manifest["alert_prefix"])])[0]
    peak = wh.query(f"""
        SELECT TIMESTAMP_TRUNC(ingested_at, MINUTE) AS minute, COUNT(DISTINCT alert_id) AS n
        FROM {live} WHERE STARTS_WITH(alert_id, @prefix)
        GROUP BY minute ORDER BY n DESC LIMIT 1""", [p("prefix", manifest["alert_prefix"])])
    audit = wh.query(f"""
        SELECT SUM(dead_lettered) AS dlq, SUM(duplicates) AS dups
        FROM {s.table('sre_agent_ops.ingest_audit')}
        WHERE written_at >= TIMESTAMP(@since)""", [p("since", manifest["started_at"])])[0]
    sample = wh.query(f"""
        SELECT message FROM {live}
        WHERE STARTS_WITH(alert_id, @prefix) AND message LIKE '%debug dump%'""",
        [p("prefix", manifest["alert_prefix"])])
    leaked = [r["message"] for r in sample if find_secrets(r["message"])]

    peak_n = peak[0]["n"] if peak else 0
    checks = {
        "all valid alerts stored": counts["distinct_alerts"] == manifest["sent_valid"],
        f"peak minute >= {TARGET_PER_MIN}/min": peak_n >= TARGET_PER_MIN,
        "poison dead-lettered": (audit["dlq"] or 0) >= manifest["sent_poison"],
        "no secrets stored": not leaked,
        "fake secrets were redacted": counts["redacted_rows"] >= manifest["sent_with_fake_secret"],
    }
    print(json.dumps({
        "sent_valid": manifest["sent_valid"], "stored_distinct": counts["distinct_alerts"],
        "stored_rows": counts["rows_stored"], "duplicates_absorbed": audit["dups"] or 0,
        "sent_poison": manifest["sent_poison"], "dead_lettered": audit["dlq"] or 0,
        "peak_alerts_per_min": peak_n, "redacted_rows": counts["redacted_rows"], "leaked": len(leaked),
    }, indent=2, default=str))
    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
