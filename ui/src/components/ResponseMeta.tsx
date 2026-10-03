import type { Meta } from "../api/types";
import { MetadataList } from "./MetadataList";
import { Timestamp } from "./Timestamp";

/** The API's own response metadata (view name, API version, served at, request id). */
export function ResponseMeta({ meta }: { meta: Meta }) {
  return (
    <MetadataList
      columns={2}
      items={[
        { term: "View", value: <code>{meta.view}</code>, field: "meta.view" },
        { term: "API version", value: <code>{meta.api_version}</code>, field: "meta.api_version" },
        { term: "Served at", value: <Timestamp iso={meta.served_at} />, field: "meta.served_at" },
        { term: "Request id", value: <code>{meta.request_id}</code>, field: "meta.request_id" },
      ]}
    />
  );
}
