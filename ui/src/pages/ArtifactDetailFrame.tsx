import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router";
import { detailQuery } from "../api/queries";
import type { FamilyViews, ItemResponse } from "../api/types";
import { useApiClient } from "../app/context";
import { Breadcrumbs } from "../components/Breadcrumbs";
import { CanonicalPanel } from "../components/CanonicalPanel";
import { DetailSkeleton } from "../components/DetailSection";
import { PageHeading } from "../components/PageHeading";
import { RequestError } from "../components/RequestError";
import { Tabs } from "../components/Tabs";
import { isArtifactId } from "../lib/artifactId";

type DetailFamily = "market-intelligence" | "alerts";

const TABS = [
  { id: "summary", label: "Summary" },
  { id: "canonical", label: "Raw / Canonical" },
] as const;

/**
 * The shared detail frame: breadcrumb, heading (focused on navigation), Summary / Raw-Canonical tabs (the tab is in
 * the URL, so back/forward and deep links work). The summary is immutable by id and never re-polled.
 */
export function ArtifactDetailFrame<F extends DetailFamily>({
  family,
  listLabel,
  noun,
  heading,
  children,
}: {
  family: F;
  listLabel: string;
  noun: string;
  heading: (view: FamilyViews[F]) => string;
  children: (response: ItemResponse<FamilyViews[F]>) => ReactNode;
}) {
  const { id = "" } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const search = new URLSearchParams(location.search);
  const tab = search.get("tab") === "canonical" ? "canonical" : "summary";
  const client = useApiClient();
  const valid = isArtifactId(id);
  const query = useQuery({ ...detailQuery(client, family, id), enabled: valid });
  const listPath = `/${family}`;

  if (!valid) {
    return (
      <>
        <Breadcrumbs items={[{ label: listLabel, to: listPath }, { label: "Invalid id" }]} />
        <PageHeading title={`${listLabel}: invalid id`} />
        <div className="state state-notice" role="status">
          <p>
            <code>{id}</code> is not a MIAS artifact id. Ids look like <code>sha256:</code> followed by 64 lowercase
            hexadecimal characters.
          </p>
          <p>
            <Link to={listPath}>Back to {listLabel}</Link>
          </p>
        </div>
      </>
    );
  }

  const title = query.data ? heading(query.data.value.data) : listLabel;
  return (
    <>
      <Breadcrumbs items={[{ label: listLabel, to: listPath }, { label: query.data ? title : "Artifact" }]} />
      <PageHeading title={title}>
        <p className="page-meta">
          {noun} · <code title={id}>{id}</code>
        </p>
      </PageHeading>
      {query.isPending ? (
        <DetailSkeleton />
      ) : query.isError ? (
        <RequestError
          error={query.error}
          onRetry={() => void query.refetch()}
          notFound={
            <>
              <p>
                <strong>Not found.</strong> No {noun.toLowerCase()} with this id exists in the artifact store.
              </p>
              <p>
                <Link to={listPath}>Back to {listLabel}</Link>
              </p>
            </>
          }
        />
      ) : (
        <Tabs
          tabs={TABS}
          active={tab}
          label={`${noun} views`}
          onChange={(next) => {
            const params = new URLSearchParams(search);
            if (next === "canonical") params.set("tab", "canonical");
            else params.delete("tab");
            const query = params.toString();
            void navigate({ pathname: location.pathname, search: query ? `?${query}` : "" }, { replace: true });
          }}
        >
          {tab === "canonical" ? <CanonicalPanel family={family} id={id} /> : children(query.data.value)}
        </Tabs>
      )}
    </>
  );
}
