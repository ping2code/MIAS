import { Link } from "react-router";
import type { AlertView } from "../../api/types";
import { ArtifactId } from "../../components/ArtifactId";
import { CursorPagination } from "../../components/CursorPagination";
import { DomainBadge } from "../../components/DomainBadge";
import { FilterBar } from "../../components/FilterBar";
import { PageHeading } from "../../components/PageHeading";
import { EmptyState } from "../../components/PageState";
import { RequestError } from "../../components/RequestError";
import { TableSkeleton } from "../../components/TableSkeleton";
import { Timestamp } from "../../components/Timestamp";
import { humanize } from "../../lib/domain";
import { hasActiveFilters } from "../../lib/filters";
import { LatestForSymbol } from "../LatestForSymbol";
import { useHistoryPage } from "../useHistoryPage";

export const ALERTS_EMPTY = "No alerts are available for the selected filters.";

/** `GET /api/v1/alerts` history. Delivery state is not part of this view and is never shown or inferred here. */
export function AlertListPage() {
  const h = useHistoryPage("alerts");
  const { query } = h;
  const rows = query.data?.value.data ?? [];

  return (
    <>
      <PageHeading title="Alerts">
        <p className="page-meta">Sealed Phase 12 alert events, newest first. Read-only.</p>
      </PageHeading>
      <FilterBar filters={h.filters} onApply={h.applyFilters} label="Filter alerts" />
      <LatestForSymbol
        family="alerts"
        symbol={h.filters.symbol}
        noun="alert"
        idOf={(v) => v.alert_id}
        summary={(v) => <DomainBadge kind="alert_code" value={v.alert_code} />}
      />
      <section className="card list-card" aria-labelledby="alert-results">
        <header className="card-header">
          <h2 id="alert-results">History</h2>
          <span className="refreshing" aria-live="polite">
            {query.isFetching && !query.isPending ? "Refreshing…" : ""}
          </span>
        </header>
        {query.isPending ? (
          <TableSkeleton columns={7} />
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
          <EmptyState message={hasActiveFilters(h.filters) || !h.isFirst ? ALERTS_EMPTY : "No alerts are available yet."} />
        ) : (
          <>
            {query.isError ? <RequestError error={query.error} onRetry={() => void query.refetch()} /> : null}
            <div className="table-wrap" aria-busy={query.isPlaceholderData}>
              <table className={`table table-data${query.isPlaceholderData ? " is-stale" : ""}`}>
                <caption className="visually-hidden-text">Alert history, newest first</caption>
                <thead>
                  <tr>
                    <th scope="col">Alert</th>
                    <th scope="col">Symbol</th>
                    <th scope="col">As of</th>
                    <th scope="col">Subject</th>
                    <th scope="col">Transition</th>
                    <th scope="col">Sources</th>
                    <th scope="col">Artifact id</th>
                    <th scope="col">
                      <span className="visually-hidden-text">Open</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <AlertRow key={row.alert_id} row={row} />
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

export function TransitionText({ transition }: { transition: AlertView["transition"] }) {
  if (transition === null) return <span className="muted">—</span>;
  return (
    <span className="transition">
      <code>{transition.previous}</code>
      <span aria-hidden="true"> → </span>
      <span className="visually-hidden-text"> to </span>
      <code>{transition.current}</code>
    </span>
  );
}

function AlertRow({ row }: { row: AlertView }) {
  return (
    <tr>
      <th scope="row" data-label="Alert">
        <DomainBadge kind="alert_code" value={row.alert_code} />
      </th>
      <td data-label="Symbol">
        <span className="symbol">{row.symbol}</span>
      </td>
      <td data-label="As of">
        <Timestamp iso={row.as_of} />
      </td>
      <td data-label="Subject">{humanize(row.subject_kind)}</td>
      <td data-label="Transition">
        <TransitionText transition={row.transition} />
      </td>
      <td data-label="Sources">
        <ul className="inline-list">
          {row.source_refs.map((ref) => (
            <li key={`${ref.role}-${ref.id}`}>
              <span className="muted">{ref.role}:</span> {humanize(ref.object_kind)}
            </li>
          ))}
        </ul>
      </td>
      <td data-label="Artifact id">
        <ArtifactId id={row.alert_id} />
      </td>
      <td data-label="Open" className="cell-action">
        <Link
          to={`/alerts/${row.alert_id}`}
          className="button button-small button-quiet"
          aria-label={`Open ${row.alert_code} alert for ${row.symbol} as of ${row.as_of}`}
        >
          Open ›
        </Link>
      </td>
    </tr>
  );
}
