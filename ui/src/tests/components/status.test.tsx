import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { MAX_REQUESTS } from "../../app/diagnostics";
import { UI_BUILD, UI_VERSION } from "../../lib/buildInfo";
import { fx } from "../api16c";
import { fixture } from "../fixtures";
import { authorized, backgroundHistoryHeads, respond, server } from "../msw";
import { renderApp } from "../render";

const READY = fx("health:ready");
const NOT_READY = fx("health:ready_not_ready");
const VERSION = fx("version");

function health({ live = "ok", ready = "ready" }: { live?: "ok" | "down" | "500" | "hang"; ready?: "ready" | "not_ready" | "down" | "hang" } = {}) {
  return [
    http.get("*/health/live", async () => {
      if (live === "down") return HttpResponse.error();
      if (live === "hang") await delay("infinite");
      if (live === "500") {
        return respond({
          status: 500,
          headers: { "content-type": "application/json", "x-request-id": "srv-live-500-0001" },
          body: { error: { code: "internal", message: "internal error", request_id: "srv-live-500-0001" } },
        });
      }
      return respond(fx("health:live"));
    }),
    http.get("*/health/ready", async () => {
      if (ready === "down") return HttpResponse.error();
      if (ready === "hang") await delay("infinite");
      return respond(ready === "not_ready" ? NOT_READY : READY);
    }),
    http.get("*/api/v1/version", ({ request }) => respond(authorized(request) ? VERSION : fixture("unauthorized"))),
    // Rendering "/" (the AppShell indicator tests) also runs the Overview's per-kind history heads.
    ...backgroundHistoryHeads(),
  ];
}

const tiles = () => within(screen.getByRole("region", { name: "Summary" }));
const shell = () => document.querySelector("[data-api-status]")?.getAttribute("data-api-status");

describe("System Status", () => {
  it("shows checking states while health is loading", async () => {
    server.use(...health({ live: "hang", ready: "hang" }));
    renderApp("/status");
    expect(await screen.findByRole("heading", { name: "System Status", level: 1 })).toHaveFocus();
    expect(tiles().getAllByText("Checking…")).toHaveLength(2);
    expect(screen.getByText("API: Checking")).toBeInTheDocument();
    expect(shell()).toBe("checking");
  });

  it("shows a healthy API: liveness and readiness separately, every check, builds and formats", async () => {
    server.use(...health());
    renderApp("/status");
    await waitFor(() => {
      expect(tiles().getByText("Ready")).toBeInTheDocument();
    });
    expect(tiles().getByText("Live")).toBeInTheDocument();
    const service = screen.getByRole("region", { name: "Service health" });
    expect(within(service).getByText("Live — the API process is running")).toBeInTheDocument();
    expect(within(service).getByText("Ready — the API can serve correctly")).toBeInTheDocument();
    expect(within(service).getByText("Ready (Healthy)")).toBeInTheDocument();
    const checks = screen.getByRole("region", { name: "Readiness checks" });
    expect(within(checks).getByText("3 of 3 checks pass.")).toBeInTheDocument();
    expect(within(checks).getAllByRole("rowheader").map((r) => r.textContent)).toEqual(["settings", "artifact_root", "artifact_index"]);
    const builds = screen.getByRole("region", { name: "Builds & runtime" });
    expect(await within(builds).findByText("phase13e-test")).toBeInTheDocument();
    expect(within(builds).getByText(UI_BUILD)).toBeInTheDocument();
    expect(within(builds).getByText(UI_VERSION)).toBeInTheDocument();
    expect(within(builds).getAllByRole("rowheader", { name: "options_intelligence" })).toHaveLength(2);
    expect(shell()).toBe("ready");
    expect(screen.getByText("API: Ready")).toBeInTheDocument();
  });

  it("renders readiness 503 as reachable but not ready, with mixed checks, not as a crash", async () => {
    server.use(...health({ ready: "not_ready" }));
    renderApp("/status");
    const checks = await screen.findByRole("region", { name: "Readiness checks" });
    expect(await within(checks).findByText(/3 of 4 checks pass — the API answered HTTP 503 \(reachable, not ready\)/)).toBeInTheDocument();
    const failing = within(checks).getByRole("rowheader", { name: "receipt_root" }).closest("tr") as HTMLElement;
    expect(failing).toHaveTextContent("Fail");
    expect(within(checks).getByRole("rowheader", { name: "settings" }).closest("tr")).toHaveTextContent("Pass");
    expect(within(checks).queryByRole("alert")).toBeNull();
    expect(screen.getByText("API reachable but not ready (HTTP 503)")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Connectivity" })).getByText("Reachable")).toBeInTheDocument();
    expect(shell()).toBe("not_ready");
    expect(screen.getByText("API: Not ready")).toBeInTheDocument();
  });

  it("distinguishes an unreachable API (offline) from not ready", async () => {
    server.use(...health({ live: "down", ready: "down" }));
    renderApp("/status");
    const service = await screen.findByRole("region", { name: "Service health" });
    await waitFor(() => {
      expect(within(service).getAllByText("API unreachable")).toHaveLength(2);
    });
    expect(within(service).getByText("Offline (Unavailable)")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Connectivity" })).getByText("Unreachable")).toBeInTheDocument();
    expect(shell()).toBe("offline");
    const connectivity = screen.getByRole("region", { name: "Connectivity" });
    expect(connectivity).toHaveTextContent(/Last network error.*GET \/health\/(live|ready)/);
  });

  it("reports a liveness failure precisely, with its request id", async () => {
    server.use(...health({ live: "500" }));
    renderApp("/status");
    expect(await screen.findByText("API liveness check failed (HTTP 500)")).toBeInTheDocument();
    const connectivity = screen.getByRole("region", { name: "Connectivity" });
    await waitFor(() => {
      expect(within(connectivity).getAllByText("srv-live-500-0001").length).toBeGreaterThan(0);
    });
    expect(shell()).toBe("degraded");
  });

  it("lists recent API activity with route templates, outcomes and copyable request ids", async () => {
    server.use(...health({ ready: "not_ready" }));
    renderApp("/status");
    const activity = await screen.findByRole("region", { name: "Recent API activity" });
    const table = await within(activity).findByRole("table");
    await waitFor(() => {
      expect(within(table).getAllByRole("rowheader").map((r) => r.textContent)).toEqual(
        expect.arrayContaining(["GET /health/live", "GET /health/ready", "GET /api/v1/version"]),
      );
    });
    const readyRow = within(table).getAllByRole("rowheader", { name: "GET /health/ready" })[0]?.closest("tr") as HTMLElement;
    expect(readyRow).toHaveTextContent("503");
    expect(readyRow).toHaveTextContent("Reported not ready");
    expect(readyRow).toHaveTextContent(/\d+ ms/);
    const versionRow = within(table).getAllByRole("rowheader", { name: "GET /api/v1/version" })[0]?.closest("tr") as HTMLElement;
    expect(versionRow).toHaveTextContent("Success");
    const id = within(versionRow).getByText(/^ui-[0-9a-f]{24}$/).textContent;
    const user = userEvent.setup();
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await user.click(within(versionRow).getByRole("button", { name: `Copy request id ${id}` }));
    expect(writeText).toHaveBeenCalledWith(id);
    expect(activity.textContent).toContain(`last ${String(MAX_REQUESTS)} API requests`);
    expect(activity.textContent).not.toMatch(/Bearer|placeholder-read-token|Authorization/);
  });

  it("filters problems, and shows the empty state after clearing", async () => {
    server.use(...health());
    renderApp("/status");
    const activity = await screen.findByRole("region", { name: "Recent API activity" });
    await within(activity).findByRole("table");
    const user = userEvent.setup();
    await user.click(within(activity).getByRole("checkbox", { name: "Problems only" }));
    expect(within(activity).getByText("No problems among the recent requests.")).toBeInTheDocument();
    await user.click(within(activity).getByRole("checkbox", { name: "Problems only" }));
    await user.click(within(activity).getByRole("button", { name: "Clear list" }));
    expect(within(activity).getByText("No API requests recorded in this tab yet.")).toBeInTheDocument();
  });

  it("marks a request that recovered after a retry", async () => {
    let calls = 0;
    server.use(
      http.get("*/health/live", () => {
        calls += 1;
        return calls === 1 ? HttpResponse.error() : respond(fx("health:live"));
      }),
      ...health(),
    );
    renderApp("/status");
    const activity = await screen.findByRole("region", { name: "Recent API activity" });
    expect(await within(activity).findByText("Recovered after retry")).toBeInTheDocument();
    expect(within(activity).queryByText("Network error")).toBeNull();
  });

  it("builds an in-memory readiness history, one observation per check", async () => {
    server.use(...health());
    renderApp("/status");
    const history = await screen.findByRole("region", { name: "Readiness history" });
    await waitFor(() => {
      expect(within(history).getAllByRole("listitem").length).toBeGreaterThan(0);
    });
    const before = within(history).getAllByRole("listitem").length;
    server.use(...health({ ready: "not_ready" }));
    await userEvent.setup().click(screen.getByRole("button", { name: "Refresh now" }));
    await waitFor(() => {
      expect(within(history).getAllByRole("listitem")).toHaveLength(before + 1);
    });
    const strip = within(history).getByRole("list", { name: /Readiness, oldest to newest/ });
    expect(within(strip).getAllByRole("listitem").at(-1)).toHaveTextContent("Not ready");
    const rows = within(within(history).getByRole("table")).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("receipt_root");
    expect(rows[0]).toHaveTextContent("503");
  });

  it("points to OpenShift monitoring without links to infrastructure", async () => {
    server.use(...health());
    renderApp("/status");
    const guidance = await screen.findByRole("region", { name: "Operational guidance" });
    expect(guidance).toHaveTextContent("Deep metrics and telemetry are available in OpenShift monitoring");
    expect(within(guidance).queryAllByRole("link")).toHaveLength(0);
  });
});

describe("AppShell API indicator", () => {
  it.each([
    [{}, "API: Ready", "ready"],
    [{ ready: "not_ready" as const }, "API: Not ready", "not_ready"],
    [{ live: "down" as const, ready: "down" as const }, "API: Offline", "offline"],
    [{ live: "hang" as const, ready: "hang" as const }, "API: Checking", "checking"],
  ])("%o → %s", async (opts, label, status) => {
    server.use(...health(opts));
    renderApp("/");
    expect(await screen.findByText(label)).toBeInTheDocument();
    expect(shell()).toBe(status);
  });
});
