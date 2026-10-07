/**
 * Presentation of `options-intelligence-activity-v1` values. Text is always shown (arrows are characters, never
 * colour alone). Nothing is recomputed: every number and state comes from the API; a missing value says why.
 */
import type { ActivityComparison, ActivityConcentration, ActivityMomentum, ChangeReason } from "../api/types";

const COUNT = new Intl.NumberFormat("en-US");

export function count(value: number | null): string {
  return value === null ? "Unavailable" : COUNT.format(value);
}

function arrow(sign: number): string {
  return sign > 0 ? "↑" : sign < 0 ? "↓" : "→";
}

/** "↑18.0%", "↓1.0%", "→0.0%"; or why there is no percentage. */
export function percentChange(pct: string | null, reason: ChangeReason): string {
  if (pct !== null) {
    const value = Number(pct);
    return `${arrow(value)}${Math.abs(value).toFixed(1)}%`;
  }
  if (reason === "prior_zero") return "no prior volume";
  if (reason === "value_unavailable") return "change unavailable";
  return "no prior same-session snapshot";
}

/** "↑27", "↓3", "→0"; or null when there is no comparable prior (the caller shows nothing). */
export function countChange(delta: number | null): string | null {
  return delta === null ? null : `${arrow(delta)}${COUNT.format(Math.abs(delta))}`;
}

/** The put/call volume ratio to two decimals, or "Unavailable". */
export function ratio(value: string | null): string {
  return value === null ? "Unavailable" : Number(value).toFixed(2);
}

/** A decimal fraction as a percentage to one decimal ("0.314" → "31.4%"), or "Unavailable". */
export function ivPercent(value: string | null): string {
  return value === null ? "Unavailable" : `${(Number(value) * 100).toFixed(1)}%`;
}

export const CONCENTRATION_LABEL: Readonly<Record<ActivityConcentration, string>> = {
  below_spot: "Below spot",
  at_spot: "At spot",
  above_spot: "Above spot",
  mixed: "Mixed",
  unavailable: "Unavailable",
};

export const MOMENTUM_LABEL: Readonly<Record<ActivityMomentum, string>> = {
  CALL: "CALL ↑",
  PUT: "PUT ↑",
  BALANCED: "BALANCED",
  UNAVAILABLE: "Unavailable",
  INSUFFICIENT_PRIOR: "Insufficient prior snapshot",
  VOLUME_CORRECTION: "Volume correction",
};

/** What this report is compared with, in words. */
export function comparisonNote(c: ActivityComparison): string {
  switch (c.status) {
    case "comparable":
      return "Compared with the previous report of this session";
    case "no_prior_snapshot":
      return "No earlier report for this symbol";
    case "prior_from_other_session":
      return "First report of this session (the previous report is from an earlier session)";
    case "prior_ambiguous":
      return "Not compared: two earlier reports share the same as-of time";
  }
}

export function expiryDates(n: number): string {
  return `${COUNT.format(n)} expiry ${n === 1 ? "date" : "dates"}`;
}
