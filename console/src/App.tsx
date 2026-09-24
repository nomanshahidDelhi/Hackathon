import { useCallback, useEffect, useState } from "react";
import { api, type Json } from "./api";
import AgentPanel from "./components/AgentPanel";
import Forecasts from "./components/Forecasts";
import IncidentView from "./components/IncidentView";
import Status from "./components/Status";

function stored(key: string) {
  try { return localStorage.getItem(key) ?? ""; } catch { return ""; }
}

export default function App() {
  const [incidents, setIncidents] = useState<Json[]>([]);
  const [metrics, setMetrics] = useState<Json | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [actor, setActor] = useState(() => stored("sre-actor"));
  const [refresh, setRefresh] = useState(0);
  const [err, setErr] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);

  const reload = useCallback(() => {
    api.incidents().then((xs: Json[]) => {
      setIncidents(xs);
      setSel((cur) => cur ?? xs.find((x) => x.agent_detected)?.incident_id ?? xs[0]?.incident_id ?? null);
    }).catch((e) => setErr(e.message));
    api.metrics().then(setMetrics).catch(() => {});
    setRefresh((r) => r + 1);
  }, []);
  useEffect(() => { reload(); const t = setInterval(() => api.metrics().then(setMetrics).catch(() => {}), 15000); return () => clearInterval(t); }, [reload]);
  useEffect(() => { try { localStorage.setItem("sre-actor", actor); } catch { /* private mode */ } }, [actor]);

  async function scan() {
    setScanning(true);
    try {
      const r = await api.scan();
      if (r.opened) setSel(r.opened.incident_id);
      reload();
    } catch (e) { setErr((e as Error).message); } finally { setScanning(false); }
  }

  const agent = metrics?.agent ?? {};
  return (
    <div className="app">
      <header className="top">
        <h1>SRE Incident Console</h1>
        <span className="sub small">alert storms → one incident → runbook → approved fix → postmortem</span>
        <label className="who">Acting as
          <input type="text" value={actor} onChange={(e) => setActor(e.target.value)} placeholder="your name" aria-label="Your name for approvals" />
        </label>
      </header>

      <section className="kpis" aria-label="Key metrics">
        <div className="kpi"><div className="label">Open incidents</div><div className="value">{metrics?.open_incidents ?? "–"}</div></div>
        <div className="kpi"><div className="label">MTTR (all resolved)</div><div className="value">{metrics?.mttr_minutes ?? "–"} min</div></div>
        <div className="kpi"><div className="label">Agent MTTR</div><div className="value">{agent.agent_mttr_minutes ?? "–"} min</div><div className="hint">{agent.agent_incidents ?? 0} agent incidents</div></div>
        <div className="kpi"><div className="label">Alerts clustered</div><div className="value">{agent.alerts_clustered ?? 0}</div><div className="hint">into {agent.agent_incidents ?? 0} incidents</div></div>
        <div className="kpi"><div className="label">Pending approvals</div><div className="value">{metrics?.approvals?.pending ?? 0}</div><div className="hint">avg decision {metrics?.approvals?.approve_minutes ?? "–"} min</div></div>
        <div className="kpi"><div className="label">Alerts / min (5 min)</div><div className="value">{metrics?.alerts_per_minute_5m ?? "–"}</div></div>
      </section>

      <aside className="stack">
        <div className="card">
          <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
            <h2 style={{ margin: 0 }}>Incidents</h2>
            <button className="primary" disabled={scanning} onClick={scan}>{scanning ? "Scanning…" : "Scan alerts now"}</button>
          </div>
          {err && <p className="notice error">{err}</p>}
          <div className="inc-list">
            {incidents.map((i) => (
              <button key={i.incident_id} className={`inc ${sel === i.incident_id ? "sel" : ""}`} onClick={() => setSel(i.incident_id)}>
                <div className="row" style={{ gap: 6 }}><span className="t">{i.incident_id}</span><Status value={i.status} /><span className="badge">{i.severity}</span></div>
                <div className="m">{i.title}</div>
                <div className="m">{i.affected_region}{i.agent_detected ? ` · agent: ${i.alert_count} alerts → 1` : ""}</div>
              </button>
            ))}
          </div>
        </div>
      </aside>

      <main className="stack">
        {sel ? <IncidentView key={sel} id={sel} actor={actor} onChanged={reload} /> : <div className="card muted">No incident selected.</div>}
        <Forecasts refresh={refresh} />
      </main>

      <div className="agent-col"><AgentPanel incidentId={sel} onDone={reload} /></div>
    </div>
  );
}
