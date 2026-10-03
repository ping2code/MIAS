#!/usr/bin/env node
// Phase 16E real-browser QA for mias-ui (local only; never part of the image or the npm gates).
//
// Drives a real Chromium over CDP (e.g. the chromedp/headless-shell container on the host network) against a
// running mias-ui (same-origin proxy) + mias-api. The read token comes from the environment and is never printed.
//
//   MIAS_QA_URL=http://127.0.0.1:18082 MIAS_QA_CDP=http://127.0.0.1:9222 MIAS_QA_TOKEN=… \
//   MIAS_QA_OUT=<dir for screenshots/downloads> MIAS_QA_RESTART=<script that restarts the API with MIAS_QA_TOKEN2> \
//   MIAS_QA_TOKEN2=… PLAYWRIGHT_CORE=<path to a playwright-core install> node ui/scripts/browser-qa.mjs
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";

const require = createRequire(join(process.env.PLAYWRIGHT_CORE ?? ".", "package.json"));
const { chromium } = require("playwright-core");

const BASE = process.env.MIAS_QA_URL ?? "http://127.0.0.1:18082";
const CDP = process.env.MIAS_QA_CDP ?? "http://127.0.0.1:9222";
const TOKEN = process.env.MIAS_QA_TOKEN;
const OUT = process.env.MIAS_QA_OUT ?? "./qa-out";
if (!TOKEN) throw new Error("MIAS_QA_TOKEN is required");
mkdirSync(join(OUT, "shots"), { recursive: true });
mkdirSync(join(OUT, "downloads"), { recursive: true });

let pass = 0;
let fail = 0;
function check(name, ok, detail = "") {
  if (ok) pass += 1;
  else fail += 1;
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${!ok && detail ? ` — ${detail}` : ""}`);
}
const redact = (s) => String(s).split(TOKEN).join("<token>");

async function apiText(path) {
  const res = await fetch(BASE + path, { headers: { Authorization: `Bearer ${TOKEN}` } });
  return { status: res.status, text: await res.text(), etag: res.headers.get("etag") };
}

const browser = await chromium.connectOverCDP(CDP);
console.log(`browser: ${browser.version()}`);
const context = await browser.newContext({ viewport: { width: 1920, height: 1080 }, acceptDownloads: true });
await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: BASE });
const page = await context.newPage();
const consoleProblems = [];
page.on("console", (m) => {
  if (m.type() === "error" || /Content Security Policy|Refused to/i.test(m.text())) consoleProblems.push(redact(m.text()));
});
page.on("pageerror", (e) => consoleProblems.push(redact(e.message)));
await context.addInitScript(() => {
  window.__cspViolations = [];
  document.addEventListener("securitypolicyviolation", (e) => {
    window.__cspViolations.push(`${e.violatedDirective} ${e.blockedURI}`);
  });
});
const requests = [];
page.on("request", (r) => requests.push(r));

async function shot(name) {
  await page.screenshot({ path: join(OUT, "shots", `${name}.png`), fullPage: false });
}
const h1 = () => page.locator("h1").first();

// ---- Sign-in: deep link is preserved through sign-in
await page.goto(`${BASE}/status`);
await page.waitForURL(/\/signin$/);
check("unauthenticated deep link redirects to /signin", page.url().endsWith("/signin"));
await shot("desktop-1920-signin-light");
await page.getByLabel("Read token").fill("wrong-token-wrong-token-wrong-token");
await page.getByRole("button", { name: "Sign in" }).click();
await page.getByText("Invalid or expired read token").waitFor();
check("wrong token shows 'Invalid or expired read token'", true);
check("focus returns to the token field after a failed sign-in", await page.evaluate(() => document.activeElement?.getAttribute("name") === "mias-read-token"));
await page.getByLabel("Read token").fill(TOKEN);
await page.getByRole("button", { name: "Sign in" }).click();
await page.getByRole("heading", { name: "System Status", level: 1 }).waitFor();
check("sign-in returns to the requested page (/status)", new URL(page.url()).pathname === "/status");
check("status page heading has focus", await page.evaluate(() => document.activeElement?.tagName === "H1"));
await page.getByText(/3 of 3 checks pass/).waitFor();
check("status shows the API's readiness checks", true);
await shot("desktop-1920-status-light");

// ---- Overview, MI list and detail
await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Overview" }).click();
await page.getByRole("heading", { name: "Overview", level: 1 }).waitFor();
await page.getByText("Artifacts by kind").waitFor();
await shot("desktop-1920-overview-light");
await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Market Intelligence" }).click();
const miTable = page.getByRole("table", { name: /Market intelligence history/ });
await miTable.waitFor();
check("MI history lists rows", (await miTable.locator("tbody tr").count()) === 3);
await shot("desktop-1920-mi-list-light");
await page.getByLabel("Symbol").fill("meta");
await page.getByRole("button", { name: "Apply" }).click();
await page.waitForURL(/symbol=META/);
check("MI filter applies as URL state", new URL(page.url()).searchParams.get("symbol") === "META");
await miTable.locator("tbody tr").first().getByRole("link", { name: /^Open/ }).click();
await page.getByRole("tab", { name: "Raw / Canonical" }).waitFor();
const miId = decodeURIComponent(new URL(page.url()).pathname.split("/").pop());
check("MI detail opened", /^sha256:[0-9a-f]{64}$/.test(miId));
await shot("desktop-1920-mi-detail-light");
await page.getByRole("tab", { name: "Raw / Canonical" }).click();
const pre = page.getByLabel("Canonical text (exact)");
await pre.waitFor();
const canonical = await apiText(`/api/v1/market-intelligence/${miId}/canonical`);
check("canonical tab shows the exact API text", (await pre.textContent()) === canonical.text);
check("ETag matches the artifact id (UI check)", await page.getByText("Matches artifact id").isVisible());
await shot("desktop-1920-mi-canonical-light");

// Copy canonical (secure context: http://127.0.0.1 / localhost)
await page.getByRole("button", { name: "Copy canonical text" }).click();
await page.getByText("Copied").waitFor();
const clip = await page.evaluate(() => navigator.clipboard.readText());
check("Copy Canonical puts the exact canonical text on the clipboard", clip === canonical.text, `length ${clip.length} vs ${canonical.text.length}`);

// Download canonical: blob URL under the production CSP
const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: /Download canonical/ }).click()]);
const expectedName = `${miId.slice(7)}.json`;
check("download file name is <64 hex>.json", download.suggestedFilename() === expectedName, download.suggestedFilename());
const dlPath = join(OUT, "downloads", download.suggestedFilename());
await download.saveAs(dlPath);
const dlBytes = readFileSync(dlPath);
check("downloaded bytes equal the API canonical bytes", dlBytes.equals(Buffer.from(canonical.text, "utf8")), `size ${dlBytes.length}`);
check("download file name/URL carry no token", !download.suggestedFilename().includes(TOKEN) && !download.url().includes(TOKEN));
check("download used a same-origin blob: URL", download.url().startsWith(`blob:${BASE}`), download.url().slice(0, 40));
await page.getByText(`Saved as ${expectedName}`).waitFor();
check("download is announced", true);

// ---- Alerts
await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Alerts" }).click();
const alertTable = page.getByRole("table", { name: /Alert history/ });
await alertTable.waitFor();
await shot("desktop-1920-alerts-light");
await alertTable.locator("tbody tr").first().getByRole("link", { name: /^Open/ }).click();
await page.getByText("Delivery information is not available in this deployment.").waitFor();
check("alert detail shows deliveries as an unavailable capability", true);
await shot("desktop-1920-alert-detail-light");

// ---- Theme
await page.getByRole("radio", { name: /Dark/ }).check();
const darkBg = await page.evaluate(() => [document.documentElement.dataset.theme, getComputedStyle(document.body).backgroundColor]);
check("Dark theme applies data-theme and dark tokens", darkBg[0] === "dark" && darkBg[1] === "rgb(15, 19, 24)", darkBg.join(" "));
const stored = await page.evaluate(() => Object.keys(localStorage).map((k) => [k, localStorage.getItem(k)]));
check("only the theme key is in localStorage", JSON.stringify(stored) === '[["mias-ui-theme","dark"]]', JSON.stringify(stored));
check("sessionStorage and cookies are empty", await page.evaluate(() => sessionStorage.length === 0 && document.cookie === ""));
await shot("desktop-1920-alert-detail-dark");
for (const [name, path] of [["overview", "/"], ["mi-list", "/market-intelligence"], ["status", "/status"]]) {
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: { "/": "Overview", "/market-intelligence": "Market Intelligence", "/status": "System Status" }[path] }).click();
  await h1().waitFor();
  await page.waitForTimeout(400);
  await shot(`desktop-1920-${name}-dark`);
}
await page.getByRole("radio", { name: /Light/ }).check();
const lightBg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
check("Light theme applies light tokens", lightBg === "rgb(243, 245, 248)", lightBg);
await page.emulateMedia({ colorScheme: "dark" });
await page.getByRole("radio", { name: /System/ }).check();
check("System follows prefers-color-scheme: dark", (await page.evaluate(() => getComputedStyle(document.body).backgroundColor)) === "rgb(15, 19, 24)");
await page.emulateMedia({ colorScheme: "light" });
check("System follows prefers-color-scheme: light", (await page.evaluate(() => getComputedStyle(document.body).backgroundColor)) === "rgb(243, 245, 248)");

// ---- 1440p screenshots
await page.setViewportSize({ width: 2560, height: 1440 });
for (const [name, link] of [["overview", "Overview"], ["mi-list", "Market Intelligence"], ["alerts", "Alerts"], ["status", "System Status"]]) {
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: link }).click();
  await h1().waitFor();
  await page.waitForTimeout(400);
  await shot(`desktop-1440p-${name}-light`);
}

// ---- Lock / unlock
await page.setViewportSize({ width: 1920, height: 1080 });
await page.getByRole("button", { name: "Lock" }).click();
await page.getByRole("heading", { name: "MIAS is locked" }).waitFor();
check("lock hides the dashboard (no main, no nav)", (await page.locator("main").count()) === 0 && (await page.getByRole("navigation").count()) === 0);
check("lock screen heading has focus", await page.evaluate(() => document.activeElement?.textContent === "MIAS is locked"));
const before = requests.length;
await page.waitForTimeout(1500);
check("no API requests while locked", requests.slice(before).filter((r) => r.url().includes("/api/")).length === 0);
await shot("desktop-1920-locked-light");
await page.getByLabel("Read token").fill("wrong-token-wrong-token-wrong-token");
await page.getByRole("button", { name: "Unlock" }).click();
await page.getByText("That token does not match this session. The dashboard stays locked.").waitFor();
check("wrong token keeps the dashboard locked", true);
await page.getByLabel("Read token").fill(TOKEN);
await page.getByRole("button", { name: "Unlock" }).click();
await page.getByRole("heading", { name: "System Status", level: 1 }).waitFor();
check("same token unlocks back to the same page", new URL(page.url()).pathname === "/status");

// ---- Tablet and phone navigation drawer
for (const [label, w, hgt] of [["tablet-820", 820, 1180], ["mobile-390", 390, 844]]) {
  await page.setViewportSize({ width: w, height: hgt });
  const menu = page.getByRole("button", { name: /Menu/ });
  await menu.waitFor();
  check(`${label}: side navigation replaced by Menu`, (await page.locator(".sidenav").count()) === 0);
  await shot(`${label}-status-light`);
  await menu.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Navigation" });
  await dialog.waitFor();
  check(`${label}: drawer opens from the keyboard with focus inside`, await dialog.evaluate((d) => d.contains(document.activeElement)));
  check(`${label}: background is inert`, await page.evaluate(() => document.querySelector("main")?.inert === true));
  await shot(`${label}-drawer-light`);
  await page.keyboard.press("Escape");
  await dialog.waitFor({ state: "detached" });
  check(`${label}: Escape closes and focus returns to Menu`, await menu.evaluate((b) => b === document.activeElement));
  await menu.click();
  await page.getByRole("dialog").getByRole("link", { name: "Market Intelligence" }).click();
  await page.getByRole("heading", { name: "Market Intelligence", level: 1 }).waitFor();
  check(`${label}: choosing a page closes the drawer and focuses its heading`, (await page.getByRole("dialog").count()) === 0 && (await page.evaluate(() => document.activeElement?.tagName === "H1")));
  await page.waitForTimeout(300);
  await shot(`${label}-mi-list-light`);
  await page.getByRole("radio", { name: /Dark/ }).check();
  await page.waitForTimeout(200);
  await shot(`${label}-mi-list-dark`);
  await page.getByRole("radio", { name: /Light/ }).check();
}

// ---- Reduced motion
await page.emulateMedia({ reducedMotion: "reduce" });
await page.getByRole("button", { name: /Menu/ }).click();
const anim = await page.locator(".drawer").evaluate((d) => getComputedStyle(d).animationName);
check("reduced motion removes the drawer animation", anim === "none", anim);
await page.keyboard.press("Escape");
await page.emulateMedia({ reducedMotion: "no-preference" });
await page.setViewportSize({ width: 1920, height: 1080 });

// ---- Session ended: the API's token is rotated while the user is signed in
if (process.env.MIAS_QA_RESTART && process.env.MIAS_QA_TOKEN2) {
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Alerts" }).click();
  await page.getByRole("table", { name: /Alert history/ }).waitFor();
  execFileSync("bash", [process.env.MIAS_QA_RESTART], { stdio: "ignore" }); // API now accepts only MIAS_QA_TOKEN2
  await page.getByLabel("Symbol").fill("ZZZZ"); // a new query: guaranteed to reach the API
  await page.getByRole("button", { name: "Apply" }).click();
  await page.getByText("Your MIAS session ended. Sign in again to continue.").waitFor({ timeout: 30000 });
  check("401 mid-session lands on /signin with the session-ended banner", new URL(page.url()).pathname === "/signin");
  check("session-ended banner says the user will return", await page.getByText("You will return to the page you were on.").isVisible());
  await shot("desktop-1920-session-ended-light");
  await page.getByLabel("Read token").fill(process.env.MIAS_QA_TOKEN2);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("heading", { name: "Alerts", level: 1 }).waitFor();
  const back = new URL(page.url());
  check("after re-sign-in the user returns to the same page and filters", back.pathname === "/alerts" && back.searchParams.get("symbol") === "ZZZZ", back.pathname + back.search);
  await page.getByText("No alerts are available for the selected filters.").waitFor();
  await page.reload();
  await page.waitForURL(/\/signin$/);
  check("a reload requires sign-in again (the token was memory-only)", true);
  check("theme preference (non-sensitive) survives the reload", (await page.evaluate(() => document.documentElement.dataset.theme ?? "system")) === "light");
}

const csp = await page.evaluate(() => window.__cspViolations);
check("no CSP violations in the session", csp.length === 0, csp.join("; "));
check("no console errors or page errors", consoleProblems.filter((m) => !/401|Unauthorized|Failed to load resource/.test(m)).length === 0, consoleProblems.join(" | "));
const external = requests.map((r) => new URL(r.url())).filter((u) => u.protocol.startsWith("http") && u.origin !== BASE);
check("every request stayed same-origin", external.length === 0, external.map((u) => u.origin).join(","));
check("no token in any request URL", requests.every((r) => !r.url().includes(TOKEN)));
check("Authorization sent only to /api/v1", requests.every((r) => !r.headers().authorization || new URL(r.url()).pathname.startsWith("/api/v1/")));

await context.close();
await browser.close();
console.log(`browser-qa: ${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
