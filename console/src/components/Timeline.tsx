import { useMemo, useState } from "react";

type Row = { bucket: string; kind: "root" | "symptom"; n: number };
type Bucket = { t: number; root: number; symptom: number };

const W = 720, H = 220, PAD = { l: 40, r: 12, t: 12, b: 26 };

function niceMax(v: number) {
  if (v <= 5) return 5;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  return Math.ceil(v / p) * p;
}

// Bar with a 4px rounded data end on top, flat on the baseline.
function bar(x: number, y: number, w: number, h: number, r = 4) {
  if (h <= 0) return "";
  const rr = Math.min(r, h, w / 2);
  return `M${x},${y + h}V${y + rr}Q${x},${y} ${x + rr},${y}H${x + w - rr}Q${x + w},${y} ${x + w},${y + rr}V${y + h}Z`;
}

export default function Timeline({ rows }: { rows: Row[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const buckets = useMemo<Bucket[]>(() => {
    const m = new Map<number, Bucket>();
    for (const r of rows) {
      const t = new Date(r.bucket).getTime();
      const b = m.get(t) ?? { t, root: 0, symptom: 0 };
      b[r.kind] += r.n;
      m.set(t, b);
    }
    return [...m.values()].sort((a, b) => a.t - b.t);
  }, [rows]);

  if (!buckets.length) return <p className="muted">No correlated alerts to plot.</p>;

  const max = niceMax(Math.max(...buckets.map((b) => b.root + b.symptom)));
  const iw = W - PAD.l - PAD.r, ih = H - PAD.t - PAD.b;
  const step = iw / buckets.length;
  const bw = Math.max(2, Math.min(18, step - 2));
  const y = (v: number) => PAD.t + ih - (v / max) * ih;
  const ticks = [0, max / 2, max];
  const labelEvery = Math.ceil(buckets.length / 6);
  const totals = buckets.reduce((a, b) => ({ root: a.root + b.root, symptom: a.symptom + b.symptom }), { root: 0, symptom: 0 });
  const h = hover != null ? buckets[hover] : null;

  return (
    <div className="chart-wrap">
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img"
        aria-label={`Correlated alerts per 10 seconds: ${totals.root} root-cause alerts, ${totals.symptom} symptom alerts`}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={PAD.l} x2={W - PAD.r} y1={y(t)} y2={y(t)} stroke={t === 0 ? "var(--axis)" : "var(--grid)"} strokeWidth={1} />
            <text x={PAD.l - 6} y={y(t) + 4} textAnchor="end" fontSize={11} fill="var(--muted)">{t}</text>
          </g>
        ))}
        {buckets.map((b, i) => {
          const x = PAD.l + i * step + (step - bw) / 2;
          const rootTop = y(b.root);
          const gap = b.root > 0 && b.symptom > 0 ? 2 : 0;
          const symH = ih - (y(b.symptom) - PAD.t) - gap;
          return (
            <g key={b.t} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
              <rect x={PAD.l + i * step} y={PAD.t} width={step} height={ih} fill="transparent" />
              {b.root > 0 && <path d={bar(x, rootTop, bw, PAD.t + ih - rootTop, b.symptom > 0 ? 0 : 4)} fill="var(--series-1)" />}
              {b.symptom > 0 && <path d={bar(x, rootTop - gap - symH, bw, symH)} fill="var(--series-2)" />}
              {i % labelEvery === 0 && (
                <text x={x + bw / 2} y={H - 8} textAnchor="middle" fontSize={11} fill="var(--muted)">
                  {new Date(b.t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      {h && hover != null && (
        <div className="tip" style={{ left: `${((PAD.l + hover * step) / W) * 100}%`, top: 0 }}>
          <div>{new Date(h.t).toLocaleTimeString()}</div>
          <div><i style={{ display: "inline-block", width: 8, height: 8, background: "var(--series-1)", borderRadius: 2, marginRight: 5 }} />root cause {h.root}</div>
          <div><i style={{ display: "inline-block", width: 8, height: 8, background: "var(--series-2)", borderRadius: 2, marginRight: 5 }} />symptoms {h.symptom}</div>
        </div>
      )}
      <div className="legend">
        <span><i style={{ background: "var(--series-1)" }} />Root cause ({totals.root})</span>
        <span><i style={{ background: "var(--series-2)" }} />Cascade symptoms ({totals.symptom})</span>
      </div>
    </div>
  );
}
