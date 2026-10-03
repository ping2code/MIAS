import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";
import { canonicalQuery } from "../api/queries";
import type { ArtifactFamily } from "../api/types";
import { useApiClient } from "../app/context";
import { canonicalFileName } from "../lib/artifactId";
import { byteLength, downloadText, formatted } from "../lib/canonical";
import { CopyButton } from "./CopyButton";
import { MetadataList } from "./MetadataList";
import { RequestError } from "./RequestError";

/**
 * The Raw / Canonical view (Phase 16A §12). The text is exactly what `GET …/{id}/canonical` returned (read with
 * `response.text()`); Copy and Download always use it. The optional formatted view is a parsed, pretty-printed copy,
 * labelled as not canonical. The ETag is checked against the requested id.
 */
export function CanonicalPanel({ family, id }: { family: ArtifactFamily; id: string }) {
  const client = useApiClient();
  const query = useQuery(canonicalQuery(client, family, id));
  const [mode, setMode] = useState<"canonical" | "formatted">("canonical");
  const [downloaded, setDownloaded] = useState(false);
  const groupId = useId();

  if (query.isPending) {
    return (
      <p className="inline-loading" role="status" aria-live="polite">
        <span className="spinner" aria-hidden="true" /> Loading canonical artifact…
      </p>
    );
  }
  if (query.isError) {
    return <RequestError error={query.error} onRetry={() => void query.refetch()} notFound={<p>That artifact could not be found.</p>} />;
  }

  const { text, etag, cacheControl } = query.data.value;
  const expectedEtag = `"${id}"`;
  const etagMatches = etag === expectedEtag;
  const pretty = mode === "formatted" ? formatted(text) : null;
  const shown = mode === "formatted" && pretty !== null ? pretty : text;

  return (
    <div className="canonical">
      <MetadataList
        columns={2}
        items={[
          {
            term: "ETag",
            value: etag ? (
              <>
                <code className="artifact-id-full">{etag}</code>{" "}
                {etagMatches ? (
                  <span className="badge badge-positive">
                    <span aria-hidden="true" className="badge-symbol">
                      ✓
                    </span>
                    Matches artifact id
                  </span>
                ) : (
                  <span className="badge badge-negative">
                    <span aria-hidden="true" className="badge-symbol">
                      ✕
                    </span>
                    Does not match artifact id
                  </span>
                )}
              </>
            ) : (
              "Not provided"
            ),
          },
          { term: "Cache-Control", value: cacheControl ? <code>{cacheControl}</code> : "Not provided" },
          { term: "Size", value: `${String(byteLength(text))} bytes (UTF-8)` },
          { term: "Immutable", value: cacheControl?.includes("immutable") ? "Yes — content-addressed, never changes" : "Not declared" },
        ]}
      />
      <div className="canonical-toolbar">
        <div role="radiogroup" aria-labelledby={`${groupId}-label`} className="segmented">
          <span id={`${groupId}-label`} className="visually-hidden-text">
            Display
          </span>
          <button
            type="button"
            role="radio"
            aria-checked={mode === "canonical"}
            className="segment"
            onClick={() => {
              setMode("canonical");
            }}
          >
            Canonical (exact)
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={mode === "formatted"}
            className="segment"
            onClick={() => {
              setMode("formatted");
            }}
          >
            Formatted (not canonical)
          </button>
        </div>
        <div className="canonical-actions">
          <CopyButton text={text} label="Copy canonical text" fallback="Copy unavailable — select the canonical text manually." />
          <button
            type="button"
            className="button button-quiet button-small"
            onClick={() => {
              downloadText(text, canonicalFileName(id));
              setDownloaded(true);
            }}
          >
            <span aria-hidden="true">⤓</span> Download canonical
          </button>
          <span className="copy-status" role="status" aria-live="polite">
            {downloaded ? `Saved as ${canonicalFileName(id)}` : ""}
          </span>
        </div>
      </div>
      {mode === "formatted" ? (
        <p className="notice notice-compact">
          Formatted view — a pretty-printed copy for reading. It is <strong>not</strong> the canonical byte sequence;
          Copy and Download always use the exact canonical text.
        </p>
      ) : null}
      <pre
        className="code-block"
        tabIndex={0}
        aria-label={mode === "formatted" ? "Formatted copy (not canonical)" : "Canonical text (exact)"}
        data-mode={mode}
      >
        {shown}
      </pre>
    </div>
  );
}
