import { Link } from "react-router";
import type { OptionsIntelligenceActivityView } from "../../api/types";
import { CursorPagination } from "../../components/CursorPagination";
import { FilterBar } from "../../components/FilterBar";
import { PageHeading } from "../../components/PageHeading";
import { EmptyState } from "../../components/PageState";
import { RequestError } from "../../components/RequestError";
import { TableSkeleton } from "../../components/TableSkeleton";
import { Timestamp } from "../../components/Timestamp";
import {
  comparisonNote,
  CONCENTRATION_LABEL,
  count,
  countChange,
  expiryDates,
  ivPercent,
  MOMENTUM_LABEL,
  percentChange,
  ratio,
} from "../../lib/optionsActivity";
import { EMPTY_MESSAGE } from "../PlaceholderPage";
import { useOptionsActivityPage } from "../useHistoryPage";

/**
 * Options Intelligence: one compact, descriptive activity card per report (`options-intelligence-activity-v1`),
 * newest first. Changes compare each report only with the previous report of the same symbol and session. "Open"
 * shows the full recorded report.
 */
export function OptionsIntelligencePage() {
  const h = useOptionsActivityPage();
  const { query } = h;
  const rows = query.data?.value.data ?? [];
  return (
    <>
      <PageHeading title="Options Intelligence">
        <p className="page-meta">
          Validated reports, newest first. Activity is descriptive: changes compare a report with the previous report of
          the same symbol and session.
        </p>
      </PageHeading>
      <FilterBar filters={h.filters} onApply={h.applyFilters} label="Filter Options Intelligence" />
      <section className="card list-card" aria-label="Options Intelligence history">
        <header className="card-header">
          <h2>History</h2>
          <span aria-live="polite">{query.isFetching && !query.isPending ? "Refreshing…" : ""}</span>
        </header>
        {query.isPending ? (
          <TableSkeleton columns={4} />
        ) : query.isError && !query.data ? (
          <RequestError
            error={query.error}
            onRetry={() => void query.refetch()}
            invalid={
              <button type="button" onClick={h.goFirst}>
                Back to the first page
              </button>
            }
          />
        ) : rows.length === 0 ? (
          <EmptyState message={EMPTY_MESSAGE["options-intelligence"]} />
        ) : (
          <>
            {query.isError ? <RequestError error={query.error} onRetry={() => void query.refetch()} /> : null}
            <ol className={`activity-list${query.isPlaceholderData ? " is-stale" : ""}`} aria-busy={query.isPlaceholderData}>
              {rows.map((row) => (
                <li key={row.options_intelligence_id}>
                  <ActivityCard row={row} />
                </li>
              ))}
            </ol>
          </>
        )}
        {query.data ? (
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
        ) : null}
      </section>
    </>
  );
}

function Metric({ term, value, change }: { term: string; value: string; change?: string | null }) {
  return (
    <div className="activity-metric">
      <dt>{term}</dt>
      <dd>
        <span className="activity-value">{value}</span>
        {change ? <span className="activity-change">{change}</span> : null}
      </dd>
    </div>
  );
}

function strikes(n: number): string {
  return `${count(n)} ${n === 1 ? "strike" : "strikes"}`;
}

export function ActivityCard({ row }: { row: OptionsIntelligenceActivityView }) {
  return (
    <article className="activity-card" aria-label={`${row.symbol} options activity as of ${row.as_of}`}>
      <header className="activity-header">
        <h3>
          <span className="symbol">{row.symbol}</span> | {count(row.contract_count)} contracts | {expiryDates(row.expiration_count)}
        </h3>
        <Link
          className="button button-small button-quiet"
          to={`/options-intelligence/${row.options_intelligence_id}`}
          aria-label={`Open ${row.symbol} Options Intelligence as of ${row.as_of}`}
        >
          Open
        </Link>
      </header>
      <p className="activity-meta">
        <Timestamp iso={row.as_of} /> · {comparisonNote(row.comparison)}
      </p>
      <dl className="activity-grid">
        <Metric term="Call Volume" value={count(row.call_volume)} change={row.call_volume === null ? null : percentChange(row.call_volume_change_pct, row.call_volume_change_reason)} />
        <Metric term="Put Volume" value={count(row.put_volume)} change={row.put_volume === null ? null : percentChange(row.put_volume_change_pct, row.put_volume_change_reason)} />
        <Metric term="Put/Call Volume" value={ratio(row.put_call_volume_ratio)} />
        <Metric term="IV Median" value={ivPercent(row.iv_median)} />
        <Metric term="Volume > OI" value={count(row.volume_gt_oi_count)} change={countChange(row.volume_gt_oi_change)} />
        <Metric term="Call Breadth" value={strikes(row.call_breadth)} change={countChange(row.call_breadth_change)} />
        <Metric term="Put Breadth" value={strikes(row.put_breadth)} change={countChange(row.put_breadth_change)} />
        <Metric term="Call Concentration" value={CONCENTRATION_LABEL[row.call_concentration]} />
        <Metric term="Put Concentration" value={CONCENTRATION_LABEL[row.put_concentration]} />
        <Metric term="Activity Bias" value={row.activity_bias === "UNAVAILABLE" ? "Unavailable" : row.activity_bias} />
        <Metric term="15m Momentum" value={MOMENTUM_LABEL[row.momentum_15m]} />
      </dl>
      <p className="activity-trend">
        <span className="activity-trend-term">Trend Summary</span> {row.trend_summary}
      </p>
      {row.call_concentration === "unavailable" && row.concentration_reason === "underlying_price_unavailable" ? (
        <p className="hint">Concentration needs an underlying price, which this report does not record.</p>
      ) : null}
      {row.momentum_15m === "VOLUME_CORRECTION" ? (
        <p className="hint">A cumulative volume went down since the previous report (a provider correction).</p>
      ) : null}
    </article>
  );
}
