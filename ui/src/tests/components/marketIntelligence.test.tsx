import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ItemResponse, MarketIntelligenceView } from "../../api/types";
import { shortId } from "../../lib/artifactId";
import { clearPages } from "../../lib/cursorTrail";
import { api16c, fx, HISTORY_PARAMS, listBody, MI_ROWS, type Recorded } from "../api16c";
import { respond, server } from "../msw";
import { renderApp } from "../render";

afterEach(() => {
  clearPages();
});

const historyCalls = (log: Recorded[]) => log.filter((r) => /\/api\/v1\/market-intelligence$/.test(r.url.pathname));
const DETAIL = fx("market-intelligence:detail:0").body as ItemResponse<MarketIntelligenceView>;
const MI = DETAIL.data;

describe("Market Intelligence list", () => {
  it("shows skeleton rows while loading", async () => {
    server.use(...api16c());
    renderApp("/market-intelligence");
    expect(await screen.findByRole("heading", { name: "Market Intelligence", level: 1 })).toBeInTheDocument();
    expect(document.querySelector(".table-skeleton")).not.toBeNull();
  });

  it("lists the API's rows in backend order with literal domain values and short ids", async () => {
    const log: Recorded[] = [];
    server.use(...api16c(log));
    renderApp("/market-intelligence");
    const table = await screen.findByRole("table", { name: /Market intelligence history/ });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    MI_ROWS.forEach((mi, i) => {
      const row = rows[i] as HTMLElement;
      expect(within(row).getByText(shortId(mi.intelligence_id))).toBeInTheDocument();
      expect(within(row).getByText(mi.as_of.slice(0, 4), { exact: false })).toBeInTheDocument();
      expect(row.querySelector(`[data-value="${mi.timeframe_pattern}"]`)).not.toBeNull();
    });
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "Symbol", "As of", "Timeframe pattern", "Technical status", "Market context", "Artifact id", "Open",
    ]);
    const [first] = historyCalls(log);
    expect(first?.url.search).toBe("?limit=50");
    expect(first?.authorization).toMatch(/^Bearer /);
    expect(screen.getByText(/end of history/)).toBeInTheDocument();
  });

  it("shows the filtered empty state", async () => {
    server.use(...api16c());
    renderApp("/market-intelligence?symbol=ZZZZ");
    expect(await screen.findByText("No market intelligence is available for the selected filters.")).toBeInTheDocument();
  });

  it("applies filters with only supported parameters, as URL state, and rejects invalid input", async () => {
    const log: Recorded[] = [];
    server.use(...api16c(log));
    renderApp("/market-intelligence");
    await screen.findByRole("table", { name: /Market intelligence history/ });
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Symbol"), "1bad");
    await user.click(screen.getByRole("button", { name: "Apply" }));
    expect(await screen.findByText(/Use 1–10 characters/)).toBeInTheDocument();
    expect(historyCalls(log)).toHaveLength(1);

    await user.clear(screen.getByLabelText("Symbol"));
    await user.type(screen.getByLabelText("Symbol"), "meta");
    fireEvent.change(screen.getByLabelText("As of from (local)"), { target: { value: "2026-09-24T00:00:00" } });
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => {
      expect(historyCalls(log)).toHaveLength(2);
    });
    const sent = historyCalls(log)[1]?.url.searchParams;
    expect([...(sent?.keys() ?? [])].every((k) => HISTORY_PARAMS.has(k))).toBe(true);
    expect(sent?.get("symbol")).toBe("META");
    expect(sent?.get("as_of_from")).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
    expect(sent?.has("as_of_to")).toBe(false);
    expect(sent?.get("limit")).toBe("50");
    expect(screen.getByTestId("search").textContent).toMatch(/^\?symbol=META&as_of_from=/);
    expect(screen.getByText(/Window sent to the API/)).toBeInTheDocument();
    await waitFor(() => {
      expect(within(screen.getByRole("table", { name: /Market intelligence history/ })).getAllByRole("row")).toHaveLength(3);
    });
  });

  it("pages with the backend cursor exactly, and back again from memory", async () => {
    const log: Recorded[] = [];
    server.use(...api16c(log, { "market-intelligence:first": fx("market-intelligence:page1") }));
    renderApp("/market-intelligence");
    const table = await screen.findByRole("table", { name: /Market intelligence history/ });
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    const user = userEvent.setup();
    expect(screen.getByRole("button", { name: "‹ Previous" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Next ›" }));
    await waitFor(() => {
      expect(within(screen.getByRole("table", { name: /Market intelligence history/ })).getAllByRole("row")).toHaveLength(2);
    });
    const expected = listBody<unknown>("market-intelligence:page1").meta.next_cursor;
    const second = historyCalls(log).at(-1)?.url.searchParams;
    expect(second?.get("cursor")).toBe(expected);
    expect(second?.get("limit")).toBe("50");
    expect(screen.getByTestId("search").textContent).toBe(""); // the cursor never enters the URL
    expect(screen.getByRole("button", { name: "Next ›" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "‹ Previous" }));
    await waitFor(() => {
      expect(within(screen.getByRole("table", { name: /Market intelligence history/ })).getAllByRole("row")).toHaveLength(3);
    });
    // Browser back/forward retrace the page visits.
    await user.click(screen.getByTestId("browser-back"));
    await waitFor(() => {
      expect(within(screen.getByRole("table", { name: /Market intelligence history/ })).getAllByRole("row")).toHaveLength(2);
    });
    await user.click(screen.getByTestId("browser-back"));
    await waitFor(() => {
      expect(within(screen.getByRole("table", { name: /Market intelligence history/ })).getAllByRole("row")).toHaveLength(3);
    });
    expect(historyCalls(log).every((r) => [...r.url.searchParams.keys()].every((k) => HISTORY_PARAMS.has(k)))).toBe(true);
  });

  it("explains a rejected cursor and offers the first page", async () => {
    server.use(...api16c([], { "market-intelligence:first": fx("market-intelligence:page1"), "market-intelligence:page2": fx("market-intelligence:bad_cursor") }));
    renderApp("/market-intelligence");
    await screen.findByRole("table", { name: /Market intelligence history/ });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Next ›" }));
    expect(await screen.findByText(/did not accept this query or page cursor/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Back to the first page" }));
    expect(await screen.findByRole("table", { name: /Market intelligence history/ })).toBeInTheDocument();
  });
});

describe("Latest for symbol", () => {
  it("prompts for a symbol when none is filtered", async () => {
    server.use(...api16c());
    renderApp("/market-intelligence");
    expect(await screen.findByText("Filter by a symbol to look up its latest market intelligence.")).toBeInTheDocument();
  });

  it("shows the latest artifact for the filtered symbol", async () => {
    const log: Recorded[] = [];
    server.use(...api16c(log));
    renderApp("/market-intelligence?symbol=META");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Show latest for META" }));
    const latest = await screen.findByRole("region", { name: "Latest market intelligence for META" });
    const id = (fx("market-intelligence:latest").body as ItemResponse<MarketIntelligenceView>).data.intelligence_id;
    expect(await within(latest).findByText(shortId(id))).toBeInTheDocument();
    expect(log.find((r) => r.url.pathname.endsWith("/latest"))?.url.search).toBe("?symbol=META");
  });

  it("says none exists on 404", async () => {
    server.use(...api16c());
    renderApp("/market-intelligence?symbol=ZZZZ");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Show latest for ZZZZ" }));
    expect(await screen.findByText("No market intelligence exists for ZZZZ.")).toBeInTheDocument();
  });

  it("explains a 409 tie without choosing an artifact", async () => {
    server.use(...api16c([], { "market-intelligence:latest": fx("market-intelligence:latest_ambiguous") }));
    renderApp("/market-intelligence?symbol=META");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Show latest for META" }));
    const latest = await screen.findByRole("region", { name: "Latest market intelligence for META" });
    expect(await within(latest).findByText(/is ambiguous/)).toBeInTheDocument();
    expect(within(latest).getByText(/does not choose between tied artifacts/)).toBeInTheDocument();
    expect(within(latest).queryByRole("link", { name: /Open/ })).toBeNull();
  });
});

describe("Market Intelligence detail", () => {
  it("shows every field of the summary view in sections", async () => {
    server.use(...api16c());
    renderApp(`/market-intelligence/${MI.intelligence_id}`);
    expect(await screen.findByRole("heading", { name: `${MI.symbol} market intelligence`, level: 1 })).toHaveFocus();
    for (const title of ["Observation", "State", "Identity", "Attention", "Conflict codes", "Response"]) {
      expect(screen.getByRole("region", { name: title })).toBeInTheDocument();
    }
    const identity = screen.getByRole("region", { name: "Identity" });
    expect(within(identity).getByText(MI.intelligence_id)).toBeInTheDocument();
    expect(within(identity).getByText(MI.synthesis_id)).toBeInTheDocument();
    expect(within(identity).getByText(MI.rules_version)).toBeInTheDocument();
    const observation = screen.getByRole("region", { name: "Observation" });
    expect(within(observation).getByText(MI.as_of)).toBeInTheDocument(); // original ISO, exact
    const attention = screen.getByRole("region", { name: "Attention" });
    expect(within(attention).getAllByRole("row")).toHaveLength(MI.attention.length + 1);
    const conflicts = screen.getByRole("region", { name: "Conflict codes" });
    expect(within(conflicts).getAllByRole("listitem")).toHaveLength(MI.conflict_codes.length);
    expect(within(screen.getByRole("region", { name: "Response" })).getByText("market-intelligence-summary-v1")).toBeInTheDocument();
    for (const banned of [/\bbuy\b/i, /\bsell\b/i, /recommend/i, /score/i]) {
      expect(document.body.textContent).not.toMatch(banned);
    }
  });

  it("shows not found for an unknown id and makes no request for a malformed one", async () => {
    const log: Recorded[] = [];
    server.use(...api16c(log));
    const { unmount } = renderApp(`/market-intelligence/sha256:${"0".repeat(64)}`);
    expect(await screen.findByText(/No market intelligence with this id exists/)).toBeInTheDocument();
    unmount();
    log.length = 0;
    renderApp("/market-intelligence/not-an-id");
    expect(await screen.findByText(/is not a MIAS artifact id/)).toBeInTheDocument();
    expect(log.filter((r) => r.url.pathname.includes("not-an-id"))).toHaveLength(0);
  });

  it("shows the exact canonical text, its ETag, and copies and downloads the original", async () => {
    const log: Recorded[] = [];
    server.use(...api16c(log));
    const canonical = fx("market-intelligence:canonical:0");
    const text = canonical.text ?? "";
    const blobs: Blob[] = [];
    const createObjectURL = vi.fn((blob: Blob) => {
      blobs.push(blob);
      return "blob:test";
    });
    Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);

    renderApp(`/market-intelligence/${MI.intelligence_id}`);
    const user = userEvent.setup();
    // After userEvent.setup(), which installs its own clipboard stub.
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await user.click(await screen.findByRole("tab", { name: "Raw / Canonical" }));
    expect(screen.getByTestId("search").textContent).toBe("?tab=canonical");
    const pre = await screen.findByLabelText("Canonical text (exact)");
    expect(pre.textContent).toBe(text);
    expect(screen.getByText(`"${MI.intelligence_id}"`)).toBeInTheDocument();
    expect(screen.getByText("Matches artifact id")).toBeInTheDocument();
    expect(screen.getByText("private, max-age=31536000, immutable")).toBeInTheDocument();
    expect(log.find((r) => r.url.pathname.endsWith("/canonical"))?.url.pathname).toBe(
      `/api/v1/market-intelligence/${MI.intelligence_id}/canonical`,
    );

    await user.click(screen.getByRole("radio", { name: "Formatted (not canonical)" }));
    expect(screen.getByLabelText("Formatted copy (not canonical)").textContent).not.toBe(text);
    expect(screen.getByText(/Formatted view — a pretty-printed copy for reading/)).toHaveTextContent("It is not the canonical byte sequence");

    await user.click(screen.getByRole("button", { name: "Copy canonical text" }));
    expect(writeText).toHaveBeenCalledWith(text);
    await user.click(screen.getByRole("button", { name: /Download canonical/ }));
    expect(click).toHaveBeenCalled();
    expect(await blobs[0]?.text()).toBe(text);
  });

  it("switches tabs with the keyboard", async () => {
    server.use(...api16c());
    renderApp(`/market-intelligence/${MI.intelligence_id}`);
    const summary = await screen.findByRole("tab", { name: "Summary" });
    expect(summary).toHaveAttribute("aria-selected", "true");
    summary.focus();
    await userEvent.setup().keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Raw / Canonical" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Raw / Canonical" })).toHaveFocus();
    expect(await screen.findByLabelText("Canonical text (exact)")).toBeInTheDocument();
  });

  it("opens a row's detail from the list and returns with browser back", async () => {
    server.use(...api16c());
    renderApp("/market-intelligence");
    const table = await screen.findByRole("table", { name: /Market intelligence history/ });
    const first = MI_ROWS[0];
    if (!first) throw new Error("fixture");
    const user = userEvent.setup();
    await user.click(within(table).getAllByRole("link", { name: /^Open META market intelligence/ })[0] as HTMLElement);
    expect(await screen.findByRole("heading", { name: "META market intelligence" })).toBeInTheDocument();
    expect(screen.getByTestId("location").textContent).toBe(`/market-intelligence/${first.intelligence_id}`);
    await user.click(screen.getByTestId("browser-back"));
    expect(await screen.findByRole("table", { name: /Market intelligence history/ })).toBeInTheDocument();
  });

  it("does not blank loaded content while a background refresh fails", async () => {
    let calls = 0;
    server.use(
      http.get("*/api/v1/market-intelligence", () => {
        calls += 1;
        return calls === 1 ? respond(fx("market-intelligence:history")) : respond(fx("market-intelligence:bad_cursor"));
      }),
      ...api16c(),
    );
    const { init } = renderApp("/market-intelligence");
    await screen.findByRole("table", { name: /Market intelligence history/ });
    await init.queryClient.refetchQueries({ queryKey: ["api", "market-intelligence", "history"] });
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: /Market intelligence history/ })).toBeInTheDocument();
  });
});
