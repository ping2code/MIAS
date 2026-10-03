/** The Status page requests only the API's health and version endpoints, same-origin: no infrastructure calls. */
import { screen, waitFor } from "@testing-library/react";
import { http } from "msw";
import { describe, expect, it } from "vitest";
import { fx } from "../api16c";
import { authorized, respond, server } from "../msw";
import { fixture } from "../fixtures";
import { renderApp } from "../render";

describe("Status page network surface", () => {
  it("calls only /health/live, /health/ready and /api/v1/version on the same origin", async () => {
    const seen: URL[] = [];
    const listener = ({ request }: { request: Request }) => {
      seen.push(new URL(request.url));
    };
    server.events.on("request:start", listener);
    server.use(
      http.get("*/health/live", () => respond(fx("health:live"))),
      http.get("*/health/ready", () => respond(fx("health:ready"))),
      http.get("*/api/v1/version", ({ request }) => respond(authorized(request) ? fx("version") : fixture("unauthorized"))),
    );
    renderApp("/status");
    await screen.findByText("3 of 3 checks pass.");
    await waitFor(() => {
      expect(new Set(seen.map((u) => u.pathname))).toEqual(new Set(["/health/live", "/health/ready", "/api/v1/version"]));
    });
    server.events.removeListener("request:start", listener);
    const origin = window.location.origin;
    for (const url of seen) {
      expect(url.origin).toBe(origin);
      expect(url.search).toBe("");
      expect(url.pathname).not.toMatch(/prometheus|thanos|otel|collector|metrics|kubernetes|openshift|\/apis?\/(?!v1\/version)/i);
    }
  });
});
