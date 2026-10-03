import { useState } from "react";
import type { RequestSummary } from "../../api/requestSummary";
import { useDiagnostics, useServices } from "../../app/context";
import { isFailure, MAX_REQUESTS } from "../../app/diagnostics";
import { CopyButton } from "../../components/CopyButton";
import { DomainBadge } from "../../components/DomainBadge";
import { humanize } from "../../lib/domain";
import { formatClock } from "../../lib/time";

const NOTE_LABEL: Record<NonNullable<RequestSummary["note"]>, string> = {
  not_ready: "Reported not ready",
  capability_unavailable: "Capability unavailable",
};

export function noteFor(summary: RequestSummary): string | null {
  if (summary.note) return NOTE_LABEL[summary.note];
  if (summary.outcome === "success" && summary.attempts > 1) return "Recovered after retry";
  if (summary.attempts > 1) return `${String(summary.attempts)} attempts`;
  return null;
}

/** The bounded in-memory request log: one row per logical request (retries folded in), newest first. */
export function RecentActivity() {
  const diagnostics = useDiagnostics();
  const { diagnostics: store } = useServices();
  const [problemsOnly, setProblemsOnly] = useState(false);
  const rows = problemsOnly ? diagnostics.requests.filter(isFailure) : diagnostics.requests;

  return (
    <>
      <div className="toolbar">
        <p className="hint toolbar-text">
          The last {MAX_REQUESTS} API requests made by this tab ({diagnostics.total} in total). Kept in memory only; a
          reload starts empty. No headers, tokens, query strings or bodies are recorded.
        </p>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={problemsOnly}
            onChange={(e) => {
              setProblemsOnly(e.target.checked);
            }}
          />
          Problems only
        </label>
        <button
          type="button"
          className="button button-quiet button-small"
          onClick={store.clearRequests}
          disabled={diagnostics.requests.length === 0}
        >
          Clear list
        </button>
      </div>
      {rows.length === 0 ? (
        <p className="state state-empty-inline">
          {diagnostics.requests.length === 0 ? "No API requests recorded in this tab yet." : "No problems among the recent requests."}
        </p>
      ) : (
        <div className="table-wrap">
          <table className="table table-data table-activity">
            <caption className="visually-hidden-text">Recent API requests, newest first</caption>
            <thead>
              <tr>
                <th scope="col">Time</th>
                <th scope="col">Request</th>
                <th scope="col">Status</th>
                <th scope="col">Outcome</th>
                <th scope="col" className="num">
                  Duration
                </th>
                <th scope="col">Note</th>
                <th scope="col">Request id</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => {
                const t = formatClock(new Date(r.startedAt));
                const note = noteFor(r);
                return (
                  <tr key={`${String(r.startedAt)}-${r.requestId ?? "none"}-${String(i)}`}>
                    <td data-label="Time">
                      <time dateTime={t.utc} title={`UTC: ${t.utc}`} className="timestamp">
                        {t.local}
                      </time>
                    </td>
                    <th scope="row" data-label="Request">
                      <code>
                        {r.method} {r.route}
                      </code>
                    </th>
                    <td data-label="Status" className="num">
                      {r.status ?? <span className="muted">—</span>}
                    </td>
                    <td data-label="Outcome">
                      <DomainBadge kind="outcome" value={r.outcome} label={humanize(r.outcome)} />
                    </td>
                    <td data-label="Duration" className="num">
                      {r.durationMs} ms
                    </td>
                    <td data-label="Note">{note ?? <span className="muted">—</span>}</td>
                    <td data-label="Request id">
                      {r.requestId ? (
                        <span className="artifact-id-wrap">
                          <code>{r.requestId}</code>
                          <CopyButton text={r.requestId} label={`Copy request id ${r.requestId}`} compact />
                        </span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
