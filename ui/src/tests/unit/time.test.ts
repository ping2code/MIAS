import { describe, expect, it } from "vitest";
import { formatTimestamp } from "../../lib/time";

describe("formatTimestamp", () => {
  it("keeps the original ISO string exactly and derives UTC", () => {
    const t = formatTimestamp("2026-09-23T20:05:00+00:00");
    expect(t.valid).toBe(true);
    expect(t.original).toBe("2026-09-23T20:05:00+00:00");
    expect(t.utc).toBe("2026-09-23T20:05:00.000Z");
    expect(t.local).toMatch(/2026/);
  });

  it("converts offsets to UTC without rewriting the original", () => {
    const t = formatTimestamp("2026-09-23T16:05:00-04:00");
    expect(t.utc).toBe("2026-09-23T20:05:00.000Z");
    expect(t.original).toBe("2026-09-23T16:05:00-04:00");
  });

  it("shows invalid values verbatim instead of fixing them", () => {
    expect(formatTimestamp("not-a-time")).toEqual({ original: "not-a-time", local: "not-a-time", utc: "not-a-time", valid: false });
  });
});
