import { Navigate, Route, Routes } from "react-router";
import { AppShell } from "../components/AppShell";
import { NotFoundPage } from "../pages/NotFoundPage";
import { OverviewPage } from "../pages/OverviewPage";
import { AlertDetailPage } from "../pages/alerts/AlertDetailPage";
import { AlertListPage } from "../pages/alerts/AlertListPage";
import { MarketIntelligenceDetailPage } from "../pages/market-intelligence/MarketIntelligenceDetailPage";
import { MarketIntelligenceListPage } from "../pages/market-intelligence/MarketIntelligenceListPage";
import { OperationalHistoryPage, OperationalDetailPage } from "../pages/OperationalPages";
import { OptionsIntelligencePage } from "../pages/options/OptionsIntelligencePage";
import { StatusPage } from "../pages/StatusPage";



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
        <Route path="options-intelligence" element={<OptionsIntelligencePage />} />
        <Route path="options-intelligence/:id" element={<OperationalDetailPage family="options-intelligence" />} />
        <Route path="trade-setups" element={<OperationalHistoryPage family="trade-setups" />} />
        <Route path="trade-setups/:id" element={<OperationalDetailPage family="trade-setups" />} />
        <Route path="invalidation-checks" element={<OperationalHistoryPage family="invalidation-checks" />} />
        <Route path="invalidation-checks/:id" element={<OperationalDetailPage family="invalidation-checks" />} />
        <Route path="status" element={<StatusPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
