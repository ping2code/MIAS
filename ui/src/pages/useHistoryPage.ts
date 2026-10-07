import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useCallback, useMemo } from "react";
import { useLocation, useNavigate } from "react-router";
import { historyQuery, optionsActivityQuery, type HistoryFilters, type PagedQuery } from "../api/queries";
import type { ArtifactFamily, FamilyViews, OptionsIntelligenceActivityView } from "../api/types";
import { useApiClient } from "../app/context";
import { loadPage, nextPage, previousPage, savePage } from "../lib/cursorTrail";
import { filtersFromSearch, filtersToSearch } from "../lib/filters";

/**
 * A history list driven by the URL (filters only) and in-memory cursor state (per history entry). Filter changes
 * and page moves are history entries, so browser back/forward work. Previous page data stays visible while the next
 * page loads; a filter change never shows another filter's rows.
 */
export function useHistoryPage<F extends ArtifactFamily>(family: F) {
  const client = useApiClient();
  return useListPage<FamilyViews[F]>((filters, cursor) => historyQuery(client, family, filters, cursor));
}

/** The options-intelligence activity views, paged exactly like the history (same filters and cursors). */
export function useOptionsActivityPage() {
  const client = useApiClient();
  return useListPage<OptionsIntelligenceActivityView>((filters, cursor) => optionsActivityQuery(client, filters, cursor));
}

function useListPage<T>(makeQuery: (filters: HistoryFilters, cursor: string | null) => PagedQuery<T>) {
  const location = useLocation();
  const navigate = useNavigate();
  const filters = useMemo(() => filtersFromSearch(new URLSearchParams(location.search)), [location.search]);
  const page = loadPage((location.state as { page?: unknown } | null)?.page);

  const query = useQuery({
    ...makeQuery(filters, page.cursor),
    // Keep the previous page visible while moving between pages of the same filters (never across filters).
    placeholderData: (previous, previousQuery) =>
      previousQuery !== undefined && sameFilters(previousQuery.queryKey[3], filters) ? keepPreviousData(previous) : undefined,
  });

  const go = useCallback(
    (search: string, state: { page: string } | null) => {
      void navigate({ pathname: location.pathname, search }, { state });
    },
    [navigate, location.pathname],
  );

  const applyFilters = useCallback(
    (next: HistoryFilters) => {
      const search = filtersToSearch(next).toString();
      go(search ? `?${search}` : "", null);
    },
    [go],
  );

  const nextCursor = query.data?.value.meta.next_cursor ?? null;
  return {
    filters,
    page,
    query,
    isFirst: page.cursor === null,
    hasPrevious: page.trail.length > 0,
    hasNext: nextCursor !== null && !query.isPlaceholderData,
    goNext: () => {
      if (nextCursor !== null) go(location.search, { page: savePage(nextPage(page, nextCursor)) });
    },
    goPrevious: () => {
      const previous = previousPage(page);
      if (previous) go(location.search, previous.cursor === null ? null : { page: savePage(previous) });
    },
    goFirst: () => {
      go(location.search, null);
    },
    applyFilters,
  };
}

function sameFilters(keyPart: unknown, filters: HistoryFilters): boolean {
  if (typeof keyPart !== "object" || keyPart === null) return false;
  const k = keyPart as Record<string, unknown>;
  return (
    k.symbol === (filters.symbol ?? null) &&
    k.asOfFrom === (filters.asOfFrom ?? null) &&
    k.asOfTo === (filters.asOfTo ?? null) &&
    k.limit === filters.limit
  );
}
