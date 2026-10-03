/**
 * Cursor pagination state, kept in memory only (never in storage or the URL; cursors are opaque but stay out of
 * shareable links). The backend supplies only `next_cursor`, so "Previous" needs the cursors that led here: each
 * page visit is a history entry whose `location.state` holds a random key into this map. Browser back/forward and
 * the Previous button therefore agree. After a reload the map is empty and the list starts at the first page.
 */
export interface PageState {
  cursor: string | null;
  /** Cursors of the pages before this one, oldest first (null = the first page). */
  trail: readonly (string | null)[];
}

export const FIRST_PAGE: PageState = { cursor: null, trail: [] };

const pages = new Map<string, PageState>();
const MAX_ENTRIES = 500;

export function nextPage(state: PageState, nextCursor: string): PageState {
  return { cursor: nextCursor, trail: [...state.trail, state.cursor] };
}

export function previousPage(state: PageState): PageState | null {
  if (state.trail.length === 0) return null;
  return { cursor: state.trail[state.trail.length - 1] ?? null, trail: state.trail.slice(0, -1) };
}

/** Remember a page state; returns the key to put in `location.state`. */
export function savePage(state: PageState): string {
  if (pages.size >= MAX_ENTRIES) {
    const oldest = pages.keys().next().value;
    if (oldest !== undefined) pages.delete(oldest);
  }
  const bytes = crypto.getRandomValues(new Uint8Array(8));
  const key = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  pages.set(key, state);
  return key;
}

export function loadPage(key: unknown): PageState {
  return typeof key === "string" ? (pages.get(key) ?? FIRST_PAGE) : FIRST_PAGE;
}

/** Forget every cursor (sign-out). */
export function clearPages(): void {
  pages.clear();
}
