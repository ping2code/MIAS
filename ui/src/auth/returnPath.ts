/**
 * Post-sign-in return paths (Phase 16E). Only known in-app routes are accepted, with only the app's own non-secret
 * query keys; everything else falls back to "/". No protocol, host, protocol-relative ("//"), backslash, encoded
 * slash trick, javascript:/data: URL or unknown route can come back out of this function.
 */
const ROUTES: readonly RegExp[] = [
  /^\/$/,
  /^\/market-intelligence$/,
  /^\/market-intelligence\/sha256:[0-9a-f]{64}$/,
  /^\/alerts$/,
  /^\/alerts\/sha256:[0-9a-f]{64}$/,
  /^\/options-intelligence$/,
  /^\/trade-setups$/,
  /^\/invalidation-checks$/,
  /^\/status$/,
];

/** The app's own query keys (filters and the detail tab). None of them can hold a secret. */
const QUERY_KEYS: ReadonlySet<string> = new Set(["symbol", "as_of_from", "as_of_to", "limit", "tab"]);
// Only a parsing base for relative paths (never requested); the same base React Router uses internally.
const BASE = "http://localhost";

function hasControlCharacter(value: string): boolean {
  for (let i = 0; i < value.length; i += 1) {
    const code = value.charCodeAt(i);
    if (code < 0x20 || code === 0x7f) return true;
  }
  return false;
}

export function isKnownRoute(pathname: string): boolean {
  return ROUTES.some((r) => r.test(pathname));
}

export function safeReturnPath(value: unknown): string {
  if (typeof value !== "string" || value.length === 0 || value.length > 512) return "/";
  // Must be a plain absolute path: no scheme, no host, no "//", no backslash, no control characters.
  if (!value.startsWith("/") || value.startsWith("//") || value.includes("\\") || hasControlCharacter(value)) return "/";
  let url: URL;
  try {
    url = new URL(value, BASE);
  } catch {
    return "/";
  }
  if (url.origin !== BASE || url.username !== "" || url.password !== "") return "/";
  if (/%2f|%5c/i.test(value)) return "/";
  if (!isKnownRoute(url.pathname)) return "/";
  const query = new URLSearchParams();
  for (const [key, val] of url.searchParams) {
    if (QUERY_KEYS.has(key) && !query.has(key) && val.length <= 64 && /^[A-Za-z0-9:.+_-]*$/.test(val)) query.set(key, val);
  }
  const search = query.toString();
  return search ? `${url.pathname}?${search}` : url.pathname;
}
