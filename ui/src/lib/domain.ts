/**
 * Presentation of domain values. Labels are derived mechanically from the returned string (snake_case → words);
 * the literal value is always kept alongside. Tones only describe system facts the backend defines (data
 * availability, a recorded change, a recorded delivery result); market patterns and alert codes are never mapped
 * to buy/sell or good/bad sentiment.
 */
export type Tone = "neutral" | "info" | "positive" | "negative" | "changed";

export function humanize(value: string): string {
  const words = value.replace(/[_-]+/g, " ").trim();
  return words === "" ? value : words.charAt(0).toUpperCase() + words.slice(1);
}

export type BadgeKind =
  | "timeframe_pattern"
  | "technical_status"
  | "availability"
  | "alert_code"
  | "attention_category"
  | "delivery_status"
  | "subject_kind";

/** Badge tone for a (kind, value) pair. Unknown values fall back to neutral. */
export function toneFor(kind: BadgeKind, value: string): Tone {
  switch (kind) {
    case "technical_status":
    case "availability":
      if (value === "available" || value === "true") return "positive";
      if (value === "unavailable" || value === "false" || value === "missing") return "changed";
      return "neutral";
    case "alert_code":
      if (value.endsWith("_changed") || value.endsWith("_invalidated")) return "changed";
      return "info";
    case "attention_category":
      if (value === "conflict" || value === "gap") return "changed";
      if (value === "presence") return "info";
      return "neutral";
    case "delivery_status":
      if (value === "delivered") return "positive";
      if (value === "failed") return "negative";
      return "neutral";
    case "timeframe_pattern":
    case "subject_kind":
      return "neutral";
  }
}

/** Kinds of referenced objects that have a detail route in the dashboard. */
export const OBJECT_ROUTE: Readonly<Record<string, string>> = {
  market_intelligence: "/market-intelligence",
  alert_event: "/alerts",
};
