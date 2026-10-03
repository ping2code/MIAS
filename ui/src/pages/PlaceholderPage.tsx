import { useQuery } from "@tanstack/react-query";
import { historyHeadQuery } from "../api/queries";
import type { ArtifactFamily } from "../api/types";
import { useApiClient } from "../app/context";
import { PageHeading } from "../components/PageHeading";
import { QueryRegion } from "../components/PageState";
import { Timestamp } from "../components/Timestamp";
import { FAMILY_LABEL } from "./OverviewPage";

/** Empty-state wording per kind (Phase 16E review). */
export const EMPTY_MESSAGE: Record<ArtifactFamily, string> = {
  "market-intelligence": "No market intelligence is available yet.",
  alerts: "No alerts are available yet.",
  "options-intelligence": "No options intelligence is available yet.",
  "trade-setups": "No trade setups are available yet.",
  "invalidation-checks": "No invalidation checks are available yet.",
};

/** A placeholder for a kind's list/detail views, with a working has-data/empty state. */
export function PlaceholderPage({ family, note }: { family: ArtifactFamily; note: string }) {
  const client = useApiClient();
  const head = useQuery(historyHeadQuery(client, family));
  return (
    <>
      <PageHeading title={FAMILY_LABEL[family]} />
      <p className="notice">{note}</p>
      <section className="card" aria-labelledby={`ph-${family}`}>
        <h2 id={`ph-${family}`}>Newest entry</h2>
        <QueryRegion query={head} isEmpty={(r) => r.value.data.length === 0} emptyMessage={EMPTY_MESSAGE[family]}>
          {(result) => {
            const first = result.value.data[0];
            return first ? (
              <dl className="kv">
                <dt>Symbol</dt>
                <dd>{first.symbol}</dd>
                <dt>As of</dt>
                <dd>
                  <Timestamp iso={first.as_of} />
                </dd>
              </dl>
            ) : null;
          }}
        </QueryRegion>
      </section>
    </>
  );
}
