import { describe, expect, it, vi } from "vitest";
import { createSessionStore } from "../../auth/session";
import { TEST_TOKEN } from "../fixtures";
import { VERSION } from "../render";

describe("session store (memory only)", () => {
  it("moves through unauthenticated → authenticating → authenticated and exposes the token only when signed in", () => {
    const s = createSessionStore();
    expect(s.getSnapshot().status).toBe("unauthenticated");
    expect(s.getToken()).toBeNull();
    s.beginAuthentication();
    expect(s.getSnapshot().status).toBe("authenticating");
    expect(s.getToken()).toBeNull();
    s.authenticated(TEST_TOKEN, VERSION);
    expect(s.getSnapshot()).toEqual({ status: "authenticated", version: VERSION, endedReason: null });
    expect(s.getToken()).toBe(TEST_TOKEN);
  });

  it("never exposes the token in its snapshot", () => {
    const s = createSessionStore();
    s.authenticated(TEST_TOKEN, VERSION);
    expect(JSON.stringify(s.getSnapshot())).not.toContain(TEST_TOKEN);
  });

  it("clears the token on sign-out and on expiry; expiry is idempotent", () => {
    const s = createSessionStore();
    const listener = vi.fn();
    s.subscribe(listener);
    s.authenticated(TEST_TOKEN, VERSION);
    s.expire();
    s.expire();
    expect(s.getToken()).toBeNull();
    expect(s.getSnapshot().endedReason).toBe("expired");
    expect(listener).toHaveBeenCalledTimes(2);
    s.authenticated(TEST_TOKEN, VERSION);
    s.signOut();
    expect(s.getToken()).toBeNull();
    expect(s.getSnapshot().endedReason).toBe("signed_out");
  });

  it("writes nothing to localStorage, sessionStorage, cookies or IndexedDB", () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const cookie = vi.spyOn(document, "cookie", "set");
    const idb = "indexedDB" in globalThis ? vi.spyOn(globalThis.indexedDB, "open") : null;
    const s = createSessionStore();
    s.beginAuthentication();
    s.authenticated(TEST_TOKEN, VERSION);
    s.expire();
    expect(setItem).not.toHaveBeenCalled();
    expect(cookie).not.toHaveBeenCalled();
    if (idb) expect(idb).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    expect(document.cookie).toBe("");
  });
});
