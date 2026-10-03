import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { fixture, TEST_TOKEN } from "../fixtures";
import { apiHandlers, respond, server } from "../msw";
import { renderApp } from "../render";

async function signIn(token: string) {
  const user = userEvent.setup();
  const input = await screen.findByLabelText("Read token");
  await user.type(input, token);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  return { user, input };
}

describe("sign-in", () => {
  it("redirects unauthenticated users to /signin and shows a password field", async () => {
    server.use(...apiHandlers());
    renderApp("/status", { signedIn: false });
    expect(await screen.findByRole("heading", { name: "Sign in to MIAS" })).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/signin");
    expect(screen.getByLabelText("Read token")).toHaveAttribute("type", "password");
    expect(screen.getByLabelText("Read token")).toHaveAttribute("autocomplete", "off");
  });

  it("validates the token with /api/v1/version, stores it in memory only and returns to the requested page", async () => {
    server.use(...apiHandlers());
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const { init } = renderApp("/status", { signedIn: false });
    await signIn(TEST_TOKEN);
    expect(await screen.findByRole("heading", { name: "System Status" })).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/status");
    expect(init.services.session.getToken()).toBe(TEST_TOKEN);
    expect(setItem).not.toHaveBeenCalled();
    expect(document.cookie).toBe("");
    expect(document.body.innerHTML).not.toContain(TEST_TOKEN);
  });

  it("lands on the Overview after signing in from /signin", async () => {
    server.use(...apiHandlers());
    renderApp("/signin", { signedIn: false });
    await signIn(TEST_TOKEN);
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/$/);
  });

  it("says 'Invalid or expired read token' on 401 and clears the field", async () => {
    server.use(...apiHandlers());
    const { init } = renderApp("/signin", { signedIn: false });
    const { input } = await signIn("wrong-token-wrong-token-wrong-token");
    expect(await screen.findByText("Invalid or expired read token")).toBeInTheDocument();
    expect(input).toHaveValue("");
    expect(init.services.session.getSnapshot().status).toBe("unauthenticated");
    expect(screen.getByTestId("location")).toHaveTextContent("/signin");
  });

  it("reports a temporary backend problem, not an invalid token, on 503", async () => {
    server.use(http.get("*/api/v1/version", () => respond(fixture("deliveries_unavailable"))));
    renderApp("/signin", { signedIn: false });
    await signIn(TEST_TOKEN);
    expect(await screen.findByText(/temporarily unavailable/)).toBeInTheDocument();
    expect(screen.queryByText("Invalid or expired read token")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Read token")).toHaveValue(TEST_TOKEN);
  });

  it("reports a temporary backend problem on a network error", async () => {
    server.use(http.get("*/api/v1/version", () => HttpResponse.error()));
    renderApp("/signin", { signedIn: false });
    await signIn(TEST_TOKEN);
    expect(await screen.findByText(/temporarily unavailable/)).toBeInTheDocument();
    expect(screen.queryByText("Invalid or expired read token")).not.toBeInTheDocument();
  });

  it("asks for a token instead of calling the API when the field is empty", async () => {
    const seen = vi.fn();
    server.use(http.get("*/api/v1/version", () => {
      seen();
      return respond(fixture("version"));
    }));
    renderApp("/signin", { signedIn: false });
    await userEvent.setup().click(await screen.findByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Enter the read token.")).toBeInTheDocument();
    expect(seen).not.toHaveBeenCalled();
  });
});

describe("global 401 and sign-out", () => {
  it("clears the token and query cache and returns to /signin once, without a loop", async () => {
    let versionCalls = 0;
    server.use(
      http.get("*/api/v1/version", () => {
        versionCalls += 1;
        return respond(fixture("version"));
      }),
      http.get("*/api/v1/:family", () => respond(fixture("unauthorized"))),
      ...apiHandlers(),
    );
    const { init } = renderApp("/");
    expect(await screen.findByRole("heading", { name: "Sign in to MIAS" })).toBeInTheDocument();
    expect(await screen.findByText(/session ended because the read token was rejected/)).toBeInTheDocument();
    expect(init.services.session.getToken()).toBeNull();
    expect(screen.getByTestId("location")).toHaveTextContent("/signin");
    await waitFor(() => {
      expect(init.queryClient.getQueryCache().getAll().filter((q) => q.queryKey[0] === "api")).toHaveLength(0);
    });
    expect(versionCalls).toBeLessThanOrEqual(1);
  });

  it("signs out on request and forgets the token", async () => {
    server.use(...apiHandlers());
    const { init } = renderApp("/status");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByText("You have signed out.")).toBeInTheDocument();
    expect(init.services.session.getToken()).toBeNull();
    expect(init.queryClient.getQueryCache().getAll()).toHaveLength(0);
  });
});
