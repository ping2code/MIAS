import { useQuery, useQueryClient } from "@tanstack/react-query";
import { NavLink, Outlet } from "react-router";
import { liveQuery, readyQuery } from "../api/queries";
import { useApiClient, useServices, useSession } from "../app/context";
import { UI_BUILD } from "../lib/buildInfo";
import { clearPages } from "../lib/cursorTrail";
import { StatusBadge, type Health } from "./StatusBadge";

export const NAV_ITEMS: readonly { to: string; label: string }[] = [
  { to: "/", label: "Overview" },
  { to: "/market-intelligence", label: "Market Intelligence" },
  { to: "/alerts", label: "Alerts" },
  { to: "/options-intelligence", label: "Options Intelligence" },
  { to: "/trade-setups", label: "Trade Setups" },
  { to: "/invalidation-checks", label: "Invalidation Checks" },
  { to: "/status", label: "System Status" },
];

function ApiStatus() {
  const client = useApiClient();
  const live = useQuery(liveQuery(client));
  const ready = useQuery(readyQuery(client));
  let health: Health = "unknown";
  let text = "API status unknown";
  if (live.isError) {
    health = "down";
    text = "API unreachable";
  } else if (ready.data) {
    health = ready.data.value.status === "ready" ? "ok" : "degraded";
    text = ready.data.value.status === "ready" ? "API ready" : "API not ready";
  } else if (ready.isError) {
    health = "down";
    text = "API readiness unavailable";
  }
  return (
    <span aria-live="polite">
      <StatusBadge health={health} text={text} />
    </span>
  );
}

export function AppShell() {
  const { session } = useServices();
  const queryClient = useQueryClient();
  const snapshot = useSession();
  const signOut = (): void => {
    session.signOut();
    queryClient.clear();
    clearPages();
  };
  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to main content
      </a>
      <header className="topbar">
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
        <button type="button" className="button button-quiet" onClick={signOut}>
          Sign out
        </button>
      </header>
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
      <main id="main" className="main" tabIndex={-1}>
        <Outlet />
      </main>
    </div>
  );
}
