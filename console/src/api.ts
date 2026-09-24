/* Thin client for the backend REST API. Every response is already redacted server-side. */

export type Json = any; // eslint-disable-line @typescript-eslint/no-explicit-any

async function call(method: string, path: string, body?: unknown): Promise<Json> {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || data.detail || `${res.status} ${res.statusText}`);
  return data;
}

export const api = {
  metrics: () => call("GET", "/api/metrics"),
  incidents: () => call("GET", "/api/incidents"),
  incident: (id: string) => call("GET", `/api/incidents/${id}`),
  timeline: (id: string) => call("GET", `/api/incidents/${id}/timeline`),
  topology: (id?: string) => call("GET", `/api/topology${id ? `?incident_id=${id}` : ""}`),
  forecasts: (explain = false) => call("GET", `/api/forecasts?explain=${explain}`),
  series: (node: string, type: string) =>
    call("GET", `/api/series?node_id=${encodeURIComponent(node)}&alert_type=${encodeURIComponent(type)}`),
  scan: () => call("POST", "/api/triage?persist=true"),
  approve: (id: string, by: string, reason = "") => call("POST", `/api/approvals/${id}/approve`, { by, reason }),
  reject: (id: string, by: string, reason = "") => call("POST", `/api/approvals/${id}/reject`, { by, reason }),
  execute: (id: string, by: string) => call("POST", `/api/approvals/${id}/execute`, { by }),
  resolve: (id: string, by: string) => call("POST", `/api/incidents/${id}/resolve`, { by }),
  postmortem: (id: string) => call("POST", `/api/incidents/${id}/postmortem`, { publish: false }),
  publish: (pm: string, by: string) => call("POST", `/api/postmortems/${pm}/publish`, { by }),
};

export const cad = (n: number | null | undefined) =>
  n == null ? "–" : `CAD ${n.toLocaleString("en-CA", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export const time = (s: string | null | undefined) => (s ? new Date(s).toLocaleTimeString() : "–");

export function parseJson<T = Json>(s: string | null | undefined, fallback: T): T {
  if (!s) return fallback;
  try {
    return JSON.parse(s) as T;
  } catch {
    return fallback;
  }
}
