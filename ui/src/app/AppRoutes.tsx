import { Route, Routes } from "react-router";
import { RequireAuth } from "../auth/RequireAuth";
import { SignInPage } from "../auth/SignInPage";
import { AppShell } from "../components/AppShell";
import { NotFoundPage } from "../pages/NotFoundPage";
import { OverviewPage } from "../pages/OverviewPage";
import { PlaceholderPage } from "../pages/PlaceholderPage";
import { StatusPage } from "../pages/StatusPage";

const PHASE_16C = "Detailed view arrives in Phase 16C";
const LATER = "Detailed view arrives in a later Phase 16 step";

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/signin" element={<SignInPage />} />
      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route index element={<OverviewPage />} />
        <Route path="market-intelligence" element={<PlaceholderPage family="market-intelligence" note={PHASE_16C} />} />
        <Route path="alerts" element={<PlaceholderPage family="alerts" note={PHASE_16C} />} />
        <Route path="options-intelligence" element={<PlaceholderPage family="options-intelligence" note={LATER} />} />
        <Route path="trade-setups" element={<PlaceholderPage family="trade-setups" note={LATER} />} />
        <Route path="invalidation-checks" element={<PlaceholderPage family="invalidation-checks" note={LATER} />} />
        <Route path="status" element={<StatusPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
