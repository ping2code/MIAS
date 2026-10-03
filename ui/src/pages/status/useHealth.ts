import { useQuery } from "@tanstack/react-query";
import { liveQuery, readyQuery } from "../../api/queries";
import { useApiClient } from "../../app/context";
import { deriveApiStatus, observeLive, observeReady } from "../../lib/apiStatus";

/**
 * The two health queries and their derived status, shared by the AppShell indicator and the Status page (same
 * cache entries, same 30 s cadence). `refreshOnMount` re-checks immediately when the Status page opens.
 */
export function useHealth({ refreshOnMount = false }: { refreshOnMount?: boolean } = {}) {
  const client = useApiClient();
  const live = useQuery({ ...liveQuery(client), ...(refreshOnMount ? { refetchOnMount: "always" as const } : {}) });
  const ready = useQuery({ ...readyQuery(client), ...(refreshOnMount ? { refetchOnMount: "always" as const } : {}) });
  const liveObs = observeLive(live);
  const readyObs = observeReady(ready);
  const lastRefresh = Math.max(live.dataUpdatedAt, live.errorUpdatedAt, ready.dataUpdatedAt, ready.errorUpdatedAt);
  return {
    live,
    ready,
    liveObs,
    readyObs,
    status: deriveApiStatus(liveObs, readyObs),
    lastRefresh: lastRefresh > 0 ? lastRefresh : null,
    refresh: () => {
      void live.refetch();
      void ready.refetch();
    },
  };
}
