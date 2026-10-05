import { Link } from "react-router";
import type { FamilyViews } from "../api/types";
import { ArtifactId } from "../components/ArtifactId";
import { CursorPagination } from "../components/CursorPagination";
import { DetailSection } from "../components/DetailSection";
import { FilterBar } from "../components/FilterBar";
import { MetadataList } from "../components/MetadataList";
import { PageHeading } from "../components/PageHeading";
import { EmptyState } from "../components/PageState";
import { RequestError } from "../components/RequestError";
import { ResponseMeta } from "../components/ResponseMeta";
import { TableSkeleton } from "../components/TableSkeleton";
import { Timestamp } from "../components/Timestamp";
import { ArtifactDetailFrame } from "./ArtifactDetailFrame";
import { FAMILY_LABEL } from "./OverviewPage";
import { EMPTY_MESSAGE } from "./PlaceholderPage";
import { useHistoryPage } from "./useHistoryPage";

export type OperationalFamily = "options-intelligence" | "trade-setups" | "invalidation-checks";
type OperationalView = FamilyViews[OperationalFamily];

function idOf(row: OperationalView): string {
  if ("invalidation_id" in row) return row.invalidation_id;
  if ("assessment_id" in row) return row.assessment_id;
  return row.options_intelligence_id;
}

function Summary({ row }: { row: OperationalView }) {
  if ("invalidation_id" in row) return <><strong>{row.result}</strong><p>{row.reason}</p></>;
  if ("assessment_id" in row) return <><strong>{row.outcome_status}</strong><p>{row.candidate_count} candidates · {row.market_bias_state}</p>{row.no_setup_reasons.map((reason, i) => <p key={i}>{reason}</p>)}</>;
  return <>{row.contract_count.toLocaleString()} contracts</>;
}

export function OperationalHistoryPage({ family }: { family: OperationalFamily }) {
  const h = useHistoryPage(family);
  const { query } = h;
  const rows = query.data?.value.data ?? [];
  return <>
    <PageHeading title={FAMILY_LABEL[family]}><p className="page-meta">Validated observations, newest first. Open an entry to inspect its recorded result.</p></PageHeading>
    <FilterBar filters={h.filters} onApply={h.applyFilters} label={`Filter ${FAMILY_LABEL[family]}`} />
    {family === "trade-setups" ? <p className="hint">No setup is a valid assessment outcome. Reasons explain why no candidates were produced.</p> : null}
    {family === "invalidation-checks" ? <p className="hint">Checks require an existing candidate setup and later market intelligence. A terminal invalidation does not reactivate.</p> : null}
    <section className="card list-card" aria-label={`${FAMILY_LABEL[family]} history`}>
      <header className="card-header"><h2>History</h2><span aria-live="polite">{query.isFetching && !query.isPending ? "Refreshing…" : ""}</span></header>
      {query.isPending ? <TableSkeleton columns={4} /> : query.isError && !query.data ?
        <RequestError error={query.error} onRetry={() => void query.refetch()} invalid={<button type="button" onClick={h.goFirst}>Back to the first page</button>} /> :
        rows.length === 0 ? <EmptyState message={EMPTY_MESSAGE[family]} /> :
        <>
          {query.isError ? <RequestError error={query.error} onRetry={() => void query.refetch()} /> : null}
          <div className="table-wrap" aria-busy={query.isPlaceholderData}>
            <table className={`table table-data${query.isPlaceholderData ? " is-stale" : ""}`}>
              <caption className="visually-hidden-text">{FAMILY_LABEL[family]} history, newest first</caption>
              <thead><tr><th scope="col">Symbol</th><th scope="col">As of</th><th scope="col">Recorded result</th><th scope="col">Details</th></tr></thead>
              <tbody>{rows.map(row => <tr key={idOf(row)}>
                <th scope="row" data-label="Symbol">{row.symbol}</th>
                <td data-label="As of"><Timestamp iso={row.as_of} /></td>
                <td data-label="Recorded result"><Summary row={row} /></td>
                <td data-label="Details"><Link className="button button-small button-quiet" to={`/${family}/${idOf(row)}`} aria-label={`Open ${row.symbol} ${FAMILY_LABEL[family]} as of ${row.as_of}`}>Open</Link></td>
              </tr>)}</tbody>
            </table>
          </div>
        </>}
      {query.data ? <CursorPagination shown={rows.length} limit={query.data.value.meta.limit} hasNext={h.hasNext} hasPrevious={h.hasPrevious} isFirst={h.isFirst} busy={query.isPlaceholderData} onNext={h.goNext} onPrevious={h.goPrevious} onFirst={h.goFirst} /> : null}
    </section>
  </>;
}

const LABELS: Record<string, string> = {
  symbol: "Symbol", as_of: "As of", contract_count: "Contract count",
  outcome_status: "Outcome", no_setup_reasons: "No-setup reasons", market_bias_state: "Market bias",
  eligible_side: "Eligible side", candidate_count: "Candidate count", result: "Result", reason: "Reason",
  side: "Side", required_pattern: "Required pattern", observed_pattern: "Observed pattern",
  observed_technical_status: "Observed technical status", policy_id: "Policy",
  snapshot_id: "Options snapshot", rules_version: "Rules version",
  market_intelligence_id: "Market intelligence", options_intelligence_id: "Options intelligence",
  assessment_id: "Trade setup assessment", invalidation_id: "Invalidation check",
  options_intelligence_format_version: "Format version", assessment_format_version: "Format version",
  invalidation_format_version: "Format version",
};
const LINK_FAMILY: Record<string, string> = {
  market_intelligence_id: "market-intelligence", options_intelligence_id: "options-intelligence",
  assessment_id: "trade-setups", invalidation_id: "invalidation-checks",
};

export function OperationalDetailPage({ family }: { family: OperationalFamily }) {
  return <ArtifactDetailFrame family={family} listLabel={FAMILY_LABEL[family]} noun={FAMILY_LABEL[family]} heading={v => `${v.symbol} ${FAMILY_LABEL[family]}`}>
    {({ data, meta }) => <div className="detail-grid">
      <DetailSection title="Recorded assessment">
        <Summary row={data} />
        <MetadataList items={Object.entries(data).map(([field, value]) => ({
          term: LABELS[field] ?? field.replaceAll("_", " "),
          value: field === "as_of" ? <Timestamp iso={String(value)} showOriginal /> :
            Array.isArray(value) ? (value.length ? <ul>{value.map((v: string, i: number) => <li key={i}>{v}</li>)}</ul> : "None recorded") :
            value === null ? "Not available" :
            typeof value === "string" && value.startsWith("sha256:") ?
              <ArtifactId id={value} variant="full" {...(LINK_FAMILY[field] && value !== idOf(data) ? { to: `/${LINK_FAMILY[field]}/${value}` } : {})} /> :
              String(value),
        }))} />
      </DetailSection>
      <DetailSection title="Response"><ResponseMeta meta={meta} /></DetailSection>
    </div>}
  </ArtifactDetailFrame>;
}
