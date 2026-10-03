/**
 * WCAG 2.1 AA contrast of the design tokens in both themes, computed from app.css itself (Phase 16E review).
 * Text pairs need 4.5:1; large/bold-only or non-text UI (focus ring, input borders, status cell symbols) need 3:1.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { cwd } from "node:process";
import { describe, expect, it } from "vitest";

const css = readFileSync(join(cwd(), "src", "styles", "app.css"), "utf8");

function tokens(block) {
  const out = {};
  for (const m of block.matchAll(/--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;/g)) out[m[1]] = m[2].toLowerCase();
  return out;
}

function blockAfter(marker) {
  const start = css.indexOf(marker);
  if (start < 0) throw new Error(`missing ${marker}`);
  const open = css.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < css.length; i += 1) {
    if (css[i] === "{") depth += 1;
    if (css[i] === "}") depth -= 1;
    if (depth === 0) return css.slice(open + 1, i);
  }
  throw new Error("unbalanced");
}

const light = tokens(blockAfter(":root {"));
const dark = tokens(blockAfter(':root[data-theme="dark"] {'));
const systemDark = tokens(blockAfter(':root:not([data-theme="light"]) {'));

function luminance(hex) {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function ratio(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const TEXT_PAIRS = [
  ["text", "bg"], ["text", "surface"], ["text", "surface-2"], ["text", "surface-sunken"], ["text", "neutral-bg"],
  ["text", "error-bg"], ["text", "accent-soft"],
  ["muted", "surface"], ["subtle", "surface"], ["subtle", "surface-2"], ["subtle", "bg"], ["subtle", "surface-sunken"],
  ["accent", "surface"], ["accent", "surface-2"], ["accent", "accent-soft"], ["accent", "bg"], ["accent-text", "accent"],
  ["ok", "ok-bg"], ["ok", "surface"], ["degraded", "degraded-bg"], ["degraded", "surface"],
  ["down", "down-bg"], ["down", "surface"], ["down", "error-bg"], ["info", "info-bg"], ["info", "surface"],
];
/** Non-text: the focus ring and the borders of interactive controls (inputs, quiet buttons, toggles). */
const UI_PAIRS = [
  ["focus", "surface"], ["focus", "bg"], ["focus", "surface-2"],
  ["control-border", "surface"], ["control-border", "bg"], ["control-border", "surface-2"],
];

describe.each([
  ["light", light],
  ["dark", dark],
])("%s theme contrast", (_name, t) => {
  it.each(TEXT_PAIRS)("%s on %s ≥ 4.5:1", (fg, bg) => {
    expect(t[fg], fg).toBeDefined();
    expect(t[bg], bg).toBeDefined();
    expect(ratio(t[fg], t[bg])).toBeGreaterThanOrEqual(4.5);
  });
  it.each(UI_PAIRS)("%s on %s ≥ 3:1 (non-text)", (fg, bg) => {
    expect(ratio(t[fg], t[bg])).toBeGreaterThanOrEqual(3);
  });
});

it("uses identical tokens for chosen Dark and System-in-dark", () => {
  expect(systemDark).toEqual(dark);
  expect(Object.keys(dark).sort()).toEqual(Object.keys(light).sort());
});
