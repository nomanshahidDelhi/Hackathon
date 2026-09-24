import { useCallback, useEffect, useState } from "react";
import { api, cad, parseJson, time, type Json } from "../api";
import Status from "./Status";
import Timeline from "./Timeline";
import Topology from "./Topology";

type Tab = "overview" | "remediation" | "impact" | "postmortem";

export default function IncidentView({ id, actor, onChanged }: { id: string; actor: string; onChanged: () => void }) {
  const [d, setD] = useState<Json | null>(null);
  const [timeline, setTimeline] = useState<Json[]>([]);
  const [tab, setTab] = useState<Tab>("overview");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setErr(null);
    api.incident(id).then(setD).catch((e) => setErr(e.message));
    api.timeline(id).then(setTimeline).catch(() => setTimeline([]));
  }, [id]);
  useEffect(() => { setD(null); load(); }, [load]);

  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    setErr(null);
    try {
      await fn();
      load();
      onChanged();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const needActor = !actor.trim();

  if (!d) return <div className="card">{err ? <p className="notice error">{err}</p> : <p className="muted">Loading {id}…</p>}</div>;
  const inc = d.incident, tr = d.triage;

  return (
    <div className="card">
      <div className="row" style={{ marginBottom: 8 }}>
        <strong style={{ fontSize: 16 }}>{inc.incident_id}</strong>
        <Status value={inc.status} />
        <span className="badge">{inc.severity}</span>
        <span className="badge">{inc.affected_region}</span>
        {inc.customer_tier_impacted && <span className="badge">highest tier {inc.customer_tier_impacted}</span>}
        {tr && <span className="badge">agent-detected</span>}
      </div>
      <div className="sub" style={{ marginBottom: 12 }}>{inc.title}</div>
      <div className="tabs" role="tablist">
        {(["overview", "remediation", "impact", "postmortem"] as Tab[]).map((t) => (
          <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)} role="tab" aria-selected={tab === t}>
            {t[0].toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>
      {err && <p className="notice error">{err}</p>}
      {needActor && tab !== "overview" && tab !== "impact" && (
        <p className="notice">Enter your name in “Acting as” (top right) to approve, execute, resolve or publish.</p>
      )}

      {tab === "overview" && (
        <div className="stack">
          {tr ? (
            <>
              <div className="hero">
                <span className="big">{d.correlated_alerts}</span>
                <span className="sub">alerts → 1 incident · {tr.signatures.length} signatures on {tr.nodes.length} nodes · confidence {Math.round(tr.confidence * 100)}%</span>
              </div>
              <div className="grid2">
                <div>
                  <h3><Status value="root" /></h3>
                  <div><strong>{tr.root_cause.alert_type}</strong> on <strong>{tr.root_cause.node_name}</strong> ({tr.root_cause.service_name}, {tr.root_cause.node_type})</div>
                  <div className="sub small" style={{ marginTop: 6 }}>{tr.root_cause.sample_message}</div>
                  <div className="small muted" style={{ marginTop: 6 }}>first seen {time(tr.root_cause.first_at)}</div>
                </div>
                <div>
                  <h3><Status value="symptom" /> services</h3>
                  <div className="row">{tr.symptom_services.map((s: string) => <span key={s} className="badge">{s}</span>)}</div>
                  <div className="small sub" style={{ marginTop: 6 }}>These nodes blamed an upstream dependency in their own messages and started later.</div>
                </div>
              </div>
              <div>
                <h3>Why this node — root-cause ranking</h3>
                <div className="table-wrap"><table>
                  <thead><tr><th>Node</th><th>Signal</th><th className="num">Score</th><th className="num">Onset</th><th className="num">Dependents</th><th className="num">Depth</th><th className="num">Saturation</th><th className="num">Blames upstream</th></tr></thead>
                  <tbody>{tr.candidates.map((c: Json) => (
                    <tr key={c.node_id}><td>{c.node_name}</td><td>{c.alert_type}</td><td className="num">{c.score.toFixed(3)}</td>
                      {["onset", "coverage", "depth", "saturation", "blames_upstream"].map((k) => <td key={k} className="num">{c.components[k]}</td>)}</tr>
                  ))}</tbody>
                </table></div>
              </div>
              <div><h3>Alert storm timeline</h3><Timeline rows={timeline} /></div>
            </>
          ) : (
            <p className="muted">Historical incident from the seeded mart (not detected by the agent).</p>
          )}
          <div><h3>Topology</h3><Topology incidentId={tr ? id : null} /></div>
        </div>
      )}

      {tab === "remediation" && (
        <div className="stack">
          {d.approvals.length === 0 && <p className="muted">No remediation proposed yet. Ask the agent to investigate.</p>}
          {d.approvals.map((a: Json) => {
            const g = parseJson<Json>(a.guardrail_report, { findings: [] });
            const sb = parseJson<Json>(a.sandbox_report, null);
            const plan = parseJson<Json>(a.plan_json, {});
            return (
              <div key={a.approval_id} className="card" style={{ background: "var(--surface-2)" }}>
                <div className="row">
                  <strong>{a.approval_id}</strong><Status value={a.status} /><Status value={a.risk_level} />
                  <span className="badge">runbook {a.runbook_id}</span>
                  {a.decided_by && <span className="small sub">by {a.decided_by} at {time(a.decided_at)}</span>}
                  {a.execution_id && <span className="badge">executed as {a.execution_id}</span>}
                </div>
                {plan.selection?.explanation && <p className="small sub">{plan.selection.explanation}</p>}
                {plan.rationale && <p className="small">{plan.rationale}</p>}
                <div className="grid2">
                  <div><h3>Fix</h3><pre className="code">{a.script}</pre></div>
                  <div><h3>Rollback</h3><pre className="code">{a.rollback_script}</pre></div>
                </div>
                <h3>Guardrails & sandbox</h3>
                {g.findings.filter((f: Json) => f.severity !== "MEDIUM").map((f: Json, i: number) => (
                  <div key={i} className="finding"><Status value={f.severity === "WARN" ? "STALE" : f.severity === "HIGH" ? "HIGH" : f.severity} /> {f.rule}: {f.message}</div>
                ))}
                {sb && <div className="finding">Sandbox: {sb.runs?.length ?? 0} dry runs, idempotent {String(sb.idempotent)}, rollback {sb.rollback?.exit_code === 0 ? "ok" : "failed"}</div>}
                <div className="row" style={{ marginTop: 10 }}>
                  {a.status === "PENDING" && (<>
                    <button className="good" disabled={busy || needActor} onClick={() => act(() => api.approve(a.approval_id, actor))}>Approve as {actor || "…"}</button>
                    <button className="danger" disabled={busy || needActor} onClick={() => act(() => api.reject(a.approval_id, actor, "rejected in console"))}>Reject</button>
                  </>)}
                  {a.status === "APPROVED" && !a.execution_id && (
                    <button className="primary" disabled={busy || needActor} onClick={() => act(() => api.execute(a.approval_id, actor))}>Execute (sandbox target)</button>
                  )}
                </div>
              </div>
            );
          })}
          {d.remediations.length > 0 && (
            <div className="table-wrap"><h3>Remediation log</h3><table>
              <thead><tr><th>Execution</th><th>Runbook</th><th>Status</th><th>HITL approved</th><th>By</th><th>Finished</th><th>Error</th></tr></thead>
              <tbody>{d.remediations.map((r: Json) => (
                <tr key={r.execution_id + r.started_at}><td>{r.execution_id}</td><td>{r.runbook_id}</td><td><Status value={r.status} /></td>
                  <td>{r.hitl_approved ? "yes" : "no"}</td><td>{r.executed_by}</td><td>{time(r.finished_at)}</td><td className="small">{r.error_output ?? ""}</td></tr>
              ))}</tbody>
            </table></div>
          )}
        </div>
      )}

      {tab === "impact" && <ImpactPanel imp={d.impact} />}

      {tab === "postmortem" && (
        <div className="stack">
          <div className="row">
            <button disabled={busy} onClick={() => act(() => api.postmortem(id))}>Draft postmortem</button>
            {inc.status !== "RESOLVED" && tr && (
              <button disabled={busy || needActor} onClick={() => act(() => api.resolve(id, actor))}>Mark incident resolved</button>
            )}
            <span className="small sub">Publishing requires the incident to be RESOLVED.</span>
          </div>
          {d.postmortems.length === 0 && <p className="muted">No postmortem yet.</p>}
          {d.postmortems.map((pm: Json) => (
            <div key={pm.postmortem_id} className="card" style={{ background: "var(--surface-2)" }}>
              <div className="row">
                <strong>{pm.postmortem_id}</strong>
                {pm.published_at ? <Status value="RESOLVED" /> : <span className="badge">DRAFT</span>}
                {pm.published_at && <span className="small sub">published {time(pm.published_at)}</span>}
                <span className="small sub">downtime {Number(pm.downtime_minutes).toFixed(1)} min</span>
                {!pm.published_at && inc.status === "RESOLVED" && (
                  <button className="primary" disabled={busy || needActor} onClick={() => act(() => api.publish(pm.postmortem_id, actor))}>Publish</button>
                )}
              </div>
              <h3>Root cause</h3><p>{pm.root_cause}</p>
              <h3>Impact</h3><p className="small">{pm.impact_summary}</p>
              <h3>Contributing factors</h3><ul>{String(pm.contributing_factors || "").split("\n").filter(Boolean).map((f, i) => <li key={i}>{f}</li>)}</ul>
              <h3>Action items</h3><ul>{String(pm.action_items || "").split("\n").filter(Boolean).map((f, i) => <li key={i}>{f}</li>)}</ul>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ImpactPanel({ imp }: { imp: Json }) {
  if (!imp.customers?.length) return <p className="muted">No customer accounts are mapped to the affected services in the affected regions.</p>;
  const maxCredit = Math.max(...imp.tiers.map((t: Json) => t.sla_credit_cad), 1);
  return (
    <div className="stack">
      <div className="kpis" style={{ gridColumn: "auto" }}>
        <div className="kpi"><div className="label">Revenue at risk (MRR)</div><div className="value">{cad(imp.revenue_at_risk_cad)}</div><div className="hint">{imp.customers.length} accounts</div></div>
        <div className="kpi"><div className="label">SLA credits owed</div><div className="value">{cad(imp.sla_credit_cad)}</div><div className="hint">breached: {imp.breached_tiers.join(", ") || "none"}</div></div>
        <div className="kpi"><div className="label">Customer-impacting downtime</div><div className="value">{imp.downtime_minutes.toFixed(1)} min</div><div className="hint">{imp.ongoing ? "ongoing" : "window closed"}</div></div>
        <div className="kpi"><div className="label">Prorated revenue in window</div><div className="value">{cad(imp.prorated_revenue_cad)}</div><div className="hint">{imp.services.join(", ")}</div></div>
      </div>
      <div className="table-wrap"><h3>SLA credits by tier</h3><table>
        <thead><tr><th>Tier</th><th className="num">Accounts</th><th className="num">MRR</th><th>Credits</th><th className="num">Rate</th><th className="num">Target</th><th>Breached</th></tr></thead>
        <tbody>{imp.tiers.map((t: Json) => (
          <tr key={t.tier}><td>{t.tier}</td><td className="num">{t.accounts}</td><td className="num">{cad(t.mrr_cad)}</td>
            <td><div className="row" style={{ gap: 6, flexWrap: "nowrap" }}>
              <svg width="120" height="10" aria-hidden><rect x="0" y="1" height="8" rx="2" width={Math.max(2, (t.sla_credit_cad / maxCredit) * 120)} fill="var(--series-1)" /></svg>
              <span style={{ fontVariantNumeric: "tabular-nums" }}>{cad(t.sla_credit_cad)}</span></div></td>
            <td className="num">{t.credit_rate}×</td><td className="num">{t.restoration_target_minutes} min</td>
            <td>{t.breached_accounts ? <Status value="BREACHED" /> : "no"}</td></tr>
        ))}</tbody>
      </table></div>
      <div className="table-wrap"><h3>Affected accounts</h3><table>
        <thead><tr><th>Account</th><th>Tier</th><th>Service</th><th>Region</th><th className="num">MRR</th><th className="num">SLA credit</th><th>SLA</th></tr></thead>
        <tbody>{imp.customers.map((c: Json) => (
          <tr key={c.customer_id + c.service_name}><td>{c.customer_name}</td><td>{c.tier}</td><td>{c.service_name}</td><td>{c.region}</td>
            <td className="num">{cad(c.mrr_cad)}</td><td className="num">{cad(c.sla_credit_cad)}</td><td>{c.sla_breached ? <Status value="BREACHED" /> : "within target"}</td></tr>
        ))}</tbody>
      </table></div>
      <p className="small muted">{imp.basis}. Credit rates are configured policy (sre_agent_ops.sla_policy), not contract values.</p>
    </div>
  );
}
