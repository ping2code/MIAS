import { Navigate, Route, Routes } from "react-router";
import { AppShell } from "../components/AppShell";
import { NotFoundPage } from "../pages/NotFoundPage";
import { OverviewPage } from "../pages/OverviewPage";
import { AlertDetailPage } from "../pages/alerts/AlertDetailPage";
import { AlertListPage } from "../pages/alerts/AlertListPage";
import { MarketIntelligenceDetailPage } from "../pages/market-intelligence/MarketIntelligenceDetailPage";
import { MarketIntelligenceListPage } from "../pages/market-intelligence/MarketIntelligenceListPage";
import { PlaceholderPage } from "../pages/PlaceholderPage";
import { StatusPage } from "../pages/StatusPage";

const LATER = "Detailed view arrives in a later Phase 16 step";

export function AppRoutes() {
  return (
    <Routes>
      {/* Sign-in is OpenShift OAuth in front of the whole app (Hardening Task 8); old /signin links land on Overview. */}
      <Route path="/signin" element={<Navigate to="/" replace />} />
      <Route element={<AppShell />}>
        <Route index element={<OverviewPage />} />
        <Route path="market-intelligence" element={<MarketIntelligenceListPage />} />
        <Route path="market-intelligence/:id" element={<MarketIntelligenceDetailPage />} />
        <Route path="alerts" element={<AlertListPage />} />
        <Route path="alerts/:id" element={<AlertDetailPage />} />
        <Route path="options-intelligence" element={<PlaceholderPage family="options-intelligence" note={LATER} />} />
        <Route path="trade-setups" element={<PlaceholderPage family="trade-setups" note={LATER} />} />
        <Route path="invalidation-checks" element={<PlaceholderPage family="invalidation-checks" note={LATER} />} />
        <Route path="status" element={<StatusPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
