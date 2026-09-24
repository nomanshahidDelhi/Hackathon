import { useState } from "react";
import type { Json } from "../api";

const W = 720, H = 240, PAD = { l: 44, r: 16, t: 14, b: 26 };

/** Observed readings, the fitted projection to the threshold, and "now". One series: no legend box. */
export default function ForecastChart({ points, f }: { points: { t: string; v: number }[]; f: Json }) {
  const [hover, setHover] = useState<number | null>(null);
  if (!points.length) return <p className="muted">No readings in the window.</p>;

  const obs = points.map((p) => ({ t: new Date(p.t).getTime(), v: p.v }));
  const last = new Date(f.last_at).getTime();
  const breach = new Date(f.predicted_breach_at).getTime();
  const now = last + f.data_age_minutes * 60000;
  const t0 = obs[0].t;
  const span0 = Math.max(breach, now, last) - t0;
  const t1 = t0 + span0 * 1.08; // headroom so the breach marker and its label stay inside the plot
  const vMin = Math.floor(Math.min(...obs.map((o) => o.v)) - 2);
  const vMax = Math.ceil(Math.max(f.threshold, ...obs.map((o) => o.v)) + 2);
  const iw = W - PAD.l - PAD.r, ih = H - PAD.t - PAD.b;
  const x = (t: number) => PAD.l + ((t - t0) / (t1 - t0)) * iw;
  const y = (v: number) => PAD.t + ih - ((v - vMin) / (vMax - vMin)) * ih;
  const ticks = [vMin, (vMin + vMax) / 2, vMax].map((v) => Math.round(v));
  const h = hover != null ? obs[hover] : null;

  function onMove(e: React.MouseEvent<SVGSVGElement>) {
    const r = e.currentTarget.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    let best = 0;
    obs.forEach((o, i) => { if (Math.abs(x(o.t) - px) < Math.abs(x(obs[best].t) - px)) best = i; });
    setHover(best);
  }

  return (
    <div className="chart-wrap">
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} onMouseMove={onMove} onMouseLeave={() => setHover(null)}
        role="img" aria-label={`${f.alert_type} on ${f.node_name}: ${f.current_value} now, threshold ${f.threshold}, projected breach ${new Date(breach).toLocaleTimeString()}`}>
        {ticks.map((v) => (
          <g key={v}>
            <line x1={PAD.l} x2={W - PAD.r} y1={y(v)} y2={y(v)} stroke="var(--grid)" />
            <text x={PAD.l - 6} y={y(v) + 4} textAnchor="end" fontSize={11} fill="var(--muted)">{v}</text>
          </g>
        ))}
        <line x1={PAD.l} x2={W - PAD.r} y1={y(f.threshold)} y2={y(f.threshold)} stroke="var(--critical)" strokeDasharray="5 4" strokeWidth={1.5} />
        <text x={PAD.l + 4} y={y(f.threshold) - 5} fontSize={11} fill="var(--ink-2)">threshold {f.threshold}</text>
        <line x1={x(now)} x2={x(now)} y1={PAD.t} y2={PAD.t + ih} stroke="var(--axis)" />
        <text x={x(now) + 4} y={PAD.t + ih - 6} fontSize={11} fill="var(--muted)">now</text>
        <line x1={x(last)} y1={y(f.current_value)} x2={x(breach)} y2={y(f.threshold)}
          stroke="var(--series-1)" strokeWidth={2} strokeDasharray="6 4" />
        {obs.map((o, i) => (
          <circle key={i} cx={x(o.t)} cy={y(o.v)} r={4} fill="var(--series-1)" stroke="var(--surface)" strokeWidth={2} />
        ))}
        <circle cx={x(breach)} cy={y(f.threshold)} r={5} fill="var(--surface)" stroke="var(--critical)" strokeWidth={2} />
        <text x={x(breach)} y={y(f.threshold) + 18} textAnchor={x(breach) > W - 90 ? "end" : "middle"} fontSize={11} fill="var(--ink-2)">
          breach {new Date(breach).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
        </text>
        {h && <line x1={x(h.t)} x2={x(h.t)} y1={PAD.t} y2={PAD.t + ih} stroke="var(--ink-2)" strokeWidth={1} />}
        <text x={PAD.l} y={H - 8} fontSize={11} fill="var(--muted)">{new Date(t0).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</text>
      </svg>
      {h && hover != null && (
        <div className="tip" style={{ left: `${(x(h.t) / W) * 100}%`, top: 0 }}>
          {new Date(h.t).toLocaleTimeString()} · {h.v.toFixed(2)}
        </div>
      )}
    </div>
  );
}
