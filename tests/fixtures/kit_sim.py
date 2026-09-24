"""Test-only simulator of the kit's telemetry generator (data/sql/06_seed_telemetry.sql).

Band structure, message formats and timing are ported from the SQL; the
node/placement/target lists are parsed straight out of the kit files so the
simulation tracks the kit. FARM_FINGERPRINT is replaced by a SHA-256 hash, so
values differ but the shape (who, what, when, how many) matches.

Never imported by the agent -- this is test input, not an answer key.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.app.models import Alert, Node

KIT = Path(__file__).resolve().parents[2] / "data" / "sql"


def h(key: str) -> int:
    return abs(int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True))


def _block(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def load_nodes() -> list[Node]:
    sql = (KIT / "05_seed_topology.sql").read_text(encoding="utf-8")
    rx = re.compile(r"\('(node-[\w-]+)',\s*'([\w-]+)',\s*'(\w+)',\s*'([\w-]+)',\s*'([\d.]+)',\s*'(\w+)'\)")
    return [Node(*m) for m in rx.findall(sql)]


def _config():
    sql = (KIT / "06_seed_telemetry.sql").read_text(encoding="utf-8")
    placements = re.findall(r"\('([\w-]+)',\s*'([\w-]+)'\)",
                            _block(sql, "ARRAY<STRUCT<service_name STRING, node_id STRING>>", "AS placements"))
    noise = [(t, s, float(a), float(b)) for t, s, a, b in re.findall(
        r"\('(\w+)',\s*'(\w+)',\s*([\d.]+),\s*([\d.]+)\)", _block(sql, "vspan FLOAT64>>", "AS noise_types"))]
    cascade = re.findall(r"\('([\w-]+)',\s*'([\w-]+)',\s*'(\w+)'\)",
                         _block(sql, "node_id STRING, alert_type STRING>>", "AS cascade_targets"))
    edges = re.findall(r"STRUCT\('(node-[\w-]+)'\)", _block(sql, "ARRAY<STRUCT<node_id STRING>>", "AS edge_nodes"))
    dom = [(n, s, t, float(lo), float(sp)) for n, s, t, lo, sp in re.findall(
        r"\('(node-[\w-]+)',\s*'([\w-]+)',\s*'(\w+)',\s*(-?[\d.]+),\s*([\d.]+)\)",
        _block(sql, "telemetry_band_e AS", "][OFFSET(MOD(i, 16))]"))]
    return placements, noise, cascade, edges, dom


def generate(seeded_at: datetime | None = None) -> list[Alert]:
    """All 3,000 alerts as the kit would produce them if seeded at `seeded_at`."""
    now = seeded_at or datetime.now(timezone.utc)
    placements, noise, cascade, edges, dom = _config()
    out: list[Alert] = []

    for i in range(1, 1401):  # band D: week of routine noise
        svc, node = placements[h(f"place-{i}") % len(placements)]
        t, sev, vmin, vspan = noise[h(f"type-{i}") % len(noise)]
        v = vmin + vspan * (h(f"val-{i}") % 1000) / 1000.0
        out.append(Alert(f"ALT-NOISE-{i:06d}", node, svc, sev, t,
                         f"{t.replace('_', ' ')} at {v:.1f} on {node}", round(v, 2),
                         now - timedelta(seconds=h(f"ts-{i}") % 604800)))

    storm_t0 = now - timedelta(minutes=14)
    for r in range(1, 21):  # band A: root
        q = 40 + h(f"root-q-{r}") % 260
        out.append(Alert(
            f"ALT-STORM-ROOT-{r:03d}", "node-db-01", "customer-billing-db", "CRITICAL",
            "connection_pool_exhausted",
            f"Connection pool exhausted on customer-billing-db-primary (node-db-01): 200/200 active "
            f"connections, {q} clients queued, acquire timeout 5000 ms exceeded",
            100.0,
            storm_t0 + timedelta(milliseconds=round(28000.0 * (r - 1) / 19.0) + h(f"root-j-{r}") % 800)))

    for j in range(1, 431):  # band B: cascade
        svc, node, t = cascade[h(f"casc-tgt-{j}") % len(cascade)]
        frac = (h(f"casc-v-{j}") % 1000) / 1000.0
        if t == "gateway_5xx_surge":
            v = 18.0 + 78.0 * frac
            msg = f"HTTP 5xx surge on {svc} via {node}: {v:.1f}% of requests returning 503 upstream_connect_error (SLO 0.5%)"
        else:
            v = 1200.0 + 7800.0 * frac
            msg = f"p99 latency spike on {svc} via {node}: {v:.0f} ms, upstream db acquire blocking (SLO 400 ms)"
        ms = round(1000.0 * (52.0 + 187.0 * (j / 430.0) ** 1.6)) + h(f"casc-j-{j}") % 1000
        out.append(Alert(f"ALT-STORM-CASC-{j:04d}", node, svc, "CRITICAL", t, msg, round(v, 2),
                         storm_t0 + timedelta(milliseconds=ms)))

    for i in range(150):  # band C: slow ramp on edge nodes
        node = edges[i % len(edges)]
        v = 60.0 + 28.0 * i / 149.0 + ((h(f"ramp-n-{i}") % 71) - 35) / 100.0
        out.append(Alert(
            f"ALT-RAMP-{i:04d}", node, "edge-gateway", "CRITICAL" if v >= 80 else "WARNING",
            "disk_pressure_edge_node",
            f"Disk usage on {node} at {v:.2f}% -- /var/log and session spool growing ~0.31 pp/min, "
            f"projected to breach 95% capacity threshold",
            round(v, 2), now - timedelta(milliseconds=round((90.0 - 90.0 * i / 149.0) * 60000.0))))

    for i in range(1000):  # band E: domain noise
        node, svc, t, lo, span = dom[i % 16]
        h1, h2 = h(f"dom-1-{i}") % 1000000, h(f"dom-2-{i}") % 1000000
        v = lo + (h1 % 1000) / 1000.0 * span
        out.append(Alert(f"ALT-DOM-{i:04d}", node, svc, "ERROR" if h1 % 5 == 0 else "WARNING", t,
                         f"{t} on {node} (service={svc}, measured={v:.2f})", round(v, 2),
                         now - timedelta(seconds=2700 + h2 % 602100)))
    return out
