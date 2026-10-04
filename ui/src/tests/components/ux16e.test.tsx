import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearPages } from "../../lib/cursorTrail";
import { COMPACT_NAV_QUERY } from "../../lib/useMediaQuery";
import { api16c, fx, type Recorded } from "../api16c";
import { fixture, TEST_TOKEN } from "../fixtures";
import { oauthHandlers, respond, server } from "../msw";
import { renderApp } from "../render";

afterEach(() => {
  clearPages();
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
  Reflect.deleteProperty(window, "matchMedia");
});

function compactViewport(compact: boolean): void {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: (query: string) => ({
      matches: compact && query === COMPACT_NAV_QUERY,
      media: query,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }),
  });
}

describe("session ended (oauth-proxy, Hardening Task 8)", () => {
  it.each([
    ["/", "*/api/v1/:family"],
    ["/alerts", "*/api/v1/alerts"],
    [`/alerts/${"sha256:" + "0".repeat(64)}`, "*/api/v1/alerts/:id"],
    ["/status", "*/api/v1/version"],
  ])("a 401 on %s with an ended proxy session reloads into the OpenShift login once, back to the same page", async (path, pattern) => {
    server.use(http.get(pattern, () => respond(fixture("unauthorized"))), ...oauthHandlers("unauthenticated"), ...api16c());
    const { navigations } = renderApp(path);
    await waitFor(() => {
      expect(navigations).toEqual([path]);
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(navigations).toEqual([path]);                      // no reload loop, however many calls failed
  });

  it("keeps the filters of the current page when reloading into the login", async () => {
    server.use(http.get("*/api/v1/market-intelligence", () => respond(fixture("unauthorized"))), ...oauthHandlers("unauthenticated"), ...api16c());
    // The test gateway reports the in-app location the browser would reload.
    const { navigations } = renderApp("/market-intelligence?symbol=META");
    await waitFor(() => {
      expect(navigations).toEqual(["/market-intelligence?symbol=META"]);
    });
  });

  it("does not reload when the proxy session is still valid: the 401 is shown as an error instead", async () => {
    server.use(http.get("*/api/v1/alerts", () => respond(fixture("unauthorized"))), ...api16c());
    const { navigations } = renderApp("/alerts");
    expect(await screen.findByText(/Your MIAS session ended. Reload the page to sign in again./)).toBeInTheDocument();
    expect(navigations).toEqual([]);
  });

  it("does not reload on an outage (the proxy cannot be asked either)", async () => {
    server.use(http.get("*/api/v1/alerts", () => HttpResponse.error()), http.get("*/oauth/auth", () => HttpResponse.error()), ...api16c());
    const { navigations } = renderApp("/alerts");
    expect(await screen.findByText("MIAS is unreachable")).toBeInTheDocument();
    expect(navigations).toEqual([]);
  });

  it("has no sign-in, token or lock UI anywhere", async () => {
    server.use(...api16c());
    renderApp("/status");
    await screen.findByRole("heading", { name: "System Status" });
    expect(screen.queryByLabelText(/token/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /Lock|Unlock|Sign in/ })).toBeNull();
    expect(screen.queryByRole("heading", { name: /Sign in to MIAS|MIAS is locked/ })).toBeNull();
    expect(document.body.textContent).not.toMatch(/read token/i);
  });

  it("sends old /signin links to the Overview", async () => {
    server.use(...api16c());
    renderApp("/signin");
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
    expect(screen.getByTestId("location").textContent).toBe("/");
  });

  it("never writes anything but the theme to storage, and no cookies, during use and theme changes", async () => {
    server.use(...api16c());
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    renderApp("/");
    await screen.findByRole("heading", { name: "Overview" });
    await userEvent.setup().click(screen.getByRole("radio", { name: /Dark/ }));
    expect(setItem.mock.calls).toEqual([["mias-ui-theme", "dark"]]);
    expect(Object.keys(localStorage).map((k) => `${k}=${localStorage.getItem(k) ?? ""}`).join(";")).not.toContain(TEST_TOKEN);
    expect(sessionStorage.length).toBe(0);
    expect(document.cookie).toBe("");
  });

  it("never sends an Authorization header (nginx adds the read token server-side)", async () => {
    const log: Recorded[] = [];
    server.use(...api16c(log));
    renderApp("/alerts");
    await screen.findByRole("table", { name: /Alert history/ });
    expect(log.length).toBeGreaterThan(0);
    for (const entry of log) expect(entry.authorization).toBeNull();
  });

  it("sign-out clears the query cache and diagnostics, then ends the proxy session at /oauth/sign_out", async () => {
    server.use(...api16c());
    const { init, navigations } = renderApp("/status");
    await screen.findByText("3 of 3 checks pass.");
    expect(init.services.diagnostics.getSnapshot().total).toBeGreaterThan(0);
    const clear = vi.spyOn(init.queryClient, "clear");
    const reset = vi.spyOn(init.services.diagnostics, "reset");
    await userEvent.setup().click(screen.getByRole("button", { name: "Sign out" }));
    expect(navigations).toEqual(["/oauth/sign_out"]);
    expect(clear).toHaveBeenCalledTimes(1);
    expect(reset).toHaveBeenCalledTimes(1);
    // (The shell stays mounted in the test, so it refetches; a browser has already left for /oauth/sign_out.)
  });
});

describe("theme control", () => {
  it("offers System, Light and Dark as a labelled radio group, defaulting to System", async () => {
    server.use(...api16c());
    renderApp("/");
    const group = await screen.findByRole("group", { name: "Theme" });
    expect(within(group).getAllByRole("radio").map((r) => r.getAttribute("value"))).toEqual(["system", "light", "dark"]);
    expect(within(group).getByRole("radio", { name: /System/ })).toBeChecked();
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  it("applies Light and Dark, and System removes the override", async () => {
    server.use(...api16c());
    renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("radio", { name: /Light/ }));
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    await user.click(screen.getByRole("radio", { name: /Dark/ }));
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(localStorage.getItem("mias-ui-theme")).toBe("dark");
    await user.click(screen.getByRole("radio", { name: /System/ }));
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    expect(localStorage.length).toBe(0);
  });

  it("is keyboard operable with arrow keys", async () => {
    server.use(...api16c());
    renderApp("/");
    const system = await screen.findByRole("radio", { name: /System/ });
    system.focus();
    await userEvent.setup().keyboard("{ArrowRight}");
    expect(screen.getByRole("radio", { name: /Light/ })).toBeChecked();
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });

  it("restores a stored preference", async () => {
    localStorage.setItem("mias-ui-theme", "dark");
    server.use(...api16c());
    renderApp("/");
    expect(await screen.findByRole("radio", { name: /Dark/ })).toBeChecked();
  });
});

describe("navigation drawer", () => {
  beforeEach(() => {
    server.use(...api16c());
  });

  it("is absent on desktop, where the side navigation stays", async () => {
    compactViewport(false);
    renderApp("/");
    await screen.findByRole("heading", { name: "Overview" });
    expect(screen.queryByRole("button", { name: /Menu/ })).toBeNull();
    expect(screen.getByRole("navigation", { name: "Primary" })).toBeInTheDocument();
  });

  it("opens from the keyboard as a modal dialog, moves focus in, and Escape returns focus to the trigger", async () => {
    compactViewport(true);
    renderApp("/");
    await screen.findByRole("heading", { name: "Overview" });
    expect(screen.queryByRole("navigation", { name: "Primary" })).toBeNull();
    const menu = screen.getByRole("button", { name: /Menu/ });
    expect(menu).toHaveAttribute("aria-expanded", "false");
    menu.focus();
    const user = userEvent.setup();
    await user.keyboard("{Enter}");
    const dialog = screen.getByRole("dialog", { name: "Navigation" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(menu).toHaveAttribute("aria-expanded", "true");
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(document.querySelector("main")).toHaveAttribute("inert");
    // Tab cycles inside the drawer.
    for (let i = 0; i < 10; i += 1) await user.tab();
    expect(dialog.contains(document.activeElement)).toBe(true);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(menu).toHaveFocus();
    expect(document.querySelector("main")).not.toHaveAttribute("inert");
  });

  it("closes with the close button and returns focus", async () => {
    compactViewport(true);
    renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /Menu/ }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: /Close/ }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("button", { name: /Menu/ })).toHaveFocus();
  });

  it("closes when a destination is chosen, and the new page heading takes focus", async () => {
    compactViewport(true);
    renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /Menu/ }));
    await user.click(within(screen.getByRole("dialog")).getByRole("link", { name: "Alerts" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(await screen.findByRole("heading", { name: "Alerts", level: 1 })).toHaveFocus();
  });
});

describe("navigation drawer: current page", () => {
  it("choosing the page already shown closes the drawer and focuses its heading", async () => {
    server.use(...api16c());
    compactViewport(true);
    renderApp("/alerts");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /Menu/ }));
    await user.click(within(screen.getByRole("dialog")).getByRole("link", { name: "Alerts" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Alerts", level: 1 })).toHaveFocus();
    });
  });
});

describe("focus after retry", () => {
  it("moves focus to the region heading when Retry replaces the error panel", async () => {
    let failing = true;
    server.use(
      http.get("*/api/v1/market-intelligence", () =>
        failing
          ? respond({
              status: 500,
              headers: { "content-type": "application/json", "x-request-id": "srv-500-0000001" },
              body: { error: { code: "internal", message: "internal error", request_id: "srv-500-0000001" } },
            })
          : respond(fx("market-intelligence:history")),
      ),
      ...api16c(),
    );
    renderApp("/market-intelligence");
    const panel = await screen.findByRole("alert");
    expect(panel).toHaveTextContent("MIAS is temporarily unavailable.");
    expect(panel).toHaveTextContent("srv-500-0000001");
    failing = false;
    await userEvent.setup().click(within(panel).getByRole("button", { name: "Retry" }));
    expect(screen.getByRole("heading", { name: "History" })).toHaveFocus();
    expect(await screen.findByRole("table", { name: /Market intelligence history/ })).toBeInTheDocument();
  });

  it("offers no Retry for errors that retrying cannot fix", async () => {
    server.use(http.get("*/api/v1/market-intelligence", () => respond(fx("market-intelligence:bad_instant"))), ...api16c());
    renderApp("/market-intelligence");
    expect(await screen.findByText(/did not accept this query/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });
});
