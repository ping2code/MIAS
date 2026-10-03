import { http, HttpResponse, type HttpHandler } from "msw";
import { setupServer } from "msw/node";
import { fixture, TEST_TOKEN, type CapturedResponse } from "./fixtures";

export const server = setupServer();

export function respond(entry: CapturedResponse): HttpResponse<string> {
  const headers = new Headers(entry.headers);
  const body = entry.text ?? JSON.stringify(entry.body);
  return new HttpResponse(body, { status: entry.status, headers });
}

export function authorized(request: Request): boolean {
  return request.headers.get("Authorization") === `Bearer ${TEST_TOKEN}`;
}

/** The happy-path API: health, version and every Overview/placeholder query, from captured responses. */
export function apiHandlers(overrides: Partial<Record<string, CapturedResponse>> = {}): HttpHandler[] {
  const pick = (name: string): CapturedResponse => overrides[name] ?? fixture(name);
  const guarded = (name: string) => ({ request }: { request: Request }) =>
    authorized(request) ? respond(pick(name)) : respond(fixture("unauthorized"));
  return [
    http.get("*/health/live", () => respond(pick("live"))),
    http.get("*/health/ready", () => respond(pick("ready"))),
    http.get("*/api/v1/version", guarded("version")),
    http.get("*/api/v1/:family/latest", ({ request, params }) => {
      if (!authorized(request)) return respond(fixture("unauthorized"));
      return respond(pick(`latest:${String(params.family)}`));
    }),
    http.get("*/api/v1/:family", ({ request, params }) => {
      if (!authorized(request)) return respond(fixture("unauthorized"));
      return respond(pick(`history_head:${String(params.family)}`));
    }),
  ];
}

/** The five artifact kinds the dashboard reads (route segments). */
export const ARTIFACT_PATHS: readonly string[] = [
  "market-intelligence",
  "options-intelligence",
  "trade-setups",
  "invalidation-checks",
  "alerts",
];

/**
 * The Overview's known background requests, served from the real captured responses:
 * - `GET /api/v1/<kind>?limit=1` per kind (has-data / newest as_of);
 * - `GET /api/v1/{market-intelligence,alerts}/latest?symbol=META` (the symbol taken from those heads).
 * They match nothing else — any other request falls through and stays unhandled (onUnhandledFrame: "error"), so
 * unexpected traffic still fails loudly.
 */
export function backgroundHistoryHeads(): HttpHandler[] {
  return [
    http.get("*/api/v1/:family/latest", ({ request, params }) => {
      const family = String(params.family);
      if ((family !== "market-intelligence" && family !== "alerts") || new URL(request.url).search !== "?symbol=META") return undefined;
      return respond(authorized(request) ? fixture(`latest:${family}`) : fixture("unauthorized"));
    }),
    http.get("*/api/v1/:family", ({ request, params }) => {
      const family = String(params.family);
      if (!ARTIFACT_PATHS.includes(family) || new URL(request.url).search !== "?limit=1") return undefined;
      return respond(authorized(request) ? fixture(`history_head:${family}`) : fixture("unauthorized"));
    }),
  ];
}

/** A fetch that resolves the client's relative URLs against the jsdom origin (browsers do this natively). */
export const relativeFetch: typeof fetch = (input, init) => {
  const url = typeof input === "string" && input.startsWith("/") ? new URL(input, window.location.origin) : input;
  return fetch(url, init);
};
