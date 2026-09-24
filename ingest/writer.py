"""Batching core of the alert ingest (M3): validate -> redact -> dedupe -> write -> ack.

Guarantees, so "no alert dropped" is provable:
  - a message is acked only after its row is written (or it is a known duplicate,
    or it has been forwarded to the dead-letter topic as malformed);
  - a failed write nacks, so Pub/Sub redelivers (and dead-letters after N tries);
  - duplicates from at-least-once delivery are acked but not written twice
    (in-memory window here, BigQuery insertId, and the v_alerts QUALIFY);
  - every flush produces an audit record: received / written / duplicates / dead-lettered.
Pure logic: the BigQuery sink, dead-letter publisher and audit sink are injected.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from agent.app.redact import redact

SEVERITIES = {"CRITICAL", "ERROR", "WARNING", "INFO"}
REQUIRED = ("alert_id", "node_id", "service_name", "severity", "alert_type", "message", "timestamp")
MAX_FUTURE = timedelta(minutes=5)

# sink(rows, row_ids) -> indexes of rows that failed to write
Sink = Callable[[list[dict[str, Any]], list[str]], list[int]]


class InvalidAlert(ValueError):
    pass


def parse_alert(data: bytes, source: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    try:
        d = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidAlert(f"not JSON: {exc}") from None
    if not isinstance(d, dict):
        raise InvalidAlert("payload is not an object")
    missing = [k for k in REQUIRED if not d.get(k)]
    if missing:
        raise InvalidAlert(f"missing {','.join(missing)}")
    if d["severity"] not in SEVERITIES:
        raise InvalidAlert(f"bad severity {d['severity']!r}")
    if not isinstance(d["alert_id"], str) or len(d["alert_id"]) > 128:
        raise InvalidAlert("bad alert_id")
    try:
        ts = datetime.fromisoformat(str(d["timestamp"]).replace("Z", "+00:00"))
    except ValueError:
        raise InvalidAlert("bad timestamp") from None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if ts > now + MAX_FUTURE:
        raise InvalidAlert("timestamp in the future")
    mv = d.get("measured_value")
    if mv is not None and not isinstance(mv, (int, float)):
        raise InvalidAlert("measured_value not numeric")
    return {
        "alert_id": d["alert_id"],
        "node_id": str(d["node_id"])[:128],
        "service_name": str(d["service_name"])[:128],
        "severity": d["severity"],
        "alert_type": str(d["alert_type"])[:128],
        "message": redact(str(d["message"])[:8000]),
        "measured_value": float(mv) if mv is not None else None,
        "timestamp": ts.isoformat(),
        "ingested_at": now.isoformat(),
        "source": str(d.get("source") or source)[:64],
    }


@dataclass
class Pending:
    row: dict[str, Any]
    ack: Callable[[], None]
    nack: Callable[[], None]


@dataclass
class Stats:
    received: int = 0
    written: int = 0
    duplicates: int = 0
    dead_lettered: int = 0
    write_failures: int = 0
    batches: int = 0
    started: float = field(default_factory=time.monotonic)

    def rate_per_min(self) -> float:
        return self.written / max(time.monotonic() - self.started, 1e-9) * 60


class BatchWriter:
    def __init__(
        self,
        sink: Sink,
        dead_letter: Callable[[bytes, str], None],
        audit: Callable[[dict[str, Any]], None] | None = None,
        source: str = "pubsub",
        max_batch: int = 500,
        seen_capacity: int = 200_000,
    ):
        self.sink, self.dead_letter, self.audit = sink, dead_letter, audit
        self.source, self.max_batch = source, max_batch
        self._buf: list[Pending] = []
        self._lock = threading.Lock()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_capacity = seen_capacity
        self.stats = Stats()
        self._dl_reported = 0

    def add(self, data: bytes, ack: Callable[[], None], nack: Callable[[], None]) -> bool:
        """Returns True when the buffer is full and should be flushed."""
        with self._lock:
            self.stats.received += 1
        try:
            row = parse_alert(data, self.source)
        except InvalidAlert as exc:
            try:
                self.dead_letter(data, str(exc))
            except Exception:
                nack()  # could not park it; let Pub/Sub retry and dead-letter it itself
                return False
            with self._lock:
                self.stats.dead_lettered += 1
            ack()
            return False
        with self._lock:
            self._buf.append(Pending(row, ack, nack))
            return len(self._buf) >= self.max_batch

    def flush(self) -> int:
        with self._lock:
            batch, self._buf = self._buf, []
            dead = self.stats.dead_lettered - self._dl_reported
            self._dl_reported = self.stats.dead_lettered
        if not batch and not dead:
            return 0

        fresh: list[Pending] = []
        dup = 0
        batch_ids: set[str] = set()
        for p in batch:
            aid = p.row["alert_id"]
            if aid in self._seen or aid in batch_ids:
                dup += 1
                p.ack()
            else:
                batch_ids.add(aid)
                fresh.append(p)

        failed: set[int] = set()
        if fresh:
            try:
                failed = set(self.sink([p.row for p in fresh], [p.row["alert_id"] for p in fresh]))
            except Exception:
                failed = set(range(len(fresh)))

        written = 0
        for i, p in enumerate(fresh):
            if i in failed:
                p.nack()
            else:
                p.ack()
                written += 1
                self._remember(p.row["alert_id"])

        with self._lock:
            self.stats.written += written
            self.stats.duplicates += dup
            self.stats.write_failures += len(failed)
            self.stats.batches += 1
        if self.audit:
            ts = [p.row["timestamp"] for p in batch] or [datetime.now(timezone.utc).isoformat()]
            try:
                self.audit({
                    "batch_id": uuid.uuid4().hex[:16], "received": len(batch) + dead, "written": written,
                    "duplicates": dup, "dead_lettered": dead,
                    "first_event_at": min(ts), "last_event_at": max(ts),
                    "written_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception:
                pass  # audit is best-effort; the data path already succeeded
        return written

    def _remember(self, alert_id: str) -> None:
        self._seen[alert_id] = None
        if len(self._seen) > self._seen_capacity:
            self._seen.popitem(last=False)
