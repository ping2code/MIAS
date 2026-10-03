import { useDiagnostics } from "../../app/context";
import { MAX_READINESS, type ReadinessResult } from "../../app/diagnostics";
import { DomainBadge } from "../../components/DomainBadge";
import { formatClock } from "../../lib/time";

const RESULT: Record<ReadinessResult, { label: string; health: string; symbol: string }> = {
  ready: { label: "Ready", health: "healthy", symbol: "●" },
  not_ready: { label: "Not ready", health: "degraded", symbol: "▲" },
  unreachable: { label: "Unreachable", health: "unavailable", symbol: "■" },
  failed: { label: "Check failed", health: "unavailable", symbol: "■" },
};

export function resultLabel(result: ReadinessResult): string {
  return RESULT[result].label;
}

/**
 * The readiness observations made by this tab (one per readiness query), newest first, at most MAX_READINESS.
 * A compact strip (text in every cell, not colour alone) plus a list. Not a metric: it resets on reload.
 */
export function ReadinessHistory() {
  const { readiness } = useDiagnostics();
  if (readiness.length === 0) {
    return <p className="state state-empty-inline">No readiness observations yet in this tab.</p>;
  }
  const oldestFirst = [...readiness].reverse();
  return (
    <>
      <ol className="history-strip" aria-label={`Readiness, oldest to newest (last ${String(readiness.length)} of up to ${String(MAX_READINESS)})`}>
        {oldestFirst.map((s, i) => {
          const r = RESULT[s.result];
          const t = formatClock(new Date(s.at));
          return (
            <li key={`${String(s.at)}-${String(i)}`} className={`history-cell history-${r.health}`} title={`${t.local} — ${r.label}`}>
              <span aria-hidden="true">{r.symbol}</span>
              <span className="visually-hidden-text">
                {t.local}: {r.label}
              </span>
            </li>
          );
        })}
      </ol>
      <div className="table-wrap">
        <table className="table table-compact">
          <caption className="visually-hidden-text">Readiness observations, newest first</caption>
          <thead>
            <tr>
              <th scope="col">Observed</th>
              <th scope="col">Result</th>
              <th scope="col" className="num">
                HTTP
              </th>
              <th scope="col">Failing checks</th>
            </tr>
          </thead>
          <tbody>
            {readiness.map((s, i) => {
              const t = formatClock(new Date(s.at));
              const failing = s.checks.filter((c) => c.status === "fail").map((c) => c.name);
              return (
                <tr key={`${String(s.at)}-${String(i)}`}>
                  <td>
                    <time dateTime={t.utc} title={`UTC: ${t.utc}`} className="timestamp">
                      {t.local}
                    </time>
                  </td>
                  <td>
                    <DomainBadge kind="health" value={RESULT[s.result].health} label={RESULT[s.result].label} />
                  </td>
                  <td className="num">{s.status ?? <span className="muted">—</span>}</td>
                  <td>
                    {s.checks.length === 0 ? (
                      <span className="muted">No response body</span>
                    ) : failing.length === 0 ? (
                      <span className="muted">None</span>
                    ) : (
                      failing.map((name) => (
                        <code key={name} className="check-name">
                          {name}
                        </code>
                      ))
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
