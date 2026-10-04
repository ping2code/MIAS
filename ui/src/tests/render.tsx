import { render } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router";
import { AppProviders, createServices, type ServicesInit } from "../app/providers";
import { AppRoutes } from "../app/AppRoutes";
import type { VersionView } from "../api/types";
import { createAuthGateway, type AuthGateway } from "../auth/oauth";
import { fixture } from "./fixtures";
import { relativeFetch } from "./msw";

export const VERSION = fixture("version").body as VersionView;

function LocationProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output data-testid="location">{location.pathname}</output>
      <output data-testid="search">{location.search}</output>
      <button
        type="button"
        data-testid="browser-back"
        onClick={() => {
          void navigate(-1);
        }}
      >
        browser back
      </button>
      <button
        type="button"
        data-testid="browser-forward"
        onClick={() => {
          void navigate(1);
        }}
      >
        browser forward
      </button>
    </>
  );
}

/** A real OAuth gateway against MSW whose full-page navigations are recorded instead of performed. */
export function testAuth(path = "/"): { auth: AuthGateway; navigations: string[] } {
  const navigations: string[] = [];
  const auth = createAuthGateway({
    fetchImpl: relativeFetch,
    navigate: (url) => {
      navigations.push(url);
    },
    currentLocation: () => path,
  });
  return { auth, navigations };
}

/**
 * Renders the whole app at `path` against MSW, with instant client backoff (no real 1 s / 3 s waits). The app runs
 * behind oauth-proxy (Hardening Task 8): there is no sign-in step in the app itself.
 */
export function renderApp(path: string) {
  const { auth, navigations } = testAuth(path);
  const init: ServicesInit = createServices({ fetchImpl: relativeFetch, sleep: () => Promise.resolve(), auth });
  const utils = render(
    <AppProviders init={init}>
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
        <Routes>
          <Route path="*" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>
    </AppProviders>,
  );
  return { ...utils, init, navigations };
}
