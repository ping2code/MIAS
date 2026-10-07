import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ListResponse, OptionsIntelligenceActivityView } from "../../api/types";
import { comparisonNote, countChange, ivPercent, percentChange, ratio } from "../../lib/optionsActivity";
import type { CapturedResponse } from "../fixtures";
import { ACTIVITY_FIXTURE, apiHandlers, server } from "../msw";
import { renderApp } from "../render";

const BODY = ACTIVITY_FIXTURE.body as ListResponse<OptionsIntelligenceActivityView>;
const [OTHER_SESSION, NVDA, COMPARABLE, FIRST] = BODY.data as [
  OptionsIntelligenceActivityView,
  OptionsIntelligenceActivityView,
  OptionsIntelligenceActivityView,
  OptionsIntelligenceActivityView,
];

function withRows(rows: OptionsIntelligenceActivityView[]): CapturedResponse {
  return { ...ACTIVITY_FIXTURE, body: { ...BODY, data: rows } };
}

async function cards(rows?: OptionsIntelligenceActivityView[]): Promise<HTMLElement[]> {
  server.use(...apiHandlers(rows ? { "options-intelligence:activity": withRows(rows) } : {}));
  renderApp("/options-intelligence");
  expect(await screen.findByRole("heading", { name: "Options Intelligence", level: 1 })).toBeInTheDocument();
  return screen.findAllByRole("article");
}

function metric(card: HTMLElement, term: string): string {
  const dt = within(card).getByText(term, { selector: "dt" });
  return dt.parentElement?.querySelector("dd")?.textContent ?? "";
}

describe("Options Intelligence activity (options-intelligence-activity-v1)", () => {
  it("renders one card per report from the activity endpoint, newest first, with Open links", async () => {
    const all = await cards();
    expect(all).toHaveLength(4);
    expect(all.map((c) => c.getAttribute("aria-label"))).toEqual(BODY.data.map((r) => `${r.symbol} options activity as of ${r.as_of}`));
    const link = within(all[2] as HTMLElement).getByRole("link", { name: /^Open META Options Intelligence/ });
    expect(link.getAttribute("href")).toBe(`/options-intelligence/${COMPARABLE.options_intelligence_id}`);
    expect(screen.queryByText("Detailed view arrives in a later Phase 16 step")).not.toBeInTheDocument();
  });

  it("shows absolute and same-session changes for a comparable report", async () => {
    const card = (await cards([COMPARABLE]))[0] as HTMLElement;
    expect(within(card).getByRole("heading", { level: 3 }).textContent).toBe(
      `META | ${String(COMPARABLE.contract_count)} contracts | 1 expiry date`,
    );
    expect(metric(card, "Call Volume")).toBe("400↑33.3%");
    expect(metric(card, "Put Volume")).toBe("200↑33.3%");
    expect(metric(card, "Put/Call Volume")).toBe("0.50");
    expect(metric(card, "Call Breadth")).toBe("2 strikes→0");
    expect(metric(card, "Put Breadth")).toBe("2 strikes↑1");
    expect(metric(card, "Activity Bias")).toBe("CALL");
    expect(metric(card, "15m Momentum")).toBe("CALL ↑");
    expect(metric(card, "Call Concentration")).toBe("Unavailable");
    expect(within(card).getByText(/Concentration needs an underlying price/)).toBeInTheDocument();
    expect(within(card).getByText(COMPARABLE.trend_summary)).toBeInTheDocument();
    expect(within(card).getByText(/Compared with the previous report of this session/)).toBeInTheDocument();
  });

  it("never fabricates changes without a same-session prior", async () => {
    const [first, other] = (await cards([FIRST, OTHER_SESSION])) as [HTMLElement, HTMLElement];
    expect(metric(first, "Call Volume")).toBe("300no prior same-session snapshot");
    expect(metric(first, "Volume > OI")).toBe(String(FIRST.volume_gt_oi_count));          // no change shown
    expect(metric(first, "15m Momentum")).toBe("Insufficient prior snapshot");
    expect(within(first).getByText("Insufficient prior same-session snapshot for intraday trend.")).toBeInTheDocument();
    expect(within(first).getByText(/No earlier report for this symbol/)).toBeInTheDocument();
    expect(within(other).getByText(/First report of this session/)).toBeInTheDocument();
    expect(metric(other, "Call Breadth")).toBe("1 strike");
  });

  it("shows unavailable volume, IV and ratio as unavailable, and a provider correction as such", async () => {
    const unavailable: OptionsIntelligenceActivityView = {
      ...NVDA, call_volume: null, put_volume: null, put_call_volume_ratio: null, iv_median: null,
      activity_bias: "UNAVAILABLE", momentum_15m: "UNAVAILABLE", trend_summary: "Current-session volume unavailable.",
    };
    const correction: OptionsIntelligenceActivityView = {
      ...COMPARABLE, options_intelligence_id: `sha256:${"c".repeat(64)}`, call_volume_change: -10,
      call_volume_change_pct: "-2.50", momentum_15m: "VOLUME_CORRECTION",
    };
    const [u, c] = (await cards([unavailable, correction])) as [HTMLElement, HTMLElement];
    expect(metric(u, "Call Volume")).toBe("Unavailable");
    expect(metric(u, "Put/Call Volume")).toBe("Unavailable");
    expect(metric(u, "IV Median")).toBe("Unavailable");
    expect(metric(u, "Activity Bias")).toBe("Unavailable");
    expect(metric(c, "Call Volume")).toBe("400↓2.5%");
    expect(metric(c, "15m Momentum")).toBe("Volume correction");
    expect(within(c).getByText(/provider correction/)).toBeInTheDocument();
  });

  it("uses descriptive words only", async () => {
    await cards();
    for (const banned of [/bullish/i, /bearish/i, /\bbuy\b/i, /\bsell\b/i, /recommend/i, /\btarget\b/i, /\bentry\b/i]) {
      expect(document.body.textContent).not.toMatch(banned);
    }
  });
});

describe("activity formatting", () => {
  it("formats changes, ratios and IV, and explains missing values", () => {
    expect(percentChange("18.00", null)).toBe("↑18.0%");
    expect(percentChange("-1.00", null)).toBe("↓1.0%");
    expect(percentChange("0.00", null)).toBe("→0.0%");
    expect(percentChange(null, "prior_zero")).toBe("no prior volume");
    expect(percentChange(null, "value_unavailable")).toBe("change unavailable");
    expect(percentChange(null, "not_comparable")).toBe("no prior same-session snapshot");
    expect(countChange(27)).toBe("↑27");
    expect(countChange(null)).toBeNull();
    expect(ratio("0.7108")).toBe("0.71");
    expect(ivPercent("0.314")).toBe("31.4%");
    expect(comparisonNote({ status: "prior_ambiguous", prior_options_intelligence_id: null, prior_as_of: null, session_date: "2026-10-06" })).toMatch(
      /two earlier reports share/,
    );
  });
});
