import json
import logging
import time
from datetime import datetime, timedelta, timezone

import pytest

from agent.app.logging_setup import RedactingFormatter
from agent.app.models import Alert, Node
from agent.app.tools.forecast import compute_forecasts, ols, parse_threshold, policy_threshold
from ingest.writer import BatchWriter, InvalidAlert, parse_alert
from tests.fixtures import kit_sim

T0 = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
NODE = Node("n1", "edge-a", "edge_node", "r1")
FAKE_KEY = "AI" + "za" + "Q" * 35  # assembled at runtime; not a real key


def ramp(n=30, start=60.0, slope=0.5, every_min=2.0, msg="disk {v:.1f}% -- projected to breach 95% capacity threshold",
         end=T0, noise=0.0):
    out = []
    for i in range(n):
        v = start + slope * every_min * i + (noise if i % 2 else -noise)
        out.append(Alert(f"a{i}", "n1", "svc", "WARNING", "disk_pressure", msg.format(v=v), v,
                         end - timedelta(minutes=every_min * (n - 1 - i))))
    return out


# ---------------------------------------------------------------------------
# forecasting
# ---------------------------------------------------------------------------
def test_ols_exact_line():
    fit = ols([(0, 1), (1, 3), (2, 5), (3, 7)])
    assert fit.slope == pytest.approx(2) and fit.intercept == pytest.approx(1) and fit.r2 == pytest.approx(1)


@pytest.mark.parametrize("msg,thr", [
    ("projected to breach 95% capacity threshold", 95.0),
    ("pool at 81% (limit 90)", 90.0),
    ("usage 88% of 92% ceiling", 92.0),
    ("cpu 71% on host", None),
])
def test_parse_threshold(msg, thr):
    assert parse_threshold(msg) == thr


def test_policy_threshold_longest_match_wins():
    assert policy_threshold("connection_pool_exhausted", {"pool": 80.0, "connection_pool": 95.0}) == 95.0
    assert policy_threshold("ntp_offset_drift", {"disk": 95.0}) is None


def test_linear_ramp_eta():
    # 60 -> 89 over 58 min at 0.5/min; 95 is 6 points away -> 12 min.
    fc = compute_forecasts(ramp(), [NODE], T0)
    assert len(fc) == 1
    f = fc[0]
    assert f.status == "WARN" and f.threshold_source == "alert message"
    assert f.slope_per_min == pytest.approx(0.5, abs=1e-3)
    assert f.eta_minutes == pytest.approx(12.0, abs=0.2)
    assert f.eta_low_minutes <= f.eta_minutes <= f.eta_high_minutes


def test_flat_or_noisy_series_is_not_forecast():
    flat = ramp(slope=0.0, noise=3.0)
    assert compute_forecasts(flat, [NODE], T0) == []
    falling = ramp(slope=-0.5, start=90)
    assert compute_forecasts(falling, [NODE], T0) == []


def test_watch_beyond_horizon_and_policy_threshold():
    slow = ramp(slope=0.05, msg="disk {v:.1f}%")
    fc = compute_forecasts(slow, [NODE], T0, policy={"disk": 95.0})
    assert fc[0].status == "WATCH" and fc[0].threshold_source == "policy"
    assert compute_forecasts(slow, [NODE], T0) == []  # no threshold anywhere -> no forecast


def test_stale_when_breach_time_passed_without_data():
    fc = compute_forecasts(ramp(), [NODE], T0 + timedelta(minutes=30))
    assert fc[0].status == "STALE" and fc[0].data_age_minutes == pytest.approx(30)


def test_kit_ramp_warns_15_to_30_minutes_ahead():
    nodes, alerts = kit_sim.load_nodes(), kit_sim.generate(T0)
    window = [a for a in alerts if T0 - timedelta(minutes=120) <= a.timestamp <= T0]
    fc = compute_forecasts(window, nodes, T0)
    assert {f.alert_type for f in fc} == {"disk_pressure_edge_node"}
    assert len(fc) == 6 and all(f.status == "WARN" for f in fc)
    assert all(15 <= f.eta_minutes <= 30 for f in fc)
    storm_ids = {a.alert_id for a in alerts if a.alert_id.startswith("ALT-STORM")}
    assert not any(f.node_id == "node-db-01" for f in fc) and storm_ids  # bursts are not trends


# ---------------------------------------------------------------------------
# ingest writer
# ---------------------------------------------------------------------------
def payload(i, **kw):
    d = {"alert_id": f"ALT-LIVE-t-{i:06d}", "node_id": "node-vm-01", "service_name": "billing-service",
         "severity": "WARNING", "alert_type": "cpu_utilization_high", "message": f"cpu {i}",
         "measured_value": 71.5, "timestamp": datetime.now(timezone.utc).isoformat(), "source": "loadgen"}
    d.update(kw)
    return json.dumps(d).encode()


class Recorder:
    def __init__(self, fail_first=0):
        self.rows, self.dlq, self.audits, self.fail_first = [], [], [], fail_first

    def sink(self, rows, ids):
        if self.fail_first:
            self.fail_first -= 1
            return list(range(len(rows)))
        self.rows += rows
        return []

    def dead_letter(self, data, reason):
        self.dlq.append(reason)


def acks():
    state = {"ack": 0, "nack": 0}
    return state, (lambda: state.__setitem__("ack", state["ack"] + 1)), (lambda: state.__setitem__("nack", state["nack"] + 1))


@pytest.mark.parametrize("data,reason", [
    (b"not json", "not JSON"),
    (payload(1, severity="LOUD"), "bad severity"),
    (payload(1, alert_id=""), "missing alert_id"),
    (payload(1, timestamp=(T0 + timedelta(hours=2)).isoformat()), "future"),
    (payload(1, measured_value="high"), "not numeric"),
])
def test_parse_alert_rejects(data, reason):
    with pytest.raises(InvalidAlert, match=reason):
        parse_alert(data, "pubsub", now=T0)


def test_parse_alert_redacts_message():
    row = parse_alert(payload(1, message=f"dump key={FAKE_KEY} PGPASSWORD=hunter2hunter2"), "pubsub", now=T0)
    assert FAKE_KEY not in row["message"] and "hunter2" not in row["message"]
    assert "[REDACTED:GCP_API_KEY]" in row["message"]


def test_writer_acks_after_write_dedupes_and_dead_letters():
    rec = Recorder()
    w = BatchWriter(rec.sink, rec.dead_letter, rec.audits.append)
    st, ack, nack = acks()
    for i in range(10):
        w.add(payload(i), ack, nack)
    w.add(payload(3), ack, nack)            # redelivered duplicate in the same batch
    w.add(b"garbage", ack, nack)            # poison
    assert st["ack"] == 1                   # only the poison is acked before the write
    w.flush()
    w.add(payload(5), ack, nack)            # duplicate arriving after it was written
    w.flush()
    assert len(rec.rows) == 10 and len({r["alert_id"] for r in rec.rows}) == 10
    assert st == {"ack": 13, "nack": 0}
    assert w.stats.duplicates == 2 and w.stats.dead_lettered == 1
    assert sum(a["written"] for a in rec.audits) == 10
    assert sum(a["dead_lettered"] for a in rec.audits) == 1


def test_writer_nacks_on_sink_failure_then_recovers():
    rec = Recorder(fail_first=1)
    w = BatchWriter(rec.sink, rec.dead_letter)
    st, ack, nack = acks()
    for i in range(5):
        w.add(payload(i), ack, nack)
    w.flush()
    assert st == {"ack": 0, "nack": 5} and rec.rows == []
    for i in range(5):                      # Pub/Sub redelivers
        w.add(payload(i), ack, nack)
    w.flush()
    assert st["ack"] == 5 and len(rec.rows) == 5


def test_writer_throughput_well_above_1000_per_minute():
    rec = Recorder()
    w = BatchWriter(rec.sink, rec.dead_letter, max_batch=500)
    n = 20_000
    msgs = [payload(i) for i in range(n)]
    t = time.perf_counter()
    for m in msgs:
        if w.add(m, lambda: None, lambda: None):
            w.flush()
    w.flush()
    per_min = n / (time.perf_counter() - t) * 60
    assert len(rec.rows) == n
    assert per_min > 60_000, f"{per_min:.0f}/min"  # processing is never the bottleneck at 1,000/min


# ---------------------------------------------------------------------------
# log redaction
# ---------------------------------------------------------------------------
def test_logs_are_redacted_including_args_and_tracebacks():
    fmt = RedactingFormatter("%(message)s")
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "connecting with %s", (f"key={FAKE_KEY}",), None)
    assert FAKE_KEY not in fmt.format(rec)
    try:
        raise RuntimeError("postgres://svc:hunter2secret@db/billing refused")
    except RuntimeError:
        import sys
        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    assert "hunter2secret" not in fmt.format(rec)
