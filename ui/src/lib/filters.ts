/**
 * History filters: exactly the API's parameters (symbol, as_of_from, as_of_to, limit). Validation mirrors the API so
 * invalid values are never sent; empty values are omitted. The URL carries only these API values (never the token).
 */
import type { HistoryFilters } from "../api/queries";

export const SYMBOL_PATTERN = /^[A-Z][A-Z0-9.-]{0,9}$/; // = artifact_store.index.SYMBOL
export const RFC3339_OFFSET = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/;
export const LIMIT_OPTIONS = [25, 50, 100, 200] as const;
export const DEFAULT_LIMIT = 50;

/** What the filter form edits: symbol text, and local date-times as produced by `<input type="datetime-local">`. */
export interface FilterForm {
  symbol: string;
  from: string;
  to: string;
  limit: number;
}

export type FilterErrors = Partial<Record<"symbol" | "from" | "to", string>>;

const LOCAL_INPUT = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/;

/** A `datetime-local` value (the browser's local time) as an RFC 3339 UTC instant with an explicit `Z`. */
export function localInputToInstant(value: string): string | null {
  const m = LOCAL_INPUT.exec(value);
  if (!m) return null;
  const [, y, mo, d, h, mi, s] = m;
  const date = new Date(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi), Number(s ?? "0"));
  if (Number.isNaN(date.getTime())) return null;
  return date.toISOString().replace(/\.\d{3}Z$/, "Z");
}

/** An RFC 3339 instant shown in a `datetime-local` input, in the browser's local time (seconds precision). */
export function instantToLocalInput(value: string): string {
  const date = new Date(value);
  if (!RFC3339_OFFSET.test(value) || Number.isNaN(date.getTime())) return "";
  const pad = (n: number): string => String(n).padStart(2, "0");
  return `${String(date.getFullYear())}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(
    date.getMinutes(),
  )}:${pad(date.getSeconds())}`;
}

export function validateForm(form: FilterForm): { filters: HistoryFilters | null; errors: FilterErrors } {
  const errors: FilterErrors = {};
  const filters: HistoryFilters = { limit: LIMIT_OPTIONS.includes(form.limit as 25) ? form.limit : DEFAULT_LIMIT };
  const symbol = form.symbol.trim().toUpperCase();
  if (symbol !== "") {
    if (SYMBOL_PATTERN.test(symbol)) filters.symbol = symbol;
    else errors.symbol = "Use 1–10 characters: a letter, then letters, digits, '.' or '-'.";
  }
  if (form.from.trim() !== "") {
    const instant = localInputToInstant(form.from.trim());
    if (instant) filters.asOfFrom = instant;
    else errors.from = "Enter a complete date and time.";
  }
  if (form.to.trim() !== "") {
    const instant = localInputToInstant(form.to.trim());
    if (instant) filters.asOfTo = instant;
    else errors.to = "Enter a complete date and time.";
  }
  if (filters.asOfFrom && filters.asOfTo && Date.parse(filters.asOfFrom) > Date.parse(filters.asOfTo)) {
    errors.to = "'To' must not be before 'From'.";
  }
  return { filters: Object.keys(errors).length === 0 ? filters : null, errors };
}

/** URL search params → filters. Invalid or empty values are dropped, so nothing invalid is ever sent. */
export function filtersFromSearch(search: URLSearchParams): HistoryFilters {
  const filters: HistoryFilters = { limit: DEFAULT_LIMIT };
  const symbol = search.get("symbol");
  if (symbol !== null && SYMBOL_PATTERN.test(symbol)) filters.symbol = symbol;
  const from = search.get("as_of_from");
  if (from !== null && RFC3339_OFFSET.test(from)) filters.asOfFrom = from;
  const to = search.get("as_of_to");
  if (to !== null && RFC3339_OFFSET.test(to)) filters.asOfTo = to;
  const limit = Number(search.get("limit"));
  if (LIMIT_OPTIONS.includes(limit as 25)) filters.limit = limit;
  return filters;
}

/** Filters → URL search params, in a fixed order, omitting unset values and the default limit. */
export function filtersToSearch(filters: HistoryFilters): URLSearchParams {
  const search = new URLSearchParams();
  if (filters.symbol !== undefined) search.set("symbol", filters.symbol);
  if (filters.asOfFrom !== undefined) search.set("as_of_from", filters.asOfFrom);
  if (filters.asOfTo !== undefined) search.set("as_of_to", filters.asOfTo);
  if (filters.limit !== DEFAULT_LIMIT) search.set("limit", String(filters.limit));
  return search;
}

export function formFromFilters(filters: HistoryFilters): FilterForm {
  return {
    symbol: filters.symbol ?? "",
    from: filters.asOfFrom ? instantToLocalInput(filters.asOfFrom) : "",
    to: filters.asOfTo ? instantToLocalInput(filters.asOfTo) : "",
    limit: filters.limit,
  };
}

export function hasActiveFilters(filters: HistoryFilters): boolean {
  return filters.symbol !== undefined || filters.asOfFrom !== undefined || filters.asOfTo !== undefined;
}
