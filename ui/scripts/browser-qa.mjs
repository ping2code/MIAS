#!/usr/bin/env node
// Phase 16E real-browser QA for mias-ui (local only; never part of the image or the npm gates).
//
// Drives a real Chromium over CDP against a running mias-ui behind oauth-proxy (Hardening Task 8). The dashboard has
// no sign-in of its own: start Chrome with --remote-debugging-port, log in to MIAS through OpenShift OAuth in that
// browser, then run this script. It reuses the browser's default (logged-in) context and never handles a password,
// session cookie or API token; nginx adds the read token server-side.
//
//   MIAS_QA_URL=https://mias-ui.apps.<domain> MIAS_QA_CDP=http://127.0.0.1:9222 \
//   MIAS_QA_OUT=<dir for screenshots/downloads> PLAYWRIGHT_CORE=<path to a playwright-core install> \
//   node ui/scripts/browser-qa.mjs
//
// The browser must trust the MIAS lab CA (docs/tls/mias-lab-ca.crt); give Node the CA with NODE_EXTRA_CA_CERTS.
import { mkdirSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";

const require = createRequire(join(process.env.PLAYWRIGHT_CORE ?? ".", "package.json"));
const { chromium } = require("playwright-core");

const BASE = process.env.MIAS_QA_URL ?? "http://127.0.0.1:18082";
const CDP = process.env.MIAS_QA_CDP ?? "http://127.0.0.1:9222";
const OUT = process.env.MIAS_QA_OUT ?? "./qa-out";
mkdirSync(join(OUT, "shots"), { recursive: true });
mkdirSync(join(OUT, "downloads"), { recursive: true });

let pass = 0;
let fail = 0;
function check(name, ok, detail = "") {
  if (ok) pass += 1;
  else fail += 1;
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${!ok && detail ? ` — ${detail}` : ""}`);
}
const redact = (s) => String(s);

const browser = await chromium.connectOverCDP(CDP);
console.log(`browser: ${browser.version()}`);
// The default context holds the operator's OAuth session (HttpOnly cookie; never read here).
const context = browser.contexts()[0];
if (!context) throw new Error("no browser context: log in to MIAS in the CDP-attached Chrome first");
await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: BASE });
const page = await context.newPage();
await page.setViewportSize({ width: 1920, height: 1080 });

// API comparisons go through the same OAuth session; nginx adds the read token (no Authorization from here).
async function apiText(path) {
  const res = await context.request.get(BASE + path, { maxRedirects: 0 });
  return { status: res.status(), text: await res.text(), etag: res.headers().etag ?? null };
}
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

// ---- OAuth session: the deep link opens directly, with no sign-in, token or lock UI in the app
await page.goto(`${BASE}/status`);
await page.getByRole("heading", { name: "System Status", level: 1 }).waitFor();
check("deep link opens directly with the OAuth session (no /signin)", new URL(page.url()).pathname === "/status");
check("no token field, sign-in or lock control in the app",
  (await page.getByLabel(/token/i).count()) === 0 && (await page.getByRole("button", { name: /^(Lock|Unlock|Sign in)$/ }).count()) === 0);
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
const miHistory = JSON.parse((await apiText("/api/v1/market-intelligence?limit=50")).text);
const miRows = await miTable.locator("tbody tr").count();
check("MI history lists exactly the API's rows", miRows === miHistory.data.length && miRows > 0, `${miRows} vs ${miHistory.data.length}`);
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

await page.getByRole("radio", { name: /Dark/ }).check();
for (const [name, link] of [["overview", "Overview"], ["status", "System Status"]]) {
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: link }).click();
  await h1().waitFor();
  await page.waitForTimeout(400);
  await shot(`desktop-1440p-${name}-dark`);
}
await page.getByRole("radio", { name: /Light/ }).check();

await page.setViewportSize({ width: 1920, height: 1080 });

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

// ---- No page-level horizontal scroll on a phone (Phase 16F finding)
await page.setViewportSize({ width: 390, height: 844 });
for (const [label, link] of [["Overview", "Overview"], ["Market Intelligence", "Market Intelligence"], ["Alerts", "Alerts"], ["System Status", "System Status"]]) {
  await page.getByRole("button", { name: /Menu/ }).click();
  await page.getByRole("dialog").getByRole("link", { name: link }).click();
  await page.getByRole("heading", { name: label, level: 1 }).waitFor();
  await page.waitForTimeout(500);
  const [sw, iw] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
  check(`mobile-390: ${label} has no page-level horizontal scroll`, sw <= iw, `${sw} > ${iw}`);
}

// ---- Reduced motion
await page.emulateMedia({ reducedMotion: "reduce" });
await page.getByRole("button", { name: /Menu/ }).click();
const anim = await page.locator(".drawer").evaluate((d) => getComputedStyle(d).animationName);
check("reduced motion removes the drawer animation", anim === "none", anim);
await page.keyboard.press("Escape");
await page.emulateMedia({ reducedMotion: "no-preference" });
await page.setViewportSize({ width: 1920, height: 1080 });

// ---- A reload keeps the OAuth session (the proxy cookie), and the theme preference
await page.reload();
await h1().waitFor();
check("a reload stays signed in (OAuth session, no /signin)", !new URL(page.url()).pathname.startsWith("/signin"));
check("theme preference (non-sensitive) survives the reload", (await page.evaluate(() => document.documentElement.dataset.theme ?? "system")) === "light");

const csp = await page.evaluate(() => window.__cspViolations);
check("no CSP violations in the session", csp.length === 0, csp.join("; "));
check("no console errors or page errors", consoleProblems.filter((m) => !/401|Unauthorized|Failed to load resource/.test(m)).length === 0, consoleProblems.join(" | "));
const external = requests.map((r) => new URL(r.url())).filter((u) => u.protocol.startsWith("http") && u.origin !== BASE);
check("every request stayed same-origin", external.length === 0, external.map((u) => u.origin).join(","));
check("no request from the browser carries an Authorization header", requests.every((r) => !r.headers().authorization));
check("the API answered through the OAuth session (nginx injects the token)", (await apiText("/api/v1/version")).status === 200);

// ---- Sign out last: it ends the proxy session at /oauth/sign_out (OpenShift may sign the browser in again silently)
const signOut = page.waitForRequest((r) => new URL(r.url()).pathname === "/oauth/sign_out");
await page.getByRole("button", { name: "Sign out" }).click();
check("Sign out goes through /oauth/sign_out", (await signOut.catch(() => null)) !== null);

await context.close();
await browser.close();
console.log(`browser-qa: ${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
