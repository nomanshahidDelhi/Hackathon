from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from agent.app.models import Alert, Node
from agent.app.tools.clustering import linear_fit, poisson_sf, triage
from agent.app.tools.topology import Topology
from tests.fixtures import kit_sim

T0 = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
BASELINE_MIN = 7 * 1440.0


def run(alerts, nodes, now, lookback_min):
    start = now - timedelta(minutes=lookback_min)
    baseline = Counter((a.node_id, a.alert_type) for a in alerts
                       if start - timedelta(days=7) <= a.timestamp < start)
    return triage(alerts, nodes, baseline, BASELINE_MIN, start, now)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def test_poisson_sf():
    assert poisson_sf(0, 2.0) == 1.0
    assert poisson_sf(1, 2.0) == pytest.approx(1 - 0.1353, abs=1e-3)
    assert poisson_sf(20, 0.1) < 1e-20


def test_linear_fit():
    slope, r2 = linear_fit([(0, 1), (1, 3), (2, 5)])
    assert slope == pytest.approx(2.0) and r2 == pytest.approx(1.0)
    assert linear_fit([(0, 5), (1, 5)]) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# generic synthetic estate (no kit data)
# ---------------------------------------------------------------------------
NODES = [
    Node("n-edge", "edge-proxy-01", "edge_node", "r1"),
    Node("n-lb", "shop-lb-01", "load_balancer", "r1"),
    Node("n-app", "shop-api-vm-01", "vm", "r1"),
    Node("n-db", "shop-db-primary", "database", "r1", status="degraded"),
    Node("n-db-r", "shop-db-replica-01", "database", "r1"),
    Node("n-other", "mail-vm-01", "vm", "r2"),
]


def _a(i, node, svc, typ, msg, secs, val=1.0, sev="CRITICAL"):
    return Alert(f"a{i}", node, svc, sev, typ, msg, val, T0 + timedelta(seconds=secs))


def test_topology_infers_edges_from_names():
    topo = Topology(NODES)
    assert "n-db" in topo.deps["n-app"]            # shared "shop" token, shallower -> deeper
    assert "n-db" in topo.deps["n-db-r"]           # replica -> primary
    assert {"n-lb", "n-app", "n-db"} <= topo.deps["n-edge"]  # edge tier fronts its region
    assert "n-other" not in topo.dependents("n-db")          # other region


def test_db_saturation_beats_louder_symptoms():
    alerts = [_a(i, "n-db", "shop-db", "pool_exhausted", "pool exhausted: 100/100 connections", i)
              for i in range(5)]
    alerts += [_a(100 + i, "n-app", "shop-api", "http_5xx", "503 from upstream database", 30 + i)
               for i in range(40)]
    alerts += [_a(200 + i, "n-edge", "edge", "latency_spike", "p99 high, upstream timeout", 40 + i)
               for i in range(40)]
    alerts += [_a(300 + i, "n-other", "mail", "cpu_high", "cpu 80%", 10 + i * 30, sev="WARNING")
               for i in range(3)]
    baseline = {("n-other", "cpu_high"): 2000}  # routine for this node

    res = triage(alerts, NODES, baseline, BASELINE_MIN, T0 - timedelta(minutes=5), T0 + timedelta(minutes=5))
    assert len(res.incidents) == 1
    inc = res.incidents[0]
    assert inc.root_cause.node_id == "n-db"
    assert inc.alert_count == 85
    assert not any(a.startswith("a3") and len(a) == 4 for a in inc.alert_ids)  # routine cpu left out
    assert set(inc.symptom_services) == {"shop-api", "edge"}


def test_trend_is_routed_to_precursors_not_clustered():
    alerts = [_a(i, "n-edge", "edge", "disk_pressure", f"disk {60 + i:.1f}%", i * 60, val=60 + i, sev="WARNING")
              for i in range(30)]
    res = triage(alerts, NODES, {}, BASELINE_MIN, T0, T0 + timedelta(minutes=40))
    assert res.incidents == []
    assert [(p.node_id, p.alert_type) for p in res.precursors] == [("n-edge", "disk_pressure")]
    assert res.precursors[0].slope_per_min == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# simulated kit data
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def kit():
    return kit_sim.load_nodes(), kit_sim.generate(T0)


def test_simulator_matches_kit_shape(kit):
    nodes, alerts = kit
    assert len(nodes) == 64
    assert len(alerts) == 3000
    assert len({a.alert_id for a in alerts}) == 3000


@pytest.mark.parametrize("minutes_after_seed,lookback", [(0, 60), (0, 180), (5, 60), (240, 360)])
def test_kit_storm_collapses_to_one_incident(kit, minutes_after_seed, lookback):
    nodes, alerts = kit
    res = run(alerts, nodes, T0 + timedelta(minutes=minutes_after_seed), lookback)

    storm = {a.alert_id for a in alerts if a.alert_id.startswith("ALT-STORM")}
    storm_incidents = [i for i in res.incidents if storm & set(i.alert_ids)]
    assert len(storm_incidents) == 1, [(i.cluster_id, i.alert_count) for i in res.incidents]
    inc = storm_incidents[0]

    assert set(inc.alert_ids) == storm, "storm must be complete and contain nothing else"
    assert inc is res.incidents[0], "storm must be the top incident"
    assert inc.root_cause.alert_type == "connection_pool_exhausted"
    assert inc.root_cause.node_id == "node-db-01"
    assert inc.confidence > 0.3


def test_kit_ramp_is_a_precursor(kit):
    nodes, alerts = kit
    res = run(alerts, nodes, T0, 120)
    ramp_ids = {a.alert_id for a in alerts if a.alert_id.startswith("ALT-RAMP")}
    for inc in res.incidents:
        assert not ramp_ids & set(inc.alert_ids)
    pre = {(p.node_id, p.alert_type) for p in res.precursors}
    assert len({n for n, t in pre if t == "disk_pressure_edge_node"}) == 6


def test_kit_background_noise_never_forms_incidents(kit):
    nodes, alerts = kit
    for hours_back in (24, 48, 96):
        now = T0 - timedelta(hours=hours_back)
        res = run([a for a in alerts if a.timestamp <= now], nodes, now, 60)
        big = [i for i in res.incidents if i.alert_count >= 10]
        assert big == [], [(i.alert_count, i.root_cause.alert_type) for i in big]
