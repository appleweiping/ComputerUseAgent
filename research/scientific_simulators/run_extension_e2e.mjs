// Scripted-automation control: drives three pinned public simulators through the
// real recorder extension and judges the final simulator state independently.
//
// Usage (from the repository root, after `npm ci --prefix research/scientific_simulators`):
//   node research/scientific_simulators/run_extension_e2e.mjs [--dry-run]
//     [--output-dir DIR] [--runtime-dir DIR] [--chrome PATH] [--reps N] [--tasks a,b]
//
// Output goes to a new or empty directory (default: a fresh directory under the OS
// temp dir) and is refused anywhere inside research/evidence, so a run can never
// overwrite the archived control. Nothing is ever deleted from the output directory.
import assert from "node:assert/strict";
import crypto from "node:crypto";
import { execFileSync } from "node:child_process";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import {
  classifyBrowserErrors,
  runWithTimeLimit,
  sanitizeConsoleSource
} from "./runner_policy.mjs";
import { validateTaskPage } from "./validator.mjs";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(SCRIPT_DIR, "../..");
const EXTENSION_PATH = path.join(REPO_ROOT, "extension", "app");
const EVIDENCE_ROOT = path.join(REPO_ROOT, "research", "evidence");
const TASK_ROOT = path.join(SCRIPT_DIR, "tasks");
const VALIDATOR_PATH = path.join(SCRIPT_DIR, "validator.mjs");
const RUNNER_POLICY_PATH = path.join(SCRIPT_DIR, "runner_policy.mjs");
const USAGE = `Usage: node research/scientific_simulators/run_extension_e2e.mjs [options]
  --dry-run            validate the task manifests and print the plan; launch nothing, write nothing
  --output-dir DIR     new or empty result directory, never inside research/evidence
                       (env CUA_SIM_E2E_RESULT_DIR; default: a fresh directory under the OS temp dir)
  --runtime-dir DIR    parent of the temporary Chrome profile (env CUA_SIM_E2E_RUNTIME_DIR)
  --chrome PATH        Chrome executable (env CUA_SIM_E2E_CHROME; default: Puppeteer's Chrome for Testing)
  --reps N             repetitions per task, 1-10 (env CUA_SIM_E2E_REPS; default 3)
  --tasks a,b          task ids to run (env CUA_SIM_E2E_TASKS; default: all)
`;

class UsageError extends Error {}

function parseOptions(argv, env) {
  const values = {
    outputDir: env.CUA_SIM_E2E_RESULT_DIR || "",
    runtimeDir: env.CUA_SIM_E2E_RUNTIME_DIR || "",
    chrome: env.CUA_SIM_E2E_CHROME || "",
    repetitions: env.CUA_SIM_E2E_REPS || "3",
    tasks: env.CUA_SIM_E2E_TASKS || "",
    dryRun: false,
    help: false
  };
  const flags = {
    "--output-dir": "outputDir",
    "--runtime-dir": "runtimeDir",
    "--chrome": "chrome",
    "--reps": "repetitions",
    "--tasks": "tasks"
  };
  for (let index = 0; index < argv.length; index += 1) {
    const flag = argv[index];
    if (flag === "--dry-run") {
      values.dryRun = true;
    } else if (flag === "--help" || flag === "-h") {
      values.help = true;
    } else if (flags[flag] && index + 1 < argv.length) {
      values[flags[flag]] = argv[index + 1];
      index += 1;
    } else {
      throw new UsageError(`Unknown or incomplete argument: ${flag}`);
    }
  }
  const repetitions = Number(values.repetitions);
  if (!Number.isInteger(repetitions) || repetitions < 1 || repetitions > 10) {
    throw new UsageError(`--reps must be an integer from 1 to 10, got ${values.repetitions}`);
  }
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
  const base = path.join(os.tmpdir(), "computer-use-agent", "scientific-simulators");
  return {
    outputDir: path.resolve(values.outputDir || path.join(base, `run-${stamp}-${process.pid}`)),
    runtimeDir: path.resolve(values.runtimeDir || path.join(base, "runtime")),
    chrome: values.chrome ? path.resolve(values.chrome) : null,
    repetitions,
    tasks: String(values.tasks).split(",").map((value) => value.trim()).filter(Boolean),
    dryRun: values.dryRun,
    help: values.help
  };
}

let options;
try {
  options = parseOptions(process.argv.slice(2), process.env);
} catch (error) {
  if (!(error instanceof UsageError)) throw error;
  process.stderr.write(`${error.message}\n${USAGE}`);
  process.exit(2);
}
if (options.help) {
  process.stdout.write(USAGE);
  process.exit(0);
}

const RESULT_ROOT = options.outputDir;
const RUN_ROOT = path.join(RESULT_ROOT, "runs");
const PROGRESS_PATH = path.join(RESULT_ROOT, "progress.log");
const RUNTIME_ROOT = options.runtimeDir;
const REPETITIONS = options.repetitions;
const TASK_FILTER = new Set(options.tasks);

const ROOT_KEY = "cui-recorder-root";
const SESSION_PREFIX = "cui-session-";
const CHUNK_PREFIX = "cui-events-";
const INDICATOR_ID = "cui-trace-recorder-indicator";
const REQUIRED_POINTER_CAPTURE_TYPES = ["navigation", "pointermove", "pointerdown", "pointerup", "click"];
const TRUSTED_INPUT_TYPES = new Set(["pointermove", "pointerdown", "pointerup", "click", "keydown", "keyup"]);

let browser;
let extension;
let extensionId;
let controlPage;
let profileRoot;

function comparablePath(value) {
  const resolved = path.resolve(value);
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

function isInside(child, parent) {
  const relative = path.relative(comparablePath(parent), comparablePath(child));
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

async function realpathOfNearestAncestor(value) {
  let current = path.resolve(value);
  const missing = [];
  for (;;) {
    try {
      return path.join(await fs.realpath(current), ...missing.reverse());
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
      const parent = path.dirname(current);
      if (parent === current) return path.resolve(value);
      missing.push(path.basename(current));
      current = parent;
    }
  }
}

// The archived control under research/evidence is read-only: refuse any output
// directory inside it (lexically or through a link), and refuse a non-empty
// directory so that nothing pre-existing is ever replaced or deleted.
async function assertSafeOutputRoot(root) {
  const evidence = await realpathOfNearestAncestor(EVIDENCE_ROOT);
  const target = await realpathOfNearestAncestor(root);
  if (isInside(root, EVIDENCE_ROOT) || isInside(target, evidence)) {
    throw new UsageError(
      `--output-dir must not be inside research/evidence (archived evidence is read-only): ${root}`
    );
  }
  let entries = [];
  try {
    entries = await fs.readdir(root);
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
  if (entries.length > 0) {
    throw new UsageError(`--output-dir must be a new or empty directory: ${root}`);
  }
}

async function readExtensionManifest() {
  return JSON.parse(await fs.readFile(path.join(EXTENSION_PATH, "manifest.json"), "utf8"));
}

function now() {
  return new Date().toISOString();
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function poll(check, { timeout = 20_000, interval = 125, label = "condition" } = {}) {
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
  throw new Error(`Timed out waiting for ${label}${lastError ? `: ${lastError.message}` : ""}`);
}

async function sha256File(filePath) {
  return crypto.createHash("sha256").update(await fs.readFile(filePath)).digest("hex");
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

async function writeJson(destination, value) {
  await fs.mkdir(path.dirname(destination), { recursive: true });
  await fs.writeFile(destination, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

async function logProgress(message) {
  const line = `${now()} ${message}`;
  process.stdout.write(`${line}\n`);
  await fs.appendFile(PROGRESS_PATH, `${line}\n`, "utf8");
}

async function readTasks() {
  const fileNames = (await fs.readdir(TASK_ROOT))
    .filter((name) => name.endsWith(".json"))
    .sort();
  const rows = [];
  for (const fileName of fileNames) {
    const filePath = path.join(TASK_ROOT, fileName);
    const task = JSON.parse(await fs.readFile(filePath, "utf8"));
    assert.equal(task.schema_version, "cui-sim-task-v1");
    assert.equal(task.task_id, path.basename(fileName, ".json"));
    assert.match(task.task_version, /^\d+\.\d+\.\d+$/);
    const simulatorUrl = new URL(task.simulator?.public_url);
    assert.equal(simulatorUrl.protocol, "https:");
    assert(["phet.colorado.edu", "netlogoweb.org"].includes(simulatorUrl.hostname));
    assert(Array.isArray(task.actions) && task.actions.length > 0);
    assert(Array.isArray(task.validator?.checks) && task.validator.checks.length > 0);
    assert(
      Number.isInteger(task.time_limit_seconds) &&
      task.time_limit_seconds >= 1 &&
      task.time_limit_seconds <= 600,
      `Invalid time_limit_seconds for ${task.task_id}`
    );
    rows.push({ task, filePath, sha256: await sha256File(filePath) });
  }
  const selected = TASK_FILTER.size
    ? rows.filter(({ task }) => TASK_FILTER.has(task.task_id))
    : rows;
  assert(selected.length > 0, "No simulator task manifests selected");
  if (TASK_FILTER.size) {
    assert.equal(selected.length, TASK_FILTER.size, "At least one requested simulator task id was not found");
  }
  return selected;
}

async function fetchSimulatorDigest(url) {
  let lastError;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      const response = await fetch(url, {
        headers: { "user-agent": "computer-use-agent-simulator-pilot/1.0" },
        signal: AbortSignal.timeout(120_000)
      });
      assert.equal(response.status, 200, `Pinned simulator returned HTTP ${response.status}: ${url}`);
      const bytes = Buffer.from(await response.arrayBuffer());
      return {
        requested_url: url,
        resolved_url: response.url,
        status: response.status,
        byte_length: bytes.length,
        sha256: crypto.createHash("sha256").update(bytes).digest("hex"),
        etag: response.headers.get("etag"),
        last_modified: response.headers.get("last-modified"),
        content_type: response.headers.get("content-type")
      };
    } catch (error) {
      if (error?.code === "ERR_ASSERTION") throw error;
      lastError = error;
      if (attempt < 3) await sleep(1_500 * attempt);
    }
  }
  throw new Error(`Could not fetch and hash pinned artifact after 3 attempts: ${url}; ${lastError?.message}`);
}

async function waitForTaskReady(page, ready) {
  if (ready.kind === "text_button") {
    await page.waitForFunction(
      (text) => [...document.querySelectorAll("button")]
        .some((element) => !element.hidden && element.textContent.trim() === text),
      { timeout: 45_000 },
      ready.text
    );
    return;
  }
  if (ready.kind === "visible_svg_text") {
    await page.waitForFunction((text) => {
      const normalize = (value) => (value || "")
        .replace(/[\u202A-\u202E\u2066-\u2069]/g, "")
        .replace(/\u00a0/g, " ")
        .trim();
      return [...document.querySelectorAll("svg text")].some((element) => {
        const rect = element.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0 && normalize(element.textContent) === text;
      });
    }, { timeout: 45_000 }, ready.text);
    return;
  }
  if (ready.kind === "netlogo_rule90") {
    await page.waitForFunction(() =>
      document.title === "NetLogo Web: CA 1D Elementary" &&
      Boolean(window.world) &&
      Boolean(window.session) &&
      Boolean(document.querySelector("label[aria-label='rule slider'] input[type='number']")),
    { timeout: 90_000 });
    return;
  }
  throw new Error(`Unsupported task ready kind: ${ready.kind}`);
}

async function runtimeRequest(payload) {
  const response = await controlPage.evaluate(async (message) => chrome.runtime.sendMessage(message), payload);
  if (!response?.ok) throw new Error(response?.error || "Extension runtime request failed");
  return response.data;
}

async function storageAll() {
  return controlPage.evaluate(async () => chrome.storage.local.get(null));
}

async function resetExtensionState() {
  await controlPage.evaluate(async ({ rootKey }) => {
    await chrome.storage.local.clear();
    await chrome.storage.local.set({ [rootKey]: { activeSessionId: null, sessionIds: [] } });
  }, { rootKey: ROOT_KEY });
  const all = await storageAll();
  assert(!Object.keys(all).some((key) => key.startsWith(SESSION_PREFIX) || key.startsWith(CHUNK_PREFIX)));
}

async function closeActionPopups() {
  for (const page of await extension.pages()) {
    if (!page.isClosed() && page.url().includes("/popup/popup.html")) {
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
      }, { timeout: 10_000, label: "extension action popup" });
      await popup.waitForSelector("#status-chip", { timeout: 8_000 });
      return popup;
    } catch (error) {
      lastError = error;
      await closeActionPopups().catch(() => undefined);
      await sleep(300);
    }
  }
  throw new Error(`Could not open extension action popup: ${lastError?.message || "unknown error"}`);
}

async function setField(page, selector, value) {
  await page.$eval(selector, (element, nextValue) => {
    element.value = nextValue;
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
  }, value);
}

async function configureAgentPopup(popup, task) {
  await popup.select("#actor-type", "agent");
  await setField(popup, "#participant-code", "week3-agent-pilot-001");
  await setField(popup, "#task-id", task.task_id);
  await setField(popup, "#pair-id", "unpaired-agent-pilot");
  await setField(popup, "#protocol-version", "scientific-simulator-pilot-v1");
  await setField(popup, "#actor-label", "puppeteer-isolated-system-chrome");
  await setField(popup, "#executor", "puppeteer-cdp");
  await setField(popup, "#executor-version", "25.4.0");
  await popup.click('input[name="key-mode"][value="safe"]');
  const consentChecked = await popup.$eval("#consent", (element) => element.checked);
  if (!consentChecked) await popup.click("#consent");
}

async function tabIdForPage(page) {
  const url = page.url();
  const tabs = await controlPage.evaluate(async () => chrome.tabs.query({}));
  const matches = tabs.filter((tab) => tab.url === url);
  if (matches.length === 1) return matches[0].id;
  const active = matches.find((tab) => tab.active);
  if (active) return active.id;
  throw new Error(`Could not resolve tab id for ${url}`);
}

async function startSession(taskPage, task, popupScreenshotPath = null) {
  const popup = await openActionPopup(taskPage);
  await configureAgentPopup(popup, task);
  if (popupScreenshotPath) {
    await popup.screenshot({ path: popupScreenshotPath, fullPage: true });
  }
  await popup.click("#start");
  await popup.waitForFunction(() => !document.querySelector("#active-panel").hidden, { timeout: 12_000 });
  const sessionId = await popup.$eval("#active-session", (element) => element.textContent.trim());
  assert.match(sessionId, /^[0-9a-f-]{36}$/i);
  await taskPage.bringToFront();
  await taskPage.waitForSelector(`#${INDICATOR_ID}`, { timeout: 10_000 });
  const tabId = await tabIdForPage(taskPage);
  assert.equal(await controlPage.evaluate((id) => chrome.action.getBadgeText({ tabId: id }), tabId), "REC");
  return sessionId;
}

async function flushPage(taskPage) {
  const tabId = await tabIdForPage(taskPage);
  const response = await controlPage.evaluate(
    async (id) => chrome.tabs.sendMessage(id, { type: "RECORDER_FLUSH" }),
    tabId
  );
  if (!response?.ok) throw new Error(response?.error || "Content-script flush failed");
}

async function stopSession(taskPage) {
  const popup = await openActionPopup(taskPage);
  await popup.waitForFunction(() => !document.querySelector("#active-panel").hidden, { timeout: 10_000 });
  const sessionId = await popup.$eval("#active-session", (element) => element.textContent.trim());
  await popup.click("#stop");
  const metadata = await poll(async () => {
    const all = await storageAll();
    const row = all[`${SESSION_PREFIX}${sessionId}`];
    return row?.status === "complete" ? row : null;
  }, { timeout: 20_000, label: "completed extension session" });
  assert.equal(metadata.capture_error, null);
  await taskPage.waitForSelector(`#${INDICATOR_ID}`, { hidden: true, timeout: 10_000 });
  const tabId = await tabIdForPage(taskPage);
  assert.equal(await controlPage.evaluate((id) => chrome.action.getBadgeText({ tabId: id }), tabId), "");
  await popup.close().catch(() => undefined);
  return { sessionId, metadata };
}

async function getPublicSession(sessionId) {
  return controlPage.evaluate(async (id) => {
    const response = await chrome.runtime.sendMessage({ type: "RECORDER_GET_SESSION", sessionId: id });
    if (!response?.ok) throw new Error(response?.error || "Could not read completed session");
    return CUIRecorderCore.sanitizeSessionForExport(response.data);
  }, sessionId);
}

async function deleteSession(sessionId) {
  await runtimeRequest({ type: "RECORDER_DELETE_SESSION", sessionId });
  const all = await storageAll();
  assert(!Object.keys(all).some((key) => key.includes(sessionId)), `Storage keys remain for ${sessionId}`);
}

async function executeTaskActions(page, actions) {
  for (const action of actions) {
    if (action.type === "wait_ms") {
      assert(Number.isInteger(action.value) && action.value >= 0 && action.value <= 15_000);
      await sleep(action.value);
      continue;
    }
    if (action.type === "focus_text_button_press") {
      const selector = await page.evaluate((text) => {
        const button = [...document.querySelectorAll("button")]
          .find((row) => row.textContent.trim() === text && !row.hidden);
        if (!button) return null;
        button.dataset.cuiTaskTarget = "true";
        return "button[data-cui-task-target='true']";
      }, action.text);
      assert(selector, `Could not find task screen button: ${action.text}`);
      await page.focus(selector);
      await page.keyboard.press(action.key);
      await page.evaluate(() => document.querySelector("[data-cui-task-target='true']")?.removeAttribute("data-cui-task-target"));
      continue;
    }
    if (action.type === "focus_selector_replace_text") {
      await page.click(action.selector);
      await page.keyboard.down("Control");
      await page.keyboard.press("KeyA");
      await page.keyboard.up("Control");
      await page.keyboard.type(String(action.value), { delay: 70 });
      await page.keyboard.press("Tab");
      continue;
    }
    if (action.type === "mouse_click_text") {
      const point = await page.evaluate(({ selector, text }) => {
        const element = [...document.querySelectorAll(selector)].find((row) => {
          const rect = row.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0 && row.textContent.trim() === text;
        });
        if (!element) return null;
        const rect = element.getBoundingClientRect();
        return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
      }, action);
      assert(point, `Could not find visible text control: ${action.text}`);
      await page.mouse.move(point.x, point.y, { steps: 10 });
      await page.mouse.click(point.x, point.y, { delay: 35 });
      continue;
    }
    if (action.type === "phet_click_visible_svg_text") {
      const point = await page.evaluate((text) => {
        const normalize = (value) => (value || "")
          .replace(/[\u202A-\u202E\u2066-\u2069]/g, "")
          .replace(/\u00a0/g, " ")
          .trim();
        const element = [...document.querySelectorAll("svg text")].find((row) => {
          const rect = row.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0 && normalize(row.textContent) === text;
        });
        if (!element) return null;
        const rect = element.getBoundingClientRect();
        return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
      }, action.text);
      assert(point, `Could not find visible PhET SVG text: ${action.text}`);
      await page.mouse.move(point.x, point.y, { steps: 10 });
      await page.mouse.click(point.x, point.y, { delay: 35 });
      continue;
    }
    if (action.type === "phet_select_balance_scales") {
      const nonePoint = await page.evaluate(() => {
        const normalize = (value) => (value || "")
          .replace(/[\u202A-\u202E\u2066-\u2069]/g, "")
          .replace(/\u00a0/g, " ")
          .trim();
        const elements = [...document.querySelectorAll("svg text")].filter((row) => {
          const rect = row.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0 && normalize(row.textContent) === "None";
        });
        const element = elements.sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y)[0];
        if (!element) return null;
        const rect = element.getBoundingClientRect();
        return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
      });
      assert(nonePoint, "Could not find PhET Tools selector");
      await page.mouse.move(nonePoint.x, nonePoint.y, { steps: 10 });
      await page.mouse.click(nonePoint.x, nonePoint.y, { delay: 35 });
      await sleep(300);
      const scalePoint = await page.evaluate(() => {
        const normalize = (value) => (value || "")
          .replace(/[\u202A-\u202E\u2066-\u2069]/g, "")
          .replace(/\u00a0/g, " ")
          .trim();
        const visible = (element) => {
          const rect = element.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0;
        };
        const none = [...document.querySelectorAll("svg text")]
          .filter((element) => visible(element) && normalize(element.textContent) === "None")
          .sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
        let group = none.at(-1)?.parentElement;
        while (group) {
          const images = [...group.querySelectorAll("image")]
            .filter(visible)
            .sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
          if (images.length >= 2 && group.getBoundingClientRect().height > 100) {
            const rect = images[0].getBoundingClientRect();
            return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
          }
          group = group.parentElement;
        }
        return null;
      });
      assert(scalePoint, "Could not find Balance Scales menu item");
      await page.mouse.move(scalePoint.x, scalePoint.y, { steps: 8 });
      await page.mouse.click(scalePoint.x, scalePoint.y, { delay: 35 });
      continue;
    }
    if (action.type === "phet_set_coefficients") {
      const points = await page.evaluate(() => {
        const normalize = (value) => (value || "")
          .replace(/[\u202A-\u202E\u2066-\u2069]/g, "")
          .trim();
        return [...document.querySelectorAll("svg text")]
          .filter((element) => {
            const rect = element.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0 && rect.y > innerHeight * 0.5 &&
              normalize(element.textContent) === "0" &&
              parseFloat(element.getAttribute("font-size") || "0") >= 30;
          })
          .map((element) => {
            const rect = element.parentElement.parentElement.getBoundingClientRect();
            return { x: rect.x + rect.width / 2, y: rect.y + Math.max(8, 0.08 * rect.height) };
          })
          .sort((a, b) => a.x - b.x);
      });
      assert.equal(points.length, 3, `Expected 3 coefficient controls, found ${points.length}`);
      assert.deepEqual(action.values.length, 3);
      for (let index = 0; index < action.values.length; index += 1) {
        for (let count = 0; count < action.values[index]; count += 1) {
          await page.mouse.move(points[index].x, points[index].y, { steps: 5 });
          await page.mouse.click(points[index].x, points[index].y, { delay: 35 });
          await sleep(100);
        }
      }
      continue;
    }
    if (action.type === "wait_for_netlogo_rule") {
      await page.waitForFunction((value) =>
        document.querySelector("label[aria-label='rule slider'] input")?.value === String(value) &&
        window.world?.observer?.getGlobal("rule") === value,
      { timeout: 15_000 }, action.value);
      continue;
    }
    if (action.type === "wait_for_netlogo_setup") {
      await page.waitForFunction(() => {
        const tick = [...document.querySelectorAll("span.netlogo-label")]
          .find((element) => /^ticks:/.test(element.textContent.trim()));
        const go = [...document.querySelectorAll("label[role='button']")]
          .find((element) => element.textContent.trim() === "go");
        return tick?.textContent.trim() === "ticks: 0" &&
          window.world?.observer?.getGlobal("row") === 64 &&
          go?.getAttribute("aria-disabled") === "false";
      }, { timeout: 15_000 });
      continue;
    }
    if (action.type === "wait_for_netlogo_rule90_complete") {
      await page.waitForFunction(() => {
        const tick = [...document.querySelectorAll("span.netlogo-label")]
          .find((element) => /^ticks:/.test(element.textContent.trim()));
        const go = [...document.querySelectorAll("label.netlogo-forever-button[role='button']")]
          .find((element) => element.textContent.trim() === "go");
        return tick?.textContent.trim() === "ticks: 128" &&
          go?.getAttribute("aria-pressed") === "false" &&
          go?.querySelector("input[type='checkbox']")?.checked === false &&
          window.world?.observer?.getGlobal("row") === -64 &&
          window.world?.ticker?._count === 128;
      }, { timeout: 60_000 });
      continue;
    }
    if (action.type === "mouse_click") {
      assert(Number.isFinite(action.x) && Number.isFinite(action.y));
      await page.mouse.move(action.x, action.y, { steps: action.move_steps || 1 });
      await page.mouse.click(action.x, action.y, { delay: 35 });
      continue;
    }
    throw new Error(`Unsupported task action type: ${action.type}`);
  }
}

function analyzeTrace(trace, task) {
  assert.equal(trace.metadata.actor_type, "agent");
  assert.equal(trace.metadata.task_id, task.task_id);
  assert.equal(trace.metadata.protocol_version, "scientific-simulator-pilot-v1");
  assert.equal(trace.metadata.executor, "puppeteer-cdp");
  assert.equal(trace.metadata.raw_key_capture, false);
  assert.equal(trace.metadata.status, "complete");
  assert.equal("tab_id" in trace.metadata, false);
  assert.equal(trace.metadata.event_count, trace.events.length);
  assert(trace.events.length >= 15, `Trace too small: ${trace.events.length} events`);

  const eventIds = new Set(trace.events.map((event) => event.event_id));
  assert.equal(eventIds.size, trace.events.length, "Event ids are not unique");
  const capturedTypes = [...new Set(trace.events.map((event) => event.type))].sort();
  const requiredTypes = [...REQUIRED_POINTER_CAPTURE_TYPES];
  if (task.allowed_inputs.includes("keyboard")) requiredTypes.push("keydown", "keyup");
  for (const type of requiredTypes) {
    assert(capturedTypes.includes(type), `Required event type was not captured: ${type}`);
  }
  const trustedInputs = trace.events.filter(
    (event) => TRUSTED_INPUT_TYPES.has(event.type) && event.is_trusted === true
  );
  assert(trustedInputs.length >= 10, `Too few trusted input events: ${trustedInputs.length}`);
  const postStopMarkerPresent = trace.events.some(
    (event) => event.pointer?.x === 23 && event.pointer?.y === 29
  );
  assert.equal(postStopMarkerPresent, false, "Post-stop negative-control pointer leaked into trace");

  const serialized = JSON.stringify(trace);
  assert(!serialized.includes("tab_id"));
  assert(!serialized.includes("chrome-extension://"));
  for (const event of trace.events) {
    if (event.url) {
      const parsed = new URL(event.url);
      assert.equal(parsed.search, "", "Trace URL contains a query string");
      assert.equal(parsed.hash, "", "Trace URL contains a fragment");
    }
  }
  return {
    event_count: trace.events.length,
    duration_ms: trace.summary.duration_ms,
    pointer_distance_px: trace.summary.pointer_distance_px,
    trusted_input_event_count: trustedInputs.length,
    captured_types: capturedTypes,
    captured_type_count: capturedTypes.length,
    post_stop_negative_control_passed: true,
    privacy_safe_export_passed: true
  };
}

async function captureDashboardEvidence(sessionId, destination) {
  const dashboardPage = await browser.newPage();
  await dashboardPage.goto(`chrome-extension://${extensionId}/dashboard/dashboard.html`, {
    waitUntil: "domcontentloaded",
    timeout: 15_000
  });
  const cardSelector = `article[data-session-id="${sessionId}"]`;
  await dashboardPage.waitForSelector(cardSelector, { timeout: 15_000 });
  await dashboardPage.screenshot({ path: destination, fullPage: true });
  await dashboardPage.close();
}

async function runOne(taskRow, repetition, simulatorDigest, provenance) {
  const { task } = taskRow;
  const repLabel = `rep-${String(repetition).padStart(2, "0")}`;
  const runDir = path.join(RUN_ROOT, task.task_id, repLabel);
  await fs.mkdir(runDir, { recursive: true });
  await resetExtensionState();

  const page = await browser.newPage();
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push({ kind: "pageerror", message: error.message }));
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    browserErrors.push({
      kind: "console.error",
      message: message.text(),
      ...sanitizeConsoleSource(message.location()?.url)
    });
  });
  await page.setViewport(task.environment.viewport);
  await logProgress(`[STEP] ${task.task_id} ${repLabel}: load simulator`);
  const response = await page.goto(task.simulator.public_url, {
    waitUntil: "domcontentloaded",
    timeout: 120_000
  });
  assert(
    [200, 304].includes(response?.status()),
    `Unexpected simulator navigation status: ${response?.status()}`
  );
  await waitForTaskReady(page, task.ready);
  await sleep(5_000);
  assert.equal(page.url(), task.simulator.public_url);

  await logProgress(`[STEP] ${task.task_id} ${repLabel}: start real extension session`);
  await page.mouse.move(17, 19);
  const popupScreenshot = repetition === 1 ? path.join(runDir, "recorder-agent-metadata.png") : null;
  const sessionId = await startSession(page, task, popupScreenshot);
  await logProgress(`[STEP] ${task.task_id} ${repLabel}: execute simulator actions`);
  const actionStartedAt = now();
  const timedAction = await runWithTimeLimit(
    () => executeTaskActions(page, task.actions),
    task.time_limit_seconds,
    `${task.task_id} ${repLabel}`
  );
  const actionFinishedAt = now();
  await logProgress(`[STEP] ${task.task_id} ${repLabel}: validate simulator state`);
  const validatorResult = await validateTaskPage(page, task, {
    simulator_url: page.url(),
    simulator_html_sha256: simulatorDigest.sha256,
    task_manifest_sha256: taskRow.sha256,
    validator_sha256: provenance.validator_sha256,
    session_id: sessionId
  });
  if (!validatorResult.passed) {
    await writeJson(path.join(runDir, "validator_debug.json"), validatorResult);
    await page.screenshot({ path: path.join(runDir, "validator-debug.png") });
  }
  assert.equal(validatorResult.passed, true, `Scientific task validator failed: ${task.task_id}`);
  await flushPage(page);
  await logProgress(`[STEP] ${task.task_id} ${repLabel}: capture and stop`);
  const recordingScreenshot = path.join(runDir, "simulator-recording.png");
  await page.screenshot({ path: recordingScreenshot });
  const stopped = await stopSession(page);
  assert.equal(stopped.sessionId, sessionId);
  const trace = await getPublicSession(sessionId);
  await logProgress(`[STEP] ${task.task_id} ${repLabel}: validate trace provenance/privacy`);
  const traceAnalysis = analyzeTrace(trace, task);
  const finalScreenshot = path.join(runDir, "simulator-final-state.png");
  await page.screenshot({ path: finalScreenshot });

  await page.mouse.move(23, 29);
  await sleep(250);
  const traceAfterPostStopControl = await getPublicSession(sessionId);
  assert.equal(traceAfterPostStopControl.metadata.event_count, trace.metadata.event_count);

  const dashboardScreenshot = repetition === 1 ? path.join(runDir, "dashboard-inspect.png") : null;
  await logProgress(`[STEP] ${task.task_id} ${repLabel}: dashboard readback and export evidence`);
  if (dashboardScreenshot) await captureDashboardEvidence(sessionId, dashboardScreenshot);
  const browserErrorReview = classifyBrowserErrors(browserErrors, task.simulator.public_url);
  assert.equal(
    browserErrorReview.unexpected.length,
    0,
    `Unexpected browser errors for ${task.task_id}: ${JSON.stringify(browserErrorReview.unexpected)}`
  );

  const tracePath = path.join(runDir, "sanitized_trace.json");
  const validatorPath = path.join(runDir, "validator_result.json");
  await writeJson(tracePath, trace);
  await writeJson(validatorPath, validatorResult);
  const artifacts = {
    sanitized_trace: {
      path: path.relative(RESULT_ROOT, tracePath).replaceAll(path.sep, "/"),
      sha256: await sha256File(tracePath)
    },
    validator_result: {
      path: path.relative(RESULT_ROOT, validatorPath).replaceAll(path.sep, "/"),
      sha256: await sha256File(validatorPath)
    },
    recording_screenshot: {
      path: path.relative(RESULT_ROOT, recordingScreenshot).replaceAll(path.sep, "/"),
      sha256: await sha256File(recordingScreenshot)
    },
    final_screenshot: {
      path: path.relative(RESULT_ROOT, finalScreenshot).replaceAll(path.sep, "/"),
      sha256: await sha256File(finalScreenshot)
    }
  };
  if (popupScreenshot) {
    artifacts.recorder_popup_screenshot = {
      path: path.relative(RESULT_ROOT, popupScreenshot).replaceAll(path.sep, "/"),
      sha256: await sha256File(popupScreenshot)
    };
  }
  if (dashboardScreenshot) {
    artifacts.dashboard_screenshot = {
      path: path.relative(RESULT_ROOT, dashboardScreenshot).replaceAll(path.sep, "/"),
      sha256: await sha256File(dashboardScreenshot)
    };
  }

  const result = {
    schema_version: "cui-simulator-run-result-v1",
    run_id: `${task.task_id}-${repLabel}`,
    task_id: task.task_id,
    task_version: task.task_version,
    repetition,
    actor_type: "agent",
    executor: "puppeteer-cdp",
    session_id: sessionId,
    simulator: {
      name: task.simulator.name,
      version: task.simulator.version,
      requested_url: task.simulator.public_url,
      resolved_url: page.url(),
      html_sha256: simulatorDigest.sha256
    },
    action_started_at: actionStartedAt,
    action_finished_at: actionFinishedAt,
    timing: {
      time_limit_seconds: task.time_limit_seconds,
      action_duration_ms: timedAction.elapsed_ms,
      action_within_time_limit: timedAction.elapsed_ms <= timedAction.limit_ms
    },
    validator_passed: validatorResult.passed,
    validator_check_count: validatorResult.checks.length,
    capture: traceAnalysis,
    causal_controls: {
      storage_empty_before_start: true,
      real_extension_indicator_seen: true,
      real_extension_badge_seen: true,
      post_stop_event_count_unchanged: true,
      dashboard_storage_readback_seen: Boolean(dashboardScreenshot)
    },
    browser_console_error_count: browserErrorReview.unexpected.length,
    browser_console_errors: browserErrorReview.unexpected,
    browser_console_allowlisted_error_count: browserErrorReview.allowlisted.length,
    browser_console_allowlisted_errors: browserErrorReview.allowlisted,
    browser_console_error_policy: browserErrorReview.policy,
    artifacts,
    status: "passed"
  };
  await writeJson(path.join(runDir, "run_result.json"), result);
  await logProgress(`[STEP] ${task.task_id} ${repLabel}: delete local extension session`);
  await deleteSession(sessionId);
  await page.close();
  return result;
}

function mean(values) {
  return Math.round((values.reduce((sum, value) => sum + value, 0) / values.length) * 100) / 100;
}

function aggregateResults(results) {
  const tasks = {};
  for (const result of results) {
    tasks[result.task_id] ||= [];
    tasks[result.task_id].push(result);
  }
  const byTask = Object.fromEntries(Object.entries(tasks).map(([taskId, rows]) => {
    const eventCounts = rows.map((row) => row.capture.event_count);
    const durations = rows.map((row) => row.capture.duration_ms);
    const pointerDistances = rows.map((row) => row.capture.pointer_distance_px);
    const trustedInputs = rows.map((row) => row.capture.trusted_input_event_count);
    return [taskId, {
      run_count: rows.length,
      passed_run_count: rows.filter((row) => row.status === "passed" && row.validator_passed).length,
      pass_rate: rows.filter((row) => row.status === "passed" && row.validator_passed).length / rows.length,
      event_count: { mean: mean(eventCounts), min: Math.min(...eventCounts), max: Math.max(...eventCounts) },
      duration_ms: { mean: mean(durations), min: Math.min(...durations), max: Math.max(...durations) },
      pointer_distance_px: { mean: mean(pointerDistances), min: Math.min(...pointerDistances), max: Math.max(...pointerDistances) },
      trusted_input_event_count: { mean: mean(trustedInputs), min: Math.min(...trustedInputs), max: Math.max(...trustedInputs) },
      captured_types: [...new Set(rows.flatMap((row) => row.capture.captured_types))].sort()
    }];
  }));
  return {
    schema_version: "cui-week3-simulator-aggregate-v1",
    generated_at: now(),
    run_count: results.length,
    task_count: Object.keys(byTask).length,
    simulator_count: new Set(results.map((row) => `${row.simulator.name}@${row.simulator.version}`)).size,
    passed_run_count: results.filter((row) => row.status === "passed" && row.validator_passed).length,
    pass_rate: results.filter((row) => row.status === "passed" && row.validator_passed).length / results.length,
    total_event_count: results.reduce((sum, row) => sum + row.capture.event_count, 0),
    unique_event_types: [...new Set(results.flatMap((row) => row.capture.captured_types))].sort(),
    by_task: byTask
  };
}

async function main() {
  await assertSafeOutputRoot(RESULT_ROOT);
  const expectedExtension = await readExtensionManifest();
  const taskRows = await readTasks();
  if (options.dryRun) {
    process.stdout.write(`${JSON.stringify({
      schema_version: "cua-simulator-e2e-plan-v1",
      output_dir: RESULT_ROOT,
      runtime_dir: RUNTIME_ROOT,
      chrome: options.chrome || "puppeteer-managed Chrome for Testing",
      repetitions_per_task: REPETITIONS,
      task_ids: taskRows.map(({ task }) => task.task_id),
      planned_run_count: taskRows.length * REPETITIONS,
      extension: { path: "extension/app", name: expectedExtension.name, version: expectedExtension.version },
      network_required: true,
      writes_performed: false
    }, null, 2)}\n`);
    return;
  }
  await fs.mkdir(RUN_ROOT, { recursive: true });
  await fs.mkdir(RUNTIME_ROOT, { recursive: true });
  const { default: puppeteer } = await import("puppeteer");
  const chromeExecutable = options.chrome || puppeteer.executablePath();
  await fs.access(chromeExecutable);

  const simulatorUrls = [...new Set(taskRows.map(({ task }) => task.simulator.public_url))].sort();
  const simulatorDigests = {};
  for (const url of simulatorUrls) {
    process.stdout.write(`[FETCH] ${url}\n`);
    simulatorDigests[url] = await fetchSimulatorDigest(url);
  }
  for (const { task } of taskRows) {
    const expected = task.simulator.expected_html_sha256;
    if (expected) {
      assert.equal(
        simulatorDigests[task.simulator.public_url].sha256,
        expected,
        `Pinned simulator HTML digest changed for ${task.task_id}`
      );
    }
  }
  const artifactSpecs = taskRows.flatMap(({ task }) => task.simulator.artifacts || []);
  const artifactDigests = {};
  for (const artifact of artifactSpecs) {
    const digest = await fetchSimulatorDigest(artifact.url);
    assert.equal(digest.sha256, artifact.expected_sha256, `Pinned ${artifact.role} digest changed`);
    artifactDigests[artifact.url] = { ...digest, role: artifact.role };
  }

  const scopedSourcePaths = [
    "extension/app",
    "research/scientific_simulators/run_extension_e2e.mjs",
    "research/scientific_simulators/runner_policy.mjs",
    "research/scientific_simulators/validator.mjs",
    "research/scientific_simulators/tasks"
  ];
  const sourceProvenance = {
    snapshot_kind: "git-base-plus-content-hashes-v1",
    git_commit: gitOutput("rev-parse", "HEAD"),
    git_status: gitOutput("status", "--short", "--", ...scopedSourcePaths),
    runner_sha256: await sha256File(fileURLToPath(import.meta.url)),
    runner_policy_sha256: await sha256File(RUNNER_POLICY_PATH),
    validator_sha256: await sha256File(VALIDATOR_PATH),
    extension: await hashDirectory(EXTENSION_PATH),
    task_manifests: Object.fromEntries(taskRows.map(({ task, sha256 }) => [task.task_id, sha256])),
    browser_executable_sha256: await sha256File(chromeExecutable)
  };

  profileRoot = await fs.mkdtemp(path.join(RUNTIME_ROOT, "profile-"));
  browser = await puppeteer.launch({
    executablePath: chromeExecutable,
    headless: false,
    userDataDir: profileRoot,
    enableExtensions: true,
    defaultViewport: { width: 1440, height: 1000 },
    protocolTimeout: 240_000,
    args: [
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-sync",
      "--disable-component-update",
      "--window-size=1440,1000"
    ]
  });
  const installResult = await browser.installExtension(EXTENSION_PATH);
  const installedExtensions = await browser.extensions();
  extensionId = installResult || [...installedExtensions.entries()]
    .find(([_id, candidate]) => candidate.name === expectedExtension.name)?.[0];
  extension = extensionId ? installedExtensions.get(extensionId) : null;
  assert(extension, `${expectedExtension.name} was not installed into the isolated Chrome profile`);

  controlPage = await browser.newPage();
  await controlPage.goto(`chrome-extension://${extensionId}/dashboard/dashboard.html`, {
    waitUntil: "domcontentloaded",
    timeout: 15_000
  });
  await controlPage.waitForSelector("#sessions");

  const extensionManifest = await controlPage.evaluate(() => chrome.runtime.getManifest());
  assert.equal(extensionManifest.name, expectedExtension.name);
  assert.equal(extensionManifest.version, expectedExtension.version);
  const browserVersion = await browser.version();
  process.stdout.write(`[BOOT] ${browserVersion}; extension ${extensionManifest.version}\n`);

  const results = [];
  for (const taskRow of taskRows) {
    for (let repetition = 1; repetition <= REPETITIONS; repetition += 1) {
      process.stdout.write(`[RUN] ${taskRow.task.task_id} ${repetition}/${REPETITIONS}\n`);
      const result = await runOne(
        taskRow,
        repetition,
        simulatorDigests[taskRow.task.simulator.public_url],
        sourceProvenance
      );
      results.push(result);
      process.stdout.write(`[PASS] ${result.run_id}: ${result.capture.event_count} events, ${result.capture.captured_type_count} types\n`);
    }
  }

  const aggregate = aggregateResults(results);
  assert.equal(aggregate.passed_run_count, aggregate.run_count);
  await writeJson(path.join(RESULT_ROOT, "aggregate_metrics.json"), aggregate);
  await writeJson(path.join(RESULT_ROOT, "validation_summary.json"), {
    schema_version: "cui-week3-validation-summary-v1",
    generated_at: now(),
    status: "passed",
    checks: {
      pinned_public_simulators: true,
      versioned_task_manifests: true,
      deterministic_independent_validator: true,
      real_extension_popup_start_stop: true,
      agent_actor_metadata: true,
      trusted_browser_inputs: true,
      privacy_safe_sanitized_exports: true,
      post_stop_negative_control: true,
      agent_only_no_human_participant_data: true
    },
    run_count: aggregate.run_count,
    passed_run_count: aggregate.passed_run_count,
    task_count: aggregate.task_count,
    simulator_count: aggregate.simulator_count,
    total_event_count: aggregate.total_event_count
  });
  await writeJson(path.join(RESULT_ROOT, "run_manifest.json"), {
    schema_version: "cui-week3-run-manifest-v1",
    started_at: results[0].action_started_at,
    finished_at: now(),
    actor_scope: "scripted-automation engineering control; no human or reasoning-agent trace",
    browser: {
      product_version: browserVersion,
      executable_path_role: "local Chrome selected by runner; absolute path intentionally omitted from public evidence",
      isolated_profile: true
    },
    extension: {
      id: extensionId,
      name: extensionManifest.name,
      version: extensionManifest.version,
      manifest_version: extensionManifest.manifest_version
    },
    repetitions_per_task: REPETITIONS,
    task_ids: taskRows.map(({ task }) => task.task_id),
    simulator_digests: simulatorDigests,
    simulator_artifact_digests: artifactDigests,
    source_provenance: sourceProvenance,
    result_files: {
      root_role: "caller-selected output directory outside research/evidence; absolute path intentionally omitted",
      aggregate_metrics: "aggregate_metrics.json",
      validation_summary: "validation_summary.json",
      runs_root: "runs"
    }
  });
}

try {
  await main();
} catch (error) {
  if (error instanceof UsageError) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 2;
  } else {
    process.stderr.write(`\nFatal simulator E2E error: ${error.stack || error.message}\n`);
    process.exitCode = 1;
  }
} finally {
  if (browser) await browser.close().catch(() => undefined);
  if (profileRoot && path.resolve(profileRoot).startsWith(`${path.resolve(RUNTIME_ROOT)}${path.sep}`)) {
    await fs.rm(profileRoot, { recursive: true, force: true }).catch(() => undefined);
  }
}
