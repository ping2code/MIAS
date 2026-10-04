import { describe, expect, it, vi } from "vitest";
import { createApiClient, type ClientOptions } from "../../api/client";
import { ApiError } from "../../api/errors";
import { REQUEST_ID_PATTERN } from "../../api/requestId";
import { fixture, TEST_TOKEN } from "../fixtures";

interface Call {
  url: string;
  init: RequestInit;
  headers: Headers;
}

type Reply = Response | Error | ((init: RequestInit) => Promise<Response>);

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
}

function envelope(status: number, code: string, requestId = "srv-req-0001"): Response {
  return jsonResponse(status, { error: { code, message: "safe message", request_id: requestId } }, { "X-Request-ID": requestId });
}

function setup(replies: Reply[], options: Partial<ClientOptions> = {}) {
  const calls: Call[] = [];
  const sleeps: number[] = [];
  const queue = [...replies];
  const fetchImpl = vi.fn((input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const requestInit = init ?? {};
    calls.push({ url: input instanceof Request ? input.url : input.toString(), init: requestInit, headers: new Headers(requestInit.headers) });
    const next = queue.shift();
    if (next === undefined) throw new Error("unexpected extra request");
    if (next instanceof Error) return Promise.reject(next);
    if (typeof next === "function") return next(requestInit);
    return Promise.resolve(next);
  });
  const onPossibleSessionLoss = vi.fn();
  const client = createApiClient({
    onPossibleSessionLoss,
    fetchImpl,
    sleep: (ms) => {
      sleeps.push(ms);
      return Promise.resolve();
    },
    ...options,
  });
  return { client, calls, sleeps, onPossibleSessionLoss, fetchImpl };
}

const VERSION = fixture("version").body;

describe("request construction", () => {
  it("uses relative same-origin URLs, sends no Authorization, and sends only same-origin cookies", async () => {
    const { client, calls } = setup([
      jsonResponse(200, { status: "live" }),
      jsonResponse(200, fixture("ready").body),
      jsonResponse(200, VERSION),
    ]);
    await client.live();
    await client.ready();
    await client.version();
    expect(calls.map((c) => c.url)).toEqual(["/health/live", "/health/ready", "/api/v1/version"]);
    for (const call of calls) {
      // Hardening Task 8: nginx injects the read token server-side; the browser never sends it.
      expect(call.headers.has("Authorization")).toBe(false);
      expect(call.url).not.toContain(TEST_TOKEN);
      expect(call.url.startsWith("/")).toBe(true);
      // The oauth-proxy session cookie goes to this origin only; redirects (an ended session) are never followed.
      expect(call.init.credentials).toBe("same-origin");
      expect(call.init.redirect).toBe("error");
      expect(call.init.method).toBe("GET");
    }
  });

  it("sends a fresh ui-<24 hex> X-Request-ID on every attempt and reports the echoed id", async () => {
    const { client, calls } = setup([
      new TypeError("network down"),
      jsonResponse(200, VERSION, { "X-Request-ID": "echoed-request-id-1" }),
    ]);
    const result = await client.version();
    const ids = calls.map((c) => c.headers.get("X-Request-ID"));
    expect(ids).toHaveLength(2);
    for (const id of ids) expect(id).toMatch(REQUEST_ID_PATTERN);
    expect(ids[0]).not.toBe(ids[1]);
    expect(result.requestId).toBe("echoed-request-id-1");
    expect(result.value).toEqual(VERSION);
  });

  it("encodes history parameters and keeps sha256 ids readable in paths", async () => {
    const head = fixture("history_head:market-intelligence").body;
    const detail = fixture("detail:market-intelligence").body as { data: { intelligence_id: string } };
    const { client, calls } = setup([jsonResponse(200, head), jsonResponse(200, detail)]);
    await client.history("market-intelligence", { symbol: "META", asOfFrom: "2026-09-24T00:00:00+00:00", limit: 1, cursor: "abc" });
    await client.detail("market-intelligence", detail.data.intelligence_id);
    expect(calls[0]?.url).toBe(
      "/api/v1/market-intelligence?symbol=META&as_of_from=2026-09-24T00%3A00%3A00%2B00%3A00&limit=1&cursor=abc",
    );
    expect(calls[1]?.url).toBe(`/api/v1/market-intelligence/${detail.data.intelligence_id}`);
  });

  it("has no token option at all: the same request is built whatever the caller passes", async () => {
    const { client, calls } = setup([jsonResponse(200, VERSION)]);
    await client.version();
    expect([...(calls[0]?.headers.keys() ?? [])].sort()).toEqual(["accept", "x-request-id"]);
  });
});

describe("responses", () => {
  it("treats readiness 503 as data, without retrying", async () => {
    const notReady = { status: "not_ready", checks: [{ name: "artifact_index", status: "fail" }] };
    const { client, calls } = setup([jsonResponse(503, notReady)]);
    const result = await client.ready();
    expect(result.status).toBe(503);
    expect(result.value).toEqual(notReady);
    expect(calls).toHaveLength(1);
  });

  it("returns canonical artifacts as the exact text with ETag and Cache-Control", async () => {
    const canonical = fixture("canonical:market-intelligence");
    const text = canonical.text ?? "";
    const { client } = setup([new Response(text, { status: 200, headers: canonical.headers })]);
    const id = (canonical.headers.etag ?? "").replaceAll('"', "");
    const result = await client.canonical("market-intelligence", id);
    expect(result.value.text).toBe(text);
    expect(result.value.etag).toBe(`"${id}"`);
    expect(result.value.cacheControl).toBe("private, max-age=31536000, immutable");
  });

  it("keeps canonical bytes that a JSON round-trip would change", async () => {
    const odd = '{"b":1.0,"a":"\\u00e9"}';
    const { client } = setup([new Response(odd, { status: 200, headers: { ETag: '"sha256:x"' } })]);
    const result = await client.canonical("alerts", "sha256:x");
    expect(result.value.text).toBe(odd);
  });

  it("maps the error envelope to a typed ApiError with code and request id", async () => {
    const nf = fixture("not_found");
    const { client } = setup([new Response(JSON.stringify(nf.body), { status: 404, headers: nf.headers })]);
    const error = await client.detail("market-intelligence", "sha256:" + "0".repeat(64)).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ kind: "http", status: 404, code: "not_found", requestId: "ui-000000000000000000000001" });
  });

  it("rejects responses that do not match the expected shape", async () => {
    const { client } = setup([jsonResponse(200, { data: [{ unexpected: true }], meta: {} })]);
    await expect(client.history("alerts")).rejects.toMatchObject({ kind: "invalid_response" });
  });

  it("rejects non-JSON success bodies as invalid_response", async () => {
    const { client } = setup([new Response("<html>proxy</html>", { status: 200 })]);
    await expect(client.version()).rejects.toMatchObject({ kind: "invalid_response" });
  });
});

describe("retry policy", () => {
  it.each([
    [400, "invalid_request"],
    [401, "unauthorized"],
    [404, "not_found"],
    [409, "ambiguous_latest"],
    [500, "artifact_invalid"],
  ])("never retries %i %s", async (status, code) => {
    const { client, calls, sleeps } = setup([envelope(status, code)]);
    await expect(client.version()).rejects.toMatchObject({ status, code });
    expect(calls).toHaveLength(1);
    expect(sleeps).toEqual([]);
  });

  it.each([
    [500, "internal"],
    [502, null],
    [503, "dependency_unavailable"],
    [504, null],
  ])("retries %i at most twice with 1 s then 3 s backoff", async (status, code) => {
    const reply = (): Response => (code ? envelope(status, code) : new Response("bad gateway", { status }));
    const { client, calls, sleeps } = setup([reply(), reply(), reply()]);
    await expect(client.version()).rejects.toMatchObject({ kind: "http", status });
    expect(calls).toHaveLength(3);
    expect(sleeps).toEqual([1000, 3000]);
  });

  it("retries network errors and succeeds when the API recovers", async () => {
    const { client, calls, sleeps } = setup([new TypeError("fetch failed"), jsonResponse(200, VERSION)]);
    await expect(client.version()).resolves.toMatchObject({ status: 200 });
    expect(calls).toHaveLength(2);
    expect(sleeps).toEqual([1000]);
  });

  it("times out each attempt after 10 s and retries timeouts", async () => {
    vi.useFakeTimers();
    try {
      const hang = (init: RequestInit): Promise<Response> =>
        new Promise((_, reject) => {
          init.signal?.addEventListener("abort", () => {
            reject(new DOMException("aborted", "AbortError"));
          });
        });
      const { client, calls, sleeps } = setup([hang, hang, hang]);
      const pending = client.version().catch((e: unknown) => e);
      await vi.advanceTimersByTimeAsync(9_999);
      expect(calls).toHaveLength(1);
      await vi.advanceTimersByTimeAsync(1);
      await vi.advanceTimersByTimeAsync(20_000);
      const error = await pending;
      expect(error).toMatchObject({ kind: "timeout" });
      expect(calls).toHaveLength(3);
      expect(sleeps).toEqual([1000, 3000]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("honours caller cancellation and does not retry it", async () => {
    const controller = new AbortController();
    const hang = (init: RequestInit): Promise<Response> =>
      new Promise((_, reject) => {
        init.signal?.addEventListener("abort", () => {
          reject(new DOMException("aborted", "AbortError"));
        });
      });
    const { client, calls } = setup([hang]);
    const pending = client.version({ signal: controller.signal }).catch((e: unknown) => e);
    controller.abort();
    expect(await pending).toMatchObject({ kind: "aborted" });
    expect(calls).toHaveLength(1);
  });
});

describe("possible session loss (oauth-proxy)", () => {
  it("is reported once for a final 401", async () => {
    const { client, onPossibleSessionLoss } = setup([envelope(401, "unauthorized")]);
    await expect(client.history("alerts")).rejects.toMatchObject({ status: 401 });
    expect(onPossibleSessionLoss).toHaveBeenCalledTimes(1);
  });

  it("is reported once for a final 403, also on health calls (the proxy guards every path)", async () => {
    const { client, onPossibleSessionLoss } = setup([jsonResponse(403, {})]);
    await expect(client.live()).rejects.toMatchObject({ status: 403 });
    expect(onPossibleSessionLoss).toHaveBeenCalledTimes(1);
  });

  it("is reported once after retries for a network error (the proxy's login redirect is refused)", async () => {
    const { client, onPossibleSessionLoss, calls } = setup([new TypeError("redirect"), new TypeError("redirect"), new TypeError("redirect")]);
    await expect(client.version()).rejects.toMatchObject({ kind: "network" });
    expect(calls).toHaveLength(3);
    expect(onPossibleSessionLoss).toHaveBeenCalledTimes(1);
  });

  it("is not reported for ordinary API errors", async () => {
    const { client, onPossibleSessionLoss } = setup([envelope(404, "not_found")]);
    await expect(client.detail("alerts", `sha256:${"a".repeat(64)}`)).rejects.toMatchObject({ status: 404 });
    expect(onPossibleSessionLoss).not.toHaveBeenCalled();
  });
});
