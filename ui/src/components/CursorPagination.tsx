/**
 * Cursor pagination exactly as the API supplies it: there is only a `next_cursor`, so there are no page numbers or
 * totals. "Previous" uses the in-memory cursor trail; "First page" always works.
 */
export function CursorPagination({
  shown,
  limit,
  hasNext,
  hasPrevious,
  isFirst,
  busy,
  onNext,
  onPrevious,
  onFirst,
}: {
  shown: number;
  limit: number;
  hasNext: boolean;
  hasPrevious: boolean;
  isFirst: boolean;
  busy: boolean;
  onNext: () => void;
  onPrevious: () => void;
  onFirst: () => void;
}) {
  return (
    <nav className="pagination" aria-label="Pagination">
      <p className="pagination-summary" aria-live="polite">
        {busy ? "Loading page…" : `Showing ${String(shown)} ${shown === 1 ? "item" : "items"} (up to ${String(limit)} per page)`}
        {isFirst ? " · newest first" : ""}
        {!hasNext && !busy ? " · end of history" : ""}
      </p>
      <div className="pagination-actions">
        <button type="button" className="button button-quiet" onClick={onFirst} disabled={isFirst || busy}>
          First page
        </button>
        <button type="button" className="button button-quiet" onClick={onPrevious} disabled={!hasPrevious || busy}>
          ‹ Previous
        </button>
        <button type="button" className="button" onClick={onNext} disabled={!hasNext || busy}>
          Next ›
        </button>
      </div>
    </nav>
  );
}
