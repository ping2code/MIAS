/**
 * Theme preference (Phase 16E): "system" (default, follows prefers-color-scheme), "light" or "dark", applied as
 * `data-theme` on <html>. The preference is a non-sensitive UI setting stored under exactly one localStorage key,
 * `mias-ui-theme`, with one of three values. Nothing else is ever written to storage — never the token or any
 * session data (the bundle audit allows localStorage only with this literal key). Storage failures are ignored.
 */
export type ThemePreference = "system" | "light" | "dark";

export const THEME_STORAGE_KEY = "mias-ui-theme";
const VALUES: readonly ThemePreference[] = ["system", "light", "dark"];

export function isThemePreference(value: unknown): value is ThemePreference {
  return typeof value === "string" && (VALUES as readonly string[]).includes(value);
}

export function readThemePreference(): ThemePreference {
  try {
    const stored = window.localStorage.getItem("mias-ui-theme");
    return isThemePreference(stored) ? stored : "system";
  } catch {
    return "system";
  }
}

export function writeThemePreference(value: ThemePreference): void {
  try {
    if (value === "system") window.localStorage.removeItem("mias-ui-theme");
    else window.localStorage.setItem("mias-ui-theme", value);
  } catch {
    // Private mode or storage disabled: the choice still applies for this tab.
  }
}

/** Apply to <html>: "system" removes the attribute so the CSS media query decides. */
export function applyTheme(value: ThemePreference, root: HTMLElement = document.documentElement): void {
  if (value === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", value);
}
