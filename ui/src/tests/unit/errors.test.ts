import { describe, expect, it } from "vitest";
import { ApiError, describeError, isApiErrorCode, isRetryable, type ApiErrorCode } from "../../api/errors";

const http = (status: number, code: ApiErrorCode | null = null) =>
  new ApiError("http", { status, code, requestId: "r-12345678" });

describe("isRetryable", () => {
  it.each([
    [400, "invalid_request"],
    [401, "unauthorized"],
    [404, "not_found"],
    [409, "ambiguous_latest"],
    [500, "artifact_invalid"],
  ] as const)("never retries %i %s", (status, code) => {
    expect(isRetryable(http(status, code))).toBe(false);
  });

  it.each([
    [500, "internal"],
    [500, null],
    [502, null],
    [503, "dependency_unavailable"],
    [504, null],
  ] as const)("retries %i %s", (status, code) => {
    expect(isRetryable(http(status, code))).toBe(true);
  });

  it("retries network errors and timeouts, not aborts or invalid responses", () => {
    expect(isRetryable(new ApiError("network"))).toBe(true);
    expect(isRetryable(new ApiError("timeout"))).toBe(true);
    expect(isRetryable(new ApiError("aborted"))).toBe(false);
    expect(isRetryable(new ApiError("invalid_response"))).toBe(false);
  });
});

describe("describeError", () => {
  it("maps codes to safe messages without leaking details", () => {
    expect(describeError(http(409, "ambiguous_latest")).title).toBe("Ambiguous latest");
    expect(describeError(http(500, "artifact_invalid")).title).toBe("Artifact failed integrity verification");
    expect(describeError(http(401, "unauthorized")).title).toBe("Authentication required");
    expect(describeError(new ApiError("timeout")).title).toBe("Request timed out");
    expect(describeError(new Error("Bearer secret")).detail).not.toContain("secret");
  });

  it("recognises only the closed code set", () => {
    expect(isApiErrorCode("not_found")).toBe(true);
    expect(isApiErrorCode("teapot")).toBe(false);
    expect(isApiErrorCode(42)).toBe(false);
  });
});
