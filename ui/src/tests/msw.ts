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

/** A fetch that resolves the client's relative URLs against the jsdom origin (browsers do this natively). */
export const relativeFetch: typeof fetch = (input, init) => {
  const url = typeof input === "string" && input.startsWith("/") ? new URL(input, window.location.origin) : input;
  return fetch(url, init);
};
