import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import type { AlertView, ItemResponse } from "../../api/types";
import { shortId } from "../../lib/artifactId";
import { clearPages } from "../../lib/cursorTrail";
import { ALERT_ROWS, api16c, fx, type Recorded } from "../api16c";
import { server } from "../msw";
import { renderApp } from "../render";

afterEach(() => {
  clearPages();
});

const detail = (i: number) => (fx(`alerts:detail:${String(i)}`).body as ItemResponse<AlertView>).data;
const SETUP = detail(0); // setup_available, no transition
const CHANGED = detail(2); // market_pattern_changed, with a transition and market_intelligence refs

describe("Alerts list", () => {
  it("shows skeleton rows while loading", async () => {
    server.use(...api16c());
    renderApp("/alerts");
    expect(await screen.findByRole("heading", { name: "Alerts", level: 1 })).toBeInTheDocument();
    expect(document.querySelector(".table-skeleton")).not.toBeNull();
  });

  it("lists alerts with code, symbol, as_of, subject, transition, sources and id", async () => {
    server.use(...api16c());
    renderApp("/alerts");
    const table = await screen.findByRole("table", { name: /Alert history/ });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(ALERT_ROWS.length);
    ALERT_ROWS.forEach((alert, i) => {
      const row = rows[i] as HTMLElement;
      expect(row.querySelector(`[data-value="${alert.alert_code}"]`)).not.toBeNull();
      expect(within(row).getByText(shortId(alert.alert_id))).toBeInTheDocument();
    });
    const changedRow = rows.find((r) => r.querySelector('[data-value="market_pattern_changed"]')) as HTMLElement;
    expect(within(changedRow).getByText("all_bullish")).toBeInTheDocument();
    expect(within(changedRow).getByText("opposed")).toBeInTheDocument();
    // The history view has no delivery state, and none is shown.
    expect(table.textContent).not.toMatch(/delivered|pending|failed/i);
  });

  it("shows the filtered empty state", async () => {
    server.use(...api16c());
    renderApp("/alerts?symbol=ZZZZ");
    expect(await screen.findByText("No alerts are available for the selected filters.")).toBeInTheDocument();
  });

  it("filters by window with only supported parameters", async () => {
    const log: Recorded[] = [];
    server.use(...api16c(log));
    renderApp("/alerts?as_of_from=2026-09-24T00:00:00Z&as_of_to=2026-09-25T23:59:59Z");
    const table = await screen.findByRole("table", { name: /Alert history/ });
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    const call = log.find((r) => r.url.pathname === "/api/v1/alerts");
    expect(call?.url.search).toBe("?as_of_from=2026-09-24T00%3A00%3A00Z&as_of_to=2026-09-25T23%3A59%3A59Z&limit=50");
  });
});

describe("Alert detail", () => {
  it("shows the event, facts verbatim, source references and identity", async () => {
    server.use(...api16c());
    renderApp(`/alerts/${CHANGED.alert_id}`);
    expect(await screen.findByRole("heading", { name: "Market pattern changed · META", level: 1 })).toHaveFocus();
    const event = screen.getByRole("region", { name: "Event" });
    expect(within(event).getByText(CHANGED.as_of)).toBeInTheDocument();
    expect(within(event).getByText("all_bullish")).toBeInTheDocument();
    const facts = screen.getByRole("region", { name: "Facts" });
    for (const [key, value] of Object.entries(CHANGED.facts)) {
      expect(within(facts).getByText(key)).toBeInTheDocument();
      expect(within(facts).getByText(String(value))).toBeInTheDocument();
    }
    const refs = screen.getByRole("region", { name: "Source references" });
    const links = within(refs).getAllByRole("link");
    expect(links.map((l) => l.getAttribute("href"))).toEqual(
      CHANGED.source_refs.map((r) => `/market-intelligence/${r.id}`),
    );
    const identity = screen.getByRole("region", { name: "Identity" });
    expect(within(identity).getByText(CHANGED.alert_id)).toBeInTheDocument();
    expect(within(identity).getByText("None")).toBeInTheDocument(); // assessment_id is null
  });

  it("states that deliveries are unavailable in this deployment (503), neutrally and without inventing a status", async () => {
    const log: Recorded[] = [];
    server.use(...api16c(log));
    renderApp(`/alerts/${SETUP.alert_id}`);
    const deliveries = await screen.findByRole("region", { name: "Deliveries" });
    expect(await within(deliveries).findByText("Delivery information is not available in this deployment.")).toBeInTheDocument();
    expect(within(deliveries).queryByRole("alert")).toBeNull();
    expect(deliveries.querySelector(".state-error")).toBeNull();
    expect(deliveries.textContent).not.toMatch(/\b(delivered|pending|failed)\b/i);
    expect(log.filter((r) => r.url.pathname.endsWith("/deliveries"))).toHaveLength(1); // not retried
  });

  it("shows only the receipts the API returns when a receipt store exists", async () => {
    server.use(...api16c([], { "alerts:deliveries_unavailable": fx("alerts:deliveries") }));
    renderApp(`/alerts/${SETUP.alert_id}`);
    const deliveries = await screen.findByRole("region", { name: "Deliveries" });
    const table = await within(deliveries).findByRole("table");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Failed");
    expect(rows[0]).toHaveTextContent("timeout");
    expect(rows[1]).toHaveTextContent("Delivered");
    expect(rows[1]).toHaveTextContent("777");
  });

  it("says when no receipts are recorded", async () => {
    server.use(...api16c([], { "alerts:deliveries_unavailable": fx("alerts:deliveries_empty") }));
    renderApp(`/alerts/${SETUP.alert_id}`);
    expect(await screen.findByText("No delivery receipts are recorded for this alert.")).toBeInTheDocument();
  });

  it("shows the exact canonical text and ETag", async () => {
    server.use(...api16c());
    renderApp(`/alerts/${SETUP.alert_id}?tab=canonical`);
    const pre = await screen.findByLabelText("Canonical text (exact)");
    expect(pre.textContent).toBe(fx("alerts:canonical:0").text);
    expect(screen.getByText(`"${SETUP.alert_id}"`)).toBeInTheDocument();
    expect(screen.getByText("Matches artifact id")).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("tab", { name: "Summary" }));
    await waitFor(() => {
      expect(screen.getByTestId("search").textContent).toBe("");
    });
    expect(screen.getByRole("region", { name: "Facts" })).toBeInTheDocument();
  });

  it("shows not found for an unknown alert", async () => {
    server.use(...api16c());
    renderApp(`/alerts/sha256:${"0".repeat(64)}`);
    expect(await screen.findByText(/No alert with this id exists/)).toBeInTheDocument();
  });

  it("navigates from an alert's source reference to the market intelligence detail", async () => {
    server.use(...api16c());
    renderApp(`/alerts/${CHANGED.alert_id}`);
    const refs = await screen.findByRole("region", { name: "Source references" });
    const previous = CHANGED.source_refs.find((r) => r.role === "previous");
    if (!previous) throw new Error("fixture");
    await userEvent.setup().click(within(refs).getByRole("link", { name: `Open ${previous.id}` }));
    expect(await screen.findByRole("heading", { name: "META market intelligence" })).toBeInTheDocument();
  });
});
