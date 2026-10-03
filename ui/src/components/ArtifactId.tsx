import { Link } from "react-router";
import { shortId } from "../lib/artifactId";
import { CopyButton } from "./CopyButton";

/**
 * A content id. `short` (tables): `a5363431…7415` with the full id in the tooltip and the copy button.
 * `full` (details): the whole id, wrapping, selectable. Optional `to` makes the id a link to its detail page.
 */
export function ArtifactId({ id, variant = "short", to }: { id: string; variant?: "short" | "full"; to?: string }) {
  const text = variant === "short" ? shortId(id) : id;
  const content = (
    <code className={`artifact-id artifact-id-${variant}`} title={id}>
      {text}
    </code>
  );
  return (
    <span className="artifact-id-wrap">
      {to ? (
        <Link to={to} aria-label={`Open ${id}`}>
          {content}
        </Link>
      ) : (
        content
      )}
      <CopyButton text={id} label={`Copy id ${id}`} compact />
    </span>
  );
}
