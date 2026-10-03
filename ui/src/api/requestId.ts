/**
 * Request ids: `ui-` + 24 lowercase hex characters from a cryptographically secure source. The format satisfies the
 * mias-api accepted pattern `[A-Za-z0-9][A-Za-z0-9._-]{7,63}`. Used only for correlation (header + error details);
 * never as auth state, a persistence key, a URL parameter or a metric label.
 */
export type RandomFill = (bytes: Uint8Array<ArrayBuffer>) => Uint8Array<ArrayBuffer>;

const defaultRandom: RandomFill = (bytes) => crypto.getRandomValues(bytes);

export const REQUEST_ID_PATTERN = /^ui-[0-9a-f]{24}$/;

export function newRequestId(random: RandomFill = defaultRandom): string {
  const bytes = random(new Uint8Array(12));
  return `ui-${Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")}`;
}
