import { afterEach, describe, expect, it, vi } from "vitest";
import { createApiClient } from "../../api/client";
import { ApiError } from "../../api/errors";
import { historyQuery, queryKeys } from "../../api/queries";
import { canonicalFileName, isArtifactId, shortId } from "../../lib/artifactId";
import { byteLength, canonicalBlob, copyText, formatted } from "../../lib/canonical";
import { clearPages, FIRST_PAGE, loadPage, nextPage, previousPage, savePage } from "../../lib/cursorTrail";
import { humanize, toneFor } from "../../lib/domain";
import {
  filtersFromSearch,
  filtersToSearch,
  formFromFilters,
  instantToLocalInput,
  localInputToInstant,
  validateForm,
} from "../../lib/filters";
import { isDeliveryCapabilityMissing } from "../../pages/alerts/AlertDeliveries";
import { fx } from "../api16c";
import { TEST_TOKEN } from "../fixtures";

const MI_ID = "sha256:a5363431c87f1ba691198f3cb9bf3ac625af9b769fb17f9795f5f24547257415";

describe("artifact ids", () => {
  it("shortens to the first 8 and last 4 hex characters", () => {
    expect(shortId(MI_ID)).toBe("a5363431…7415");
  });
  it("leaves anything that is not a content id unchanged", () => {
    expect(shortId("not-an-id")).toBe("not-an-id");
    expect(shortId("sha256:ABC")).toBe("sha256:ABC");
  });
  it("validates ids and names canonical downloads <64 hex>.json", () => {
    expect(isArtifactId(MI_ID)).toBe(true);
    expect(isArtifactId(MI_ID.toUpperCase())).toBe(false);
    expect(isArtifactId("sha256:abc")).toBe(false);
    expect(canonicalFileName(MI_ID)).toBe("a5363431c87f1ba691198f3cb9bf3ac625af9b769fb17f9795f5f24547257415.json");
  });
});

describe("filter serialization", () => {
  it("omits empty values, upper-cases the symbol and keeps the limit", () => {
    expect(validateForm({ symbol: " meta ", from: "", to: "", limit: 100 })).toEqual({
      filters: { symbol: "META", limit: 100 },
      errors: {},
    });
    expect(validateForm({ symbol: "", from: "", to: "", limit: 50 }).filters).toEqual({ limit: 50 });
  });

  it("rejects symbols the API would reject, and sends nothing", () => {
    for (const bad of ["1ABC", "TOOLONGSYMBOL", "ME TA", "ME$"]) {
      const result = validateForm({ symbol: bad, from: "", to: "", limit: 50 });
      expect(result.filters).toBeNull();
      expect(result.errors.symbol).toBeDefined();
    }
  });

  it("converts local date-times to RFC 3339 UTC instants with an explicit Z", () => {
    const instant = localInputToInstant("2026-09-24T10:30:15");
    expect(instant).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
    expect(new Date(instant ?? "").getTime()).toBe(new Date(2026, 8, 24, 10, 30, 15).getTime());
    expect(localInputToInstant("2026-09-24T10:30")).toMatch(/:00Z$/);
    expect(instantToLocalInput(instant ?? "")).toBe("2026-09-24T10:30:15");
  });

  it("never sends incomplete or inverted windows", () => {
    expect(validateForm({ symbol: "", from: "2026-09-24", to: "", limit: 50 }).errors.from).toBeDefined();
    const inverted = validateForm({ symbol: "", from: "2026-09-25T00:00", to: "2026-09-24T00:00", limit: 50 });
    expect(inverted.filters).toBeNull();
    expect(inverted.errors.to).toBeDefined();
  });

  it("reads only valid API values from the URL and writes them in a fixed order", () => {
    const search = new URLSearchParams(
      "limit=25&as_of_to=2026-09-25T23:59:59Z&symbol=META&as_of_from=2026-09-24T00:00:00%2B02:00&token=x&sort=asc",
    );
    const filters = filtersFromSearch(search);
    expect(filters).toEqual({ symbol: "META", asOfFrom: "2026-09-24T00:00:00+02:00", asOfTo: "2026-09-25T23:59:59Z", limit: 25 });
    expect(filtersToSearch(filters).toString()).toBe(
      "symbol=META&as_of_from=2026-09-24T00%3A00%3A00%2B02%3A00&as_of_to=2026-09-25T23%3A59%3A59Z&limit=25",
    );
    expect(filtersFromSearch(new URLSearchParams("symbol=meta&as_of_from=yesterday&limit=7"))).toEqual({ limit: 50 });
    expect(filtersToSearch({ limit: 50 }).toString()).toBe("");
    expect(formFromFilters({ limit: 50 })).toEqual({ symbol: "", from: "", to: "", limit: 50 });
  });
});

describe("cursor pagination state", () => {
  afterEach(() => {
    clearPages();
  });

  it("pushes the current cursor on next and pops it on previous", () => {
    const p2 = nextPage(FIRST_PAGE, "c2");
    const p3 = nextPage(p2, "c3");
    expect(p3).toEqual({ cursor: "c3", trail: [null, "c2"] });
    expect(previousPage(p3)).toEqual({ cursor: "c2", trail: [null] });
    expect(previousPage(p2)).toEqual(FIRST_PAGE);
    expect(previousPage(FIRST_PAGE)).toBeNull();
  });

  it("keeps states in memory under random keys and forgets them on clear", () => {
    const key = savePage(nextPage(FIRST_PAGE, "c2"));
    expect(key).toMatch(/^[0-9a-f]{16}$/);
    expect(loadPage(key).cursor).toBe("c2");
    expect(loadPage(undefined)).toBe(FIRST_PAGE);
    expect(loadPage("unknown")).toBe(FIRST_PAGE);
    clearPages();
    expect(loadPage(key)).toBe(FIRST_PAGE);
  });
});

describe("badges", () => {
  it("never gives market patterns or subjects a sentiment tone", () => {
    for (const p of ["all_bullish", "all_bearish", "opposed", "higher_aligned_5m_opposed"]) {
      expect(toneFor("timeframe_pattern", p)).toBe("neutral");
    }
    expect(toneFor("subject_kind", "setup")).toBe("neutral");
  });

  it("uses tones only for backend-defined system facts, with a neutral fallback", () => {
    expect(toneFor("technical_status", "available")).toBe("positive");
    expect(toneFor("technical_status", "unavailable")).toBe("changed");
    expect(toneFor("technical_status", "something_new")).toBe("neutral");
    expect(toneFor("alert_code", "market_pattern_changed")).toBe("changed");
    expect(toneFor("alert_code", "setup_invalidated")).toBe("changed");
    expect(toneFor("alert_code", "setup_available")).toBe("info");
    expect(toneFor("delivery_status", "delivered")).toBe("positive");
    expect(toneFor("delivery_status", "failed")).toBe("negative");
    expect(toneFor("delivery_status", "pending")).toBe("neutral");
    expect(toneFor("attention_category", "unknown_category")).toBe("neutral");
  });

  it("derives labels mechanically from the literal value", () => {
    expect(humanize("all_bullish")).toBe("All bullish");
    expect(humanize("market_pattern_changed")).toBe("Market pattern changed");
    expect(humanize("")).toBe("");
  });
});

describe("canonical copy and download", () => {
  const text = fx("market-intelligence:canonical:0").text ?? "";

  it("downloads exactly the original bytes", async () => {
    const blob = canonicalBlob(text);
    expect(blob.type).toBe("application/json");
    const bytes = Array.from(new Uint8Array(await blob.arrayBuffer()));
    expect(bytes).toEqual(Array.from(new TextEncoder().encode(text)));
    expect(blob.size).toBe(bytes.length);
    expect(byteLength(text)).toBe(new TextEncoder().encode(text).length);
  });

  it("copies exactly the original text", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    expect(await copyText(text)).toBe(true);
    expect(writeText).toHaveBeenCalledWith(text);
  });

  it("formats a separate copy, never the source", () => {
    const pretty = formatted(text);
    expect(pretty).not.toBe(text);
    expect(JSON.parse(pretty ?? "null")).toEqual(JSON.parse(text));
    expect(formatted("not json")).toBeNull();
  });
});

describe("deliveries and latest error mapping", () => {
  it("treats only 503 dependency_unavailable as the missing delivery capability", () => {
    expect(isDeliveryCapabilityMissing(new ApiError("http", { status: 503, code: "dependency_unavailable" }))).toBe(true);
    expect(isDeliveryCapabilityMissing(new ApiError("http", { status: 503, code: null }))).toBe(false);
    expect(isDeliveryCapabilityMissing(new ApiError("http", { status: 500, code: "internal" }))).toBe(false);
    expect(isDeliveryCapabilityMissing(new ApiError("network"))).toBe(false);
  });

  function client(replies: { status: number; body: unknown }[]) {
    const calls: string[] = [];
    const queue = [...replies];
    const c = createApiClient({
      sleep: () => Promise.resolve(),
      fetchImpl: (input) => {
        calls.push(input instanceof Request ? input.url : input.toString());
        const next = queue.shift();
        if (!next) return Promise.reject(new Error("extra request"));
        return Promise.resolve(new Response(JSON.stringify(next.body), { status: next.status }));
      },
    });
    return { c, calls };
  }

  it("does not retry the deliveries capability answer, but retries other 503s", async () => {
    const unavailable = fx("alerts:deliveries_unavailable");
    const a = client([{ status: 503, body: unavailable.body }]);
    await expect(a.c.deliveries(MI_ID)).rejects.toMatchObject({ status: 503, code: "dependency_unavailable" });
    expect(a.calls).toEqual([`/api/v1/alerts/${MI_ID}/deliveries`]);
    const generic = { error: { code: "internal", message: "x", request_id: "r-12345678" } };
    const b = client([{ status: 503, body: generic }, { status: 503, body: generic }, { status: 503, body: generic }]);
    await expect(b.c.history("alerts")).rejects.toMatchObject({ status: 503 });
    expect(b.calls).toHaveLength(3);
  });

  it("surfaces 409 ambiguous_latest from the real API without retrying or choosing", async () => {
    const tie = fx("market-intelligence:latest_ambiguous");
    const a = client([{ status: tie.status, body: tie.body }]);
    await expect(a.c.latest("market-intelligence", "META")).rejects.toMatchObject({ status: 409, code: "ambiguous_latest" });
    expect(a.calls).toEqual(["/api/v1/market-intelligence/latest?symbol=META"]);
  });
});

describe("query keys", () => {
  it("are deterministic and hold only kind, filters and cursor", () => {
    const c = createApiClient({});
    const a = historyQuery(c, "alerts", { symbol: "META", limit: 50 }, null).queryKey;
    const b = historyQuery(c, "alerts", { limit: 50, symbol: "META" }, null).queryKey;
    expect(JSON.stringify(a)).toBe(JSON.stringify(b));
    expect(a).toEqual(["api", "alerts", "history", { symbol: "META", asOfFrom: null, asOfTo: null, limit: 50, cursor: null }]);
    const text = JSON.stringify([a, queryKeys.deliveries(MI_ID), queryKeys.canonical("alerts", MI_ID)]);
    expect(text).not.toContain(TEST_TOKEN);
    expect(text).not.toMatch(/ui-[0-9a-f]{24}/);
  });

  it("polls only the first history page", () => {
    const c = createApiClient({});
    expect(historyQuery(c, "alerts", { limit: 50 }, null).refetchInterval).toBe(60_000);
    expect(historyQuery(c, "alerts", { limit: 50 }, "cursor").refetchInterval).toBe(false);
  });
});
