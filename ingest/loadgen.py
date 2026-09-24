"""Synthetic alert load for the M3 throughput test (source='loadgen', excluded from triage).

    python -m ingest.loadgen --rate 1500 --minutes 5
    python -m ingest.loadgen --rate 1500 --minutes 5 --secret-rate 0.01 --poison-rate 0.005

Alerts reference real node_ids from the topology table (FK-valid). A small
share carry fake credentials (built at runtime) to prove redaction, and a small
share are malformed to prove dead-lettering. Prints a JSON manifest the
reconciler checks against.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import uuid
from datetime import datetime, timezone

TYPES = [
    ("cpu_utilization_high", "WARNING", 70, 25, "CPU utilization sustained at {v:.1f}% on {n}"),
    ("memory_pressure", "WARNING", 72, 18, "Memory working set at {v:.1f}% on {n}"),
    ("packet_loss_minor", "WARNING", 0.5, 3, "Upstream packet loss {v:.2f}% observed on {n}"),
    ("log_ingest_lag", "INFO", 30, 500, "Log shipping pipeline lagging {v:.1f} s on {n}"),
    ("api_rate_limit_warning", "INFO", 60, 30, "Client API quota at {v:.1f}% on {n}"),
]


def fake_secret(rng: random.Random) -> str:
    # Assembled at runtime so no credential-shaped literal lives in the repo.
    choice = rng.randrange(3)
    if choice == 0:
        return "PGPASSWORD=" + "".join(rng.choices("abcdefXYZ0123456789", k=14))
    if choice == 1:
        return "Authorization: Bearer " + "".join(rng.choices("abcdefghijklmnop0123456789", k=32))
    return "key=" + "AI" + "za" + "".join(rng.choices("ABCDEFabcdef0123456789_-", k=35))


def make_alert(run_id: str, seq: int, nodes: list[tuple[str, str]], rng: random.Random,
               secret: bool) -> dict:
    node_id, service = rng.choice(nodes)
    alert_type, sev, lo, span, fmt = rng.choice(TYPES)
    v = lo + span * rng.random()
    msg = fmt.format(v=v, n=node_id)
    if secret:
        msg += f" | debug dump: {fake_secret(rng)}"
    return {
        "alert_id": f"ALT-LIVE-{run_id}-{seq:07d}",
        "node_id": node_id, "service_name": service, "severity": sev, "alert_type": alert_type,
        "message": msg, "measured_value": round(v, 2),
        "timestamp": datetime.now(timezone.utc).isoformat(), "source": "loadgen",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rate", type=int, default=1500, help="alerts per minute")
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--topic", default="alerts-in")
    ap.add_argument("--secret-rate", type=float, default=0.01)
    ap.add_argument("--poison-rate", type=float, default=0.005)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)

    from google.cloud import pubsub_v1
    from agent.app.config import load_settings
    from agent.app.tools.bq import Warehouse

    s = load_settings()
    wh = Warehouse(s)
    placements = wh.query(f"""
        SELECT DISTINCT node_id, service_name FROM {s.table('sre_telemetry.alert_stream')}""")
    nodes = [(r["node_id"], r["service_name"]) for r in placements]

    publisher = pubsub_v1.PublisherClient(
        batch_settings=pubsub_v1.types.BatchSettings(max_messages=500, max_latency=0.05))
    topic = publisher.topic_path(s.project, args.topic)
    rng = random.Random(args.seed)
    run_id = uuid.uuid4().hex[:8]
    total = int(args.rate * args.minutes)
    per_tick = max(1, args.rate // 60)  # one tick per second
    futures, sent_valid, sent_poison, sent_secret = [], 0, 0, 0
    t0 = time.monotonic()
    started = datetime.now(timezone.utc)

    seq = 0
    while seq < total:
        tick_start = time.monotonic()
        for _ in range(min(per_tick, total - seq)):
            seq += 1
            if rng.random() < args.poison_rate:
                data = rng.choice([b"not json", b'{"alert_id": "x"}', b'{"severity": "LOUD"}'])
                sent_poison += 1
            else:
                secret = rng.random() < args.secret_rate
                sent_secret += secret
                data = json.dumps(make_alert(run_id, seq, nodes, rng, secret)).encode()
                sent_valid += 1
            futures.append(publisher.publish(topic, data))
        elapsed = time.monotonic() - tick_start
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)

    failures = 0
    for f in futures:
        try:
            f.result(timeout=60)
        except Exception:
            failures += 1
    duration = time.monotonic() - t0
    manifest = {
        "run_id": run_id, "alert_prefix": f"ALT-LIVE-{run_id}-", "sent_valid": sent_valid,
        "sent_poison": sent_poison, "sent_with_fake_secret": sent_secret, "publish_failures": failures,
        "started_at": started.isoformat(), "duration_s": round(duration, 1),
        "publish_rate_per_min": round((sent_valid + sent_poison) / duration * 60),
    }
    print(json.dumps(manifest))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
