/** MSW integration: the real client against realistic endpoints serving captured Phase 13 responses. */
import { beforeEach, describe, expect, it } from "vitest";
import { createApiClient } from "../../api/client";
import { REQUEST_ID_PATTERN } from "../../api/requestId";
import type { AlertView, ItemResponse } from "../../api/types";
import { api16c, fx, HISTORY_PARAMS, listBody, type Recorded } from "../api16c";
import { TEST_TOKEN } from "../fixtures";
import { relativeFetch, server } from "../msw";

let log: Recorded[];
const client = createApiClient({ fetchImpl: relativeFetch, sleep: () => Promise.resolve() });

beforeEach(() => {
  log = [];
  server.use(...api16c(log));
});

describe("history over MSW", () => {
  it("sends exactly the supported parameters and no Authorization header (nginx adds it)", async () => {
    await client.history("market-intelligence", { symbol: "META", asOfFrom: "2026-09-24T00:00:00Z", asOfTo: "2026-09-25T23:59:59Z", limit: 25 });
    const [call] = log;
    expect([...(call?.url.searchParams.keys() ?? [])]).toEqual(["symbol", "as_of_from", "as_of_to", "limit"]);
    expect([...(call?.url.searchParams.keys() ?? [])].every((k) => HISTORY_PARAMS.has(k))).toBe(true);
    expect(call?.authorization).toBeNull();
    expect(call?.url.toString()).not.toContain(TEST_TOKEN);
    expect(call?.requestId).toMatch(REQUEST_ID_PATTERN);
  });

  it("follows the backend cursor exactly", async () => {
    const page1 = listBody<AlertView>("alerts:page1");
    const cursor = page1.meta.next_cursor ?? "";
    const result = await client.history("alerts", { limit: 2, cursor });
    expect(log[0]?.url.searchParams.get("cursor")).toBe(cursor);
    expect(result.value.data.map((a) => a.alert_id)).toEqual(listBody<AlertView>("alerts:page2").data.map((a) => a.alert_id));
    expect(result.value.meta.next_cursor).toBeNull();
  });

  it("keeps latest to symbol only", async () => {
    await client.latest("alerts", "META");
    expect(`${log[0]?.url.pathname ?? ""}${log[0]?.url.search ?? ""}`).toBe("/api/v1/alerts/latest?symbol=META");
  });
});

describe("detail, canonical and deliveries over MSW", () => {
  it("reads the canonical artifact as text and captures the ETag", async () => {
    const id = (fx("alerts:detail:1").body as ItemResponse<AlertView>).data.alert_id;
    const result = await client.canonical("alerts", id);
    expect(result.value.text).toBe(fx("alerts:canonical:1").text);
    expect(result.value.etag).toBe(`"${id}"`);
    expect(result.value.cacheControl).toBe("private, max-age=31536000, immutable");
    expect(log[0]?.url.pathname).toBe(`/api/v1/alerts/${id}/canonical`);
    expect(log[0]?.url.search).toBe("");
  });

  it("returns the summary view for an id", async () => {
    const body = fx("market-intelligence:detail:2").body as ItemResponse<{ intelligence_id: string }>;
    const result = await client.detail("market-intelligence", body.data.intelligence_id);
    expect(result.value).toEqual(body);
  });

  it("maps deliveries 503 to dependency_unavailable with the request id, once", async () => {
    const id = (fx("alerts:detail:0").body as ItemResponse<AlertView>).data.alert_id;
    await expect(client.deliveries(id)).rejects.toMatchObject({ status: 503, code: "dependency_unavailable", requestId: "ui-000000000000000000000001" });
    expect(log).toHaveLength(1);
  });
});
