/** System-health status: always text plus a symbol, never colour alone (WCAG 1.4.1). */
export type Health = "ok" | "degraded" | "down" | "unknown";

const SYMBOL: Record<Health, string> = { ok: "●", degraded: "▲", down: "■", unknown: "○" };

export function StatusBadge({ health, text }: { health: Health; text: string }) {
  return (
    <span className={`status-badge status-${health}`}>
      <span aria-hidden="true" className="status-symbol">
        {SYMBOL[health]}
      </span>{" "}
      {text}
    </span>
  );
}
