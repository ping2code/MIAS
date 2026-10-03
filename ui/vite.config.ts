/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Non-secret build identifier only (git SHA); never a token or URL.
const buildId = process.env.MIAS_UI_BUILD_ID ?? "dev";
if (!/^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/.test(buildId)) {
  throw new Error("MIAS_UI_BUILD_ID must be 1-64 characters of [A-Za-z0-9._+-]");
}

export default defineConfig({
  plugins: [react()],
  define: { __MIAS_UI_BUILD__: JSON.stringify(buildId) },
  build: {
    sourcemap: false,
    assetsInlineLimit: 0, // no data: URIs for scripts/styles; everything is a same-origin file
    modulePreload: { polyfill: false }, // keep index.html free of inline scripts (strict CSP)
  },
  server: {
    // Local development only: same-origin proxy to a local mias-api (never used in builds).
    proxy: {
      "/api/v1": "http://127.0.0.1:8080",
      "/health": "http://127.0.0.1:8080",
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/tests/setup.ts"],
    restoreMocks: true,
    css: false,
  },
});
