import assert from "node:assert/strict";
import crypto from "node:crypto";
import { execFileSync } from "node:child_process";
import { createServer } from "node:http";
import { existsSync, readFileSync, promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import puppeteer from "puppeteer";

import { resolveChromiumLaunchPolicy } from "./chromium_launch_policy.mjs";

// Configuration (all optional):
//   CUA_E2E_CHROME          explicit Chrome/Chromium executable
//   CUA_E2E_RESULT_DIR      bundle output directory (default: <OS temp>/cua-recorder-e2e-results)
//   CUA_E2E_RUNTIME_DIR     parent of the disposable profile (default: <OS temp>/cua-recorder-e2e-runtime)
//   CUA_E2E_CASES           comma-separated case IDs, e.g. T01,T02,T03
//   CUA_E2E_CI_NO_SANDBOX=1 disable the Chromium sandbox (CI containers only)
//   CUA_E2E_HEADLESS=1      run Chrome's new headless mode instead of a visible window
const SCRIPT_PATH = fileURLToPath(import.meta.url);
const SCRIPT_DIR = path.dirname(SCRIPT_PATH);
const EXTENSION_ROOT = path.resolve(SCRIPT_DIR, "../..");
const REPO_ROOT = path.resolve(EXTENSION_ROOT, "..");
const EXTENSION_PATH = path.join(EXTENSION_ROOT, "app");
const EXTENSION_MANIFEST = JSON.parse(readFileSync(path.join(EXTENSION_PATH, "manifest.json"), "utf8"));
const RECORDER_NAME = EXTENSION_MANIFEST.name;
const RECORDER_VERSION = EXTENSION_MANIFEST.version;
const MINIMUM_CHROME_MAJOR = Number(EXTENSION_MANIFEST.minimum_chrome_version);
const EXPECTED_PERMISSIONS = ["activeTab", "scripting", "storage", "unlimitedStorage"];
const DISCLOSURE_VERSION = readFileSync(path.join(EXTENSION_PATH, "privacy", "disclosure.html"), "utf8")
  .match(/<meta name="cua-disclosure-version" content="([^"]+)"/)?.[1];
const PUPPETEER_VERSION = JSON.parse(
  readFileSync(path.join(EXTENSION_ROOT, "node_modules", "puppeteer", "package.json"), "utf8")
).version;
const RESULT_ROOT = path.resolve(
  process.env.CUA_E2E_RESULT_DIR || path.join(os.tmpdir(), "cua-recorder-e2e-results")
);
const SCREENSHOT_ROOT = path.join(RESULT_ROOT, "screenshots");
const EXPORT_ROOT = path.join(RESULT_ROOT, "exports");
const RUNTIME_ROOT = path.resolve(
  process.env.CUA_E2E_RUNTIME_DIR || path.join(os.tmpdir(), "cua-recorder-e2e-runtime")
);
const HEADLESS = process.env.CUA_E2E_HEADLESS === "1";
let puppeteerChrome = "";
try {
  puppeteerChrome = await puppeteer.executablePath();
} catch (_error) {
  // A clean install with PUPPETEER_SKIP_DOWNLOAD may not have a managed browser.
}
const systemChromeCandidates = process.platform === "win32"
  ? [
      path.join(process.env.ProgramFiles || "C:\\Program Files", "Google", "Chrome", "Application", "chrome.exe"),
      path.join(process.env.LOCALAPPDATA || "", "Google", "Chrome", "Application", "chrome.exe")
    ]
  : process.platform === "darwin"
    ? ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    : ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser"];
const systemChrome = systemChromeCandidates.find((candidate) => candidate && existsSync(candidate)) || "";
const CHROME_EXECUTABLE = process.env.CUA_E2E_CHROME
  ? path.resolve(process.env.CUA_E2E_CHROME)
  : puppeteerChrome && existsSync(puppeteerChrome)
    ? path.resolve(puppeteerChrome)
    : systemChrome
      ? path.resolve(systemChrome)
      : "";
const CHROME_PROVENANCE = process.env.CUA_E2E_CHROME
  ? "explicit CUA_E2E_CHROME executable"
  : puppeteerChrome && CHROME_EXECUTABLE === path.resolve(puppeteerChrome)
    ? "Puppeteer-managed Chrome for Testing"
    : CHROME_EXECUTABLE
      ? "system Chrome with a new isolated profile"
      : "no browser found";
const AGENT_LABEL = "puppeteer-isolated-chrome";
const TRACE_SCHEMA_VERSION = "cui-trace-v2";
const COVERAGE_SPEC_PATH = path.join(EXTENSION_ROOT, "event_coverage.json");
const CHROMIUM_LAUNCH_POLICY = resolveChromiumLaunchPolicy();
const EXPORT_SUFFIXES = { json: ".json", jsonl: ".jsonl", csv: ".csv" };

const ROOT_KEY = "cui-recorder-root";
const SESSION_PREFIX = "cui-session-";
const CHUNK_PREFIX = "cui-events-";
const INDICATOR_ID = "cui-trace-recorder-indicator";
const QUERY_SENTINEL = "QUERY_SENTINEL_SHOULD_NOT_PERSIST";
const FRAGMENT_SENTINEL = "FRAGMENT_SENTINEL_SHOULD_NOT_PERSIST";
const SAFE_TEXT_SENTINEL = "PlainFixture42!";
const PASSWORD_SENTINEL = "PasswordFixture42!";
const PRIVATE_SENTINEL = "PrivateFixture42!";
const SHADOW_SENTINEL = "ShadowFixture42!";
// Custom elements in the fixture whose private field lives in a shadow root:
// a password field in an open root, a password field in a closed root, and a
// plain field inside a [data-recorder-private] region of an open root.
const SHADOW_FIELD_HOSTS = ["shadow-open-login", "shadow-closed-login", "shadow-private-note"];
const RAW_TEXT_SENTINEL = "RawFixture42!";
const COMPOSITION_SENTINEL = "CompositionFixture42!";
const FIXTURE_PATH = "/task.html";
const FRAME_PATH = "/frame.html";
const CASE_FILTER = new Set(
  String(process.env.CUA_E2E_CASES || "")
    .split(",")
    .map((value) => value.trim().toUpperCase())
    .filter(Boolean)
);

const startedAt = new Date().toISOString();
const cases = [];
const browserErrors = [];
// Errors raised by Chrome's own WebUI pages (chrome://...), which T10 opens to
// prove they cannot be recorded. They are not extension or fixture errors.
const browserUiErrors = [];
const storageLifecycle = [];
const exportEvidence = [];
const artifacts = [];
const browserDownloadEvents = [];
let browser;
let extension;
let extensionId;
let controlPage;
let originA;
let originB;
let serverA;
let serverB;
let profileRoot;
let downloadRoot;
let safeSession;
let safePublicSession;
let eventCoverageSummary;
let browserProductVersion;
let browserMajorVersion = null;
let browserDownloadClient;

class SkipCase extends Error {}

function now() {
  return new Date().toISOString();
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function poll(check, { timeout = 15_000, interval = 100, label = "condition" } = {}) {
  const deadline = Date.now() + timeout;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const value = await check();
      if (value) return value;
    } catch (error) {
      lastError = error;
    }
    await sleep(interval);
  }
  const suffix = lastError ? `; last error: ${lastError.message}` : "";
  throw new Error(`Timed out waiting for ${label}${suffix}`);
}

async function sha256(filePath) {
  const hash = crypto.createHash("sha256");
  hash.update(await fs.readFile(filePath));
  return hash.digest("hex");
}

async function hashDirectory(root) {
  const files = [];
  async function walk(current) {
    for (const entry of await fs.readdir(current, { withFileTypes: true })) {
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) await walk(full);
      else if (entry.isFile()) files.push(full);
    }
  }
  await walk(root);
  files.sort((a, b) => a.localeCompare(b));
  const hash = crypto.createHash("sha256");
  for (const file of files) {
    hash.update(path.relative(root, file).replaceAll(path.sep, "/"));
    hash.update("\0");
    hash.update(await fs.readFile(file));
    hash.update("\0");
  }
  return { sha256: hash.digest("hex"), file_count: files.length };
}

function gitOutput(...args) {
  try {
    return execFileSync("git", args, { cwd: REPO_ROOT, encoding: "utf8" }).trim();
  } catch (_error) {
    return "unavailable";
  }
}

// Record source cleanliness before the run; the bundle is written outside the
// repository, so a clean tree stays clean.
const repositoryStatusBeforeRun = gitOutput("status", "--short");

// Every path recorded inside the bundle is relative to the bundle root and uses
// "/" separators, so a bundle can be moved or uploaded as a CI artifact.
function bundlePath(destination) {
  return path.relative(RESULT_ROOT, destination).split(path.sep).join("/");
}

async function writeJson(fileName, value) {
  const destination = path.join(RESULT_ROOT, fileName);
  await fs.writeFile(destination, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  artifacts.push(bundlePath(destination));
}

async function screenshot(page, fileName, { fullPage = true } = {}) {
  const destination = path.join(SCREENSHOT_ROOT, fileName);
  await page.screenshot({ path: destination, fullPage });
  artifacts.push(bundlePath(destination));
  return destination;
}

function fixtureHtml(label) {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Recorder E2E Fixture ${label}</title>
  <style>
    :root { font-family: system-ui, sans-serif; color: #17212b; background: #f7f8fa; }
    body { margin: 0; }
    main { width: min(900px, calc(100% - 48px)); margin: 36px auto; }
    .card { background: white; border: 1px solid #d8dee6; border-radius: 16px; padding: 24px; box-shadow: 0 8px 30px #1b27331a; }
    label { display: grid; gap: 6px; margin: 14px 0; font-weight: 650; }
    input, textarea, select { font: inherit; padding: 10px 12px; border: 1px solid #9ba8b6; border-radius: 8px; }
    button { font: inherit; font-weight: 700; padding: 10px 16px; border: 0; border-radius: 8px; background: #3157d5; color: white; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
    #drag-source, #drop-target { margin-top: 12px; padding: 14px; border-radius: 8px; }
    #drag-source { width: 180px; background: #e7edff; cursor: grab; }
    #drop-target { border: 2px dashed #8794a5; min-height: 52px; }
    details, form { margin-top: 14px; padding: 14px; border: 1px solid #d8dee6; border-radius: 10px; }
    #status { min-height: 1.5em; color: #3157d5; }
    .spacer { height: 1800px; background: linear-gradient(#f7f8fa, #dfe8f8); border-radius: 16px; margin-top: 22px; }
  </style>
</head>
<body>
  <main>
    <section class="card">
      <p>Loopback fixture · origin ${label}</p>
      <h1>Matched recorder task</h1>
      <label>Ordinary input <input id="plain-input" autocomplete="off"></label>
      <label>Password input <input id="password-input" type="password" autocomplete="section-e2e current-password"></label>
      <div data-recorder-private>
        <label>Explicit private region <input id="private-input" autocomplete="off"></label>
      </div>
      <shadow-open-login id="shadow-open-host"></shadow-open-login>
      <shadow-closed-login id="shadow-closed-host"></shadow-closed-login>
      <shadow-private-note id="shadow-private-host"></shadow-private-note>
      <label>Bulk event input <input id="bulk-input" autocomplete="off"></label>
      <div class="row">
        <label>Notes <textarea id="notes-input" rows="2"></textarea></label>
        <label>Choice
          <select id="choice-select">
            <option value="one">One</option>
            <option value="two">Two</option>
          </select>
        </label>
      </div>
      <button id="action-button" type="button">Run fixture action</button>
      <div id="drag-source" draggable="true">Drag this agent action</div>
      <div id="drop-target">Drop target</div>
      <details id="details-control"><summary>More controls</summary><p>Toggle event target</p></details>
      <form id="fixture-form" method="post" action="/submit?private=must-not-persist#fragment">
        <label>Required form input <input id="form-input" required></label>
        <button id="submit-button" type="submit">Submit form</button>
        <button id="reset-button" type="reset">Reset form</button>
      </form>
      <p id="status" role="status"></p>
    </section>
    <div class="spacer" aria-hidden="true"></div>
  </main>
  <script>
    // Private fields inside shadow roots. A closed root cannot be reached
    // from outside once attached, so the fixture keeps handles for the
    // harness to focus; the recorder has to find the field through chrome.dom.
    window.__shadowFixtureFields = {};
    const defineShadowField = (name, mode, markup) => customElements.define(name, class extends HTMLElement {
      constructor() {
        super();
        const root = this.attachShadow({ mode });
        root.innerHTML = markup;
        window.__shadowFixtureFields[name] = root.querySelector('input');
      }
    });
    defineShadowField('shadow-open-login', 'open',
      '<label>Shadow password (open root) <input id="shadow-open-password" type="password"></label>');
    defineShadowField('shadow-closed-login', 'closed',
      '<label>Shadow password (closed root) <input id="shadow-closed-password" type="password" autocomplete="current-password"></label>');
    defineShadowField('shadow-private-note', 'open',
      '<div data-recorder-private><label>Shadow private region <input id="shadow-private-input" autocomplete="off"></label></div>');
    document.querySelector('#action-button').addEventListener('click', () => {
      document.querySelector('#status').textContent = 'Fixture action completed';
    });
    document.querySelector('#fixture-form').addEventListener('submit', (event) => {
      event.preventDefault();
      document.querySelector('#status').textContent = 'Fixture form submitted';
    });
    document.querySelector('#drop-target').addEventListener('dragover', (event) => event.preventDefault());
    document.querySelector('#drop-target').addEventListener('drop', (event) => {
      event.preventDefault();
      document.querySelector('#status').textContent = 'Fixture drag completed';
    });
  </script>
</body>
</html>`;
}

function frameHtml(label) {
  return `<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Dynamic recorder frame ${label}</title></head>
<body>
  <button id="frame-action" type="button">Run frame action</button>
  <label>Frame input <input id="frame-input" autocomplete="off"></label>
  <p id="frame-status" role="status"></p>
  <script>
    document.querySelector('#frame-action').addEventListener('click', () => {
      document.querySelector('#frame-status').textContent = 'Frame action completed';
    });
  </script>
</body>
</html>`;
}

async function startFixture(label) {
  const server = createServer((request, response) => {
    if (request.url === "/favicon.ico") {
      response.writeHead(204, { "cache-control": "public, max-age=3600" });
      response.end();
      return;
    }
    if (request.url?.startsWith(FRAME_PATH)) {
      response.writeHead(200, {
        "content-type": "text/html; charset=utf-8",
        "cache-control": "no-store"
      });
      response.end(frameHtml(label));
      return;
    }
    if (request.url?.startsWith(FIXTURE_PATH) || request.url === "/") {
      response.writeHead(200, {
        "content-type": "text/html; charset=utf-8",
        "cache-control": "no-store"
      });
      response.end(fixtureHtml(label));
      return;
    }
    response.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
    response.end("Not found");
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  return { server, origin: `http://127.0.0.1:${address.port}` };
}

function recordPageDiagnostics(page, label, sink = browserErrors) {
  page.on("pageerror", (error) => sink.push({ at: now(), page: label, kind: "pageerror", message: error.message }));
  page.on("console", (message) => {
    if (message.type() === "error") {
      sink.push({ at: now(), page: label, kind: "console.error", message: message.text() });
    }
  });
}

async function runtimeRequest(payload) {
  const response = await controlPage.evaluate(async (message) => chrome.runtime.sendMessage(message), payload);
  if (!response?.ok) throw new Error(response?.error || "Extension runtime request failed");
  return response.data;
}

async function storageAll() {
  return controlPage.evaluate(async () => chrome.storage.local.get(null));
}

async function getStoredSession(sessionId) {
  const all = await storageAll();
  const metadata = all[`${SESSION_PREFIX}${sessionId}`];
  assert(metadata, `Missing storage metadata for ${sessionId}`);
  const chunkKeys = Object.keys(all)
    .filter((key) => key.startsWith(`${CHUNK_PREFIX}${sessionId}-`))
    .sort((a, b) => Number(a.split("-").at(-1)) - Number(b.split("-").at(-1)));
  const chunks = chunkKeys.map((key) => all[key] || []);
  return {
    metadata,
    chunk_keys: chunkKeys,
    chunk_lengths: chunks.map((chunk) => chunk.length),
    events: chunks.flat()
  };
}

async function waitForMetadata(sessionId, predicate, label) {
  return poll(async () => {
    const all = await storageAll();
    const metadata = all[`${SESSION_PREFIX}${sessionId}`];
    return metadata && predicate(metadata) ? metadata : null;
  }, { timeout: 20_000, interval: 150, label });
}

async function resetExtensionState() {
  if (!controlPage || controlPage.isClosed()) return;
  await controlPage.evaluate(async ({ rootKey, sessionPrefix }) => {
    const all = await chrome.storage.local.get(null);
    const root = all[rootKey];
    const activeId = root?.activeSessionId;
    const metadata = activeId ? all[`${sessionPrefix}${activeId}`] : null;
    if (metadata?.tab_id != null) {
      try {
        await chrome.tabs.sendMessage(metadata.tab_id, { type: "RECORDER_SESSION_STOPPED" });
      } catch (_error) {}
      try {
        await chrome.action.setBadgeText({ tabId: metadata.tab_id, text: "" });
      } catch (_error) {}
    }
    await chrome.storage.local.clear();
    await chrome.storage.local.set({ [rootKey]: { activeSessionId: null, sessionIds: [] } });
  }, { rootKey: ROOT_KEY, sessionPrefix: SESSION_PREFIX });
}

async function deleteSession(sessionId) {
  await runtimeRequest({ type: "RECORDER_DELETE_SESSION", sessionId });
  const all = await storageAll();
  assert(!Object.keys(all).some((key) => key.includes(sessionId)), `Storage keys remain for ${sessionId}`);
}

async function newTaskPage(origin = originA, suffix = "") {
  const page = await browser.newPage();
  recordPageDiagnostics(page, `task-${origin}`);
  await page.setViewport({ width: 1280, height: 900, deviceScaleFactor: 1 });
  await page.goto(
    `${origin}${FIXTURE_PATH}?fixture=${encodeURIComponent(QUERY_SENTINEL)}${suffix}#${FRAGMENT_SENTINEL}`,
    { waitUntil: "networkidle0" }
  );
  return page;
}

async function closeActionPopups() {
  for (const page of await extension.pages()) {
    if (page.url().includes("/popup/popup.html") && !page.isClosed()) {
      await page.close().catch(() => undefined);
    }
  }
}

async function openActionPopup(taskPage) {
  let lastError;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      await closeActionPopups();
      await taskPage.bringToFront();
      await extension.triggerAction(taskPage);
      const popup = await poll(async () => {
        const pages = await extension.pages();
        return pages.find((page) => !page.isClosed() && page.url().includes("/popup/popup.html")) || null;
      }, { timeout: 10_000, interval: 100, label: "extension action popup" });
      assert(popup, "Extension action target did not resolve to a Page");
      recordPageDiagnostics(popup, "action-popup");
      await popup.waitForSelector("#status-chip", { timeout: 8_000 });
      return popup;
    } catch (error) {
      lastError = error;
      await closeActionPopups().catch(() => undefined);
      if (attempt < 3) await sleep(300);
    }
  }
  throw new Error(`Extension action popup failed after 3 attempts: ${lastError?.message || "unknown error"}`);
}

async function setField(page, selector, value) {
  await page.$eval(selector, (element, nextValue) => {
    element.value = nextValue;
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
  }, value);
}

async function configureAgentPopup(popup, taskId, { raw = false } = {}) {
  await popup.select("#actor-type", "agent");
  await setField(popup, "#participant-code", "e2e-agent-001");
  await setField(popup, "#task-id", taskId);
  await setField(popup, "#pair-id", "e2e-pair-001");
  await setField(popup, "#protocol-version", "matched-task-v1");
  await setField(popup, "#actor-label", AGENT_LABEL);
  await setField(popup, "#executor", "puppeteer-cdp");
  await setField(popup, "#executor-version", PUPPETEER_VERSION);
  await setField(popup, "#capture-adapter", "browser_mv3");
  await setField(popup, "#environment-scope", "loopback_fixture");
  await popup.select("#source-class", "synthetic_coverage");
  await popup.$eval(
    `input[name="key-mode"][value="${raw ? "raw" : "safe"}"]`,
    (element) => {
      element.checked = true;
      element.dispatchEvent(new Event("change", { bubbles: true }));
    }
  );
  const checked = await popup.$eval("#consent", (element) => element.checked);
  if (!checked) await popup.click("#consent");
}

async function startSession(
  taskPage,
  taskId,
  { raw = false, acceptRaw = true, screenshotName, topScreenshotName } = {}
) {
  const popup = await openActionPopup(taskPage);
  await configureAgentPopup(popup, taskId, { raw });
  if (topScreenshotName) {
    await popup.evaluate(() => window.scrollTo(0, 0));
    await screenshot(popup, topScreenshotName, { fullPage: false });
  }
  if (screenshotName) {
    await popup.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    await screenshot(popup, screenshotName, { fullPage: false });
  }
  let dialogSeen = false;
  if (raw) {
    popup.once("dialog", async (dialog) => {
      dialogSeen = true;
      if (acceptRaw) await dialog.accept();
      else await dialog.dismiss();
    });
  }
  await popup.click("#start");
  if (raw) await poll(() => dialogSeen, { timeout: 5_000, label: "raw-key confirmation dialog" });
  if (!acceptRaw) {
    await sleep(250);
    const state = await runtimeRequest({ type: "RECORDER_GET_STATE" });
    assert.equal(state.activeSession, null);
    await popup.close().catch(() => undefined);
    return null;
  }
  await popup.waitForFunction(() => !document.querySelector("#active-panel").hidden, { timeout: 12_000 });
  const sessionId = await popup.$eval("#active-session", (element) => element.textContent.trim());
  assert.match(sessionId, /^[0-9a-f-]{36}$/i);
  await taskPage.bringToFront();
  await taskPage.waitForSelector(`#${INDICATOR_ID}`, { timeout: 10_000 });
  const tabId = await tabIdForPage(taskPage);
  const badge = await controlPage.evaluate((id) => chrome.action.getBadgeText({ tabId: id }), tabId);
  assert.equal(badge, "REC");
  return sessionId;
}

async function tabIdForPage(page) {
  const url = page.url();
  const tabs = await controlPage.evaluate(async () => chrome.tabs.query({}));
  const matches = tabs.filter((tab) => tab.url === url);
  if (matches.length === 1) return matches[0].id;
  const active = tabs.find((tab) => tab.active && tab.url === url);
  if (active) return active.id;
  throw new Error(`Could not resolve tab id for ${url}`);
}

async function flushPage(taskPage) {
  const tabId = await tabIdForPage(taskPage);
  const response = await controlPage.evaluate(async (id) => chrome.tabs.sendMessage(id, { type: "RECORDER_FLUSH" }), tabId);
  if (!response?.ok) throw new Error(response?.error || "Content-script flush failed");
}

async function stopSession(taskPage) {
  const popup = await openActionPopup(taskPage);
  await popup.waitForFunction(() => !document.querySelector("#active-panel").hidden, { timeout: 10_000 });
  const sessionId = await popup.$eval("#active-session", (element) => element.textContent.trim());
  await popup.click("#stop");
  const metadata = await waitForMetadata(sessionId, (row) => row.status !== "recording", "session finalization");
  if (!popup.isClosed()) {
    await popup
      .waitForFunction(() => !document.querySelector("#idle-panel").hidden, { timeout: 5_000 })
      .catch(() => undefined);
  }
  assert.equal(metadata.status, "complete", metadata.capture_error || "Session did not complete");
  const tabId = await tabIdForPage(taskPage);
  const badge = await controlPage.evaluate((id) => chrome.action.getBadgeText({ tabId: id }), tabId);
  assert.equal(badge, "");
  assert.equal(await taskPage.$(`#${INDICATOR_ID}`), null);
  await popup.close().catch(() => undefined);
  return metadata;
}

async function exerciseStandardTask(page) {
  await page.mouse.move(80, 100);
  await sleep(70);
  await page.mouse.move(300, 220);
  await sleep(70);
  await page.mouse.move(700, 420);
  await page.click("#action-button");
  await page.click("#action-button", { clickCount: 2, delay: 30 });
  await page.click("#action-button", { button: "right" });
  await page.click("#action-button", { button: "middle" });
  await page.click("#plain-input");
  await page.keyboard.type(SAFE_TEXT_SENTINEL, { delay: 8 });
  await page.keyboard.down("Control");
  await page.keyboard.press("KeyA");
  await page.keyboard.press("KeyC");
  await page.keyboard.press("KeyV");
  await page.keyboard.press("KeyX");
  await page.keyboard.press("KeyV");
  await page.keyboard.up("Control");
  await page.keyboard.press("Enter");
  await page.keyboard.press("Tab");
  await page.click("#notes-input");
  await page.keyboard.type("metadata only", { delay: 5 });
  await page.select("#choice-select", "two");
  await page.click("#submit-button");
  await page.click("#form-input");
  await page.keyboard.type("valid", { delay: 5 });
  await page.click("#submit-button");
  await page.click("#reset-button");
  await page.click("#details-control summary");
  const dragHandle = await page.$("#drag-source");
  const dropHandle = await page.$("#drop-target");
  const dragBox = await dragHandle?.boundingBox();
  const dropBox = await dropHandle?.boundingBox();
  assert(dragBox && dropBox, "Drag fixture elements were not measurable");
  await page.mouse.move(dragBox.x + dragBox.width / 2, dragBox.y + dragBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(dropBox.x + dropBox.width / 2, dropBox.y + dropBox.height / 2, { steps: 12 });
  await page.mouse.up();
  await sleep(80);
  await page.evaluate((compositionText) => {
    const target = document.querySelector("#notes-input");
    document.querySelector("#action-button").dispatchEvent(
      new MouseEvent("dblclick", { bubbles: true, button: 0, detail: 2 })
    );
    document.querySelector("#drop-target").dispatchEvent(
      new DragEvent("dragover", { bubbles: true, dataTransfer: new DataTransfer() })
    );
    for (const type of ["compositionstart", "compositionupdate", "compositionend"]) {
      target.dispatchEvent(new CompositionEvent(type, { bubbles: true, data: compositionText }));
    }
    document.dispatchEvent(new Event("touchstart", { bubbles: true }));
    document.dispatchEvent(new Event("touchend", { bubbles: true }));
    target.dispatchEvent(new PointerEvent("pointercancel", { bubbles: true, pointerType: "mouse" }));
  }, COMPOSITION_SENTINEL);
  await page.setViewport({ width: 1260, height: 880, deviceScaleFactor: 1 });
  await sleep(100);
  await page.setViewport({ width: 1280, height: 900, deviceScaleFactor: 1 });
  await page.click("#password-input");
  await page.keyboard.type(PASSWORD_SENTINEL, { delay: 5 });
  await page.click("#private-input");
  await page.keyboard.type(PRIVATE_SENTINEL, { delay: 5 });
  await page.mouse.wheel({ deltaY: 750 });
  await sleep(1300);
  await flushPage(page);
}

async function clearFixtureValuesForScreenshot(page) {
  await page.$$eval(
    "#plain-input, #password-input, #private-input, #bulk-input, #notes-input, #form-input",
    (inputs) => {
    for (const input of inputs) input.value = "";
    }
  );
}

function assertAgentMetadata(metadata, taskId, { raw = false } = {}) {
  assert.equal(metadata.schema_version, TRACE_SCHEMA_VERSION);
  assert.equal(metadata.actor_type, "agent");
  assert.equal(metadata.actor_label, AGENT_LABEL);
  assert.equal(metadata.participant_code, "e2e-agent-001");
  assert.equal(metadata.pair_id, "e2e-pair-001");
  assert.equal(metadata.task_id, taskId);
  assert.equal(metadata.protocol_version, "matched-task-v1");
  assert.equal(metadata.executor, "puppeteer-cdp");
  assert.equal(metadata.executor_version, PUPPETEER_VERSION);
  assert.equal(metadata.recorder_version, RECORDER_VERSION);
  assert.equal(metadata.capture_adapter, "browser_mv3");
  assert.equal(metadata.environment_scope, "loopback_fixture");
  assert.equal(metadata.source_class, "synthetic_coverage");
  assert.equal(metadata.raw_key_capture, raw);
  assert.equal(metadata.include_query, false);
  assert.equal(metadata.consent_confirmed, true);
  assert.equal(metadata.disclosure_version, DISCLOSURE_VERSION);
  assert.equal(metadata.retention_policy, "local-until-export-or-delete");
}

function assertTraceEnvelopeV2(session) {
  const { metadata, events } = session;
  assert.equal(metadata.schema_version, TRACE_SCHEMA_VERSION);
  assert.equal(metadata.timing_model, "session-global-monotonic-v1");
  assert.equal(metadata.event_seq_last, events.length);
  assert.equal(metadata.event_count, events.length);
  assert(Number.isInteger(metadata.document_epoch_count));
  for (const counter of [
    "dropped_event_count",
    "rejected_event_count",
    "flush_failure_count",
    "quota_rejection_count"
  ]) {
    assert(Number.isInteger(metadata[counter]) && metadata[counter] >= 0, `${counter} must be a nonnegative integer`);
  }
  const sessionTimes = [];
  const documentEpochs = [];
  events.forEach((event, index) => {
    assert.equal(event.schema_version, TRACE_SCHEMA_VERSION);
    assert.equal(event.event_seq, index + 1);
    assert(Number.isFinite(event.t_session_ms) && event.t_session_ms >= 0);
    assert(Number.isFinite(event.t_document_ms) && event.t_document_ms >= 0);
    assert(Number.isInteger(event.document_epoch) && event.document_epoch >= 0);
    sessionTimes.push(event.t_session_ms);
    documentEpochs.push(event.document_epoch);
  });
  assert.deepEqual(sessionTimes, [...sessionTimes].sort((a, b) => a - b));
  assert.deepEqual(documentEpochs, [...documentEpochs].sort((a, b) => a - b));
  const expectedEpochCount = events.length ? Math.max(...documentEpochs) + 1 : 0;
  assert.equal(metadata.document_epoch_count, expectedEpochCount);
}

// Recorder >= 0.5.0: private targets keep only the fact and time of a key event.
function assertPrivateKeyEvent(event) {
  assert.equal(event.key, "<redacted>");
  assert.equal(event.key_class, null, "private key event retained its key class");
  assert.deepEqual(
    { repeat: event.keyboard?.repeat, location: event.keyboard?.location, hold_ms: event.keyboard?.hold_ms },
    { repeat: null, location: null, hold_ms: null },
    "private key event retained keyboard timing detail"
  );
  assert.deepEqual(
    event.modifiers,
    { alt: null, ctrl: null, meta: null, shift: null },
    "private key event retained modifier state"
  );
}

function assertPrivacySafe(session) {
  const serialized = JSON.stringify(session);
  for (const forbidden of [
    QUERY_SENTINEL,
    FRAGMENT_SENTINEL,
    SAFE_TEXT_SENTINEL,
    PASSWORD_SENTINEL,
    PRIVATE_SENTINEL,
    COMPOSITION_SENTINEL
  ]) {
    assert(!serialized.includes(forbidden), `Forbidden sentinel leaked: ${forbidden}`);
  }
  for (const event of session.events) {
    assert(!event.url.includes("?"), `Query retained in ${event.url}`);
    assert(!event.url.includes("#"), `Fragment retained in ${event.url}`);
    if (["keydown", "keyup"].includes(event.type) && ["letter", "digit", "punctuation", "whitespace"].includes(event.key_class)) {
      if (event.target.password_like) assert.equal(event.key, "<redacted>");
      else assert.equal(event.key, null);
    }
    if (["keydown", "keyup"].includes(event.type)) {
      assert(event.keyboard, "Keyboard event lost its bounded metadata object");
      assert.equal("code" in event.keyboard, false);
      assert.equal("key" in event.keyboard, false);
      assert(
        event.keyboard.hold_ms === null ||
          (Number.isFinite(event.keyboard.hold_ms) && event.keyboard.hold_ms >= 0),
        "Keyboard hold duration must be null or a nonnegative number"
      );
    }
    if (event.target.password_like) {
      assert.equal(event.input, null);
      assert(!event.target.selector_hint.includes("password-input"));
      assert(!event.target.selector_hint.includes("private-input"));
      if (["keydown", "keyup"].includes(event.type)) assertPrivateKeyEvent(event);
    }
  }
  assert(session.events.some((event) => event.target.password_like), "No password/private events were captured");
  assert(
    session.events.some((event) => event.target.password_like && event.type === "keydown"),
    "No private-field key events were captured"
  );
  assert(session.events.some((event) => event.type === "keydown" && event.key === "Enter"), "Control key was not retained");
  assert(
    session.events.some((event) => event.type === "keyup" && Number.isFinite(event.keyboard?.hold_ms)),
    "No matched keyup persisted a numeric hold duration"
  );
  const expectedTypes = [
    "auxclick",
    "beforeinput",
    "change",
    "click",
    "compositionend",
    "compositionstart",
    "compositionupdate",
    "contextmenu",
    "copy",
    "cut",
    "dblclick",
    "dragend",
    "dragover",
    "dragstart",
    "drop",
    "focusin",
    "focusout",
    "input",
    "invalid",
    "keydown",
    "keyup",
    "navigation",
    "paste",
    "pointercancel",
    "pointerdown",
    "pointermove",
    "pointerup",
    "reset",
    "resize",
    "scroll",
    "selectionchange",
    "submit",
    "toggle",
    "touchend",
    "touchstart",
    "wheel"
  ];
  const capturedTypes = new Set(session.events.map((event) => event.type));
  const missingTypes = expectedTypes.filter((type) => !capturedTypes.has(type));
  assert.deepEqual(missingTypes, [], `Expected event types were not captured: ${missingTypes.join(", ")}`);
}

async function typeIntoShadowFields(page) {
  for (const host of SHADOW_FIELD_HOSTS) {
    const focused = await page.evaluate((name) => {
      const field = window.__shadowFixtureFields?.[name];
      field?.focus();
      return Boolean(field) && field.getRootNode().activeElement === field;
    }, host);
    assert(focused, `Could not focus the shadow-DOM field of <${host}>`);
    await page.keyboard.type(SHADOW_SENTINEL, { delay: 5 });
  }
}

// Key and input events raised inside a shadow root reach the content script
// retargeted to the shadow host. Fields in open and closed roots and fields in
// a [data-recorder-private] region of a shadow tree must still be private.
function assertShadowFieldsPrivate(session) {
  const typed = session.events.filter((event) =>
    ["keydown", "keyup", "beforeinput", "input"].includes(event.type)
  );
  const keydowns = typed.filter((event) => event.type === "keydown");
  const expectedKeydowns = SHADOW_FIELD_HOSTS.length * SHADOW_SENTINEL.length;
  assert(
    keydowns.length >= expectedKeydowns,
    `Expected at least ${expectedKeydowns} shadow-DOM keydown events, received ${keydowns.length}`
  );
  for (const event of typed) {
    assert(
      !SHADOW_FIELD_HOSTS.includes(event.target.tag),
      `${event.type} was attributed to the shadow host <${event.target.tag}>`
    );
    assert.equal(event.target.password_like, true, `${event.type} on a shadow-DOM field was not private`);
    assert.equal(event.target.selector_hint, event.target.tag, "shadow-DOM private target kept its id");
    assert.equal(event.input, null, `${event.type} on a shadow-DOM field kept input metadata`);
    if (event.type === "keydown" || event.type === "keyup") assertPrivateKeyEvent(event);
  }
  assert(!JSON.stringify(session).includes(SHADOW_SENTINEL), "Shadow-DOM sentinel leaked");
  return { private_key_events: typed.filter((event) => event.type !== "input" && event.type !== "beforeinput").length };
}

// A dedicated session in which only the shadow-DOM fields receive keystrokes,
// so every key and input event in it must be private. Deleted afterwards.
async function runShadowPrivacySession(taskPage, { raw }) {
  const taskId = raw ? "e2e-raw-shadow-private" : "e2e-safe-shadow-private";
  const sessionId = await startSession(taskPage, taskId, { raw, acceptRaw: true });
  await typeIntoShadowFields(taskPage);
  await flushPage(taskPage);
  await stopSession(taskPage);
  const stored = await getStoredSession(sessionId);
  assertAgentMetadata(stored.metadata, taskId, { raw });
  assertTraceEnvelopeV2(stored);
  const { private_key_events } = assertShadowFieldsPrivate(stored);
  await deleteSession(sessionId);
  return {
    raw_key_capture: raw,
    shadow_fields: SHADOW_FIELD_HOSTS,
    private_key_events,
    session_deleted: true
  };
}

async function refreshDashboard() {
  await controlPage.bringToFront();
  await controlPage.reload({ waitUntil: "networkidle0" });
  await controlPage.waitForSelector("#sessions", { timeout: 10_000 });
}

// Downloads are observed only through the DevTools protocol: the extension has
// no `downloads` permission. Browser.setDownloadBehavior("allowAndName") saves
// each file as <downloadRoot>/<guid>, and Browser.downloadWillBegin /
// Browser.downloadProgress report the suggested name and completion.
async function exportFromDashboard(format) {
  const suffix = EXPORT_SUFFIXES[format];
  assert(suffix, `Unsupported export format ${format}`);
  assert.equal(path.dirname(downloadRoot), profileRoot, "Refusing to clean a download directory outside the isolated profile");
  for (const entry of await fs.readdir(downloadRoot, { withFileTypes: true })) {
    if (entry.isFile()) await fs.rm(path.join(downloadRoot, entry.name), { force: true });
  }
  const beforeEventIndex = browserDownloadEvents.length;
  await controlPage.click(`[data-format="${format}"]`);
  const outcome = await poll(async () => {
    const events = browserDownloadEvents.slice(beforeEventIndex);
    const started = events.find((event) =>
      event.method === "Browser.downloadWillBegin" &&
      String(event.params.suggestedFilename || "").endsWith(suffix)
    );
    if (!started) {
      const dashboardMessage = await controlPage.$eval("#message", (element) => element.textContent || "");
      if (dashboardMessage && !/Preparing|Prepared|stored locally|session/i.test(dashboardMessage)) {
        return { error: `Dashboard export failed: ${dashboardMessage}` };
      }
      return null;
    }
    const finished = [...events].reverse().find((event) =>
      event.method === "Browser.downloadProgress" &&
      event.params.guid === started.params.guid &&
      ["completed", "canceled"].includes(event.params.state)
    );
    if (!finished) return null;
    if (finished.params.state === "canceled") {
      return { error: "CDP reported a canceled browser download", started, finished };
    }
    return { started, finished };
  }, { timeout: 25_000, interval: 200, label: `${suffix} CDP download completion` });
  if (outcome.error) throw new Error(outcome.error);

  const guid = outcome.started.params.guid;
  assert.match(guid, /^[0-9a-f-]{8,64}$/i, "unexpected CDP download guid");
  const downloaded = path.join(downloadRoot, guid);
  const expectedBytes = Number(outcome.finished.params.receivedBytes);
  await poll(async () => {
    const stat = await fs.stat(downloaded).catch(() => null);
    return stat?.isFile() && stat.size === expectedBytes ? stat : null;
  }, { timeout: 10_000, interval: 100, label: `${suffix} download file on disk` });
  if (Number(outcome.finished.params.totalBytes) > 0) {
    assert.equal(expectedBytes, Number(outcome.finished.params.totalBytes), "partial download");
  }
  const suggestedName = path.basename(String(outcome.started.params.suggestedFilename));
  assert.match(suggestedName, /^cui-trace-[A-Za-z0-9_.:-]+$/, "unexpected export file name");
  const retained = path.join(EXPORT_ROOT, suggestedName);
  await fs.copyFile(downloaded, retained);
  artifacts.push(bundlePath(retained));
  const stat = await fs.stat(retained);
  exportEvidence.push({
    format,
    file: bundlePath(retained),
    bytes: stat.size,
    sha256: await sha256(retained),
    cdp_guid: guid,
    cdp_completed: true,
    cdp_received_bytes: expectedBytes
  });
  return retained;
}

async function runCase(id, name, fn) {
  const row = { id, name, status: "running", started_at: now() };
  cases.push(row);
  if (CASE_FILTER.size && !CASE_FILTER.has(id)) {
    row.status = "skipped";
    row.error = `Not selected by CUA_E2E_CASES=${[...CASE_FILTER].join(",")}`;
    row.finished_at = now();
    process.stdout.write(`\n[${id}] SKIPPED by case filter\n`);
    return row;
  }
  process.stdout.write(`\n[${id}] ${name}\n`);
  try {
    const details = await fn();
    row.status = "passed";
    row.details = details || {};
    process.stdout.write(`[${id}] PASS\n`);
  } catch (error) {
    row.status = error instanceof SkipCase ? "skipped" : "failed";
    row.error = error.message;
    row.stack = error.stack;
    process.stdout.write(`[${id}] ${row.status.toUpperCase()}: ${error.message}\n`);
  } finally {
    row.finished_at = now();
  }
  return row;
}

async function main() {
  process.stdout.write("[BOOT] preparing isolated runtime\n");
  assert(
    path.basename(RESULT_ROOT).toLowerCase().includes("e2e"),
    `CUA_E2E_RESULT_DIR must name a dedicated E2E directory, received: ${RESULT_ROOT}`
  );
  const archivedEvidence = path.join(REPO_ROOT, "research", "evidence");
  assert(
    path.relative(archivedEvidence, RESULT_ROOT).startsWith("..") || path.isAbsolute(path.relative(archivedEvidence, RESULT_ROOT)),
    "CUA_E2E_RESULT_DIR must not point into research/evidence (archival, hash-bound bytes)"
  );
  assert(DISCLOSURE_VERSION, "privacy/disclosure.html has no cua-disclosure-version meta tag");
  if (!CHROME_EXECUTABLE) {
    throw new Error(
      "No Chrome executable found. Run `npx puppeteer browsers install chrome` in extension/ " +
      "or set CUA_E2E_CHROME to a Chrome/Chromium binary."
    );
  }
  process.stdout.write(`[BOOT] result bundle: ${RESULT_ROOT}\n`);
  for (const directory of [SCREENSHOT_ROOT, EXPORT_ROOT]) {
    assert.equal(path.dirname(directory), RESULT_ROOT, `Refusing to clean unexpected evidence path: ${directory}`);
    await fs.rm(directory, { recursive: true, force: true });
  }
  for (const fileName of [
    "validation_summary.json",
    "run_manifest.json",
    "storage_lifecycle_summary.json",
    "exports_manifest.json",
    "event_coverage_summary.json",
    "agent_extension_e2e_trace.json",
    "E2E_REPORT.md"
  ]) {
    await fs.rm(path.join(RESULT_ROOT, fileName), { force: true });
  }
  await fs.mkdir(RESULT_ROOT, { recursive: true });
  await fs.mkdir(SCREENSHOT_ROOT, { recursive: true });
  await fs.mkdir(EXPORT_ROOT, { recursive: true });
  await fs.mkdir(RUNTIME_ROOT, { recursive: true });
  await fs.access(CHROME_EXECUTABLE);

  const fixtureA = await startFixture("A");
  const fixtureB = await startFixture("B");
  serverA = fixtureA.server;
  serverB = fixtureB.server;
  originA = fixtureA.origin;
  originB = fixtureB.origin;

  profileRoot = await fs.mkdtemp(path.join(RUNTIME_ROOT, "profile-"));
  downloadRoot = path.join(profileRoot, "downloads");
  await fs.mkdir(downloadRoot, { recursive: true });
  const defaultRoot = path.join(profileRoot, "Default");
  await fs.mkdir(defaultRoot, { recursive: true });
  await fs.writeFile(path.join(defaultRoot, "Preferences"), JSON.stringify({
    download: {
      default_directory: downloadRoot,
      prompt_for_download: false,
      directory_upgrade: true
    }
  }), "utf8");

  browser = await puppeteer.launch({
    executablePath: CHROME_EXECUTABLE,
    headless: HEADLESS,
    userDataDir: profileRoot,
    enableExtensions: true,
    defaultViewport: { width: 1280, height: 900 },
    protocolTimeout: 60_000,
    args: CHROMIUM_LAUNCH_POLICY.args
  });
  browserProductVersion = await browser.version();
  browserMajorVersion = Number(String(browserProductVersion).match(/\/(\d+)\./)?.[1]) || null;
  process.stdout.write(`[BOOT] browser launched: ${browserProductVersion}${HEADLESS ? " (headless)" : ""}\n`);

  let installTimedOut = false;
  const installResult = await Promise.race([
    browser.installExtension(EXTENSION_PATH),
    sleep(15_000).then(() => {
      installTimedOut = true;
      return null;
    })
  ]);
  const installedExtensions = await browser.extensions();
  extensionId = installResult || [...installedExtensions.entries()]
    .find(([_id, candidate]) => candidate.name === RECORDER_NAME)?.[0];
  extension = extensionId ? installedExtensions.get(extensionId) : null;
  assert(extension, `Installed extension ${extensionId} was not returned by browser.extensions()`);
  process.stdout.write(`[BOOT] extension ready${installTimedOut ? " (install acknowledgement timed out; discovered from browser state)" : ""}\n`);

  controlPage = await browser.newPage();
  recordPageDiagnostics(controlPage, "dashboard-control");
  await controlPage.goto(`chrome-extension://${extensionId}/dashboard/dashboard.html`, {
    waitUntil: "domcontentloaded",
    timeout: 30_000
  });
  await controlPage.waitForSelector("#sessions");
  browserDownloadClient = await controlPage.createCDPSession();
  await browserDownloadClient.send("Browser.setDownloadBehavior", {
    behavior: "allowAndName",
    downloadPath: downloadRoot,
    eventsEnabled: true
  });
  browserDownloadClient.on("Browser.downloadWillBegin", (params) => {
    browserDownloadEvents.push({ at: now(), method: "Browser.downloadWillBegin", params });
  });
  browserDownloadClient.on("Browser.downloadProgress", (params) => {
    browserDownloadEvents.push({ at: now(), method: "Browser.downloadProgress", params });
  });
  process.stdout.write("[BOOT] dashboard control page ready\n");

  await runCase("T00", "isolated bootstrap, manifest, and fresh storage", async () => {
    assert.equal(extension.enabled, true);
    assert.equal(extension.name, RECORDER_NAME);
    assert.equal(extension.version, RECORDER_VERSION);
    assert.equal(path.resolve(extension.path), path.resolve(EXTENSION_PATH));
    const manifest = await controlPage.evaluate(() => chrome.runtime.getManifest());
    assert.equal(manifest.manifest_version, 3);
    assert.equal(manifest.version, RECORDER_VERSION);
    assert.deepEqual([...manifest.permissions].sort(), [...EXPECTED_PERMISSIONS].sort());
    assert.equal(manifest.permissions.includes("downloads"), false);
    assert.deepEqual(manifest.optional_host_permissions, ["http://*/*", "https://*/*"]);
    assert(
      browserMajorVersion === null || browserMajorVersion >= MINIMUM_CHROME_MAJOR,
      `Browser ${browserProductVersion} is below minimum_chrome_version ${MINIMUM_CHROME_MAJOR}`
    );
    const all = await storageAll();
    assert(!Object.keys(all).some((key) => key.startsWith(SESSION_PREFIX) || key.startsWith(CHUNK_PREFIX)));
    const root = all[ROOT_KEY] || { activeSessionId: null, sessionIds: [] };
    assert.equal(root.activeSessionId, null);
    assert.deepEqual(root.sessionIds, []);
    return { extension_id: extensionId, storage_keys: Object.keys(all).sort() };
  });

  await runCase("T01", "consent gate and versioned disclosure", async () => {
    await resetExtensionState();
    const taskPage = await newTaskPage();
    const popup = await openActionPopup(taskPage);
    await popup.select("#actor-type", "agent");
    await popup.click("#start");
    await popup.waitForFunction(() => document.querySelector("#error").textContent.includes("Affirmative consent"));
    const state = await runtimeRequest({ type: "RECORDER_GET_STATE" });
    assert.equal(state.activeSession, null);
    // Read the footer first: opening the disclosure tab closes the action popup.
    const footer = await popup.$eval("#recorder-version", (element) => element.textContent.trim());
    assert.equal(footer, `v${RECORDER_VERSION}`);
    const existingTargets = new Set(browser.targets());
    const disclosureTarget = browser.waitForTarget(
      (target) => !existingTargets.has(target) && target.url().includes("/privacy/disclosure.html"),
      { timeout: 10_000 }
    );
    await popup.click('a[href="../privacy/disclosure.html"]');
    const disclosurePage = await (await disclosureTarget).asPage();
    assert(disclosurePage);
    recordPageDiagnostics(disclosurePage, "privacy-disclosure");
    await disclosurePage.waitForSelector("body");
    const disclosureText = await disclosurePage.$eval("body", (element) => element.innerText);
    // innerText follows CSS text-transform, so read the version element's raw text.
    const shownVersion = await disclosurePage.$eval("#disclosure-version", (element) => element.textContent.trim());
    assert.equal(shownVersion, DISCLOSURE_VERSION);
    assert(disclosureText.toLowerCase().includes("consent"));
    await screenshot(disclosurePage, "01-versioned-disclosure.png");
    await disclosurePage.close();
    await popup.close().catch(() => undefined);
    await taskPage.close();
    return {
      consent_blocked_before_storage: true,
      disclosure_version: DISCLOSURE_VERSION,
      popup_version_from_manifest: true
    };
  });

  await runCase("T02", "real action broad-event capture and privacy invariants", async () => {
    await resetExtensionState();
    const taskPage = await newTaskPage();
    const taskId = "e2e-safe-happy-path";
    const sessionId = await startSession(taskPage, taskId, {
      topScreenshotName: "02-action-popup-agent-metadata.png",
      screenshotName: "02-action-popup-agent-consent.png"
    });
    await exerciseStandardTask(taskPage);
    await clearFixtureValuesForScreenshot(taskPage);
    await screenshot(taskPage, "02-page-recording-indicator.png");
    const metadata = await stopSession(taskPage);
    const stored = await getStoredSession(sessionId);
    assert.equal(metadata.session_id, sessionId);
    assert.equal(stored.metadata.status, "complete");
    assert.equal(stored.metadata.event_count, stored.events.length);
    assert(stored.events.length >= 30, `Expected at least 30 events, received ${stored.events.length}`);
    assertAgentMetadata(stored.metadata, taskId);
    assertTraceEnvelopeV2(stored);
    assertPrivacySafe(stored);
    safeSession = { metadata: stored.metadata, events: stored.events };
    safePublicSession = await controlPage.evaluate(async (id) => {
      const response = await chrome.runtime.sendMessage({ type: "RECORDER_GET_SESSION", sessionId: id });
      if (!response?.ok) throw new Error(response?.error || "Could not read safe session");
      return CUIRecorderCore.sanitizeSessionForExport(response.data);
    }, sessionId);
    assert.equal("tab_id" in safePublicSession.metadata, false);
    assertTraceEnvelopeV2(safePublicSession);
    assertPrivacySafe(safePublicSession);
    await writeJson("agent_extension_e2e_trace.json", safePublicSession);
    const coverageSpec = JSON.parse(await fs.readFile(COVERAGE_SPEC_PATH, "utf8"));
    const summarizeCoverage = (events) => ({
      event_count: events.length,
      types: [...new Set(events.map((event) => event.type))].sort()
    });
    eventCoverageSummary = {
      schema_version: "cui-recorder-e2e-event-coverage-v1",
      source_trace: "agent_extension_e2e_trace.json",
      supported_browser_event_types: coverageSpec.supported_browser_event_types,
      recorder_generated_event_types: coverageSpec.recorder_generated_event_types,
      naturally_observed_in_fixture: summarizeCoverage(
        stored.events.filter((event) => event.is_trusted === true)
      ),
      synthetically_exercised_in_fixture: summarizeCoverage(
        stored.events.filter((event) => event.is_trusted === false)
      ),
      recorder_generated: summarizeCoverage(
        stored.events.filter((event) => event.is_trusted === null)
      ),
      interpretation_warning: coverageSpec.interpretation_warning
    };
    await writeJson("event_coverage_summary.json", eventCoverageSummary);
    storageLifecycle.push({
      case: "T02",
      session_id: sessionId,
      status: stored.metadata.status,
      event_count: stored.events.length,
      chunk_count: stored.metadata.chunk_count,
      chunk_lengths: stored.chunk_lengths,
      internal_tab_id_present: Number.isInteger(stored.metadata.tab_id),
      public_tab_id_removed: !("tab_id" in safePublicSession.metadata),
      privacy_safe_assertions_passed: true
    });
    const shadowDomPrivacy = await runShadowPrivacySession(taskPage, { raw: false });
    await taskPage.close();
    return {
      session_id: sessionId,
      event_count: stored.events.length,
      captured_types: [...new Set(stored.events.map((event) => event.type))].sort(),
      raw_key_capture: false,
      shadow_dom_private_fields: shadowDomPrivacy
    };
  });

  await runCase("T03", "dashboard inspect, JSON/JSONL/CSV export, and delete", async () => {
    assert(safeSession && safePublicSession, "T02 did not leave a completed safe session for dashboard validation");
    await refreshDashboard();
    await controlPage.waitForSelector(`article[data-session-id="${safeSession.metadata.session_id}"]`, { timeout: 10_000 });
    await controlPage.click('[data-action="inspect"]');
    await controlPage.waitForFunction(() => !document.querySelector(".event-preview").hidden);
    const previewText = await controlPage.$eval(".event-preview", (element) => element.textContent);
    assert(!previewText.includes("tab_id"));
    assert(!previewText.includes(QUERY_SENTINEL));
    await screenshot(controlPage, "03-dashboard-inspect-sanitized.png");

    const jsonFile = await exportFromDashboard("json");
    const jsonlFile = await exportFromDashboard("jsonl");
    const csvFile = await exportFromDashboard("csv");
    const jsonExport = JSON.parse(await fs.readFile(jsonFile, "utf8"));
    assert.deepEqual(jsonExport, safePublicSession);
    assert.equal("tab_id" in jsonExport.metadata, false);
    const jsonlLines = (await fs.readFile(jsonlFile, "utf8")).trim().split(/\r?\n/);
    assert.equal(jsonlLines.length, safePublicSession.events.length + 1);
    const jsonlHeader = JSON.parse(jsonlLines[0]);
    assert.equal(jsonlHeader.record_type, "session");
    assert.equal(jsonlHeader.session.actor_type, "agent");
    assert.equal("tab_id" in jsonlHeader.session, false);
    const csvLines = (await fs.readFile(csvFile, "utf8")).trim().split(/\r?\n/);
    assert.equal(csvLines.length, safePublicSession.events.length + 1);
    assert(csvLines[0].includes('"session_id"'));

    controlPage.once("dialog", (dialog) => void dialog.accept());
    await controlPage.click('[data-action="delete"]');
    await controlPage.waitForFunction(() => document.querySelector("#sessions").innerText.includes("No sessions yet"));
    const all = await storageAll();
    assert(!Object.keys(all).some((key) => key.includes(safeSession.metadata.session_id)));
    await screenshot(controlPage, "03-dashboard-post-delete.png");
    storageLifecycle.push({
      case: "T03",
      session_id: safeSession.metadata.session_id,
      exports: exportEvidence.map((row) => row.format),
      storage_keys_after_delete: Object.keys(all).sort(),
      delete_verified: true
    });
    return { exports: exportEvidence, delete_verified: true };
  });

  await runCase("T04", "same-origin reload resumes capture", async () => {
    await resetExtensionState();
    const taskPage = await newTaskPage();
    const taskId = "e2e-same-origin-reload";
    const sessionId = await startSession(taskPage, taskId);
    await taskPage.click("#action-button");
    await flushPage(taskPage);
    await taskPage.goto(`${originA}${FIXTURE_PATH}?fixture=AFTER_RELOAD#AFTER_RELOAD`, { waitUntil: "networkidle0" });
    await taskPage.waitForSelector(`#${INDICATOR_ID}`, { timeout: 12_000 });
    await taskPage.click("#plain-input");
    await taskPage.keyboard.type("ReloadFixture", { delay: 5 });
    await flushPage(taskPage);
    await screenshot(taskPage, "04-same-origin-reload-resumed.png");
    await stopSession(taskPage);
    const stored = await getStoredSession(sessionId);
    assertAgentMetadata(stored.metadata, taskId);
    assertTraceEnvelopeV2(stored);
    assert.equal(stored.metadata.status, "complete");
    assert(stored.events.filter((event) => event.type === "navigation").length >= 2);
    assert(stored.events.every((event) => !event.url.includes("?") && !event.url.includes("#")));
    await deleteSession(sessionId);
    await taskPage.close();
    return { navigation_events: stored.events.filter((event) => event.type === "navigation").length };
  });

  await runCase("T05", "cross-origin navigation interrupts and clears indicators", async () => {
    await resetExtensionState();
    const taskPage = await newTaskPage();
    const taskId = "e2e-cross-origin-interrupt";
    const sessionId = await startSession(taskPage, taskId);
    await taskPage.click("#action-button");
    await flushPage(taskPage);
    await taskPage.goto(`${originB}${FIXTURE_PATH}`, { waitUntil: "networkidle0" });
    const metadata = await waitForMetadata(sessionId, (row) => row.status === "interrupted", "cross-origin interruption");
    assert.match(metadata.capture_error, /scoped origin|revoked/i);
    assert.equal(await taskPage.$(`#${INDICATOR_ID}`), null);
    const stored = await getStoredSession(sessionId);
    assert(!stored.events.some((event) => event.url.startsWith(originB)));
    await refreshDashboard();
    await screenshot(controlPage, "05-cross-origin-interrupted.png");
    await deleteSession(sessionId);
    await taskPage.close();
    return { status: metadata.status, capture_error: metadata.capture_error };
  });

  await runCase("T06", "recording tab closure interrupts without stranding active state", async () => {
    await resetExtensionState();
    const taskPage = await newTaskPage();
    const taskId = "e2e-tab-close-interrupt";
    const sessionId = await startSession(taskPage, taskId);
    await taskPage.click("#action-button");
    await flushPage(taskPage);
    await taskPage.close();
    const metadata = await waitForMetadata(sessionId, (row) => row.status === "interrupted", "tab-close interruption");
    assert.match(metadata.capture_error, /tab closed/i);
    const state = await runtimeRequest({ type: "RECORDER_GET_STATE" });
    assert.equal(state.activeSession, null);
    await refreshDashboard();
    await screenshot(controlPage, "06-tab-close-interrupted.png");
    await deleteSession(sessionId);
    return { status: metadata.status, active_session_cleared: true };
  });

  await runCase("T07", "MV3 service-worker restart during active capture", async () => {
    await resetExtensionState();
    const taskPage = await newTaskPage();
    const taskId = "e2e-mv3-worker-restart";
    const sessionId = await startSession(taskPage, taskId);
    await taskPage.click("#action-button");
    await flushPage(taskPage);
    const workers = await extension.workers();
    if (!workers.length) throw new SkipCase("No active MV3 worker target was exposed by this Chrome build");
    await workers[0].close();
    await taskPage.click("#plain-input");
    await taskPage.keyboard.type("WorkerRestartFixture", { delay: 4 });
    await sleep(1200);
    await flushPage(taskPage);
    await poll(async () => (await extension.workers()).length > 0, { timeout: 12_000, label: "restarted MV3 worker" });
    await screenshot(taskPage, "07-service-worker-restart-active.png");
    await stopSession(taskPage);
    const stored = await getStoredSession(sessionId);
    assert.equal(stored.metadata.status, "complete");
    assert(stored.events.length > 10);
    await deleteSession(sessionId);
    await taskPage.close();
    return { worker_restart_observed: true, event_count: stored.events.length };
  });

  await runCase("T08", "storage rollover beyond one 500-event chunk", async () => {
    await resetExtensionState();
    const taskPage = await newTaskPage();
    const taskId = "e2e-storage-chunk-rollover";
    const sessionId = await startSession(taskPage, taskId);
    await taskPage.click("#bulk-input");
    await taskPage.keyboard.type("a".repeat(280));
    await flushPage(taskPage);
    await waitForMetadata(sessionId, (row) => row.event_count > 500, "more than 500 stored events");
    await stopSession(taskPage);
    const stored = await getStoredSession(sessionId);
    assert(stored.events.length > 500);
    assert(stored.chunk_lengths.length >= 2);
    assert.equal(stored.chunk_lengths[0], 500);
    assert(stored.chunk_lengths.every((length) => length > 0 && length <= 500));
    await refreshDashboard();
    await screenshot(controlPage, "08-storage-chunk-rollover.png");
    storageLifecycle.push({
      case: "T08",
      session_id: sessionId,
      event_count: stored.events.length,
      chunk_count: stored.chunk_lengths.length,
      chunk_lengths: stored.chunk_lengths,
      rollover_verified: true
    });
    await deleteSession(sessionId);
    await taskPage.close();
    return { event_count: stored.events.length, chunk_lengths: stored.chunk_lengths };
  });

  await runCase("T09", "raw-key double confirmation and private-field invariant", async () => {
    await resetExtensionState();
    const taskPage = await newTaskPage();
    await startSession(taskPage, "e2e-raw-confirm-dismiss", { raw: true, acceptRaw: false });
    const taskId = "e2e-raw-private-invariant";
    const sessionId = await startSession(taskPage, taskId, {
      raw: true,
      acceptRaw: true,
      screenshotName: "09-raw-mode-consent.png"
    });
    await taskPage.click("#plain-input");
    await taskPage.keyboard.type(RAW_TEXT_SENTINEL, { delay: 5 });
    await taskPage.click("#password-input");
    await taskPage.keyboard.type(PASSWORD_SENTINEL, { delay: 5 });
    await taskPage.click("#private-input");
    await taskPage.keyboard.type(PRIVATE_SENTINEL, { delay: 5 });
    await flushPage(taskPage);
    await stopSession(taskPage);
    const stored = await getStoredSession(sessionId);
    assertAgentMetadata(stored.metadata, taskId, { raw: true });
    assertTraceEnvelopeV2(stored);
    assert(stored.events.some((event) => event.type === "keydown" && event.key === "R" && !event.target.password_like));
    const privateEvents = stored.events.filter((event) => event.target.password_like);
    assert(privateEvents.length > 0);
    assert(privateEvents.every((event) => event.input === null));
    const privateKeyEvents = privateEvents.filter((event) => ["keydown", "keyup"].includes(event.type));
    assert(privateKeyEvents.length > 0, "raw mode captured no private key events");
    privateKeyEvents.forEach(assertPrivateKeyEvent);
    const privateSerialized = JSON.stringify(privateEvents);
    assert(!privateSerialized.includes(PASSWORD_SENTINEL));
    assert(!privateSerialized.includes(PRIVATE_SENTINEL));
    // Raw traces are deliberately not exported or retained as evidence.
    await deleteSession(sessionId);
    const shadowDomPrivacy = await runShadowPrivacySession(taskPage, { raw: true });
    await taskPage.close();
    return {
      dismissed_confirmation_created_no_session: true,
      ordinary_raw_key_present: true,
      private_key_redaction_verified: true,
      shadow_dom_private_fields: shadowDomPrivacy,
      raw_trace_exported: false,
      raw_trace_deleted_immediately: true
    };
  });

  await runCase("T10", "protected browser page is not recordable", async () => {
    await resetExtensionState();
    const page = await browser.newPage();
    recordPageDiagnostics(page, "protected-page", browserUiErrors);
    await page.goto("chrome://settings/", { waitUntil: "domcontentloaded" });
    let popup;
    try {
      popup = await openActionPopup(page);
    } catch (error) {
      throw new SkipCase(`Chrome did not expose the action popup on a protected page: ${error.message}`);
    }
    await configureAgentPopup(popup, "e2e-protected-page-blocked");
    const disabled = await popup.$eval("#start", (element) => element.disabled);
    const scope = await popup.$eval("#scope", (element) => element.textContent);
    assert.equal(disabled, true);
    assert.match(scope, /protected page cannot be recorded/i);
    const state = await runtimeRequest({ type: "RECORDER_GET_STATE" });
    assert.equal(state.activeSession, null);
    await popup.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    await screenshot(popup, "10-protected-page-blocked.png", { fullPage: false });
    await popup.close().catch(() => undefined);
    await page.close();
    return { start_disabled: true, active_session_created: false };
  });

  await runCase("T11", "event-time step boundary survives one delayed content batch", async () => {
    await resetExtensionState();
    const taskPage = await newTaskPage();
    const taskId = "e2e-event-time-step-boundary";
    const sessionId = await startSession(taskPage, taskId);
    await runtimeRequest({ type: "RECORDER_SET_STEP", stepId: "step-01" });
    await taskPage.click("#action-button");
    // Deliberately leave the first click in the content-script batch while the
    // background step advances. The stored label must remain the event-time one.
    await runtimeRequest({ type: "RECORDER_SET_STEP", stepId: "step-02" });
    await taskPage.click("#action-button");
    await flushPage(taskPage);
    await stopSession(taskPage);
    const stored = await getStoredSession(sessionId);
    const actionClicks = stored.events.filter(
      (event) => event.type === "click" && event.target.selector_hint.includes("#action-button")
    );
    const first = actionClicks.find((event) => event.step_id === "step-01");
    const second = actionClicks.find((event) => event.step_id === "step-02");
    assert(first, "The delayed first click lost step-01");
    assert(second, "The second click lost step-02");
    assert(first.event_seq < second.event_seq, "Step-boundary event order was reversed");
    const publicSession = await controlPage.evaluate(async (id) => {
      const response = await chrome.runtime.sendMessage({ type: "RECORDER_GET_SESSION", sessionId: id });
      return CUIRecorderCore.sanitizeSessionForExport(response.data);
    }, sessionId);
    assert.equal("known_step_ids" in publicSession.metadata, false);
    await deleteSession(sessionId);
    await taskPage.close();
    return {
      delayed_step_id: first.step_id,
      next_step_id: second.step_id,
      event_seq_order: [first.event_seq, second.event_seq]
    };
  });

  await runCase("T12", "dynamic same-origin iframe capture and all-frame final flush", async () => {
    await resetExtensionState();
    const taskPage = await newTaskPage();
    const taskId = "e2e-dynamic-same-origin-iframe";
    const sessionId = await startSession(taskPage, taskId);
    await taskPage.evaluate((src) => {
      const frame = document.createElement("iframe");
      frame.id = "dynamic-task-frame";
      frame.src = src;
      document.querySelector("main").appendChild(frame);
    }, `${originA}${FRAME_PATH}?private=FRAME_QUERY_MUST_NOT_PERSIST`);
    const taskFrame = await poll(
      () => taskPage.frames().find((frame) => frame.url().includes(FRAME_PATH)) || null,
      { timeout: 12_000, label: "dynamic task iframe" }
    );
    await taskFrame.waitForSelector(`#${INDICATOR_ID}`, { timeout: 12_000 });
    await taskFrame.click("#frame-action");
    await taskFrame.click("#frame-input");
    await taskFrame.type("#frame-input", "DynamicFrameFixture", { delay: 8 });
    await sleep(1300);
    await taskPage.evaluate((src) => {
      const frame = document.createElement("iframe");
      frame.id = "outside-scope-frame";
      frame.src = src;
      document.querySelector("main").appendChild(frame);
    }, `${originB}${FRAME_PATH}?private=OUTSIDE_SCOPE_QUERY`);
    const outsideFrame = await poll(
      () => taskPage.frames().find(
        (frame) => frame.url().startsWith(originB) && frame.url().includes(FRAME_PATH)
      ) || null,
      { timeout: 12_000, label: "outside-scope dynamic iframe" }
    );
    await outsideFrame.waitForSelector("#frame-action", { timeout: 12_000 });
    assert.equal(
      await outsideFrame.$(`#${INDICATOR_ID}`),
      null,
      "Recorder entered a cross-origin frame without a scoped grant"
    );
    await outsideFrame.click("#frame-action");
    await outsideFrame.type("#frame-input", "OutsideScopeFixture", { delay: 8 });
    await sleep(1200);
    await flushPage(taskPage);
    await stopSession(taskPage);
    assert.equal(await taskFrame.$(`#${INDICATOR_ID}`), null, "Frame recording indicator survived stop");
    const stored = await getStoredSession(sessionId);
    const frameEvents = stored.events.filter((event) => event.url.endsWith(FRAME_PATH));
    assert(frameEvents.some((event) => event.type === "navigation"));
    assert(
      frameEvents.some(
        (event) => event.type === "click" && event.target.selector_hint.includes("#frame-action")
      ),
      "Dynamic-frame click was not stored"
    );
    assert.equal(JSON.stringify(frameEvents).includes("FRAME_QUERY_MUST_NOT_PERSIST"), false);
    assert.equal(JSON.stringify(frameEvents).includes("DynamicFrameFixture"), false);
    assert.equal(stored.events.some((event) => event.url.startsWith(originB)), false);
    assert.equal(JSON.stringify(stored.events).includes("OutsideScopeFixture"), false);
    await deleteSession(sessionId);
    await taskPage.close();
    return {
      captured_frame_event_count: frameEvents.length,
      query_and_input_content_absent: true,
      frame_indicator_cleared: true,
      cross_origin_without_grant_excluded: true
    };
  });

  await runCase("T13", "trusted-only storage and post-stop negative control", async () => {
    await resetExtensionState();
    const taskPage = await newTaskPage();
    const taskId = "e2e-post-stop-negative-control";
    const sessionId = await startSession(taskPage, taskId);
    const tabId = await tabIdForPage(taskPage);
    const exposure = await controlPage.evaluate(async (id) => {
      return chrome.scripting.executeScript({
        target: { tabId: id },
        func: async () => {
          const apiPresent = Boolean(globalThis.chrome?.storage?.local);
          try {
            await globalThis.chrome.storage.local.get(null);
            return { api_present: apiPresent, read_succeeded: true, error: null };
          } catch (error) {
            return {
              api_present: apiPresent,
              read_succeeded: false,
              error: String(error?.message || error)
            };
          }
        }
      });
    }, tabId);
    assert.equal(
      exposure.every((row) => row.result?.read_succeeded === false),
      true,
      `Content script could read local storage: ${JSON.stringify(exposure)}`
    );
    await taskPage.click("#action-button");
    await flushPage(taskPage);
    await stopSession(taskPage);
    const countAtStop = (await getStoredSession(sessionId)).events.length;
    await taskPage.click("#plain-input");
    await taskPage.keyboard.type("PostStopFixture", { delay: 8 });
    await taskPage.click("#action-button");
    await sleep(1300);
    const after = await getStoredSession(sessionId);
    assert.equal(after.events.length, countAtStop, "Events were appended after stop");
    assert.equal(after.metadata.status, "complete");
    assert.equal(await taskPage.$(`#${INDICATOR_ID}`), null);
    await deleteSession(sessionId);
    await taskPage.close();
    return {
      trusted_context_storage_only: true,
      event_count_at_stop: countAtStop,
      event_count_after_negative_control: after.events.length
    };
  });

  await runCase("T14", "content-script senders cannot invoke privileged recorder messages", async () => {
    await resetExtensionState();
    const taskPage = await newTaskPage();
    const taskId = "e2e-sender-authority";
    const sessionId = await startSession(taskPage, taskId);
    const tabId = await tabIdForPage(taskPage);
    const privilegedTypes = [
      "RECORDER_LIST_SESSIONS",
      "RECORDER_GET_SESSION",
      "RECORDER_DELETE_SESSION",
      "RECORDER_START",
      "RECORDER_STOP",
      "RECORDER_SET_STEP"
    ];
    const probe = await controlPage.evaluate(async ({ id, types, session }) => {
      const rows = await chrome.scripting.executeScript({
        target: { tabId: id },
        args: [types, session],
        func: async (messageTypes, targetSession) => {
          const responses = {};
          for (const type of messageTypes) {
            try {
              responses[type] = await chrome.runtime.sendMessage({
                type,
                sessionId: targetSession,
                stepId: "forged-step"
              });
            } catch (error) {
              responses[type] = { ok: false, error: String(error?.message || error) };
            }
          }
          const state = await chrome.runtime.sendMessage({ type: "RECORDER_GET_STATE" });
          return { responses, state };
        }
      });
      return rows.find((row) => row.frameId === 0)?.result || null;
    }, { id: tabId, types: privilegedTypes, session: sessionId });
    assert(probe, "content-world probe returned no result");
    for (const type of privilegedTypes) {
      assert.equal(probe.responses[type]?.ok, false, `${type} was accepted from a content script`);
      assert.match(String(probe.responses[type]?.error), /not accepted from this sender/, type);
    }
    assert.equal(probe.state?.ok, true);
    const projected = probe.state.data.activeSession;
    assert.equal(projected?.session_id, sessionId);
    assert.deepEqual(Object.keys(projected).sort(), [
      "current_document_epoch",
      "current_step_id",
      "include_query",
      "raw_key_capture",
      "schema_version",
      "scope_origin",
      "session_id",
      "status"
    ]);
    assert.equal(JSON.stringify(probe.state).includes(taskId), false, "content state leaked the task id");
    await taskPage.click("#action-button");
    await flushPage(taskPage);
    await stopSession(taskPage);
    const stored = await getStoredSession(sessionId);
    assert.equal(stored.metadata.status, "complete");
    assert.equal(stored.metadata.current_step_id ?? null, null, "a content script changed the step id");
    await deleteSession(sessionId);
    await taskPage.close();
    return {
      privileged_messages_rejected: privilegedTypes.length,
      content_state_fields: Object.keys(projected).sort()
    };
  });
}

async function writeEvidence(fatalError) {
  const extensionHash = await hashDirectory(EXTENSION_PATH).catch(() => ({ sha256: "unavailable", file_count: 0 }));
  const chromeInfo = CHROME_EXECUTABLE ? await fs.stat(CHROME_EXECUTABLE).catch(() => null) : null;
  const finishedAt = now();
  const passCount = cases.filter((row) => row.status === "passed").length;
  const failCount = cases.filter((row) => row.status === "failed").length;
  const skipCount = cases.filter((row) => row.status === "skipped").length;
  const runManifest = {
    schema_version: "cui-recorder-extension-e2e-run-v1",
    artifact_path_base: "bundle_root",
    started_at: startedAt,
    finished_at: finishedAt,
    runner: "extension/test/e2e/recorder.e2e.mjs",
    repository_commit: gitOutput("rev-parse", "HEAD"),
    repository_status_before_run: repositoryStatusBeforeRun,
    node_version: process.version,
    platform: `${process.platform}-${process.arch}`,
    puppeteer_version: PUPPETEER_VERSION,
    reproducibility: {
      runner_sha256: await sha256(SCRIPT_PATH),
      launch_policy_sha256: await sha256(path.join(SCRIPT_DIR, "chromium_launch_policy.mjs")),
      package_json_sha256: await sha256(path.join(EXTENSION_ROOT, "package.json")),
      package_lock_sha256: await sha256(path.join(EXTENSION_ROOT, "package-lock.json"))
    },
    browser_runtime: {
      product_version: browserProductVersion || "unavailable",
      major_version: browserMajorVersion,
      headless: HEADLESS,
      executable_name: CHROME_EXECUTABLE ? path.basename(CHROME_EXECUTABLE) : null,
      executable_bytes: chromeInfo?.size || null,
      executable_sha256: chromeInfo ? await sha256(CHROME_EXECUTABLE) : null,
      provenance: CHROME_PROVENANCE,
      puppeteer_managed_default_used: CHROME_PROVENANCE === "Puppeteer-managed Chrome for Testing",
      sandbox_mode: CHROMIUM_LAUNCH_POLICY.sandbox_mode,
      ci_no_sandbox_enabled: CHROMIUM_LAUNCH_POLICY.ci_no_sandbox_enabled
    },
    extension: {
      id: extensionId || null,
      name: extension?.name || RECORDER_NAME,
      version: extension?.version || RECORDER_VERSION,
      minimum_chrome_version: EXTENSION_MANIFEST.minimum_chrome_version,
      permissions: [...EXTENSION_MANIFEST.permissions].sort(),
      source_tree_sha256: extensionHash.sha256,
      source_file_count: extensionHash.file_count
    },
    isolation: {
      user_chrome_profile_used: false,
      user_chrome_process_touched: false,
      unique_profile_under_configured_runtime: true,
      loopback_fixture_only: true,
      fixture_origins: [originA, originB].filter(Boolean)
    },
    actor_claim: {
      actor_type: "agent",
      actor_label: AGENT_LABEL,
      source_class: "synthetic_coverage",
      cui_framework_claimed: false,
      human_data_claimed: false,
      comparison_claimed: false
    },
    fatal_error: fatalError ? { message: fatalError.message, stack: fatalError.stack } : null
  };
  const validationSummary = {
    schema_version: "cui-recorder-extension-e2e-summary-v1",
    artifact_path_base: "bundle_root",
    recorder_version: RECORDER_VERSION,
    overall_status: fatalError || failCount ? "failed" : "passed",
    claim_boundary: "Automated extension QA on deterministic loopback fixtures; this is not a computer-use-agent framework run, a human trace, a human-vs-agent comparison, or a detector result.",
    counts: { total: cases.length, passed: passCount, failed: failCount, skipped: skipCount },
    cases,
    browser_errors: browserErrors,
    browser_ui_errors: browserUiErrors,
    generated_artifacts: []
  };
  for (const fileName of [
    "run_manifest.json",
    "storage_lifecycle_summary.json",
    "exports_manifest.json",
    "validation_summary.json",
    "E2E_REPORT.md"
  ]) {
    artifacts.push(bundlePath(path.join(RESULT_ROOT, fileName)));
  }
  validationSummary.generated_artifacts = [...new Set(artifacts)].sort();
  await writeJson("run_manifest.json", runManifest);
  await writeJson("storage_lifecycle_summary.json", { sessions: storageLifecycle });
  const publicDownloadEvents = browserDownloadEvents.map((event) => {
    const params = event.params || {};
    const safeParams = {
      guid: params.guid || null,
      state: params.state || null,
      total_bytes: Number.isFinite(params.totalBytes) ? params.totalBytes : null,
      received_bytes: Number.isFinite(params.receivedBytes) ? params.receivedBytes : null,
      suggested_filename: params.suggestedFilename ? path.basename(params.suggestedFilename) : null
    };
    return { at: event.at, method: event.method, params: safeParams };
  });
  await writeJson("exports_manifest.json", {
    exports: exportEvidence,
    browser_download_events: publicDownloadEvents
  });
  await writeJson("validation_summary.json", validationSummary);

  const lines = [
    `# ${RECORDER_NAME} ${RECORDER_VERSION} - isolated extension E2E report`,
    "",
    `- Overall: **${validationSummary.overall_status.toUpperCase()}**`,
    `- Run: ${startedAt} to ${finishedAt}`,
    `- Cases: ${passCount} passed, ${failCount} failed, ${skipCount} skipped`,
    `- Runtime: Puppeteer ${PUPPETEER_VERSION} + ${browserProductVersion || "Chrome (version unavailable)"} (${CHROME_PROVENANCE}${HEADLESS ? ", headless" : ""})`,
    "- Actor boundary: the coarse actor field is `agent`, while `source_class=synthetic_coverage`; no agent framework drove the browser.",
    "- Scope boundary: deterministic loopback fixtures only; no human-vs-agent comparison is claimed.",
    "- Isolation: a unique disposable profile was used; the user's normal Chrome/profile was not read or modified.",
    "",
    "## Validation matrix",
    "",
    "| ID | Result | Validation | Evidence / detail |",
    "|---|---|---|---|"
  ];
  for (const row of cases) {
    const detail = row.status === "passed"
      ? JSON.stringify(row.details || {}).replaceAll("|", "\\|")
      : String(row.error || "").replaceAll("|", "\\|");
    lines.push(`| ${row.id} | ${row.status.toUpperCase()} | ${row.name} | ${detail} |`);
  }
  lines.push(
    "",
    "## Evidence set",
    "",
    "- `validation_summary.json`: machine-readable case outcomes and diagnostics.",
    "- `run_manifest.json`: pinned runtime, source hash, commit, isolation, and claim boundary.",
    "- `storage_lifecycle_summary.json`: storage/chunk/delete assertions without raw typed content.",
    "- `exports_manifest.json`: retained safe-mode export hashes.",
    "- `event_coverage_summary.json`: supported, naturally observed, synthetic, and recorder-generated classes kept separate.",
    "- `agent_extension_e2e_trace.json`: legacy-named sanitized synthetic-coverage trace produced by the real extension.",
    "- `screenshots/`: real action popup, page indicator, dashboard, interruption, rollover, raw-mode warning, and protected-page UI.",
    "- `exports/`: dashboard-produced safe JSON, JSONL, and CSV samples, confirmed through CDP download events.",
    "- All paths inside the bundle are relative to the bundle root.",
    "",
    "## Important interpretation",
    "",
    "This report validates extension behavior and data lifecycle. It does not substitute for a real computer-use-agent run, consented human data collection, or a matched human-vs-agent study. Raw-key mode was tested only with fixed synthetic input; that trace was not exported and was deleted immediately."
  );
  const reportPath = path.join(RESULT_ROOT, "E2E_REPORT.md");
  await fs.writeFile(reportPath, `${lines.join("\n")}\n`, "utf8");
  artifacts.push(bundlePath(reportPath));
  return validationSummary;
}

let fatalError;
try {
  await main();
} catch (error) {
  fatalError = error;
  process.stderr.write(`\nFatal E2E error: ${error.stack || error.message}\n`);
} finally {
  if (browser) await browser.close().catch(() => undefined);
  if (serverA) await new Promise((resolve) => serverA.close(resolve));
  if (serverB) await new Promise((resolve) => serverB.close(resolve));
  const validation = await writeEvidence(fatalError).catch((error) => {
    process.stderr.write(`Could not write E2E evidence: ${error.stack || error.message}\n`);
    return { overall_status: "failed" };
  });
  if (profileRoot && path.dirname(profileRoot) === path.resolve(RUNTIME_ROOT) && path.basename(profileRoot).startsWith("profile-")) {
    await fs.rm(profileRoot, { recursive: true, force: true }).catch((error) => {
      process.stderr.write(`Could not remove isolated profile ${profileRoot}: ${error.message}\n`);
    });
  }
  if (fatalError || validation.overall_status !== "passed") process.exitCode = 1;
}
