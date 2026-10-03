/**
 * Real Phase 13 API responses, captured from the API itself (create_app + TestClient over the sealed Phase 13 test
 * samples, temporary directories, a placeholder token). See docs/phase16b-dashboard-foundation.md.
 */
import raw from "./fixtures/phase13.json";

export interface CapturedResponse {
  status: number;
  headers: Record<string, string>;
  body?: unknown;
  text?: string;
}

export const captured = raw as Record<string, CapturedResponse>;

export function fixture(name: string): CapturedResponse {
  const entry = captured[name];
  if (!entry) throw new Error(`missing fixture ${name}`);
  return entry;
}

/** A placeholder used only in tests; never a real credential. */
export const TEST_TOKEN = "placeholder-read-token-0123456789abcdef";
