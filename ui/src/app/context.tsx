import { createContext, useContext, useSyncExternalStore } from "react";
import type { ApiClient } from "../api/client";
import type { SessionSnapshot, SessionStore } from "../auth/session";
import type { DiagnosticsSnapshot, DiagnosticsStore } from "./diagnostics";

export interface AppServices {
  client: ApiClient;
  session: SessionStore;
  diagnostics: DiagnosticsStore;
}

export const ServicesContext = createContext<AppServices | null>(null);

export function useServices(): AppServices {
  const services = useContext(ServicesContext);
  if (services === null) throw new Error("useServices must be used inside <AppProviders>");
  return services;
}

export function useApiClient(): ApiClient {
  return useServices().client;
}

export function useSession(): SessionSnapshot {
  const { session } = useServices();
  return useSyncExternalStore(session.subscribe, session.getSnapshot);
}

export function useDiagnostics(): DiagnosticsSnapshot {
  const { diagnostics } = useServices();
  return useSyncExternalStore(diagnostics.subscribe, diagnostics.getSnapshot);
}
