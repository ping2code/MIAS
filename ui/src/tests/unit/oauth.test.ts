import { describe, expect, it, vi } from "vitest";
import { createAuthGateway, SESSION_CHECK_PATH, SIGN_OUT_PATH } from "../../auth/oauth";

function gateway(response: Response | Error, location = "/alerts?symbol=META") {
  const calls: { url: string; init: RequestInit | undefined }[] = [];
  const navigations: string[] = [];
  const fetchImpl = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: typeof input === "string" ? input : input instanceof URL ? input.href : input.url, init });
    return response instanceof Error ? Promise.reject(response) : Promise.resolve(response);
  });
  const auth = createAuthGateway({
    fetchImpl,
    navigate: (url) => {
      navigations.push(url);
    },
    currentLocation: () => location,
  });
  return { auth, calls, navigations, fetchImpl };
}

describe("OAuth gateway (oauth-proxy, Hardening Task 8)", () => {
  it("asks /oauth/auth with the same-origin session cookie, without following redirects or caching", async () => {
    const { auth, calls } = gateway(new Response(null, { status: 202 }));
    expect(await auth.checkSession()).toBe("authenticated");
    expect(calls).toEqual([
      { url: SESSION_CHECK_PATH, init: { method: "GET", credentials: "same-origin", redirect: "manual", cache: "no-store" } },
    ]);
  });

  it("maps 401 to unauthenticated, other answers and failures to unknown", async () => {
    expect(await gateway(new Response(null, { status: 401 })).auth.checkSession()).toBe("unauthenticated");
    expect(await gateway(new Response(null, { status: 500 })).auth.checkSession()).toBe("unknown");
    expect(await gateway(new TypeError("offline")).auth.checkSession()).toBe("unknown");
  });

  it("shares one check between concurrent callers", async () => {
    const { auth, fetchImpl } = gateway(new Response(null, { status: 202 }));
    await Promise.all([auth.checkSession(), auth.checkSession(), auth.checkSession()]);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("signs out through /oauth/sign_out, once", () => {
    const { auth, navigations } = gateway(new Response(null, { status: 202 }));
    auth.signOut();
    auth.signOut();
    auth.reauthenticate();
    expect(navigations).toEqual([SIGN_OUT_PATH]);
  });

  it("reauthenticates by reloading the current safe location, once", () => {
    const { auth, navigations } = gateway(new Response(null, { status: 401 }));
    auth.reauthenticate();
    auth.reauthenticate();
    expect(navigations).toEqual(["/alerts?symbol=META"]);
  });

  it("never reloads to an unsafe location", () => {
    const { auth, navigations } = gateway(new Response(null, { status: 401 }), "//evil.example/steal");
    auth.reauthenticate();
    expect(navigations).toEqual(["/"]);
  });
});
