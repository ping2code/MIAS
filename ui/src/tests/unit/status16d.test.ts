import { describe, expect, it } from "vitest";
import { createApiClient } from "../../api/client";
import { ApiError } from "../../api/errors";
import { readyQuery } from "../../api/queries";
import { outcomeFor, recoveredAfterRetry, type RequestSummary } from "../../api/requestSummary";
import { createDiagnosticsStore, isFailure, MAX_READINESS, MAX_REQUESTS, sanitizeSummary } from "../../app/diagnostics";
import { createServices } from "../../app/providers";
import {
  deriveApiStatus,
  describeLive,
  describeReady,
  observeLive,
  observeReady,
  reachability,
  type LiveObservation,
  type ReadyObservation,
} from "../../lib/apiStatus";
import { noteFor } from "../../pages/status/RecentActivity";
import { fx } from "../api16c";

const base: RequestSummary = {
  startedAt: 1_000,
  method: "GET",
  route: "/health/live",
  status: 200,
  outcome: "success",
  durationMs: 12,
  attempts: 1,
  requestId: "ui-0123456789abcdef01234567",
  note: null,
};

describe("diagnostics store", () => {
  it("keeps at most MAX_REQUESTS summaries, newest first, and counts every request", () => {
    const store = createDiagnosticsStore();
    for (let i = 0; i < MAX_REQUESTS + 10; i += 1) store.recordRequest({ ...base, startedAt: i });
    const snap = store.getSnapshot();
    expect(snap.requests).toHaveLength(MAX_REQUESTS);
    expect(snap.requests[0]?.startedAt).toBe(MAX_REQUESTS + 9);
    expect(snap.requests.at(-1)?.startedAt).toBe(10);
    expect(snap.total).toBe(MAX_REQUESTS + 10);
  });

  it("keeps the last event of each kind even after it is evicted from the list", () => {
    const store = createDiagnosticsStore();
    store.recordRequest({ ...base, outcome: "timeout", status: null, startedAt: 1 });
    for (let i = 0; i < MAX_REQUESTS; i += 1) store.recordRequest({ ...base, startedAt: 100 + i });
    const snap = store.getSnapshot();
    expect(snap.requests.some((r) => r.outcome === "timeout")).toBe(false);
    expect(snap.lastTimeout?.startedAt).toBe(1);
    expect(snap.lastFailure?.startedAt).toBe(1);
    expect(snap.lastSuccess?.startedAt).toBe(100 + MAX_REQUESTS - 1);
  });

  it("keeps at most MAX_READINESS readiness samples, newest first", () => {
    const store = createDiagnosticsStore();
    for (let i = 0; i < MAX_READINESS + 5; i += 1) {
      store.recordReadiness({ at: i, result: "ready", status: 200, checks: [{ name: "settings", status: "pass" }], requestId: null });
    }
    const { readiness } = store.getSnapshot();
    expect(readiness).toHaveLength(MAX_READINESS);
    expect(readiness[0]?.at).toBe(MAX_READINESS + 4);
  });

  it("stores only allow-listed, validated fields (canaries never survive)", () => {
    const store = createDiagnosticsStore();
    const hostile = {
      ...base,
      route: "/api/v1/alerts?symbol=META&token=placeholder-canary-token",
      requestId: "Bearer placeholder-canary-token",
      authorization: "Bearer placeholder-canary-token",
      headers: { Authorization: "Bearer placeholder-canary-token", Cookie: "session=placeholder-canary-cookie" },
      body: '{"api_key":"placeholder-canary-key","password":"placeholder-canary-password"}',
      canonical: "placeholder-canary-canonical",
    } as unknown as RequestSummary;
    store.recordRequest(hostile);
    store.recordReadiness({
      at: 1,
      result: "not_ready",
      status: 503,
      checks: [
        { name: "artifact_index", status: "fail" },
        { name: "Bearer placeholder-canary-token", status: "pass" },
      ],
      requestId: "placeholder-canary-token value",
    });
    const text = JSON.stringify(store.getSnapshot());
    for (const canary of ["placeholder-canary", "Bearer", "Authorization", "Cookie", "api_key", "password", "symbol=", "token="]) {
      expect(text).not.toContain(canary);
    }
    const [row] = store.getSnapshot().requests;
    expect(row?.route).toBe("(other)");
    expect(row?.requestId).toBeNull();
    expect(Object.keys(row ?? {}).sort()).toEqual(
      ["attempts", "durationMs", "method", "note", "outcome", "requestId", "route", "startedAt", "status"].sort(),
    );
    expect(store.getSnapshot().readiness[0]?.checks).toEqual([{ name: "artifact_index", status: "fail" }]);
  });

  it("accepts the real route templates", () => {
    for (const route of [
      "/health/live",
      "/health/ready",
      "/api/v1/version",
      "/api/v1/alerts",
      "/api/v1/market-intelligence/latest",
      "/api/v1/alerts/{id}",
      "/api/v1/alerts/{id}/canonical",
      "/api/v1/alerts/{id}/deliveries",
    ]) {
      expect(sanitizeSummary({ ...base, route }).route).toBe(route);
    }
    expect(sanitizeSummary({ ...base, route: "/api/v1/alerts/sha256:abc" }).route).toBe("(other)");
  });

  it("clears the list and resets completely, in memory", () => {
    const store = createDiagnosticsStore();
    store.recordRequest(base);
    store.clearRequests();
    expect(store.getSnapshot().requests).toEqual([]);
    expect(store.getSnapshot().lastSuccess).not.toBeNull();
    store.reset();
    expect(store.getSnapshot().lastSuccess).toBeNull();
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("clears a route's retry state when its request completes", () => {
    const store = createDiagnosticsStore();
    store.recordRetry({ route: "/health/live", attempt: 1, delayMs: 1000 }, 5);
    expect(store.getSnapshot().retrying).toEqual([{ route: "/health/live", attempt: 1, delayMs: 1000, at: 5 }]);
    store.recordRequest(base);
    expect(store.getSnapshot().retrying).toEqual([]);
  });
});

describe("outcome categories", () => {
  it("maps statuses and transport errors", () => {
    expect(outcomeFor(200, undefined)).toBe("success");
    expect(outcomeFor(404, new ApiError("http", { status: 404 }))).toBe("client_error");
    expect(outcomeFor(503, undefined)).toBe("server_error");
    expect(outcomeFor(null, new ApiError("network"))).toBe("network_error");
    expect(outcomeFor(null, new ApiError("timeout"))).toBe("timeout");
    expect(outcomeFor(null, new ApiError("aborted"))).toBe("cancelled");
    expect(outcomeFor(200, new ApiError("invalid_response", { status: 200 }))).toBe("server_error");
  });

  it("treats contract answers and cancellations as non-failures", () => {
    expect(isFailure({ ...base, outcome: "server_error", status: 503, note: "not_ready" })).toBe(false);
    expect(isFailure({ ...base, outcome: "server_error", status: 503, note: "capability_unavailable" })).toBe(false);
    expect(isFailure({ ...base, outcome: "cancelled", status: null })).toBe(false);
    expect(isFailure({ ...base, outcome: "server_error", status: 500 })).toBe(true);
  });

  it("labels recovery after retry only when the final attempt succeeded", () => {
    expect(recoveredAfterRetry({ ...base, attempts: 2 })).toBe(true);
    expect(noteFor({ ...base, attempts: 2 })).toBe("Recovered after retry");
    expect(noteFor({ ...base, attempts: 3, outcome: "timeout", status: null })).toBe("3 attempts");
    expect(noteFor(base)).toBeNull();
    expect(noteFor({ ...base, status: 503, outcome: "server_error", note: "not_ready" })).toBe("Reported not ready");
  });
});

describe("API status derivation", () => {
  const cases: [LiveObservation, ReadyObservation, string][] = [
    ["ok", "ready", "ready"],
    ["pending", "ready", "ready"],
    ["failed", "ready", "degraded"],
    ["unreachable", "ready", "degraded"],
    ["ok", "not_ready", "not_ready"],
    ["unreachable", "not_ready", "not_ready"],
    ["unreachable", "unreachable", "offline"],
    ["ok", "unreachable", "degraded"],
    ["pending", "unreachable", "degraded"],
    ["pending", "pending", "checking"],
    ["ok", "pending", "checking"],
    ["unreachable", "pending", "checking"],
    ["ok", "failed", "degraded"],
  ];
  it.each(cases)("live=%s ready=%s → %s", (live, ready, expected) => {
    expect(deriveApiStatus(live, ready)).toBe(expected);
  });

  it("uses the newest observation: an error newer than the data wins, an older one does not", () => {
    const data = { value: { status: "ready" as const, checks: [] }, requestId: null, status: 200 };
    const net = new ApiError("network");
    expect(observeReady({ data, error: net, dataUpdatedAt: 1, errorUpdatedAt: 2 })).toBe("unreachable");
    expect(observeReady({ data, error: net, dataUpdatedAt: 3, errorUpdatedAt: 2 })).toBe("ready");
    expect(observeReady({ data: undefined, error: null, dataUpdatedAt: 0, errorUpdatedAt: 0 })).toBe("pending");
    const notReady = { value: { status: "not_ready" as const, checks: [] }, requestId: null, status: 503 };
    expect(observeReady({ data: notReady, error: null, dataUpdatedAt: 1, errorUpdatedAt: 0 })).toBe("not_ready");
    const http500 = new ApiError("http", { status: 500, code: "internal" });
    expect(observeLive({ data: undefined, error: http500, dataUpdatedAt: 0, errorUpdatedAt: 1 })).toBe("failed");
  });

  it("words each state precisely", () => {
    expect(describeReady("not_ready", null).text).toBe("API reachable but not ready (HTTP 503)");
    expect(describeReady("unreachable", null).text).toBe("API unreachable");
    expect(describeLive("failed", new ApiError("http", { status: 500 })).text).toBe("API liveness check failed (HTTP 500)");
    expect(describeLive("ok", null).text).toBe("Live — the API process is running");
    expect(describeReady("ready", null).text).toBe("Ready — the API can serve correctly");
    expect(reachability("ok", "not_ready")).toBe("reachable");
    expect(reachability("unreachable", "unreachable")).toBe("unreachable");
    expect(reachability("pending", "pending")).toBe("unknown");
  });
});

describe("client instrumentation", () => {
  function setup(replies: (Response | Error)[]) {
    let clock = 10_000;
    const summaries: RequestSummary[] = [];
    const retries: { route: string; attempt: number; delayMs: number }[] = [];
    const queue = [...replies];
    const client = createApiClient({
      now: () => clock,
      sleep: (ms) => {
        clock += ms;
        return Promise.resolve();
      },
      onRequest: (s) => summaries.push(s),
      onRetry: (r) => retries.push(r),
      fetchImpl: () => {
        clock += 25;
        const next = queue.shift();
        if (next instanceof Error) return Promise.reject(next);
        return next ? Promise.resolve(next) : Promise.reject(new Error("extra"));
      },
    });
    return { client, summaries, retries };
  }
  const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });

  it("records one final summary per logical request with duration, attempts and recovery", async () => {
    const { client, summaries, retries } = setup([new TypeError("down"), json(200, { status: "live" }, { "X-Request-ID": "srv-req-123456" })]);
    await client.live();
    expect(summaries).toEqual([
      {
        startedAt: 10_000,
        method: "GET",
        route: "/health/live",
        status: 200,
        outcome: "success",
        durationMs: 25 + 1000 + 25,
        attempts: 2,
        requestId: "srv-req-123456",
        note: null,
      },
    ]);
    expect(retries).toEqual([{ route: "/health/live", attempt: 1, delayMs: 1000 }]);
    expect(recoveredAfterRetry(summaries[0] as RequestSummary)).toBe(true);
  });

  it("records route templates without queries or ids, and the contract notes", async () => {
    const id = "sha256:" + "a".repeat(64);
    const { client, summaries } = setup([
      json(200, { data: [], meta: { api_version: "v1", view: "v", request_id: "r-12345678", served_at: "x", limit: 50, next_cursor: null } }),
      json(503, fx("health:ready_not_ready").body),
      json(503, fx("alerts:deliveries_unavailable").body),
      new Response("x", { status: 200, headers: { ETag: `"${id}"` } }),
    ]);
    await client.history("alerts", { symbol: "META", cursor: "abc" });
    await client.ready();
    await client.deliveries(id).catch(() => undefined);
    await client.canonical("alerts", id);
    expect(summaries.map((s) => [s.route, s.status, s.note])).toEqual([
      ["/api/v1/alerts", 200, null],
      ["/health/ready", 503, "not_ready"],
      ["/api/v1/alerts/{id}/deliveries", 503, "capability_unavailable"],
      ["/api/v1/alerts/{id}/canonical", 200, null],
    ]);
    expect(JSON.stringify(summaries)).not.toMatch(/META|abc|aaaa|placeholder|Bearer/);
  });

  it("records the final failure once after the retries are exhausted", async () => {
    const { client, summaries } = setup([new TypeError("a"), new TypeError("b"), new TypeError("c")]);
    await expect(client.ready()).rejects.toMatchObject({ kind: "network" });
    expect(summaries).toHaveLength(1);
    expect(summaries[0]).toMatchObject({ outcome: "network_error", attempts: 3, status: null });
  });

  it("never lets a throwing diagnostics hook break a request", async () => {
    const client = createApiClient({
      onRequest: () => {
        throw new Error("hook");
      },
      fetchImpl: () => Promise.resolve(new Response(JSON.stringify({ status: "live" }), { status: 200 })),
    });
    await expect(client.live()).resolves.toMatchObject({ status: 200 });
  });
});

describe("readiness sampling", () => {
  it("records a 503 readiness answer as not ready with its checks", async () => {
    const body = fx("health:ready_not_ready").body;
    const init = createServices({
      fetchImpl: () => Promise.resolve(new Response(JSON.stringify(body), { status: 503 })),
      sleep: () => Promise.resolve(),
      now: () => 42,
    });
    await init.queryClient.query(readyQuery(init.services.client));
    const [sample] = init.services.diagnostics.getSnapshot().readiness;
    expect(sample?.requestId).toMatch(/^ui-[0-9a-f]{24}$/); // no echo: the id the client sent
    expect({ ...sample, requestId: null }).toEqual({
      at: 42,
      result: "not_ready",
      status: 503,
      checks: [
        { name: "settings", status: "pass" },
        { name: "artifact_root", status: "pass" },
        { name: "artifact_index", status: "pass" },
        { name: "receipt_root", status: "fail" },
      ],
      requestId: null,
    });
  });

  it("records an unreachable readiness check without a body", async () => {
    const init = createServices({ fetchImpl: () => Promise.reject(new TypeError("down")), sleep: () => Promise.resolve(), now: () => 7 });
    await init.queryClient.query(readyQuery(init.services.client)).catch(() => undefined);
    expect(init.services.diagnostics.getSnapshot().readiness[0]).toMatchObject({ at: 7, result: "unreachable", status: null, checks: [] });
  });
});
