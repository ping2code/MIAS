import { useSyncExternalStore } from "react";

/** A CSS media query as React state (false where matchMedia is unavailable). */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      if (typeof window.matchMedia !== "function") return () => undefined;
      const mql = window.matchMedia(query);
      mql.addEventListener("change", onChange);
      return () => {
        mql.removeEventListener("change", onChange);
      };
    },
    () => (typeof window.matchMedia === "function" ? window.matchMedia(query).matches : false),
  );
}

/** Below this width the persistent side navigation becomes a drawer (matches app.css). */
export const COMPACT_NAV_QUERY = "(max-width: 900px)";
