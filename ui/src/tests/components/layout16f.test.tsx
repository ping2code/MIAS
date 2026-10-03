/**
 * Phase 16F live QA found page-level horizontal scroll on phones (390 px) from tables that were not inside a
 * horizontally scrollable wrapper. Every data table must sit in `.table-wrap` (or be a stacked `.table-data`).
 */
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { api16c } from "../api16c";
import { server } from "../msw";
import { renderApp } from "../render";

function unwrappedTables(): string[] {
  return Array.from(document.querySelectorAll("main table"))
    .filter((t) => t.closest(".table-wrap") === null)
    .map((t) => t.querySelector("caption")?.textContent ?? t.outerHTML.slice(0, 80));
}

describe("tables never widen the page", () => {
  it.each([
    ["/", "Overview", "Artifacts by kind"],
    ["/status", "System Status", "3 of 3 checks pass."],
    ["/market-intelligence", "Market Intelligence", "History"],
    ["/alerts", "Alerts", "History"],
  ])("every table on %s is inside a scroll wrapper", async (path, heading, ready) => {
    server.use(...api16c());
    renderApp(path);
    await screen.findByRole("heading", { name: heading, level: 1 });
    await screen.findByText(ready);
    await screen.findAllByRole("table");
    expect(unwrappedTables()).toEqual([]);
  });
});
