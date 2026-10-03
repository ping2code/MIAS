import { describe, expect, it } from "vitest";
import { newRequestId, REQUEST_ID_PATTERN } from "../../api/requestId";

describe("newRequestId", () => {
  it("is ui- plus 24 lowercase hex characters, accepted by the API's request-id pattern", () => {
    const id = newRequestId();
    expect(id).toMatch(REQUEST_ID_PATTERN);
    expect(id).toMatch(/^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$/);
  });

  it("draws 12 bytes from the supplied cryptographic source", () => {
    const id = newRequestId((array) => {
      const bytes = array as Uint8Array;
      expect(bytes.length).toBe(12);
      bytes.set([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 255]);
      return array;
    });
    expect(id).toBe("ui-000102030405060708090aff");
  });

  it("is unique per call", () => {
    const ids = new Set(Array.from({ length: 200 }, () => newRequestId()));
    expect(ids.size).toBe(200);
  });
});
