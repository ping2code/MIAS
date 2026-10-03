import { describe, expect, it } from "vitest";
import { createApiClient } from "../../api/client";
import {
  canonicalQuery,
  detailQuery,
  historyHeadQuery,
  latestQuery,
  liveQuery,
  readyQuery,
  versionQuery,
} from "../../api/queries";
import { createQueryClient } from "../../app/providers";
import { TEST_TOKEN } from "../fixtures";

const client = createApiClient({ getToken: () => TEST_TOKEN, fetchImpl: () => Promise.reject(new Error("unused")) });

describe("query cadence", () => {
  it("polls health every 30 s, version every 5 min, history head every 60 s, latest every 30 s", () => {
    expect(liveQuery(client).refetchInterval).toBe(30_000);
    expect(readyQuery(client).refetchInterval).toBe(30_000);
    expect(versionQuery(client).refetchInterval).toBe(300_000);
    expect(historyHeadQuery(client, "alerts").refetchInterval).toBe(60_000);
    expect(latestQuery(client, "alerts", "META").refetchInterval).toBe(30_000);
  });

  it("treats detail and canonical as immutable", () => {
    for (const q of [detailQuery(client, "alerts", "sha256:a"), canonicalQuery(client, "alerts", "sha256:a")]) {
      expect(q.staleTime).toBe(Infinity);
      expect(q.refetchInterval).toBeUndefined();
      expect(q.refetchOnWindowFocus).toBe(false);
    }
  });

  it("never puts the token in a query key", () => {
    const keys = [liveQuery(client), versionQuery(client), historyHeadQuery(client, "alerts"), latestQuery(client, "alerts", "META")];
    expect(JSON.stringify(keys.map((k) => k.queryKey))).not.toContain(TEST_TOKEN);
  });

  it("disables React Query retries (the client owns the retry contract)", () => {
    const qc = createQueryClient();
    expect(qc.getDefaultOptions().queries?.retry).toBe(false);
    expect(qc.getDefaultOptions().queries?.refetchIntervalInBackground).toBe(false);
  });
});
