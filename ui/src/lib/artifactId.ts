/** Artifact ids are `sha256:<64 hex>` content ids. Shortened only for display; the full id is always available. */
const ID = /^sha256:([0-9a-f]{64})$/;

export function isArtifactId(value: string): boolean {
  return ID.test(value);
}

/** `sha256:a5363431…7415` → `a5363431…7415` (first 8, last 4 hex). Anything else is returned unchanged. */
export function shortId(value: string): string {
  const hex = ID.exec(value)?.[1];
  return hex ? `${hex.slice(0, 8)}…${hex.slice(-4)}` : value;
}

/** The download file name for a canonical artifact: `<64 hex>.json`. */
export function canonicalFileName(value: string): string {
  const hex = ID.exec(value)?.[1];
  return `${hex ?? "artifact"}.json`;
}
