import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { shortId } from "../../lib/artifactId";
import { UI_BUILD } from "../../lib/buildInfo";
import { fixture, type CapturedResponse } from "../fixtures";
import { apiHandlers, server } from "../msw";
import { renderApp } from "../render";

const MI_LATEST = (fixture("latest:market-intelligence").body as { data: { intelligence_id: string; timeframe_pattern: string } }).data;
const ALERT_LATEST = (fixture("latest:alerts").body as { data: { alert_id: string; alert_code: string } }).data;

function emptyHistory(): CapturedResponse {
  const head = fixture("history_head:alerts");
  const body = head.body as { meta: Record<string, unknown> };
  return { ...head, body: { data: [], meta: { ...body.meta, next_cursor: null } } };
}

describe("Overview", () => {
  it("shows live, ready, builds, latest MI and Alert, and has-data per kind from real responses", async () => {
    server.use(...apiHandlers());
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "Overview", level: 1 })).toBeInTheDocument();
    const api = screen.getByRole("region", { name: "MIAS API" });
    await waitFor(() => {
      const badges = Array.from(api.querySelectorAll(".status-badge"), (b) => b.textContent);
      expect(badges).toEqual(["● Live", "● Ready"]);
    });
    expect(within(api).getByText("phase13e-test")).toBeInTheDocument();
    expect(within(api).getByText(UI_BUILD)).toBeInTheDocument();

    const mi = screen.getByRole("region", { name: "Latest Market Intelligence" });
    expect(await within(mi).findByText(shortId(MI_LATEST.intelligence_id))).toBeInTheDocument();
    expect(within(mi).getByText(MI_LATEST.timeframe_pattern)).toBeInTheDocument();
    expect(within(mi).getByRole("link", { name: `Open ${MI_LATEST.intelligence_id}` })).toHaveAttribute("href", `/market-intelligence/${MI_LATEST.intelligence_id}`);
    expect(within(mi).getByText(/taken from the newest history entry/)).toBeInTheDocument();
    const alert = screen.getByRole("region", { name: "Latest Alert" });
    expect(await within(alert).findByText(shortId(ALERT_LATEST.alert_id))).toBeInTheDocument();
    expect(within(alert).getByText(ALERT_LATEST.alert_code)).toBeInTheDocument();

    const kinds = screen.getByRole("region", { name: "Artifacts by kind" });
    const rows = within(kinds).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(5);
    await waitFor(() => {
      for (const row of rows) expect(within(row).getByText("Yes")).toBeInTheDocument();
    });
    expect(screen.getByText(/Last refreshed/)).toBeInTheDocument();
    // No invented totals: the table has exactly kind / has data / newest as of.
    expect(within(kinds).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["Kind", "Has data", "Newest as of"]);
    expect(within(kinds).getByText(/Totals are not shown/)).toBeInTheDocument();
  });

  it("shows empty states, not errors, when a kind has no data", async () => {
    const empty = emptyHistory();
    server.use(
      ...apiHandlers({
        "history_head:market-intelligence": empty,
        "history_head:alerts": empty,
        "history_head:options-intelligence": empty,
      }),
    );
    renderApp("/");
    const mi = await screen.findByRole("region", { name: "Latest Market Intelligence" });
    expect(await within(mi).findByText("No market intelligence is available yet.")).toBeInTheDocument();
    const alert = screen.getByRole("region", { name: "Latest Alert" });
    expect(await within(alert).findByText("No alerts are available yet.")).toBeInTheDocument();
    const oiRow = screen.getByRole("rowheader", { name: "Options Intelligence" }).closest("tr");
    expect(oiRow).not.toBeNull();
    await waitFor(() => {
      expect(within(oiRow as HTMLElement).getByText("None yet")).toBeInTheDocument();
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("explains an ambiguous latest (409) with its request id", async () => {
    const ambiguous: CapturedResponse = {
      status: 409,
      headers: { "content-type": "application/json", "x-request-id": "srv-ambiguous-01" },
      body: { error: { code: "ambiguous_latest", message: "ambiguous", request_id: "srv-ambiguous-01" } },
    };
    server.use(...apiHandlers({ "latest:alerts": ambiguous }));
    renderApp("/");
    const alert = await screen.findByRole("region", { name: "Latest Alert" });
    expect(await within(alert).findByText("Ambiguous latest")).toBeInTheDocument();
    expect(within(alert).getByText("ambiguous_latest")).toBeInTheDocument();
    expect(within(alert).getByText("srv-ambiguous-01")).toBeInTheDocument();
  });

  it("shows loading states first", async () => {
    server.use(...apiHandlers());
    renderApp("/");
    expect((await screen.findAllByText("Loading…")).length).toBeGreaterThan(0);
  });
});

describe("placeholders and navigation", () => {
  it.each([
    ["/options-intelligence", "Options Intelligence", "Detailed view arrives in a later Phase 16 step"],
    ["/trade-setups", "Trade Setups", "Detailed view arrives in a later Phase 16 step"],
    ["/invalidation-checks", "Invalidation Checks", "Detailed view arrives in a later Phase 16 step"],
  ])("%s is a working placeholder", async (path, title, note) => {
    server.use(...apiHandlers());
    renderApp(path);
    expect(await screen.findByRole("heading", { name: title, level: 1 })).toBeInTheDocument();
    expect(screen.getByText(note)).toBeInTheDocument();
    expect(await screen.findByText("As of")).toBeInTheDocument();
  });

  it("shows the empty state on a placeholder with no data", async () => {
    server.use(...apiHandlers({ "history_head:trade-setups": emptyHistory() }));
    renderApp("/trade-setups");
    expect(await screen.findByText("No trade setups are available yet.")).toBeInTheDocument();
  });

  it("renders a 404 page for unknown routes", async () => {
    server.use(...apiHandlers());
    renderApp("/no-such-page");
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });

  it("is keyboard navigable: skip link first, then the shell controls and navigation", async () => {
    server.use(...apiHandlers());
    renderApp("/");
    await screen.findByRole("heading", { name: "Overview" });
    const user = userEvent.setup();
    (document.activeElement as HTMLElement | null)?.blur();
    await user.tab();
    expect(screen.getByRole("link", { name: "Skip to main content" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("radio", { name: /System/ })).toHaveFocus(); // the theme radio group is one tab stop
    await user.tab();
    expect(screen.getByRole("button", { name: "Sign out" })).toHaveFocus();
    await user.tab();
    const nav = screen.getByRole("navigation", { name: "Primary" });
    expect(within(nav).getByRole("link", { name: "Overview" })).toHaveFocus();
    expect(within(nav).getByRole("link", { name: "Overview" })).toHaveAttribute("aria-current", "page");
    await user.keyboard("{Tab}{Enter}");
    expect(await screen.findByRole("heading", { name: "Market Intelligence", level: 1 })).toBeInTheDocument();
  });
});
