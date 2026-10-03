import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearPages } from "../../lib/cursorTrail";
import { COMPACT_NAV_QUERY } from "../../lib/useMediaQuery";
import { api16c, fx, type Recorded } from "../api16c";
import { fixture, TEST_TOKEN } from "../fixtures";
import { authorized, respond, server } from "../msw";
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

async function signIn(token = TEST_TOKEN) {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("Read token"), token);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("session ended", () => {
  it("shows the banner, preserves the safe return path with its filters, and returns there after sign-in", async () => {
    let first = true;
    server.use(
      http.get("*/api/v1/market-intelligence", ({ request }) => {
        if (first) {
          first = false;
          return respond(fixture("unauthorized"));
        }
        return respond(authorized(request) ? fx("market-intelligence:symbol") : fixture("unauthorized"));
      }),
      ...api16c(),
    );
    const { init } = renderApp("/market-intelligence?symbol=META");
    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent("Your MIAS session ended. Sign in again to continue.");
    expect(banner).toHaveTextContent("You will return to the page you were on.");
    expect(screen.getByRole("heading", { name: "Sign in to MIAS" })).toHaveFocus();
    expect(init.services.session.getToken()).toBeNull();
    expect(init.queryClient.getQueryCache().getAll().filter((q) => q.queryKey[0] === "api")).toHaveLength(0);
    await signIn();
    expect(await screen.findByRole("heading", { name: "Market Intelligence", level: 1 })).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/market-intelligence");
    expect(screen.getByTestId("search")).toHaveTextContent("?symbol=META");
  });

  it.each([
    ["/", "*/api/v1/:family"],
    ["/alerts", "*/api/v1/alerts"],
    [`/alerts/${"sha256:" + "0".repeat(64)}`, "*/api/v1/alerts/:id"],
    ["/status", "*/api/v1/version"],
  ])("a 401 on %s clears the token and returns to sign-in once", async (path, pattern) => {
    server.use(http.get(pattern, () => respond(fixture("unauthorized"))), ...api16c());
    const { init } = renderApp(path);
    expect(await screen.findByText("Your MIAS session ended. Sign in again to continue.")).toBeInTheDocument();
    expect(init.services.session.getToken()).toBeNull();
    expect(screen.getByTestId("location")).toHaveTextContent("/signin");
  });

  it("never offers an unsafe return path from history state", async () => {
    server.use(...api16c());
    // Simulate a crafted location state: /signin with an external "from".
    const { init } = renderApp("/signin", { signedIn: false });
    init.services.session.expire();
    await signIn();
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
    expect(screen.getByTestId("location").textContent).toBe("/");
  });

  it("loses the session on a reload (a fresh app instance starts signed out)", async () => {
    server.use(...api16c());
    const first = renderApp("/status");
    await screen.findByRole("heading", { name: "System Status" });
    first.unmount();
    renderApp("/status", { signedIn: false });
    expect(await screen.findByRole("heading", { name: "Sign in to MIAS" })).toBeInTheDocument();
  });

  it("never writes the token to storage or cookies during sign-in, use and theme changes", async () => {
    server.use(...api16c());
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    renderApp("/signin", { signedIn: false });
    await signIn();
    await screen.findByRole("heading", { name: "Overview" });
    await userEvent.setup().click(screen.getByRole("radio", { name: /Dark/ }));
    expect(setItem.mock.calls).toEqual([["mias-ui-theme", "dark"]]);
    expect(Object.keys(localStorage).map((k) => `${k}=${localStorage.getItem(k) ?? ""}`).join(";")).not.toContain(TEST_TOKEN);
    expect(sessionStorage.length).toBe(0);
    expect(document.cookie).toBe("");
  });

  it("sign-out clears the token, the query cache and the diagnostics", async () => {
    server.use(...api16c());
    const { init } = renderApp("/status");
    await screen.findByText("3 of 3 checks pass.");
    expect(init.services.diagnostics.getSnapshot().total).toBeGreaterThan(0);
    await userEvent.setup().click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByText("You have signed out.")).toBeInTheDocument();
    expect(init.services.session.getToken()).toBeNull();
    expect(init.queryClient.getQueryCache().getAll()).toHaveLength(0);
    expect(init.services.diagnostics.getSnapshot().total).toBe(0);
  });
});

describe("lock", () => {
  it("hides protected content, stops requests and focuses the lock screen", async () => {
    const log: Recorded[] = [];
    server.use(...api16c(log));
    const { init } = renderApp("/market-intelligence");
    await screen.findByRole("table", { name: /Market intelligence history/ });
    await userEvent.setup().click(screen.getByRole("button", { name: "Lock" }));
    expect(await screen.findByRole("heading", { name: "MIAS is locked" })).toHaveFocus();
    expect(screen.queryByRole("main")).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByRole("navigation", { name: "Primary" })).toBeNull();
    expect(init.queryClient.getQueryCache().getAll()).toHaveLength(0);
    expect(init.services.session.getToken()).toBeNull();
    const before = log.length;
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(log.length).toBe(before);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("stays locked for a different token without calling the API", async () => {
    const versionCalls = vi.fn();
    server.use(
      http.get("*/api/v1/version", ({ request }) => {
        versionCalls();
        return respond(authorized(request) ? fx("version") : fixture("unauthorized"));
      }),
      ...api16c(),
    );
    renderApp("/status");
    await screen.findByRole("heading", { name: "System Status" });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Lock" }));
    const calls = versionCalls.mock.calls.length;
    await user.type(screen.getByLabelText("Read token"), "wrong-token-wrong-token-wrong-token");
    await user.click(screen.getByRole("button", { name: "Unlock" }));
    expect(await screen.findByText("That token does not match this session. The dashboard stays locked.")).toBeInTheDocument();
    expect(screen.getByLabelText("Read token")).toHaveFocus();
    expect(screen.getByRole("heading", { name: "MIAS is locked" })).toBeInTheDocument();
    expect(versionCalls.mock.calls.length).toBe(calls);
  });

  it("unlocks with the same token after validating it, back on the same page", async () => {
    const log: Recorded[] = [];
    server.use(...api16c(log));
    renderApp("/alerts");
    await screen.findByRole("table", { name: /Alert history/ });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Lock" }));
    await user.type(screen.getByLabelText("Read token"), TEST_TOKEN);
    await user.click(screen.getByRole("button", { name: "Unlock" }));
    expect(await screen.findByRole("table", { name: /Alert history/ })).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/alerts");
  });

  it("goes to sign-in when the API rejects the token at unlock (401)", async () => {
    server.use(...api16c());
    const { init } = renderApp("/status");
    await screen.findByRole("heading", { name: "System Status" });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Lock" }));
    server.use(http.get("*/api/v1/version", () => respond(fixture("unauthorized"))));
    await user.type(screen.getByLabelText("Read token"), TEST_TOKEN);
    await user.click(screen.getByRole("button", { name: "Unlock" }));
    expect(await screen.findByText("Your MIAS session ended. Sign in again to continue.")).toBeInTheDocument();
    expect(init.services.session.getSnapshot()).toMatchObject({ status: "unauthenticated", locked: false });
  });

  it("stays locked when the API is unreachable at unlock", async () => {
    server.use(...api16c());
    renderApp("/status");
    await screen.findByRole("heading", { name: "System Status" });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Lock" }));
    server.use(http.get("*/api/v1/version", () => HttpResponse.error()));
    await user.type(screen.getByLabelText("Read token"), TEST_TOKEN);
    await user.click(screen.getByRole("button", { name: "Unlock" }));
    expect(await screen.findByText(/MIAS is temporarily unavailable. The dashboard stays locked/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "MIAS is locked" })).toBeInTheDocument();
  });

  it("signs out from the lock screen", async () => {
    server.use(...api16c());
    const { init } = renderApp("/status");
    await screen.findByRole("heading", { name: "System Status" });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Lock" }));
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByText("You have signed out.")).toBeInTheDocument();
    expect(init.services.session.matchesToken(TEST_TOKEN)).toBe(false);
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
