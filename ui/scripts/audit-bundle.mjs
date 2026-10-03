#!/usr/bin/env node
// Phase 16B bundle audit: proves the built dist/ is self-contained and secret-free.
// Usage: node scripts/audit-bundle.mjs <dist-dir>. Exit 0 = clean; 1 = findings (printed without secret values).
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const dist = process.argv[2] ?? "dist";
const findings = [];
const files = [];
(function walk(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) walk(path);
    else files.push(path);
  }
})(dist);

// Documentation URLs embedded in library error messages / XML namespaces. Never fetched by the app.
const ALLOWED_URL_PREFIXES = [
  "https://react.dev/errors/",
  "https://reactrouter.com/",
  "http://www.w3.org/",
  "https://tanstack.com/",
];
// Exact strings React Router uses only as a dummy base for `new URL()` parsing (never requested).
const ALLOWED_URL_EXACT = new Set(["http://localhost"]);

const CHECKS = [
  [/sourceMappingURL/, "source map reference"],
  [/placeholder-read-token/, "test placeholder token"],
  [/Bearer\s+[A-Za-z0-9._~+\/=-]{16,}/, "literal bearer token"],
  [/MIAS_API_(READ|OPERATOR)_TOKEN/, "API token variable name"],
  [/VITE_[A-Z0-9_]+/, "Vite env variable"],
  [/\bsessionStorage\b/, "sessionStorage"],
  [/\bindexedDB\b/, "indexedDB"],
  [/document\.cookie/, "document.cookie"],
  [/fonts\.googleapis|fonts\.gstatic|cdn\.jsdelivr|unpkg\.com|cdnjs|googletagmanager|google-analytics|segment\.io|sentry/i, "CDN/analytics host"],
  [/-----BEGIN [A-Z ]*PRIVATE KEY-----/, "private key"],
  // Phase 16D: the browser must never call infrastructure APIs directly.
  [/\/api\/v1\/query(_range)?\b|thanos-querier|:9090\b|:9091\b|:4317\b|:4318\b|:8889\b|\/apis\/[a-z.]+\/v1|openshift-monitoring|\/v1\/traces|\/v1\/metrics/, "infrastructure endpoint"],
  [/eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\./, "JWT-like string"],
];

for (const file of files) {
  const rel = relative(dist, file);
  if (rel.endsWith(".map")) findings.push(`${rel}: source map file`);
  if (!/\.(html|js|css|svg|txt|json)$/.test(rel)) continue;
  const text = readFileSync(file, "utf8");
  for (const [pattern, label] of CHECKS) {
    if (pattern.test(text)) findings.push(`${rel}: ${label}`);
  }
  // localStorage is allowed for exactly one purpose: the non-sensitive theme preference under the literal key
  // "mias-ui-theme" (Phase 16E). Every other use — any other key, a computed key, or a bare reference — fails.
  if (rel.endsWith(".js")) {
    for (const m of text.matchAll(/localStorage/g)) {
      const after = text.slice(m.index + "localStorage".length, m.index + "localStorage".length + 40);
      if (!/^\.(getItem|setItem|removeItem)\(["'`]mias-ui-theme["'`]/.test(after)) findings.push(`${rel}: localStorage use other than the theme key`);
    }
  }
  for (const match of text.matchAll(/https?:\/\/[^\s"'`)<>\\]+/g)) {
    const url = match[0];
    if (!ALLOWED_URL_EXACT.has(url) && !ALLOWED_URL_PREFIXES.some((p) => url.startsWith(p))) findings.push(`${rel}: external URL ${url}`);
  }
  if (rel.endsWith(".html")) {
    if (/<script(?![^>]*\bsrc=)[^>]*>/i.test(text)) findings.push(`${rel}: inline <script>`);
    if (/<script[^>]*src=["']?(https?:)?\/\//i.test(text)) findings.push(`${rel}: external <script>`);
    if (/<link[^>]*href=["']?(https?:)?\/\//i.test(text)) findings.push(`${rel}: external <link>`);
    if (/\sstyle=/i.test(text)) findings.push(`${rel}: inline style attribute`);
    if (/<style[\s>]/i.test(text)) findings.push(`${rel}: inline <style>`);
  }
  if (rel.endsWith(".css") && /@import|url\(\s*["']?(https?:)?\/\//i.test(text)) findings.push(`${rel}: external CSS import/url`);
}

const total = files.reduce((n, f) => n + statSync(f).size, 0);
const urls = new Set();
for (const file of files.filter((f) => f.endsWith(".js"))) {
  for (const m of readFileSync(file, "utf8").matchAll(/https?:\/\/[^\s"'`)<>\\]+/g)) urls.add(m[0].replace(/(errors\/).*/, "$1…"));
}
console.log(`allowed non-fetched URL strings in JS: ${[...urls].sort().join(", ") || "none"}`);
console.log(`audited ${files.length} files, ${total} bytes in ${dist}`);
for (const f of files) console.log(`  ${relative(dist, f)}  ${statSync(f).size}`);
if (findings.length > 0) {
  console.log(`FAIL: ${findings.length} finding(s)`);
  for (const f of findings) console.log(`  - ${f}`);
  process.exit(1);
}
console.log("PASS: no source maps, tokens, auth/session storage (localStorage only for the theme key), inline scripts/styles, CDN/analytics, infrastructure endpoints or unexpected external URLs");
