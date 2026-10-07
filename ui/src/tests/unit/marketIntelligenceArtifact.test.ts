import { describe, expect, it } from "vitest";
import {
  INTERVALS,
  intervalReadings,
  READING_LABEL,
  readMarketIntelligence,
  type TimeframeStructure,
} from "../../lib/marketIntelligenceArtifact";
import { fx } from "../api16c";

function structure(pattern: string, lists: Partial<Pick<TimeframeStructure, "directional_intervals" | "non_directional_intervals" | "unavailable_intervals">>): TimeframeStructure {
  return {
    pattern,
    directional_intervals: [],
    non_directional_intervals: [],
    unavailable_intervals: [],
    opposition_shape: "none",
    isolated_interval: null,
    opposing_pairs: [],
    ...lists,
  };
}

const readings = (s: TimeframeStructure) => intervalReadings(s).map((r) => [r.interval, r.reading]);
const ALL = ["1d", "1h", "5m"];

describe("interval readings (exact, never inferred)", () => {
  it("are always 1d, 1h, 5m in that order", () => {
    expect(INTERVALS).toEqual(ALL);
    expect(intervalReadings(structure("opposed", { directional_intervals: ["5m", "1d", "1h"] })).map((r) => r.interval)).toEqual(ALL);
  });

  it("all_bullish: every interval bullish", () => {
    expect(readings(structure("all_bullish", { directional_intervals: ALL }))).toEqual([["1d", "bullish"], ["1h", "bullish"], ["5m", "bullish"]]);
  });

  it("all_bearish: every interval bearish", () => {
    expect(readings(structure("all_bearish", { directional_intervals: ALL }))).toEqual([["1d", "bearish"], ["1h", "bearish"], ["5m", "bearish"]]);
  });

  it("opposed: directional without a direction, neutral where recorded", () => {
    expect(readings(structure("opposed", { directional_intervals: ["1d", "5m"], non_directional_intervals: ["1h"] }))).toEqual([
      ["1d", "directional"], ["1h", "neutral"], ["5m", "directional"],
    ]);
  });

  it("partially_directional: the direction is not recorded", () => {
    expect(readings(structure("partially_directional", { directional_intervals: ["1d"], non_directional_intervals: ["1h", "5m"] }))).toEqual([
      ["1d", "directional"], ["1h", "neutral"], ["5m", "neutral"],
    ]);
  });

  it("all_non_directional: every interval neutral", () => {
    expect(readings(structure("all_non_directional", { non_directional_intervals: ALL }))).toEqual([["1d", "neutral"], ["1h", "neutral"], ["5m", "neutral"]]);
  });

  it("incomplete: unavailable intervals stay unavailable", () => {
    expect(readings(structure("incomplete", { directional_intervals: ["1d"], non_directional_intervals: ["1h"], unavailable_intervals: ["5m"] }))).toEqual([
      ["1d", "directional"], ["1h", "neutral"], ["5m", "unavailable"],
    ]);
  });

  it("never guesses: a uniform pattern that disagrees with the lists, or an interval listed 0 or 2 times", () => {
    expect(readings(structure("all_bullish", { directional_intervals: ["1d", "1h"], non_directional_intervals: ["5m"] }))).toEqual([
      ["1d", "directional"], ["1h", "directional"], ["5m", "neutral"],
    ]);
    expect(readings(structure("opposed", { directional_intervals: ["1d", "1h"], non_directional_intervals: ["1h"] }))).toEqual([
      ["1d", "directional"], ["1h", "not_recorded"], ["5m", "not_recorded"],
    ]);
  });

  it("labels each reading in words", () => {
    expect(READING_LABEL).toEqual({
      bullish: "Bullish",
      bearish: "Bearish",
      directional: "Directional (direction not recorded)",
      neutral: "Neutral",
      unavailable: "Unavailable",
      not_recorded: "Not recorded",
    });
  });
});

describe("reading the canonical artifact", () => {
  it("reads the captured artifacts (all_bullish and opposed)", () => {
    const bullish = readMarketIntelligence(fx("market-intelligence:canonical:0").text ?? "");
    expect(bullish?.timeframe_structure.pattern).toBe("all_bullish");
    expect(bullish?.market_context_alignment.by_interval.map((r) => r.interval)).toEqual(ALL);
    expect(bullish?.comparison).toBeNull();
    const opposed = readMarketIntelligence(fx("market-intelligence:canonical:2").text ?? "");
    expect(opposed?.timeframe_structure).toMatchObject({
      pattern: "opposed",
      opposition_shape: "isolated_interval",
      isolated_interval: "5m",
      opposing_pairs: [["1h", "5m"], ["1d", "5m"]],
    });
    expect(opposed?.conflicts.length).toBeGreaterThan(0);
  });

  it("returns null for text that is not the recorded shape", () => {
    const good = JSON.parse(fx("market-intelligence:canonical:0").text ?? "") as Record<string, unknown>;
    expect(readMarketIntelligence("not json")).toBeNull();
    expect(readMarketIntelligence("[]")).toBeNull();
    expect(readMarketIntelligence(JSON.stringify({ ...good, timeframe_structure: { pattern: "all_bullish" } }))).toBeNull();
    expect(readMarketIntelligence(JSON.stringify({ ...good, conflicts: [{ code: 1, subjects: [] }] }))).toBeNull();
  });
});
