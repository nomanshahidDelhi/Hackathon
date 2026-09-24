"""Pub/Sub -> BigQuery alert consumer (M3).

    python -m ingest.consumer                       # runs until Ctrl+C
    python -m ingest.consumer --max-batch 500 --flush-seconds 1

Streaming pull with flow control; a background thread flushes every
--flush-seconds or whenever a batch fills. Rows go to
sre_agent_ops.alert_stream_live via the streaming API with insertId=alert_id.
Malformed messages are published to the dead-letter topic and acked.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time

from agent.app.config import load_settings
from agent.app.logging_setup import setup_logging
from ingest.writer import BatchWriter

log = logging.getLogger("ingest")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--subscription", default=os.environ.get("ALERTS_SUBSCRIPTION", "alerts-in-ingest"))
    ap.add_argument("--dlq-topic", default=os.environ.get("ALERTS_DLQ_TOPIC", "alerts-in-dlq"))
    ap.add_argument("--max-batch", type=int, default=500)
    ap.add_argument("--flush-seconds", type=float, default=1.0)
    ap.add_argument("--max-outstanding", type=int, default=5000)
    args = ap.parse_args(argv)
    setup_logging()

    from google.cloud import bigquery, pubsub_v1

    s = load_settings()
    bq = bigquery.Client(project=s.project, location=s.location)
    live_table = f"{s.project}.sre_agent_ops.alert_stream_live"
    audit_table = f"{s.project}.sre_agent_ops.ingest_audit"
    publisher = pubsub_v1.PublisherClient()
    dlq_path = publisher.topic_path(s.project, args.dlq_topic)
    subscriber = pubsub_v1.SubscriberClient()
    sub_path = subscriber.subscription_path(s.project, args.subscription)

    def sink(rows, row_ids):
        errors = bq.insert_rows_json(live_table, rows, row_ids=row_ids)
        return [e["index"] for e in errors]

    def dead_letter(data: bytes, reason: str):
        publisher.publish(dlq_path, data, reason=reason[:500]).result(timeout=30)

    def audit(rec):
        bq.insert_rows_json(audit_table, [rec])

    writer = BatchWriter(sink, dead_letter, audit, max_batch=args.max_batch)
    wake = threading.Event()
    stop = threading.Event()

    def callback(message):
        if writer.add(message.data, message.ack, message.nack):
            wake.set()

    def flusher():
        while not stop.is_set():
            wake.wait(args.flush_seconds)
            wake.clear()
            try:
                writer.flush()
            except Exception:
                log.exception("flush failed; messages will be redelivered")

    def reporter():
        while not stop.wait(15):
            st = writer.stats
            log.info("received=%d written=%d dup=%d dlq=%d write_failures=%d rate=%.0f/min",
                     st.received, st.written, st.duplicates, st.dead_lettered, st.write_failures,
                     st.rate_per_min())

    threads = [threading.Thread(target=flusher, daemon=True), threading.Thread(target=reporter, daemon=True)]
    for t in threads:
        t.start()
    flow = pubsub_v1.types.FlowControl(max_messages=args.max_outstanding)
    future = subscriber.subscribe(sub_path, callback=callback, flow_control=flow)
    log.info("consuming %s -> %s", sub_path, live_table)

    def shutdown(*_):
        future.cancel()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        future.result()
    except Exception:
        pass
    stop.set()
    time.sleep(args.flush_seconds)
    writer.flush()
    st = writer.stats
    print(json.dumps({"received": st.received, "written": st.written, "duplicates": st.duplicates,
                      "dead_lettered": st.dead_lettered, "write_failures": st.write_failures}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
