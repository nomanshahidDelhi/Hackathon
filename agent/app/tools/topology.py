"""Infer a dependency graph from the node inventory.

The warehouse has no edge list, so edges come from three kinds of evidence:
  1. Static: same region, a shallower tier depends on a deeper tier when the
     nodes share a domain token from their names/services (billing-lb-01 ->
     billing-service-vm-01 -> customer-billing-db-primary).
  2. Naming: `<base>-replica-NN` depends on `<base>-primary`; edge-tier nodes
     (names containing "edge") front everything deeper in their region.
  3. Alert evidence (per incident): a message that blames an upstream / the db
     adds edges from that node to deeper nodes in the same incident.
Edge direction: A -> B means "A depends on B" (a fault in B can surface at A).
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

from ..models import Alert, Node

# Shallow -> deep. Requests enter at the edge and bottom out in state.
LAYER = {"edge_node": 0, "gateway": 1, "load_balancer": 2, "vm": 3, "cache": 4, "database": 5}
MAX_LAYER = max(LAYER.values())

# Name parts that describe the kind of box, not the business domain.
GENERIC_TOKENS = {
    "node", "vm", "lb", "db", "cache", "ctrl", "service", "cluster", "primary", "replica",
    "gateway", "broker", "hub", "chassis", "pop", "shield", "origin", "ro",
    "cc1", "ce1", "cw1",
}

UPSTREAM_RE = re.compile(r"\bupstream", re.IGNORECASE)  # also matches upstream_connect_error
DB_BLAME_RE = re.compile(r"\b(db|database)\b", re.IGNORECASE)


def layer(node: Node) -> int:
    return LAYER.get(node.node_type, 3)


def name_tokens(text: str) -> set[str]:
    return {
        t for t in re.split(r"[^a-z0-9]+", text.lower())
        if t and not t.isdigit() and t not in GENERIC_TOKENS and len(t) > 1
    }


class Topology:
    def __init__(self, nodes: Iterable[Node], node_services: dict[str, set[str]] | None = None):
        self.nodes: dict[str, Node] = {n.node_id: n for n in nodes}
        self.node_services = node_services or {}
        self.deps: dict[str, set[str]] = defaultdict(set)
        self._build_static()

    # -- construction -------------------------------------------------------
    def tokens(self, node_id: str) -> set[str]:
        node = self.nodes[node_id]
        toks = name_tokens(node.node_name)
        for svc in self.node_services.get(node_id, ()):
            toks |= name_tokens(svc)
        return toks

    def _build_static(self) -> None:
        by_region: dict[str, list[Node]] = defaultdict(list)
        for n in self.nodes.values():
            by_region[n.region].append(n)

        for region_nodes in by_region.values():
            toks = {n.node_id: self.tokens(n.node_id) for n in region_nodes}
            for a in region_nodes:
                fronting = "edge" in name_tokens(a.node_name) and layer(a) <= LAYER["gateway"]
                for b in region_nodes:
                    if a.node_id == b.node_id or layer(a) >= layer(b):
                        continue
                    if fronting or toks[a.node_id] & toks[b.node_id]:
                        self.deps[a.node_id].add(b.node_id)

            names = {n.node_name: n.node_id for n in region_nodes}
            for n in region_nodes:
                if "-replica" in n.node_name:
                    base = n.node_name.split("-replica")[0]
                    for name, nid in names.items():
                        if name.startswith(base) and "primary" in name:
                            self.deps[n.node_id].add(nid)

    def with_alert_evidence(self, alerts: Iterable[Alert]) -> "Topology":
        """Copy of this graph plus edges implied by what the alerts say."""
        alerts = list(alerts)
        involved = {a.node_id for a in alerts if a.node_id in self.nodes}
        g = Topology.__new__(Topology)
        g.nodes, g.node_services = self.nodes, self.node_services
        g.deps = defaultdict(set, {k: set(v) for k, v in self.deps.items()})

        for a in alerts:
            src = self.nodes.get(a.node_id)
            if src is None:
                continue
            blames_upstream = bool(UPSTREAM_RE.search(a.message))
            blames_db = blames_upstream and bool(DB_BLAME_RE.search(a.message))
            if not blames_upstream:
                continue
            for nid in involved:
                dst = self.nodes[nid]
                if nid == src.node_id or dst.region != src.region or layer(dst) <= layer(src):
                    continue
                if blames_db and dst.node_type != "database":
                    continue
                g.deps[src.node_id].add(nid)
        return g

    # -- queries -------------------------------------------------------------
    def dependents(self, target: str, within: set[str] | None = None) -> set[str]:
        """Nodes that (transitively) depend on target, optionally restricted to a subset."""
        reverse: dict[str, set[str]] = defaultdict(set)
        for a, bs in self.deps.items():
            for b in bs:
                reverse[b].add(a)
        seen: set[str] = set()
        stack = [target]
        while stack:
            cur = stack.pop()
            for up in reverse.get(cur, ()):
                if up not in seen and up != target:
                    seen.add(up)
                    stack.append(up)
        return seen & within if within is not None else seen

    def edges(self, within: set[str] | None = None) -> list[tuple[str, str]]:
        return sorted(
            (a, b) for a, bs in self.deps.items() for b in bs
            if within is None or (a in within and b in within)
        )
