import { render } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router";
import { AppProviders, createServices, type ServicesInit } from "../app/providers";
import { AppRoutes } from "../app/AppRoutes";
import type { VersionView } from "../api/types";
import { fixture, TEST_TOKEN } from "./fixtures";
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

/** Renders the whole app at `path` against MSW, with instant client backoff (no real 1 s / 3 s waits). */
export function renderApp(path: string, { signedIn = true }: { signedIn?: boolean } = {}) {
  const init: ServicesInit = createServices({ fetchImpl: relativeFetch, sleep: () => Promise.resolve() });
  if (signedIn) init.services.session.authenticated(TEST_TOKEN, VERSION);
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
  return { ...utils, init };
}
