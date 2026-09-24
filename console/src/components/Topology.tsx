import { useEffect, useMemo, useState } from "react";
import { Background, Controls, MarkerType, ReactFlow, type Edge, type Node } from "@xyflow/react";
import { api, type Json } from "../api";

const LAYER: Record<string, number> = { edge_node: 0, gateway: 1, load_balancer: 2, vm: 3, cache: 4, database: 5 };

/** Inferred dependency graph (there is no edge list in the warehouse). Tiers run top to bottom. */
export default function Topology({ incidentId }: { incidentId: string | null }) {
  const [data, setData] = useState<Json | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [scope, setScope] = useState<"incident" | "region">("incident");

  useEffect(() => {
    setData(null);
    api.topology(incidentId ?? undefined).then(setData).catch((e) => setErr(String(e.message ?? e)));
  }, [incidentId]);

  const { nodes, edges } = useMemo(() => {
    if (!data) return { nodes: [] as Node[], edges: [] as Edge[] };
    const involved = new Set<string>(data.nodes.filter((n: Json) => n.role).map((n: Json) => n.node_id));
    const regions = new Set<string>(data.nodes.filter((n: Json) => n.role).map((n: Json) => n.region));
    const shown = data.nodes.filter((n: Json) =>
      involved.size === 0 ? true : scope === "incident" ? involved.has(n.node_id) : regions.has(n.region));
    const byCell = new Map<string, number>();
    const nodes: Node[] = shown.map((n: Json) => {
      const layer = LAYER[n.node_type] ?? 3;
      const key = `${n.region}|${layer}`;
      const i = byCell.get(key) ?? 0;
      byCell.set(key, i + 1);
      const col = [...regions].sort().indexOf(n.region);
      const role = n.role as string | null;
      const border = role === "root" ? "var(--critical)" : role === "symptom" ? "var(--serious)" : "var(--axis)";
      return {
        id: n.node_id,
        position: { x: Math.max(col, 0) * 760 + i * 180, y: layer * 110 },
        data: {
          label: (
            <div style={{ fontSize: 11, lineHeight: 1.3 }}>
              {role && <div style={{ fontWeight: 700, color: border }}>{role === "root" ? "● ROOT CAUSE" : "▲ symptom"}</div>}
              <div style={{ fontWeight: 600 }}>{n.node_name}</div>
              <div style={{ color: "var(--muted)" }}>{n.node_type} · {n.region}{n.status === "degraded" ? " · degraded" : ""}</div>
            </div>
          ),
        },
        style: { border: `${role ? 2 : 1}px solid ${border}`, borderRadius: 8, background: "var(--surface)", color: "var(--ink)", width: 165, padding: 6 },
      };
    });
    const ids = new Set(nodes.map((n) => n.id));
    const root = data.nodes.find((n: Json) => n.role === "root")?.node_id;
    const edges: Edge[] = data.edges
      .filter((e: Json) => ids.has(e.source) && ids.has(e.target) && (involved.size === 0 || involved.has(e.source)))
      .map((e: Json) => ({
        id: `${e.source}->${e.target}`, source: e.source, target: e.target,
        animated: e.target === root,
        style: { stroke: e.target === root ? "var(--critical)" : "var(--axis)", strokeWidth: e.target === root ? 2 : 1 },
        markerEnd: { type: MarkerType.ArrowClosed },
      }));
    return { nodes, edges };
  }, [data, scope]);

  if (err) return <p className="notice error">{err}</p>;
  if (!data) return <p className="muted">Loading topology…</p>;
  return (
    <div>
      <div className="row small sub" style={{ marginBottom: 6 }}>
        Arrows point from a node to what it depends on; red arrows lead to the root cause.
        {incidentId && (
          <span style={{ marginLeft: "auto" }}>
            <button onClick={() => setScope(scope === "incident" ? "region" : "incident")}>
              {scope === "incident" ? "Show whole region" : "Show incident nodes only"}
            </button>
          </span>
        )}
      </div>
      <div className="topo">
        <ReactFlow nodes={nodes} edges={edges} fitView minZoom={0.2} proOptions={{ hideAttribution: true }}
          nodesDraggable nodesConnectable={false}>
          <Background color="var(--grid)" />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
    </div>
  );
}
