import { Link } from "react-router";
import type { MarketIntelligenceView } from "../../api/types";
import { ArtifactId } from "../../components/ArtifactId";
import { CursorPagination } from "../../components/CursorPagination";
import { DomainBadge } from "../../components/DomainBadge";
import { FilterBar } from "../../components/FilterBar";
import { PageHeading } from "../../components/PageHeading";
import { EmptyState } from "../../components/PageState";
import { RequestError } from "../../components/RequestError";
import { TableSkeleton } from "../../components/TableSkeleton";
import { Timestamp } from "../../components/Timestamp";
import { hasActiveFilters } from "../../lib/filters";
import { LatestForSymbol } from "../LatestForSymbol";
import { useHistoryPage } from "../useHistoryPage";

export const MI_EMPTY = "No market intelligence is available for the selected filters.";

/** `GET /api/v1/market-intelligence` history: backend order (as_of descending, then id), cursor pagination. */
export function MarketIntelligenceListPage() {
  const h = useHistoryPage("market-intelligence");
  const { query } = h;
  const rows = query.data?.value.data ?? [];

  return (
    <>
      <PageHeading title="Market Intelligence">
        <p className="page-meta">Sealed Phase 8 market intelligence, newest first. Read-only.</p>
      </PageHeading>
      <FilterBar filters={h.filters} onApply={h.applyFilters} label="Filter market intelligence" />
      <LatestForSymbol
        family="market-intelligence"
        symbol={h.filters.symbol}
        noun="market intelligence"
        idOf={(v) => v.intelligence_id}
        summary={(v) => (
          <>
            <DomainBadge kind="timeframe_pattern" value={v.timeframe_pattern} />
            <DomainBadge kind="technical_status" value={v.technical_status} label={`Technical: ${v.technical_status}`} />
          </>
        )}
      />
      <section className="card list-card" aria-labelledby="mi-results">
        <header className="card-header">
          <h2 id="mi-results" tabIndex={-1}>
            History
          </h2>
          <span className="refreshing" aria-live="polite">
            {query.isFetching && !query.isPending ? "Refreshing…" : ""}
          </span>
        </header>
        {query.isPending ? (
          <TableSkeleton columns={6} />
        ) : query.isError && !query.data ? (
          <RequestError
            error={query.error}
            onRetry={() => void query.refetch()}
            invalid={
              <>
                <p>The API did not accept this query{h.isFirst ? "" : " or page cursor"}.</p>
                <button type="button" className="button button-small" onClick={h.goFirst}>
                  Back to the first page
                </button>
              </>
            }
          />
        ) : rows.length === 0 ? (
          <EmptyState message={hasActiveFilters(h.filters) || !h.isFirst ? MI_EMPTY : "No market intelligence is available yet."} />
        ) : (
          <>
            {query.isError ? <RequestError error={query.error} onRetry={() => void query.refetch()} /> : null}
            <div className="table-wrap" aria-busy={query.isPlaceholderData}>
              <table className={`table table-data${query.isPlaceholderData ? " is-stale" : ""}`}>
                <caption className="visually-hidden-text">Market intelligence history, newest first</caption>
                <thead>
                  <tr>
                    <th scope="col">Symbol</th>
                    <th scope="col">As of</th>
                    <th scope="col">Timeframe pattern</th>
                    <th scope="col">Technical status</th>
                    <th scope="col">Market context</th>
                    <th scope="col">Artifact id</th>
                    <th scope="col">
                      <span className="visually-hidden-text">Open</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <MarketIntelligenceRow key={row.intelligence_id} row={row} />
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
        {query.data && rows.length > 0 ? (
          <CursorPagination
            shown={rows.length}
            limit={query.data.value.meta.limit}
            hasNext={h.hasNext}
            hasPrevious={h.hasPrevious}
            isFirst={h.isFirst}
            busy={query.isPlaceholderData}
            onNext={h.goNext}
            onPrevious={h.goPrevious}
            onFirst={h.goFirst}
          />
        ) : !h.isFirst ? (
          <div className="pagination">
            <button type="button" className="button button-quiet" onClick={h.goFirst}>
              First page
            </button>
          </div>
        ) : null}
      </section>
    </>
  );
}

function MarketIntelligenceRow({ row }: { row: MarketIntelligenceView }) {
  const to = `/market-intelligence/${row.intelligence_id}`;
  return (
    <tr>
      <th scope="row" data-label="Symbol">
        <span className="symbol">{row.symbol}</span>
      </th>
      <td data-label="As of">
        <Timestamp iso={row.as_of} />
      </td>
      <td data-label="Timeframe pattern">
        <DomainBadge kind="timeframe_pattern" value={row.timeframe_pattern} />
      </td>
      <td data-label="Technical status">
        <DomainBadge kind="technical_status" value={row.technical_status} />
      </td>
      <td data-label="Market context">
        <DomainBadge
          kind="availability"
          value={String(row.market_context_available)}
          label={row.market_context_available ? "Available" : "Not available"}
        />
      </td>
      <td data-label="Artifact id">
        <ArtifactId id={row.intelligence_id} />
      </td>
      <td data-label="Open" className="cell-action">
        <Link to={to} className="button button-small button-quiet" aria-label={`Open ${row.symbol} market intelligence as of ${row.as_of}`}>
          Open ›
        </Link>
      </td>
    </tr>
  );
}
