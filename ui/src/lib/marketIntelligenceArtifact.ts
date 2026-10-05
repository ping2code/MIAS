/**
 * The full Market Intelligence artifact (phase8-v1), read from the exact canonical text the API returned
 * (`GET …/market-intelligence/{id}/canonical`). Only the parts the detail page shows are read; anything that is not
 * the recorded shape makes the whole read `null`, so the page falls back to the summary instead of guessing.
 *
 * Interval readings never infer a direction. The artifact records which intervals are directional, non-directional
 * or unavailable, but not whether a directional interval is bullish or bearish. A direction is shown only where the
 * recorded pattern itself fixes it for every interval: `all_bullish` (all three bullish) and `all_bearish` (all three
 * bearish). Any other directional interval is "directional, direction not recorded".
 */

export const INTERVALS = ["1d", "1h", "5m"] as const;
export type Interval = (typeof INTERVALS)[number];

export type IntervalReading = "bullish" | "bearish" | "directional" | "neutral" | "unavailable" | "not_recorded";

export interface ReferenceKey {
  reference: string;
  basis: string;
}

export interface ReferenceSign extends ReferenceKey {
  value_sign: string;
}

export interface TimeframeStructure {
  pattern: string;
  directional_intervals: string[];
  non_directional_intervals: string[];
  unavailable_intervals: string[];
  opposition_shape: string;
  isolated_interval: string | null;
  opposing_pairs: [string, string][];
}

export interface IntervalContext {
  interval: string;
  agree_count: number;
  oppose_count: number;
  non_directional_count: number;
  unavailable_count: number;
  opposing_references: ReferenceKey[];
}

export interface MarketContextAlignment {
  available: boolean;
  freshness_status: string | null;
  context_age_seconds: number | null;
  own_return_profile: string | null;
  own_return_signs: ReferenceSign[];
  relative_return_profile: string | null;
  relative_return_signs: ReferenceSign[];
  by_interval: IntervalContext[];
}

export interface CodedItem {
  code: string;
  subjects: string[];
}

export interface AttentionItemFull extends CodedItem {
  category: string;
}

export interface Comparison {
  status: string;
  reasons: string[];
  elapsed_seconds: number | null;
  previous_synthesis_id: string | null;
}

export interface MarketIntelligenceArtifact {
  intelligence_id: string;
  timeframe_structure: TimeframeStructure;
  market_context_alignment: MarketContextAlignment;
  attention: AttentionItemFull[];
  conflicts: CodedItem[];
  comparison: Comparison | null;
  transitions: CodedItem[];
}

type Json = Record<string, unknown>;

const isObject = (v: unknown): v is Json => typeof v === "object" && v !== null && !Array.isArray(v);
const isString = (v: unknown): v is string => typeof v === "string";
const isStringArray = (v: unknown): v is string[] => Array.isArray(v) && v.every(isString);
const isCount = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v) && v >= 0;
const optString = (v: unknown): string | null | undefined => (v === null ? null : isString(v) ? v : undefined);
const optCount = (v: unknown): number | null | undefined => (v === null ? null : typeof v === "number" ? v : undefined);

class Malformed extends Error {}

function need<T>(value: T | undefined): T {
  if (value === undefined) throw new Malformed();
  return value;
}

function list<T>(value: unknown, item: (v: unknown) => T): T[] {
  if (!Array.isArray(value)) throw new Malformed();
  return value.map(item);
}

function strings(value: unknown): string[] {
  if (!isStringArray(value)) throw new Malformed();
  return value;
}

function referenceKey(v: unknown): ReferenceKey {
  if (!isObject(v) || !isString(v.reference) || !isString(v.basis)) throw new Malformed();
  return { reference: v.reference, basis: v.basis };
}

function referenceSign(v: unknown): ReferenceSign {
  if (!isObject(v) || !isString(v.value_sign)) throw new Malformed();
  return { ...referenceKey(v), value_sign: v.value_sign };
}

function coded(v: unknown): CodedItem {
  if (!isObject(v) || !isString(v.code)) throw new Malformed();
  return { code: v.code, subjects: strings(v.subjects) };
}

function timeframeStructure(v: unknown): TimeframeStructure {
  if (!isObject(v) || !isString(v.pattern) || !isString(v.opposition_shape)) throw new Malformed();
  return {
    pattern: v.pattern,
    directional_intervals: strings(v.directional_intervals),
    non_directional_intervals: strings(v.non_directional_intervals),
    unavailable_intervals: strings(v.unavailable_intervals),
    opposition_shape: v.opposition_shape,
    isolated_interval: need(optString(v.isolated_interval)),
    opposing_pairs: list(v.opposing_pairs, (pair) => {
      if (!isStringArray(pair) || pair.length !== 2) throw new Malformed();
      return [pair[0] as string, pair[1] as string];
    }),
  };
}

function marketContextAlignment(v: unknown): MarketContextAlignment {
  if (!isObject(v) || typeof v.available !== "boolean") throw new Malformed();
  return {
    available: v.available,
    freshness_status: need(optString(v.freshness_status ?? null)),
    context_age_seconds: need(optCount(v.context_age_seconds ?? null)),
    own_return_profile: need(optString(v.own_return_profile ?? null)),
    own_return_signs: list(v.own_return_signs ?? [], referenceSign),
    relative_return_profile: need(optString(v.relative_return_profile ?? null)),
    relative_return_signs: list(v.relative_return_signs ?? [], referenceSign),
    by_interval: list(v.by_interval, (row) => {
      if (!isObject(row) || !isString(row.interval)) throw new Malformed();
      for (const key of ["agree_count", "oppose_count", "non_directional_count", "unavailable_count"]) {
        if (!isCount(row[key])) throw new Malformed();
      }
      return {
        interval: row.interval,
        agree_count: row.agree_count as number,
        oppose_count: row.oppose_count as number,
        non_directional_count: row.non_directional_count as number,
        unavailable_count: row.unavailable_count as number,
        opposing_references: list(row.opposing_references, referenceKey),
      };
    }),
  };
}

function comparison(v: unknown): Comparison | null {
  if (v === null) return null;
  if (!isObject(v) || !isString(v.status)) throw new Malformed();
  const previous = v.previous_ref;
  return {
    status: v.status,
    reasons: strings(v.reasons ?? []),
    elapsed_seconds: need(optCount(v.elapsed_seconds ?? null)),
    previous_synthesis_id: isObject(previous) && isString(previous.synthesis_id) ? previous.synthesis_id : null,
  };
}

/** The recorded artifact, or null when the text is not JSON or not the recorded shape. */
export function readMarketIntelligence(text: string): MarketIntelligenceArtifact | null {
  try {
    const v = JSON.parse(text) as unknown;
    if (!isObject(v) || !isString(v.intelligence_id)) return null;
    return {
      intelligence_id: v.intelligence_id,
      timeframe_structure: timeframeStructure(v.timeframe_structure),
      market_context_alignment: marketContextAlignment(v.market_context_alignment),
      attention: list(v.attention, (item) => {
        if (!isObject(item) || !isString(item.category)) throw new Malformed();
        return { ...coded(item), category: item.category };
      }),
      conflicts: list(v.conflicts, coded),
      comparison: comparison(v.comparison ?? null),
      transitions: list(v.transitions ?? [], coded),
    };
  } catch {
    return null;
  }
}

/** Patterns that fix one direction for every interval (evidence_synthesis.rules.timeframe_pattern). */
const UNIFORM_DIRECTION: Readonly<Record<string, "bullish" | "bearish">> = {
  all_bullish: "bullish",
  all_bearish: "bearish",
};

/**
 * One reading per tracked interval, in the order 1d, 1h, 5m. An interval listed in exactly one of the recorded
 * lists gets that list's reading; an interval listed in none or several is "not_recorded" (never guessed). A
 * directional interval is bullish or bearish only under all_bullish / all_bearish with all three intervals directional.
 */
export function intervalReadings(structure: TimeframeStructure): { interval: Interval; reading: IntervalReading }[] {
  // The pattern fixes the direction only if the lists agree with it: every interval recorded as directional.
  const uniform = INTERVALS.every((i) => structure.directional_intervals.includes(i))
    ? UNIFORM_DIRECTION[structure.pattern]
    : undefined;
  return INTERVALS.map((interval) => {
    const directional = structure.directional_intervals.includes(interval);
    const neutral = structure.non_directional_intervals.includes(interval);
    const unavailable = structure.unavailable_intervals.includes(interval);
    if (Number(directional) + Number(neutral) + Number(unavailable) !== 1) return { interval, reading: "not_recorded" };
    if (neutral) return { interval, reading: "neutral" };
    if (unavailable) return { interval, reading: "unavailable" };
    return { interval, reading: uniform ?? "directional" };
  });
}

export const READING_LABEL: Readonly<Record<IntervalReading, string>> = {
  bullish: "Bullish",
  bearish: "Bearish",
  directional: "Directional (direction not recorded)",
  neutral: "Neutral",
  unavailable: "Unavailable",
  not_recorded: "Not recorded",
};

/** A text symbol per reading, so the reading never depends on colour. */
export const READING_SYMBOL: Readonly<Record<IntervalReading, string>> = {
  bullish: "▲",
  bearish: "▼",
  directional: "◆",
  neutral: "•",
  unavailable: "–",
  not_recorded: "?",
};
