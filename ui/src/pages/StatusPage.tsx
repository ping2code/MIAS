import { useQuery } from "@tanstack/react-query";
import { liveQuery, readyQuery, versionQuery } from "../api/queries";
import { useApiClient, useDiagnostics } from "../app/context";
import { PageHeading } from "../components/PageHeading";
import { ErrorPanel, LoadingState, QueryRegion } from "../components/PageState";
import { StatusBadge } from "../components/StatusBadge";
import { UI_BUILD } from "../lib/buildInfo";
import { formatClock } from "../lib/time";

/**
 * A lightweight status summary from the API's own endpoints only. Deep metrics stay in OpenShift monitoring; the UI
 * never calls Prometheus, the collector or Kubernetes.
 */
export function StatusPage() {
  const client = useApiClient();
  const live = useQuery(liveQuery(client));
  const ready = useQuery(readyQuery(client));
  const version = useQuery(versionQuery(client));
  const diagnostics = useDiagnostics();
  const lastSuccess = diagnostics.lastSuccessAt ? formatClock(diagnostics.lastSuccessAt) : null;
  const lastError = diagnostics.lastErrorAt ? formatClock(diagnostics.lastErrorAt) : null;

  return (
    <>
      <PageHeading title="System Status" />
      <div className="grid">
        <section className="card" aria-labelledby="st-live">
          <h2 id="st-live">Liveness</h2>
          {live.isPending ? (
            <LoadingState />
          ) : live.isError ? (
            <ErrorPanel error={live.error} onRetry={() => void live.refetch()} />
          ) : (
            <StatusBadge health="ok" text="Live" />
          )}
        </section>

        <section className="card" aria-labelledby="st-ready">
          <h2 id="st-ready">Readiness</h2>
          <QueryRegion query={ready}>
            {(result) => (
              <>
                {result.value.status === "ready" ? (
                  <StatusBadge health="ok" text="Ready" />
                ) : (
                  <StatusBadge health="degraded" text="Not ready (HTTP 503)" />
                )}
                <table className="table">
                  <thead>
                    <tr>
                      <th scope="col">Check</th>
                      <th scope="col">Result</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.value.checks.map((check) => (
                      <tr key={check.name}>
                        <th scope="row">
                          <code>{check.name}</code>
                        </th>
                        <td>
                          <StatusBadge health={check.status === "pass" ? "ok" : "down"} text={check.status} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </QueryRegion>
        </section>

        <section className="card" aria-labelledby="st-version">
          <h2 id="st-version">Version</h2>
          <QueryRegion query={version}>
            {(result) => (
              <>
                <dl className="kv">
                  <dt>Service</dt>
                  <dd>{result.value.service}</dd>
                  <dt>API version</dt>
                  <dd>
                    <code>{result.value.api_version}</code>
                  </dd>
                  <dt>API build</dt>
                  <dd>
                    <code>{result.value.build}</code>
                  </dd>
                </dl>
                <table className="table">
                  <caption>Analytical formats</caption>
                  <thead>
                    <tr>
                      <th scope="col">Object</th>
                      <th scope="col">Format version</th>
                      <th scope="col">Rules version</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.value.analytical_formats.map((f) => (
                      <tr key={`${f.object}:${f.format_version}`}>
                        <th scope="row">{f.object}</th>
                        <td>
                          <code>{f.format_version}</code>
                        </td>
                        <td>
                          <code>{f.rules_version}</code>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </QueryRegion>
        </section>

        <section className="card" aria-labelledby="st-ui">
          <h2 id="st-ui">Dashboard</h2>
          <dl className="kv">
            <dt>UI build</dt>
            <dd>
              <code>{UI_BUILD}</code>
            </dd>
            <dt>Last successful fetch</dt>
            <dd>{lastSuccess ? <span title={`UTC: ${lastSuccess.utc}`}>{lastSuccess.local}</span> : "None yet"}</dd>
            <dt>Last error</dt>
            <dd>
              {lastError ? (
                <>
                  <span title={`UTC: ${lastError.utc}`}>{lastError.local}</span> · {diagnostics.lastErrorSummary}
                </>
              ) : (
                "None"
              )}
            </dd>
            <dt>Last error request id</dt>
            <dd>{diagnostics.lastErrorRequestId ? <code>{diagnostics.lastErrorRequestId}</code> : "—"}</dd>
          </dl>
          <p className="hint">Latency, index and collector metrics are in OpenShift monitoring, not in this dashboard.</p>
        </section>
      </div>
    </>
  );
}
