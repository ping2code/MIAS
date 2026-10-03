import { afterEach, describe, expect, it, vi } from "vitest";
import { isKnownRoute, safeReturnPath } from "../../auth/returnPath";
import { createSessionStore } from "../../auth/session";
import { applyTheme, readThemePreference, THEME_STORAGE_KEY, writeThemePreference } from "../../lib/theme";
import { TEST_TOKEN } from "../fixtures";
import { VERSION } from "../render";

const MI = `/market-intelligence/sha256:${"a".repeat(64)}`;

describe("safe return paths", () => {
  it.each([
    ["/", "/"],
    ["/status", "/status"],
    ["/alerts", "/alerts"],
    [MI, MI],
    [`${MI}?tab=canonical`, `${MI}?tab=canonical`],
    ["/market-intelligence?symbol=META&as_of_from=2026-09-24T00:00:00Z", "/market-intelligence?symbol=META&as_of_from=2026-09-24T00%3A00%3A00Z"],
    ["/alerts?symbol=META&token=placeholder-canary&cursor=abc", "/alerts?symbol=META"],
    ["/options-intelligence", "/options-intelligence"],
  ])("keeps the known in-app path %s", (input, expected) => {
    expect(safeReturnPath(input)).toBe(expected);
  });

  it.each([
    "http://evil.example/",
    "https://evil.example/status",
    "//evil.example/status",
    "///evil.example",
    "/\\evil.example",
    "\\\\evil.example",
    "javascript:alert(1)",
    "data:text/html,<p>x</p>",
    "/%2F%2Fevil.example",
    "/%5cevil.example",
    "/signin",
    "/signin?next=/status",
    "/no-such-route",
    "/market-intelligence/not-an-id",
    "/alerts/sha256:ABC",
    "/status\n",
    "/status\u0000",
    "",
    "status",
    "/" + "a".repeat(600),
  ])("rejects %j", (input) => {
    expect(safeReturnPath(input)).toBe("/");
  });

  it("rejects non-strings", () => {
    for (const value of [undefined, null, 42, {}, ["/status"]]) expect(safeReturnPath(value)).toBe("/");
  });

  it("knows exactly the app routes", () => {
    expect(isKnownRoute("/trade-setups")).toBe(true);
    expect(isKnownRoute("/trade-setups/x")).toBe(false);
  });
});

describe("memory-only lock", () => {
  it("hides the token while locked and unlocks only with the same token", () => {
    const s = createSessionStore();
    s.authenticated(TEST_TOKEN, VERSION);
    s.lock();
    expect(s.getSnapshot()).toMatchObject({ status: "authenticated", locked: true });
    expect(s.getToken()).toBeNull(); // no request can carry it while locked
    expect(s.matchesToken("wrong-token-wrong-token-wrong-token")).toBe(false);
    expect(s.matchesToken(`${TEST_TOKEN}x`)).toBe(false);
    expect(s.matchesToken(TEST_TOKEN)).toBe(true);
    s.unlock(VERSION);
    expect(s.getSnapshot().locked).toBe(false);
    expect(s.getToken()).toBe(TEST_TOKEN);
  });

  it("never exposes the token in the locked snapshot and writes no storage", () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const s = createSessionStore();
    s.authenticated(TEST_TOKEN, VERSION);
    s.lock();
    expect(JSON.stringify(s.getSnapshot())).not.toContain(TEST_TOKEN);
    expect(setItem).not.toHaveBeenCalled();
  });

  it("signs out or expires from the locked state", () => {
    const s = createSessionStore();
    s.authenticated(TEST_TOKEN, VERSION);
    s.lock();
    s.signOut();
    expect(s.getSnapshot()).toMatchObject({ status: "unauthenticated", locked: false, endedReason: "signed_out" });
    expect(s.matchesToken(TEST_TOKEN)).toBe(false);
    s.authenticated(TEST_TOKEN, VERSION);
    s.lock();
    s.expire();
    expect(s.getSnapshot()).toMatchObject({ status: "unauthenticated", locked: false, endedReason: "expired" });
  });

  it("cannot lock or unlock an unauthenticated session, and a new store starts unauthenticated (a reload)", () => {
    const s = createSessionStore();
    s.lock();
    expect(s.getSnapshot().locked).toBe(false);
    s.unlock(VERSION);
    expect(s.getSnapshot().status).toBe("unauthenticated");
    expect(createSessionStore().getSnapshot()).toEqual({ status: "unauthenticated", version: null, endedReason: null, locked: false });
  });
});

describe("theme preference", () => {
  afterEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
  });

  it("defaults to system and applies no attribute", () => {
    expect(readThemePreference()).toBe("system");
    applyTheme("system");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  it("stores only the UI key with one of three values", () => {
    writeThemePreference("dark");
    expect(Object.keys(localStorage)).toEqual([THEME_STORAGE_KEY]);
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    applyTheme(readThemePreference());
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    writeThemePreference("system");
    expect(localStorage.length).toBe(0);
  });

  it("ignores tampered values and storage failures", () => {
    localStorage.setItem(THEME_STORAGE_KEY, "<script>");
    expect(readThemePreference()).toBe("system");
    const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(readThemePreference()).toBe("system");
    get.mockRestore();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    expect(() => {
      writeThemePreference("light");
    }).not.toThrow();
  });
});
