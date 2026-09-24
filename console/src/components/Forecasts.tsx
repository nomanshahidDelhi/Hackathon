import { useEffect, useState } from "react";
import { api, time, type Json } from "../api";
import ForecastChart from "./ForecastChart";
import Status from "./Status";

export default function Forecasts({ refresh }: { refresh: number }) {
  const [fc, setFc] = useState<Json[] | null>(null);
  const [sel, setSel] = useState(0);
  const [points, setPoints] = useState<Json[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.forecasts(false).then((d) => { setFc(d); setSel(0); }).catch((e) => setErr(e.message));
  }, [refresh]);
  const f = fc?.[sel];
  useEffect(() => {
    if (f) api.series(f.node_id, f.alert_type).then(setPoints).catch(() => setPoints([]));
  }, [f?.node_id, f?.alert_type]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="card">
      <h2>Breach forecast</h2>
      {err && <p className="notice error">{err}</p>}
      {!fc && !err && <p className="muted">Fitting trends…</p>}
      {fc && fc.length === 0 && <p className="muted">No signal is trending toward a threshold.</p>}
      {fc && fc.length > 0 && f && (
        <div className="stack">
          <div className="hero">
            <span className="big">{f.status === "STALE" ? "—" : `${Math.round(f.eta_minutes)} min`}</span>
            <span className="sub">
              until <strong>{f.alert_type}</strong> on {f.node_name} crosses {f.threshold} ·
              rising {f.slope_per_min >= 0 ? "+" : ""}{f.slope_per_min}/min · R² {f.r_squared}
            </span>
          </div>
          {f.status === "STALE" && <p className="notice">Projected breach time has passed with no new readings for {Math.round(f.data_age_minutes)} min: verify the node and its collector.</p>}
          <ForecastChart points={points} f={f} />
          <div className="table-wrap"><table>
            <thead><tr><th>Status</th><th>Node</th><th>Region</th><th className="num">Now</th><th className="num">Threshold</th><th className="num">Slope/min</th><th className="num">ETA (min)</th><th className="num">Band</th><th>Breach at</th></tr></thead>
            <tbody>{fc.map((x, i) => (
              <tr key={x.node_id + x.alert_type} onClick={() => setSel(i)} style={{ cursor: "pointer", background: i === sel ? "var(--surface-2)" : undefined }}>
                <td><Status value={x.status} /></td><td>{x.node_name}</td><td>{x.region}</td>
                <td className="num">{x.current_value.toFixed(1)}</td><td className="num">{x.threshold}</td>
                <td className="num">{x.slope_per_min.toFixed(3)}</td><td className="num">{x.eta_minutes.toFixed(1)}</td>
                <td className="num">{x.eta_low_minutes.toFixed(1)}–{x.eta_high_minutes == null ? "?" : x.eta_high_minutes.toFixed(1)}</td>
                <td>{time(x.predicted_breach_at)}</td>
              </tr>
            ))}</tbody>
          </table></div>
        </div>
      )}
    </div>
  );
}
