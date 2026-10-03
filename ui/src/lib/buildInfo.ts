/** The non-secret UI build identifier (git SHA), injected at build time. */
export const UI_BUILD: string = typeof __MIAS_UI_BUILD__ === "string" ? __MIAS_UI_BUILD__ : "dev";

/** The UI package version (package.json). */
export const UI_VERSION: string = typeof __MIAS_UI_VERSION__ === "string" ? __MIAS_UI_VERSION__ : "0.0.0";
