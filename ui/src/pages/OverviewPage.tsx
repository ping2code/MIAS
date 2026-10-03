import { useQueries, useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { historyHeadQuery, latestQuery, liveQuery, readyQuery, versionQuery } from "../api/queries";
import { ARTIFACT_FAMILIES, ID_FIELD, type AlertView, type ArtifactFamily, type MarketIntelligenceView } from "../api/types";
import { useApiClient } from "../app/context";
import { PageHeading } from "../components/PageHeading";
import { EmptyState, ErrorPanel, LoadingState, QueryRegion } from "../components/PageState";
import { StatusBadge } from "../components/StatusBadge";
import { Timestamp } from "../components/Timestamp";
import { ArtifactId } from "../components/ArtifactId";
import { UI_BUILD } from "../lib/buildInfo";
import { formatClock } from "../lib/time";

export const FAMILY_LABEL: Record<ArtifactFamily, string> = {
  "market-intelligence": "Market Intelligence",
  "options-intelligence": "Options Intelligence",
  "trade-setups": "Trade Setups",
  "invalidation-checks": "Invalidation Checks",
  alerts: "Alerts",
};

/**
 * Overview: only facts the API states. No totals (there is no count endpoint) and no derived judgments.
 * "Latest" needs a symbol and there is no symbols endpoint, so the symbol is taken from the newest history entry and
 * labelled as such; with no history the widget says so instead of guessing.
 */
export function OverviewPage() {
  const client = useApiClient();
  const live = useQuery(liveQuery(client));
  const ready = useQuery(readyQuery(client));
  const version = useQuery(versionQuery(client));
  const heads = useQueries({ queries: ARTIFACT_FAMILIES.map((family) => historyHeadQuery(client, family)) });
  const updated = [live, ready, version, ...heads].map((q) => q.dataUpdatedAt).filter((t) => t > 0);
  const lastRefreshed = updated.length > 0 ? formatClock(new Date(Math.max(...updated))) : null;

  return (
    <>
      <PageHeading title="Overview">
        <p className="page-meta" aria-live="polite">
          {lastRefreshed ? (
            <span title={`UTC: ${lastRefreshed.utc}`}>Last refreshed {lastRefreshed.local}</span>
          ) : (
            "Not refreshed yet"
          )}
        </p>
      </PageHeading>

      <div className="grid">
        <section className="card" aria-labelledby="ov-api">
          <h2 id="ov-api">MIAS API</h2>
          <dl className="kv">
            <dt>Live</dt>
            <dd>
              {live.isPending ? "Checking…" : live.isError ? <StatusBadge health="down" text="Unreachable" /> : <StatusBadge health="ok" text="Live" />}
            </dd>
            <dt>Ready</dt>
            <dd>
              {ready.isPending ? (
                "Checking…"
              ) : ready.data ? (
                ready.data.value.status === "ready" ? (
                  <StatusBadge health="ok" text="Ready" />
                ) : (
                  <StatusBadge health="degraded" text="Not ready" />
                )
              ) : (
                <StatusBadge health="down" text="Unavailable" />
              )}
            </dd>
            <dt>API build</dt>
            <dd>{version.data ? <code>{version.data.value.build}</code> : version.isError ? "Unavailable" : "…"}</dd>
            <dt>API version</dt>
            <dd>{version.data ? <code>{version.data.value.api_version}</code> : version.isError ? "Unavailable" : "…"}</dd>
            <dt>UI build</dt>
            <dd>
              <code>{UI_BUILD}</code>
            </dd>
          </dl>
        </section>

        <LatestCard
          family="market-intelligence"
          headSymbol={symbolOf(heads[0]?.data?.value.data[0])}
          headPending={heads[0]?.isPending ?? true}
          headError={heads[0]?.error ?? null}
          retryHead={() => void heads[0]?.refetch()}
        />
        <LatestCard
          family="alerts"
          headSymbol={symbolOf(heads[4]?.data?.value.data[0])}
          headPending={heads[4]?.isPending ?? true}
          headError={heads[4]?.error ?? null}
          retryHead={() => void heads[4]?.refetch()}
        />
      </div>

      <section className="card" aria-labelledby="ov-kinds">
        <h2 id="ov-kinds">Artifacts by kind</h2>
        <p className="hint">From the first history entry of each kind. Totals are not shown: the API has no count endpoint.</p>
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Kind</th>
                <th scope="col">Has data</th>
                <th scope="col">Newest as of</th>
              </tr>
            </thead>
            <tbody>
              {ARTIFACT_FAMILIES.map((family, index) => {
                const q = heads[index];
                const first = q?.data?.value.data[0];
                return (
                  <tr key={family}>
                    <th scope="row">
                      <Link to={`/${family}`}>{FAMILY_LABEL[family]}</Link>
                    </th>
                    <td>{!q || q.isPending ? "Loading…" : q.data ? (first ? "Yes" : "None yet") : "Unavailable"}</td>
                    <td>
                      {first ? <Timestamp iso={first.as_of} /> : q?.isError && !q.data ? <ErrorPanel error={q.error} onRetry={() => void q.refetch()} /> : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}

function symbolOf(item: { symbol: string } | undefined): string | null {
  return item ? item.symbol : null;
}

function LatestCard({
  family,
  headSymbol,
  headPending,
  headError,
  retryHead,
}: {
  family: "market-intelligence" | "alerts";
  headSymbol: string | null;
  headPending: boolean;
  headError: unknown;
  retryHead: () => void;
}) {
  const client = useApiClient();
  const latest = useQuery({ ...latestQuery(client, family, headSymbol ?? ""), enabled: headSymbol !== null });
  const title = family === "alerts" ? "Latest Alert" : "Latest Market Intelligence";
  const headingId = `ov-latest-${family}`;
  let body: React.ReactNode;
  if (headPending) body = <LoadingState />;
  else if (headError && headSymbol === null) body = <ErrorPanel error={headError} onRetry={retryHead} />;
  else if (headSymbol === null)
    body = <EmptyState message={family === "alerts" ? "No alerts are available yet." : "No market intelligence is available yet."} />;
  else
    body = (
      <>
        <p className="hint">
          Symbol <code>{headSymbol}</code>, taken from the newest history entry.
        </p>
        <QueryRegion query={latest}>
          {(result) => {
            const view = result.value.data;
            return family === "alerts" ? (
              <AlertSummary view={view as AlertView} />
            ) : (
              <MarketIntelligenceSummary view={view as MarketIntelligenceView} />
            );
          }}
        </QueryRegion>
      </>
    );
  return (
    <section className="card" aria-labelledby={headingId}>
      <h2 id={headingId}>{title}</h2>
      {body}
    </section>
  );
}

function MarketIntelligenceSummary({ view }: { view: MarketIntelligenceView }) {
  return (
    <dl className="kv">
      <dt>Symbol</dt>
      <dd>{view.symbol}</dd>
      <dt>As of</dt>
      <dd>
        <Timestamp iso={view.as_of} />
      </dd>
      <dt>Timeframe pattern</dt>
      <dd>
        <span className="chip">{view.timeframe_pattern}</span>
      </dd>
      <dt>Technical status</dt>
      <dd>
        <span className="chip">{view.technical_status}</span>
      </dd>
      <dt>Id</dt>
      <dd>
        <ArtifactId id={view[ID_FIELD["market-intelligence"]]} to={`/market-intelligence/${view[ID_FIELD["market-intelligence"]]}`} />
      </dd>
    </dl>
  );
}

function AlertSummary({ view }: { view: AlertView }) {
  return (
    <dl className="kv">
      <dt>Symbol</dt>
      <dd>{view.symbol}</dd>
      <dt>As of</dt>
      <dd>
        <Timestamp iso={view.as_of} />
      </dd>
      <dt>Alert code</dt>
      <dd>
        <span className="chip">{view.alert_code}</span>
      </dd>
      <dt>Subject</dt>
      <dd>{view.subject_kind}</dd>
      <dt>Id</dt>
      <dd>
        <ArtifactId id={view[ID_FIELD.alerts]} to={`/alerts/${view[ID_FIELD.alerts]}`} />
      </dd>
    </dl>
  );
}
