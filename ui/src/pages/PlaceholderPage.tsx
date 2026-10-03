import { useQuery } from "@tanstack/react-query";
import { historyHeadQuery } from "../api/queries";
import type { ArtifactFamily } from "../api/types";
import { useApiClient } from "../app/context";
import { PageHeading } from "../components/PageHeading";
import { QueryRegion } from "../components/PageState";
import { Timestamp } from "../components/Timestamp";
import { FAMILY_LABEL } from "./OverviewPage";

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
        <QueryRegion query={head} isEmpty={(r) => r.value.data.length === 0}>
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
