import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { RequestSummary } from "../api/requestSummary";
import { versionQuery } from "../api/queries";
import { useApiClient, useDiagnostics } from "../app/context";
import { CopyButton } from "../components/CopyButton";
import { DetailSection } from "../components/DetailSection";
import { DomainBadge } from "../components/DomainBadge";
import { MetadataList } from "../components/MetadataList";
import { PageHeading } from "../components/PageHeading";
import { LoadingState } from "../components/PageState";
import { RequestError } from "../components/RequestError";
import {
  API_STATUS_HEALTH,
  API_STATUS_LABEL,
  describeLive,
  describeReady,
  reachability,
  type Health,
} from "../lib/apiStatus";
import { UI_BUILD, UI_VERSION } from "../lib/buildInfo";
import { humanize } from "../lib/domain";
import { formatClock } from "../lib/time";
import { CHECK_DESCRIPTION } from "./status/checks";
import { ReadinessHistory } from "./status/ReadinessHistory";
import { RecentActivity } from "./status/RecentActivity";
import { useHealth } from "./status/useHealth";

const HEALTH_LABEL: Record<Health, string> = {
  healthy: "Healthy",
  degraded: "Degraded",
  unavailable: "Unavailable",
  unknown: "Unknown",
};

/**
 * System Status (Phase 16D): an operational summary from the API's own health and version endpoints plus this
 * tab's client diagnostics. It never calls Prometheus, Thanos, the collector, Kubernetes or OpenShift.
 */
export function StatusPage() {
  const client = useApiClient();
  const health = useHealth({ refreshOnMount: true });
  const version = useQuery(versionQuery(client));
  const diagnostics = useDiagnostics();
  const live = describeLive(health.liveObs, health.live.error);
  const ready = describeReady(health.readyObs, health.ready.error);
  const overall = API_STATUS_HEALTH[health.status];
  const readyBody = health.ready.data?.value;
  const lastRefresh = health.lastRefresh ? formatClock(new Date(health.lastRefresh)) : null;
  const reach = reachability(health.liveObs, health.readyObs);

  return (
    <>
      <PageHeading title="System Status">
        <p className="page-meta" aria-live="polite">
          {lastRefresh ? <span title={`UTC: ${lastRefresh.utc}`}>Status refreshed {lastRefresh.local}</span> : "Checking…"}
          {" · "}health every 30 s, version every 5 min
        </p>
        <button
          type="button"
          className="button button-quiet button-small"
          onClick={health.refresh}
          disabled={health.live.isFetching || health.ready.isFetching}
        >
          Refresh now
        </button>
      </PageHeading>

      <section className="stat-row" aria-label="Summary">
        <StatTile label="Liveness" live>
          <DomainBadge kind="health" value={live.health} label={tileText(health.liveObs === "ok" ? "Live" : live.text)} />
        </StatTile>
        <StatTile label="Readiness" live>
          <DomainBadge kind="health" value={ready.health} label={tileText(health.readyObs === "ready" ? "Ready" : ready.text)} />
        </StatTile>
        <StatTile label="API build">
          {version.data ? <code>{version.data.value.build}</code> : version.isError ? <span className="muted">Unavailable</span> : <span className="muted">…</span>}
        </StatTile>
        <StatTile label="UI build">
          <code>{UI_BUILD}</code>
        </StatTile>
      </section>

      <div className="detail-grid">
        <DetailSection
          title="Service health"
          description="Liveness means the API process is running. Readiness means it can serve correctly (settings, artifact root and index). They are reported separately."
        >
          <MetadataList
            items={[
              { term: "API liveness", value: <DomainBadge kind="health" value={live.health} label={live.text} />, field: "GET /health/live" },
              { term: "API readiness", value: <DomainBadge kind="health" value={ready.health} label={ready.text} />, field: "GET /health/ready" },
              {
                term: "Overall",
                value: (
                  <span aria-live="polite">
                    <DomainBadge kind="health" value={overall} label={`${API_STATUS_LABEL[health.status]} (${HEALTH_LABEL[overall]})`} />
                  </span>
                ),
              },
              { term: "Dashboard", value: <DomainBadge kind="health" value="healthy" label="Running in this browser tab" /> },
              { term: "Last successful API response", value: <SummaryRef summary={diagnostics.lastSuccess} empty="None yet" /> },
              {
                term: "Last status refresh",
                value: lastRefresh ? <span title={`UTC: ${lastRefresh.utc}`}>{lastRefresh.local}</span> : "Not yet",
              },
            ]}
          />
        </DetailSection>

        <DetailSection
          title="Readiness checks"
          description={
            readyBody
              ? `${String(readyBody.checks.filter((c) => c.status === "pass").length)} of ${String(readyBody.checks.length)} checks pass${
                  health.ready.data?.status === 503 ? " — the API answered HTTP 503 (reachable, not ready)" : ""
                }.`
              : "Every check the API reports, as reported."
          }
        >
          {health.ready.isPending ? (
            <LoadingState label="Checking readiness…" />
          ) : !readyBody ? (
            <RequestError error={health.ready.error} onRetry={health.refresh} />
          ) : (
            <>
              {health.readyObs === "unreachable" || health.readyObs === "failed" ? (
                <p className="notice notice-compact">The latest readiness check did not get an answer; the checks below are from the previous one.</p>
              ) : null}
              <div className="table-wrap">
                <table className="table table-compact">
                  <caption className="visually-hidden-text">Readiness checks</caption>
                  <thead>
                    <tr>
                      <th scope="col">Check</th>
                      <th scope="col">Result</th>
                      <th scope="col">Meaning</th>
                    </tr>
                  </thead>
                  <tbody>
                    {readyBody.checks.map((check) => (
                      <tr key={check.name}>
                        <th scope="row">
                          <code className="nowrap">{check.name}</code>
                        </th>
                        <td>
                          <DomainBadge kind="check" value={check.status} label={check.status === "pass" ? "Pass" : "Fail"} />
                        </td>
                        <td className="muted">{CHECK_DESCRIPTION[check.name] ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </DetailSection>

        <DetailSection title="Connectivity" description="As observed by this browser tab only.">
          <MetadataList
            items={[
              {
                term: "API reachable",
                value:
                  reach === "reachable" ? (
                    <DomainBadge kind="health" value="healthy" label="Reachable" />
                  ) : reach === "unreachable" ? (
                    <DomainBadge kind="health" value="unavailable" label="Unreachable" />
                  ) : (
                    <DomainBadge kind="health" value="unknown" label="Unknown" />
                  ),
              },
              {
                term: "Retrying now",
                value:
                  diagnostics.retrying.length === 0 ? (
                    <span className="muted">No</span>
                  ) : (
                    <ul className="inline-list">
                      {diagnostics.retrying.map((r) => (
                        <li key={r.route}>
                          <code>{r.route}</code> after attempt {r.attempt}, waiting {Math.round(r.delayMs / 1000)} s
                        </li>
                      ))}
                    </ul>
                  ),
              },
              { term: "Last failure", value: <SummaryRef summary={diagnostics.lastFailure} empty="None" /> },
              { term: "Last network error", value: <SummaryRef summary={diagnostics.lastNetworkError} empty="None" /> },
              { term: "Last timeout", value: <SummaryRef summary={diagnostics.lastTimeout} empty="None" /> },
              { term: "Last server error", value: <SummaryRef summary={diagnostics.lastServerError} empty="None" /> },
            ]}
          />
        </DetailSection>

        <DetailSection title="Builds & runtime">
          <MetadataList
            columns={2}
            items={[
              { term: "API service", value: version.data ? version.data.value.service : "…", field: "service" },
              { term: "API version", value: version.data ? <code>{version.data.value.api_version}</code> : "…", field: "api_version" },
              { term: "API build", value: version.data ? <code>{version.data.value.build}</code> : "…", field: "build" },
              { term: "UI build", value: <code>{UI_BUILD}</code> },
              { term: "UI package version", value: <code>{UI_VERSION}</code> },
            ]}
          />
          {version.isError && !version.data ? (
            <RequestError error={version.error} onRetry={() => void version.refetch()} />
          ) : version.data ? (
            <div className="table-wrap">
              <table className="table table-compact">
                <caption>Analytical formats</caption>
                <thead>
                  <tr>
                    <th scope="col">Object</th>
                    <th scope="col">Format version</th>
                    <th scope="col">Rules version</th>
                  </tr>
                </thead>
                <tbody>
                  {version.data.value.analytical_formats.map((f) => (
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
            </div>
          ) : (
            <LoadingState label="Loading version…" />
          )}
        </DetailSection>
      </div>

      <div className="stack">
        <DetailSection title="Readiness history" description="One observation per readiness check made by this tab (every 30 s while visible). Memory only; resets on reload.">
          <ReadinessHistory />
        </DetailSection>
        <DetailSection title="Recent API activity">
          <RecentActivity />
        </DetailSection>
        <DetailSection title="Operational guidance">
          <div className="prose">
            <p>
              Deep metrics and telemetry are available in <strong>OpenShift monitoring</strong>, not in this dashboard.
              mias-api exports OpenTelemetry metrics and traces through the in-namespace collector to User Workload
              Monitoring, and writes structured JSON logs.
            </p>
            <ul>
              <li>
                <strong>Request ids</strong> are the troubleshooting handle: every request from this tab sends an{" "}
                <code>X-Request-ID</code> (<code>ui-…</code>) that the API echoes and writes into its JSON log line as{" "}
                <code>request_id</code>, next to the trace and span ids. Copy an id above and search the mias-api logs for it.
              </li>
              <li>
                Request latency and status by route: <code>http_server_request_duration_seconds</code>; artifact index
                health: <code>mias_artifact_index_*</code> (OpenShift console → Observe → Metrics, namespace <code>mias</code>).
              </li>
              <li>This page shows only what the API's health and version endpoints report and what this tab observed. It does not
                query Prometheus, the collector, Kubernetes or OpenShift.</li>
            </ul>
          </div>
        </DetailSection>
      </div>
    </>
  );
}

function tileText(text: string): string {
  return text.replace(/^API /, "");
}

function StatTile({ label, live = false, children }: { label: string; live?: boolean; children: ReactNode }) {
  return (
    <div className="stat-tile">
      <span className="stat-label">{label}</span>
      <span className="stat-value" {...(live ? { "aria-live": "polite" as const } : {})}>
        {children}
      </span>
    </div>
  );
}

/** One recorded request: when, what, outcome and its request id (copyable). */
function SummaryRef({ summary, empty }: { summary: RequestSummary | null; empty: string }) {
  if (!summary) return <span className="muted">{empty}</span>;
  const t = formatClock(new Date(summary.startedAt));
  return (
    <span className="summary-ref">
      <span title={`UTC: ${t.utc}`}>{t.local}</span>
      <code>
        {summary.method} {summary.route}
      </code>
      <span className="muted">
        {summary.status ?? humanize(summary.outcome)}
      </span>
      {summary.requestId ? (
        <span className="artifact-id-wrap">
          <code>{summary.requestId}</code>
          <CopyButton text={summary.requestId} label={`Copy request id ${summary.requestId}`} compact />
        </span>
      ) : null}
    </span>
  );
}
