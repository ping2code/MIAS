import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ItemResponse, MarketIntelligenceView } from "../../api/types";
import { api16c, fx, type Recorded } from "../api16c";
import type { CapturedResponse } from "../fixtures";
import { server } from "../msw";
import { renderApp } from "../render";

const DETAIL = fx("market-intelligence:detail:0");
const CANONICAL = fx("market-intelligence:canonical:0");
const ID = (DETAIL.body as ItemResponse<MarketIntelligenceView>).data.intelligence_id;
const OPPOSED_ID = (fx("market-intelligence:detail:2").body as ItemResponse<MarketIntelligenceView>).data.intelligence_id;

type Lists = { directional_intervals: string[]; non_directional_intervals: string[]; unavailable_intervals: string[] };

/** The captured all_bullish artifact with a different recorded timeframe structure, and a summary that agrees. */
function variant(pattern: string, lists: Lists, extra: Record<string, unknown> = {}): Partial<Record<string, CapturedResponse>> {
  const artifact = JSON.parse(CANONICAL.text ?? "") as Record<string, Record<string, unknown>>;
  artifact.timeframe_structure = { ...artifact.timeframe_structure, pattern, ...lists, opposing_pairs: [], opposition_shape: "not_applicable", isolated_interval: null };
  Object.assign(artifact, extra);
  const body = structuredClone(DETAIL.body) as ItemResponse<MarketIntelligenceView>;
  body.data.timeframe_pattern = pattern;
  return {
    "market-intelligence:canonical:0": { ...CANONICAL, text: JSON.stringify(artifact) },
    "market-intelligence:detail:0": { ...DETAIL, body },
  };
}

/** The value of a labelled metadata row (its <dd> text). */
function row(region: HTMLElement, term: string): string {
  const dt = within(region).getByText(term, { selector: "dt" });
  return dt.closest(".meta-row")?.querySelector("dd")?.textContent ?? "";
}

async function readings(path = `/market-intelligence/${ID}`): Promise<string[][]> {
  renderApp(path);
  const region = await screen.findByRole("region", { name: "Timeframe structure" });
  const table = await within(region).findByRole("table", { name: /Recorded state of each timeframe/ });
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => [within(row).getByRole("rowheader").textContent, within(row).getByRole("cell").textContent]);
}

describe("Market Intelligence timeframe structure (from the full artifact)", () => {
  it("all bullish: keeps the Pattern badge and shows 1d, 1h, 5m bullish, with arrows as text", async () => {
    const log: Recorded[] = [];
    server.use(...api16c(log));
    expect(await readings()).toEqual([["1d", "▲Bullish"], ["1h", "▲Bullish"], ["5m", "▲Bullish"]]);
    const region = screen.getByRole("region", { name: "Timeframe structure" });
    expect(within(region).getByText("Pattern:").querySelector('[data-value="all_bullish"]')).not.toBeNull();
    expect(screen.getByRole("region", { name: "State" }).querySelector('[data-value="all_bullish"]')).not.toBeNull(); // preserved
    expect(row(region, "Opposing pairs")).toBe("None");
    expect(row(region, "Opposition shape")).toBe("None none");
    expect(log.some((r) => r.url.pathname === `/api/v1/market-intelligence/${ID}/canonical`)).toBe(true);
    expect(log.every((r) => r.authorization === null)).toBe(true);
  });

  it("all bearish: every interval bearish", async () => {
    server.use(...api16c([], variant("all_bearish", { directional_intervals: ["1d", "1h", "5m"], non_directional_intervals: [], unavailable_intervals: [] })));
    expect(await readings()).toEqual([["1d", "▼Bearish"], ["1h", "▼Bearish"], ["5m", "▼Bearish"]]);
  });

  it("opposed (captured): directional without a guessed direction, with the opposing pairs and shape", async () => {
    server.use(...api16c());
    expect(await readings(`/market-intelligence/${OPPOSED_ID}`)).toEqual([
      ["1d", "◆Directional (direction not recorded)"],
      ["1h", "◆Directional (direction not recorded)"],
      ["5m", "◆Directional (direction not recorded)"],
    ]);
    const region = screen.getByRole("region", { name: "Timeframe structure" });
    const pairs = within(region).getAllByRole("listitem").map((li) => li.textContent);
    expect(pairs).toEqual(["1h ↔ 5m", "1d ↔ 5m"]);
    expect(row(region, "Opposition shape")).toBe("Isolated interval isolated_interval");
    expect(row(region, "Isolated interval")).toBe("5m");
    expect(document.body.textContent).not.toMatch(/Bullish|Bearish/);
  });

  it("partially directional: the directional interval has no guessed direction; the rest are neutral", async () => {
    server.use(...api16c([], variant("partially_directional", { directional_intervals: ["1d"], non_directional_intervals: ["1h", "5m"], unavailable_intervals: [] })));
    expect(await readings()).toEqual([["1d", "◆Directional (direction not recorded)"], ["1h", "•Neutral"], ["5m", "•Neutral"]]);
  });

  it("all non-directional: every interval neutral", async () => {
    server.use(...api16c([], variant("all_non_directional", { directional_intervals: [], non_directional_intervals: ["1d", "1h", "5m"], unavailable_intervals: [] })));
    expect(await readings()).toEqual([["1d", "•Neutral"], ["1h", "•Neutral"], ["5m", "•Neutral"]]);
  });

  it("incomplete: an unavailable timeframe shows Unavailable", async () => {
    server.use(...api16c([], variant("incomplete", { directional_intervals: ["1d"], non_directional_intervals: ["1h"], unavailable_intervals: ["5m"] })));
    expect(await readings()).toEqual([["1d", "◆Directional (direction not recorded)"], ["1h", "•Neutral"], ["5m", "–Unavailable"]]);
    const region = screen.getByRole("region", { name: "Timeframe structure" });
    expect(row(region, "Unavailable intervals")).toBe("5m");
    expect(row(region, "Directional intervals")).toBe("1d");
    expect(row(region, "Non-directional intervals")).toBe("1h");
  });

  it("shows market context alignment, subjects of attention and conflicts, and transitions", async () => {
    server.use(...api16c());
    renderApp(`/market-intelligence/${ID}`);
    const alignment = await screen.findByRole("region", { name: "Market context alignment" });
    const table = await within(alignment).findByRole("table", { name: /alignment by interval/ });
    expect(within(table).getAllByRole("rowheader").map((h) => h.textContent)).toEqual(["1d", "1h", "5m"]);
    const artifact = JSON.parse(CANONICAL.text ?? "") as { attention: { subjects: string[] }[]; conflicts: { subjects: string[] }[] };
    const attention = screen.getByRole("region", { name: "Attention" });
    expect(within(attention).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["#", "Category", "Code", "Subjects"]);
    const firstRow = within(attention).getAllByRole("row")[1] as HTMLElement;
    expect(within(firstRow).getByText(artifact.attention[0]?.subjects.join(" · ") ?? "")).toBeInTheDocument();
    const conflicts = screen.getByRole("region", { name: "Conflict codes" });
    expect(within(conflicts).getAllByRole("listitem")[0]?.textContent).toContain(artifact.conflicts[0]?.subjects.join(" · ") ?? "");
    const transitions = screen.getByRole("region", { name: "Transitions" });
    expect(within(transitions).getByText("No previous synthesis was compared (current-only build).")).toBeInTheDocument();
    expect(within(transitions).getByText("No transitions recorded.")).toBeInTheDocument();
  });

  it("shows recorded transitions and their comparison", async () => {
    const extra = {
      comparison: { status: "comparable", reasons: [], elapsed_seconds: 1800, previous_ref: { synthesis_id: `sha256:${"a".repeat(64)}` } },
      transitions: [{ code: "pattern_changed", subjects: ["timeframe_alignment"], previous_pointers: [], current_pointers: [] }],
    };
    server.use(...api16c([], variant("all_bullish", { directional_intervals: ["1d", "1h", "5m"], non_directional_intervals: [], unavailable_intervals: [] }, extra)));
    renderApp(`/market-intelligence/${ID}`);
    const transitions = await screen.findByRole("region", { name: "Transitions" });
    expect(await within(transitions).findByText("pattern_changed")).toBeInTheDocument();
    expect(within(transitions).getByText("comparable")).toBeInTheDocument();
    expect(within(transitions).getByText("1800 s")).toBeInTheDocument();
  });

  it("falls back to the summary when the full artifact disagrees with it or cannot be read", async () => {
    const mismatch = variant("opposed", { directional_intervals: ["1d", "5m"], non_directional_intervals: ["1h"], unavailable_intervals: [] });
    const body = structuredClone(DETAIL.body) as ItemResponse<MarketIntelligenceView>; // summary still says all_bullish
    server.use(...api16c([], { ...mismatch, "market-intelligence:detail:0": { ...DETAIL, body } }));
    const { unmount } = renderApp(`/market-intelligence/${ID}`);
    const region = await screen.findByRole("region", { name: "Timeframe structure" });
    expect(await within(region).findByText(/does not match the summary/)).toBeInTheDocument();
    expect(within(region).queryByRole("table")).toBeNull();
    expect(screen.getByRole("region", { name: "State" }).querySelector('[data-value="all_bullish"]')).not.toBeNull();
    unmount();

    server.use(...api16c([], { "market-intelligence:canonical:0": { ...CANONICAL, text: "{}" } }));
    renderApp(`/market-intelligence/${ID}`);
    const again = await screen.findByRole("region", { name: "Timeframe structure" });
    expect(await within(again).findByText(/not in the recorded shape/)).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Attention" })).getAllByText("—").length).toBeGreaterThan(0);
  });
});
