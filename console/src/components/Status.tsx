/* Status is never color alone: every state ships an icon and a label. */
const MAP: Record<string, { color: string; icon: string; label?: string }> = {
  INVESTIGATING: { color: "var(--critical)", icon: "●" },
  MONITORING: { color: "var(--warning)", icon: "◐" },
  RESOLVED: { color: "var(--good)", icon: "✓" },
  PENDING: { color: "var(--warning)", icon: "⏳" },
  APPROVED: { color: "var(--good)", icon: "✓" },
  REJECTED: { color: "var(--critical)", icon: "✕" },
  SUCCESS: { color: "var(--good)", icon: "✓" },
  FAILED: { color: "var(--critical)", icon: "✕" },
  ROLLED_BACK: { color: "var(--serious)", icon: "↺", label: "ROLLED BACK" },
  WARN: { color: "var(--serious)", icon: "▲" },
  BREACHED: { color: "var(--critical)", icon: "●" },
  STALE: { color: "var(--warning)", icon: "?" },
  WATCH: { color: "var(--muted)", icon: "○" },
  HIGH: { color: "var(--critical)", icon: "▲", label: "HIGH RISK" },
  MEDIUM: { color: "var(--serious)", icon: "▲", label: "MEDIUM RISK" },
  LOW: { color: "var(--good)", icon: "●", label: "LOW RISK" },
  root: { color: "var(--critical)", icon: "●", label: "ROOT CAUSE" },
  symptom: { color: "var(--serious)", icon: "▲", label: "symptom" },
};

export default function Status({ value }: { value: string | null | undefined }) {
  if (!value) return null;
  const s = MAP[value] ?? { color: "var(--muted)", icon: "○" };
  return (
    <span className="status">
      <span aria-hidden style={{ color: s.color }}>{s.icon}</span>
      {s.label ?? value}
    </span>
  );
}
