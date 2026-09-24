import { useRef, useState } from "react";
import { HttpAgent } from "@ag-ui/client";

type Item =
  | { kind: "user"; id: string; text: string }
  | { kind: "assistant"; id: string; text: string }
  | { kind: "tool"; id: string; name: string; args: string; result?: string }
  | { kind: "error"; id: string; text: string };

// Which specialist a tool belongs to, so the stream reads as a team of agents.
const OWNER: Record<string, string> = {
  triage_alert_storm: "triage agent", open_incident: "triage agent", find_runbook: "diagnosis agent",
  propose_and_request_approval: "remediation agent", forecast_breaches: "forecast agent",
  business_impact: "impact agent", draft_incident_postmortem: "postmortem agent",
  transfer_to_agent: "commander",
};

const PROMPTS = [
  "Alerts are firing across the estate. Investigate what is happening.",
  "Are any systems likely to breach a threshold in the next 30 minutes?",
];

export default function AgentPanel({ incidentId, onDone }: { incidentId: string | null; onDone: () => void }) {
  const agentRef = useRef<HttpAgent | null>(null);
  if (!agentRef.current) agentRef.current = new HttpAgent({ url: "/agent" });
  const [items, setItems] = useState<Item[]>([]);
  const [input, setInput] = useState("");
  const [running, setRunning] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);

  const scroll = () => requestAnimationFrame(() => logRef.current?.scrollTo({ top: 1e9 }));
  const upsert = (fn: (xs: Item[]) => Item[]) => { setItems(fn); scroll(); };

  async function send(text: string) {
    if (!text.trim() || running) return;
    const agent = agentRef.current!;
    const id = crypto.randomUUID();
    agent.addMessage({ id, role: "user", content: text });
    upsert((xs) => [...xs, { kind: "user", id, text }]);
    setInput("");
    setRunning(true);
    try {
      await agent.runAgent({}, {
        onTextMessageStartEvent: ({ event }) => upsert((xs) => [...xs, { kind: "assistant", id: event.messageId, text: "" }]),
        onTextMessageContentEvent: ({ event }) =>
          upsert((xs) => xs.map((x) => (x.kind === "assistant" && x.id === event.messageId ? { ...x, text: x.text + event.delta } : x))),
        onToolCallStartEvent: ({ event }) =>
          upsert((xs) => [...xs, { kind: "tool", id: event.toolCallId, name: event.toolCallName, args: "" }]),
        onToolCallArgsEvent: ({ event }) =>
          upsert((xs) => xs.map((x) => (x.kind === "tool" && x.id === event.toolCallId ? { ...x, args: x.args + event.delta } : x))),
        onToolCallResultEvent: ({ event }) =>
          upsert((xs) => xs.map((x) => (x.kind === "tool" && x.id === event.toolCallId
            ? { ...x, result: typeof event.content === "string" ? event.content : JSON.stringify(event.content) } : x))),
        onRunErrorEvent: ({ event }) => upsert((xs) => [...xs, { kind: "error", id: crypto.randomUUID(), text: event.message }]),
      });
    } catch (e) {
      upsert((xs) => [...xs, { kind: "error", id: crypto.randomUUID(), text: (e as Error).message }]);
    } finally {
      setRunning(false);
      onDone();
    }
  }

  const prompts = incidentId ? [...PROMPTS, `Write the postmortem for ${incidentId}.`] : PROMPTS;
  return (
    <div className="card agent">
      <h2>Agent · Gemini on ADK</h2>
      <div className="log" ref={logRef} aria-live="polite">
        {items.length === 0 && (
          <div className="small sub">
            The agents triage, diagnose, propose a fix and request approval, forecast and size the impact.
            They cannot approve or execute anything — that stays with you.
          </div>
        )}
        {items.map((it) =>
          it.kind === "user" ? <div key={it.id} className="msg user">{it.text}</div>
          : it.kind === "assistant" ? (it.text ? <div key={it.id} className="msg assistant">{it.text}</div> : null)
          : it.kind === "error" ? <div key={it.id} className="notice error small">{it.text}</div>
          : (
            <details key={it.id} className="tool">
              <summary>{it.result ? "✓" : "…"} {OWNER[it.name] ?? "agent"} → <code>{it.name}</code></summary>
              {it.args && it.args !== "{}" && <pre className="small">{it.args}</pre>}
              {it.result && <pre className="small" style={{ maxHeight: 220, overflow: "auto" }}>{pretty(it.result)}</pre>}
            </details>
          ),
        )}
        {running && <div className="small muted">working…</div>}
      </div>
      <div className="row" style={{ marginTop: 8 }}>
        {prompts.map((p) => <button key={p} className="small" disabled={running} onClick={() => send(p)}>{p}</button>)}
      </div>
      <form onSubmit={(e) => { e.preventDefault(); send(input); }}>
        <input type="text" value={input} onChange={(e) => setInput(e.target.value)} placeholder="Ask the SRE agent…" aria-label="Message the agent" />
        <button className="primary" disabled={running || !input.trim()}>Send</button>
      </form>
    </div>
  );
}

function pretty(s: string) {
  try { return JSON.stringify(JSON.parse(s), null, 2); } catch { return s; }
}
