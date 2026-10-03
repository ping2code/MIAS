import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { NavLink, Outlet } from "react-router";
import { useServices, useSession } from "../app/context";
import { API_STATUS_LABEL } from "../lib/apiStatus";
import { UI_BUILD } from "../lib/buildInfo";
import { clearPages } from "../lib/cursorTrail";
import { COMPACT_NAV_QUERY, useMediaQuery } from "../lib/useMediaQuery";
import { useHealth } from "../pages/status/useHealth";
import { NavDrawer } from "./NavDrawer";
import { StatusBadge, type Health } from "./StatusBadge";
import { ThemeControl } from "./ThemeControl";

export const NAV_ITEMS: readonly { to: string; label: string }[] = [
  { to: "/", label: "Overview" },
  { to: "/market-intelligence", label: "Market Intelligence" },
  { to: "/alerts", label: "Alerts" },
  { to: "/options-intelligence", label: "Options Intelligence" },
  { to: "/trade-setups", label: "Trade Setups" },
  { to: "/invalidation-checks", label: "Invalidation Checks" },
  { to: "/status", label: "System Status" },
];

/**
 * The global API indicator: Checking, Ready, Not ready, Degraded or Offline, derived deterministically from the latest
 * liveness and readiness observations (lib/apiStatus.ts). "Offline" needs both health endpoints unreachable, each
 * after the client's own retries, so one transient failure never shows it.
 */
function ApiStatus() {
  const { status } = useHealth();
  const health: Health = status === "ready" ? "ok" : status === "checking" ? "unknown" : status === "offline" ? "down" : "degraded";
  return (
    <span aria-live="polite" data-api-status={status}>
      <StatusBadge health={health} text={`API: ${API_STATUS_LABEL[status]}`} />
    </span>
  );
}

export function AppShell() {
  const { session, diagnostics } = useServices();
  const queryClient = useQueryClient();
  const snapshot = useSession();
  const compact = useMediaQuery(COMPACT_NAV_QUERY);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const drawerVisible = compact && drawerOpen;

  const signOut = (): void => {
    session.signOut();
    queryClient.clear();
    clearPages();
    diagnostics.reset();
  };
  const lock = (): void => {
    // Unmounts the protected UI (RequireAuth shows the lock screen); cached API data is dropped as well.
    session.lock();
    void queryClient.cancelQueries();
    queryClient.clear();
  };
  const closeDrawer = ({ returnFocus }: { returnFocus: boolean }): void => {
    setDrawerOpen(false);
    if (returnFocus) menuButton.current?.focus();
  };

  return (
    <div className={`shell${compact ? " shell-compact" : ""}`}>
      <a className="skip-link" href="#main" inert={drawerVisible}>
        Skip to main content
      </a>
      <header className="topbar" inert={drawerVisible}>
        {compact ? (
          <button
            ref={menuButton}
            type="button"
            className="button button-quiet menu-button"
            aria-expanded={drawerVisible}
            aria-controls="nav-drawer"
            aria-haspopup="dialog"
            onClick={() => {
              setDrawerOpen(true);
            }}
          >
            <span aria-hidden="true">☰</span> Menu
          </button>
        ) : null}
        <span className="brand">MIAS Dashboard</span>
        <span className="topbar-status">
          <ApiStatus />
        </span>
        <span className="topbar-build">
          UI build <code>{UI_BUILD}</code>
          {snapshot.version ? (
            <>
              {" · "}API build <code>{snapshot.version.build}</code>
            </>
          ) : null}
        </span>
        <ThemeControl />
        <div className="topbar-actions">
          <button type="button" className="button button-quiet" onClick={lock} title="Hide the dashboard until the read token is re-entered">
            Lock
          </button>
          <button type="button" className="button button-quiet" onClick={signOut}>
            Sign out
          </button>
        </div>
      </header>
      {compact ? null : (
        <nav className="sidenav" aria-label="Primary">
          <ul>
            {NAV_ITEMS.map((item) => (
              <li key={item.to}>
                <NavLink to={item.to} end={item.to === "/"}>
                  {item.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
      )}
      <main id="main" className="main" tabIndex={-1} inert={drawerVisible}>
        <Outlet />
      </main>
      {drawerVisible ? <NavDrawer items={NAV_ITEMS} onClose={closeDrawer} triggerRef={menuButton} /> : null}
    </div>
  );
}
