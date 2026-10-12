#!/usr/bin/env node

/**
 * Own one real Chrome profile and the recorder extension UI for a formal run.
 * Stdout is JSONL protocol only; diagnostics go to stderr.
 */

import fs from "node:fs/promises";
import { createHash, randomUUID } from "node:crypto";
import path from "node:path";
import process from "node:process";
import readline from "node:readline";
import { createRequire } from "node:module";
import puppeteer from "puppeteer-core";

const require = createRequire(import.meta.url);
const EXPECTED_PUPPETEER = "25.8.0";
const PINNED_LOCALE = "en-US";
const PINNED_TIMEZONE = "UTC";
// Browser-Use normally supplies these switches when it owns Chrome.  This
// harness owns Chrome instead, so external-CDP sessions would otherwise lose
// that protection.  On Windows an occluded/minimized headed renderer can make
// Page.captureScreenshot return only after Bubus has cancelled its 15-second
// ScreenshotEvent handler.  Keep the minimal capture-stability contract here
// and attest the effective Chrome command line before exposing the CDP URL.
const CAPTURE_STABILITY_SWITCHES = Object.freeze([
  "--disable-background-timer-throttling",
  "--disable-backgrounding-occluded-windows",
  "--disable-renderer-backgrounding",
]);
const CAPTURE_STABILITY_DISABLED_FEATURE = "CalculateNativeWinOcclusion";

function parseArgs(argv) {
  const parsed = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith("--") || value == null) throw new Error(`Invalid argument near ${key || "end"}`);
    parsed[key.slice(2)] = value;
  }
  return parsed;
}

function send(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

function fail(message) {
  process.stderr.write(`${message}\n`);
}

async function establishSingleTaskPage(browser) {
  // A normal Puppeteer launch already supplies one about:blank page. Reusing
  // it avoids the second hidden episode/page that an unconditional newPage()
  // created in the first compatibility probe. Multiple regular pages in a
  // fresh profile are unexpected and fail closed rather than being deleted.
  const initialPages = await browser.pages();
  if (initialPages.length > 1) {
    throw new Error(`Fresh Chrome exposed ${initialPages.length} regular pages; expected at most one`);
  }
  const page = initialPages[0] || await browser.newPage();
  const establishedPages = await browser.pages();
  if (establishedPages.length !== 1 || establishedPages[0] !== page) {
    throw new Error(`Could not establish exactly one owned task page; observed ${establishedPages.length}`);
  }
  return page;
}

async function packageVersion(name) {
  const packageJson = require.resolve(`${name}/package.json`);
  return JSON.parse(await fs.readFile(packageJson, "utf8")).version;
}

async function verifyCaptureStabilityCommandLine(browser) {
  const session = await browser.target().createCDPSession();
  let commandLine;
  try {
    commandLine = await session.send("Browser.getBrowserCommandLine");
  } finally {
    await session.detach().catch(() => undefined);
  }
  const effectiveArguments = commandLine?.arguments;
  if (!Array.isArray(effectiveArguments) || !effectiveArguments.every((value) => typeof value === "string")) {
    throw new Error("Chrome did not expose an attestable runtime command line");
  }
  const missingSwitches = CAPTURE_STABILITY_SWITCHES.filter(
    // Puppeteer 24 supplies these defaults and our explicit contract repeats
    // them intentionally; Chrome exposes both copies in argv. Presence, not a
    // single occurrence, is the effective invariant for boolean switches.
    (required) => !effectiveArguments.includes(required),
  );
  const disabledFeatureArguments = effectiveArguments.filter(
    (value) => value.startsWith("--disable-features="),
  );
  const disabledFeatures = new Set(
    (disabledFeatureArguments[0] || "")
      .slice("--disable-features=".length)
      .split(",")
      .filter(Boolean),
  );
  if (
    missingSwitches.length
    || disabledFeatureArguments.length !== 1
    || !disabledFeatures.has(CAPTURE_STABILITY_DISABLED_FEATURE)
  ) {
    throw new Error(
      "Chrome capture-stability command-line attestation failed "
      + `(missing_switches=${missingSwitches.length}, disable_features_arguments=${disabledFeatureArguments.length}, native_occlusion_disabled=${disabledFeatures.has(CAPTURE_STABILITY_DISABLED_FEATURE)})`,
    );
  }
  return {
    capture_stability_verified: true,
    capture_stability_switch_count: CAPTURE_STABILITY_SWITCHES.length,
    native_window_occlusion_disabled: true,
  };
}

async function pinAndVerifyTaskEnvironment(page) {
  const session = await page.createCDPSession();
  await session.send("Emulation.setLocaleOverride", { locale: PINNED_LOCALE });
  await session.send("Emulation.setTimezoneOverride", { timezoneId: PINNED_TIMEZONE });
  const observed = await page.evaluate(() => ({
    locale: navigator.language,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  }));
  if (observed.locale !== PINNED_LOCALE || observed.timezone !== PINNED_TIMEZONE) {
    await session.detach().catch(() => undefined);
    throw new Error(
      `Pinned browser environment mismatch: locale=${observed.locale} timezone=${observed.timezone}`,
    );
  }
  return { session, observed };
}

async function poll(operation, { timeout = 10_000, interval = 100, label = "condition" } = {}) {
  const deadline = Date.now() + timeout;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const result = await operation();
      if (result) return result;
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, interval));
  }
  throw new Error(`${label} did not resolve${lastError ? `: ${lastError.message}` : ""}`);
}

async function selfTest() {
  const actual = await packageVersion("puppeteer-core");
  return {
    schema_version: "cui-week1-browser-host-self-test-v1",
    ready: actual === EXPECTED_PUPPETEER,
    checks: [{
      id: "puppeteer-core",
      status: actual === EXPECTED_PUPPETEER ? "pass" : "fail",
      detail: `installed=${actual} expected=${EXPECTED_PUPPETEER}`,
    }],
    writes_performed: false,
  };
}

if (process.argv.includes("--self-test")) {
  const result = await selfTest();
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  process.exitCode = result.ready ? 0 : 2;
} else {
  const args = parseArgs(process.argv.slice(2));
  const required = ["chrome", "extension", "profile", "attempt-id", "width", "height"];
  for (const key of required) {
    if (!args[key]) throw new Error(`Missing --${key}`);
  }
  const width = Number(args.width);
  const height = Number(args.height);
  if (!Number.isInteger(width) || !Number.isInteger(height)) throw new Error("Viewport must be integer dimensions");
  const actualPuppeteer = await packageVersion("puppeteer-core");
  if (actualPuppeteer !== EXPECTED_PUPPETEER) {
    throw new Error(`puppeteer-core ${actualPuppeteer} is not pinned ${EXPECTED_PUPPETEER}`);
  }

  let browser;
  let extension;
  let extensionId;
  let taskPage;
  let taskEnvironmentSession;
  let activeSessionId;
  let episodeGuard;

  function recordEpisodeViolation(code) {
    if (!episodeGuard || episodeGuard.violations.some((row) => row.code === code)) return;
    episodeGuard.violations.push({ code, observed_after_arm_ms: Date.now() - episodeGuard.armedAtMs });
  }

  async function captureEpisodeState(documentToken) {
    const frameTree = await taskEnvironmentSession.send("Page.getFrameTree");
    const targetResponse = await taskEnvironmentSession.send("Target.getTargetInfo");
    const document = await taskPage.evaluate((token) => {
      if (token != null && !("__cuiEpisodeGuardToken" in globalThis)) {
        Object.defineProperty(globalThis, "__cuiEpisodeGuardToken", {
          value: token,
          configurable: false,
          enumerable: false,
          writable: false,
        });
      }
      return {
        document_token: globalThis.__cuiEpisodeGuardToken || null,
        time_origin_ms: performance.timeOrigin,
        episode_started_at_ms: Number(globalThis.core?.ept0),
        wob_episode_id: Number(globalThis.WOB_EPISODE_ID),
        viewport: { width: innerWidth, height: innerHeight },
        // CDP reports deviceScaleFactor=1 on Windows as
        // 1.0000000149011612 due to float32 transport. Normalize only this
        // representation noise; the exact guard below still rejects any
        // materially different display scale.
        device_pixel_ratio: Math.round(devicePixelRatio * 1e6) / 1e6,
        locale: navigator.language,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      };
    }, documentToken);
    const mainFrame = frameTree.frameTree?.frame || {};
    const targetInfo = targetResponse.targetInfo || {};
    return {
      target_id: String(targetInfo.targetId || ""),
      target_url: String(targetInfo.url || taskPage.url()),
      main_frame_id: String(mainFrame.id || ""),
      loader_id: String(mainFrame.loaderId || ""),
      ...document,
    };
  }

  function publicEpisodeState(state) {
    return {
      target_id_verified: Boolean(state.target_id),
      url: state.target_url,
      loader_id_sha256: createHash("sha256").update(state.loader_id).digest("hex"),
      document_token_sha256: createHash("sha256").update(state.document_token).digest("hex"),
      time_origin_ms: state.time_origin_ms,
      episode_started_at_ms: state.episode_started_at_ms,
      wob_episode_id: state.wob_episode_id,
      viewport: state.viewport,
      device_pixel_ratio: state.device_pixel_ratio,
      locale: state.locale,
      timezone: state.timezone,
    };
  }

  async function armEpisodeGuard(payload) {
    if (!activeSessionId) throw new Error("Recorder must be active before arming the episode guard");
    if (episodeGuard) throw new Error("Episode guard is already armed");
    await closeActionPopups();
    const pages = await browser.pages();
    if (pages.length !== 1 || pages[0] !== taskPage) {
      throw new Error(`Episode guard expected exactly one task page, observed ${pages.length}`);
    }
    await taskEnvironmentSession.send("Page.enable");
    await taskEnvironmentSession.send("Runtime.enable");
    const token = randomUUID();
    const baseline = await captureEpisodeState(token);
    const baselineMismatches = [
      [baseline.target_id !== payload.target_id, "target_id"],
      [baseline.target_url !== payload.url, "target_url"],
      [baseline.viewport.width !== width || baseline.viewport.height !== height, "viewport"],
      [baseline.device_pixel_ratio !== 1, "device_pixel_ratio"],
      [baseline.locale !== PINNED_LOCALE, "locale"],
      [baseline.timezone !== PINNED_TIMEZONE, "timezone"],
      [!baseline.loader_id, "loader_id"],
      [!baseline.main_frame_id, "main_frame_id"],
      [baseline.document_token !== token, "document_token"],
      [!Number.isFinite(baseline.time_origin_ms), "time_origin"],
      [!Number.isFinite(baseline.episode_started_at_ms), "miniwob_episode_start"],
      [!Number.isFinite(baseline.wob_episode_id), "miniwob_episode_counter"],
    ].filter(([failed]) => failed).map(([_failed, name]) => name);
    if (baselineMismatches.length) {
      throw new Error(
        `Post-setup episode/environment identity mismatch: ${baselineMismatches.join(",")}`,
      );
    }
    const taskTarget = taskPage.target();
    episodeGuard = {
      armedAtMs: Date.now(),
      baseline,
      taskTarget,
      token,
      violations: [],
      assertionCount: 0,
      listeners: {},
    };
    episodeGuard.listeners.targetcreated = (target) => {
      if (target.type() === "page" && target !== taskTarget) recordEpisodeViolation("regular_page_target_created");
    };
    episodeGuard.listeners.targetdestroyed = (target) => {
      if (target.type() === "page") recordEpisodeViolation(
        target === taskTarget ? "official_page_target_destroyed" : "regular_page_target_destroyed",
      );
    };
    episodeGuard.listeners.targetchanged = (target) => {
      if (target === taskTarget && target.url() !== baseline.target_url) {
        recordEpisodeViolation("official_page_target_url_changed");
      }
    };
    episodeGuard.listeners.frameNavigated = (event) => {
      if (event.frame?.id === baseline.main_frame_id && (
        event.frame.loaderId !== baseline.loader_id || event.frame.url !== baseline.target_url
      )) recordEpisodeViolation("main_frame_navigated_or_reloaded");
    };
    episodeGuard.listeners.navigatedWithinDocument = (event) => {
      if (event.frameId === baseline.main_frame_id) recordEpisodeViolation("main_frame_same_document_navigation");
    };
    episodeGuard.listeners.frameStartedLoading = (event) => {
      if (event.frameId === baseline.main_frame_id) recordEpisodeViolation("main_frame_started_loading");
    };
    episodeGuard.listeners.executionContextsCleared = () => recordEpisodeViolation("document_execution_context_cleared");
    browser.on("targetcreated", episodeGuard.listeners.targetcreated);
    browser.on("targetdestroyed", episodeGuard.listeners.targetdestroyed);
    browser.on("targetchanged", episodeGuard.listeners.targetchanged);
    taskEnvironmentSession.on("Page.frameNavigated", episodeGuard.listeners.frameNavigated);
    taskEnvironmentSession.on("Page.navigatedWithinDocument", episodeGuard.listeners.navigatedWithinDocument);
    taskEnvironmentSession.on("Page.frameStartedLoading", episodeGuard.listeners.frameStartedLoading);
    taskEnvironmentSession.on("Runtime.executionContextsCleared", episodeGuard.listeners.executionContextsCleared);
    return {
      armed: true,
      sticky_lifecycle_monitoring: true,
      baseline: publicEpisodeState(baseline),
      violations: [],
    };
  }

  async function assertEpisodeGuard() {
    if (!episodeGuard) throw new Error("Episode guard is not armed");
    const pages = await browser.pages();
    if (pages.length !== 1 || pages[0] !== taskPage) recordEpisodeViolation("regular_page_count_changed");
    const current = await captureEpisodeState(null);
    const baseline = episodeGuard.baseline;
    for (const [key, code] of [
      ["target_id", "official_target_id_changed"],
      ["target_url", "official_target_url_changed"],
      ["main_frame_id", "main_frame_id_changed"],
      ["loader_id", "main_frame_loader_changed"],
      ["document_token", "document_token_changed"],
      ["time_origin_ms", "document_time_origin_changed"],
      ["episode_started_at_ms", "miniwob_episode_start_changed"],
      ["device_pixel_ratio", "device_pixel_ratio_changed"],
      ["locale", "locale_changed"],
      ["timezone", "timezone_changed"],
    ]) {
      if (current[key] !== baseline[key]) recordEpisodeViolation(code);
    }
    if (
      current.viewport.width !== baseline.viewport.width
      || current.viewport.height !== baseline.viewport.height
    ) recordEpisodeViolation("viewport_changed");
    if (
      !Number.isFinite(current.wob_episode_id)
      || current.wob_episode_id < baseline.wob_episode_id
      || current.wob_episode_id > baseline.wob_episode_id + 1
    ) recordEpisodeViolation("miniwob_episode_counter_invalid");
    if (episodeGuard.violations.length) {
      throw new Error(
        `Sticky episode guard violations: ${episodeGuard.violations.map((row) => row.code).join(",")}`,
      );
    }
    episodeGuard.assertionCount += 1;
    return {
      stable: true,
      sticky_lifecycle_monitoring: true,
      current: publicEpisodeState(current),
      violation_count: 0,
      attestation_sequence: episodeGuard.assertionCount,
    };
  }

  async function disarmEpisodeGuard() {
    if (!episodeGuard) return { disarmed: false };
    const listeners = episodeGuard.listeners;
    browser.off("targetcreated", listeners.targetcreated);
    browser.off("targetdestroyed", listeners.targetdestroyed);
    browser.off("targetchanged", listeners.targetchanged);
    taskEnvironmentSession.off("Page.frameNavigated", listeners.frameNavigated);
    taskEnvironmentSession.off("Page.navigatedWithinDocument", listeners.navigatedWithinDocument);
    taskEnvironmentSession.off("Page.frameStartedLoading", listeners.frameStartedLoading);
    taskEnvironmentSession.off("Runtime.executionContextsCleared", listeners.executionContextsCleared);
    const violationCount = episodeGuard.violations.length;
    const finalAttestationSequence = episodeGuard.assertionCount;
    episodeGuard = undefined;
    return {
      disarmed: true,
      violation_count: violationCount,
      final_attestation_sequence: finalAttestationSequence,
    };
  }

  async function closeActionPopups() {
    if (!extension) return;
    for (const page of await extension.pages()) {
      if (!page.isClosed() && page.url().includes("/popup/popup.html")) {
        await page.close().catch(() => undefined);
      }
    }
  }

  async function pinPostSetupEnvironment() {
    if (activeSessionId) throw new Error("Post-setup environment must be pinned before recording starts");
    if (episodeGuard) throw new Error("Post-setup environment cannot change while the episode guard is armed");
    await taskEnvironmentSession.send("Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await taskEnvironmentSession.send("Emulation.setLocaleOverride", { locale: PINNED_LOCALE });
    await taskEnvironmentSession.send("Emulation.setTimezoneOverride", { timezoneId: PINNED_TIMEZONE });
    const observed = await taskPage.evaluate(() => ({
      viewport: { width: innerWidth, height: innerHeight },
      device_pixel_ratio: Math.round(devicePixelRatio * 1e6) / 1e6,
      locale: navigator.language,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    }));
    if (
      observed.viewport.width !== width
      || observed.viewport.height !== height
      || observed.device_pixel_ratio !== 1
      || observed.locale !== PINNED_LOCALE
      || observed.timezone !== PINNED_TIMEZONE
    ) throw new Error(
      `Could not enforce post-setup environment: ${JSON.stringify(observed)}`,
    );
    return { pinned: true, ...observed };
  }

  async function openActionPopup() {
    await closeActionPopups();
    await taskPage.bringToFront();
    await extension.triggerAction(taskPage);
    const popup = await poll(async () => {
      const pages = await extension.pages();
      return pages.find((page) => !page.isClosed() && page.url().includes("/popup/popup.html"));
    }, { timeout: 10_000, label: "recorder action popup" });
    await popup.waitForSelector("#status-chip", { timeout: 8_000 });
    return popup;
  }

  async function setField(page, selector, value) {
    await page.$eval(selector, (element, nextValue) => {
      element.value = nextValue;
      element.dispatchEvent(new Event("input", { bubbles: true }));
      element.dispatchEvent(new Event("change", { bubbles: true }));
    }, value);
  }

  async function startRecorder(metadata) {
    if (activeSessionId) throw new Error("Recorder session is already active");
    const popup = await openActionPopup();
    await popup.select("#actor-type", "agent");
    await setField(popup, "#participant-code", metadata.participantCode);
    await setField(popup, "#task-id", metadata.taskId);
    await setField(popup, "#pair-id", metadata.pairId || "");
    await setField(popup, "#protocol-version", metadata.protocolVersion);
    await setField(popup, "#actor-label", metadata.actorLabel);
    await setField(popup, "#executor", metadata.executor);
    await setField(popup, "#executor-version", metadata.executorVersion);
    await setField(popup, "#capture-adapter", metadata.captureAdapter);
    await setField(popup, "#environment-scope", metadata.environmentScope);
    await popup.select("#source-class", metadata.sourceClass);
    await popup.$eval('input[name="key-mode"][value="safe"]', (element) => {
      element.checked = true;
      element.dispatchEvent(new Event("change", { bubbles: true }));
    });
    const consent = await popup.$eval("#consent", (element) => element.checked);
    if (!consent) await popup.click("#consent");
    await popup.click("#start");
    await popup.waitForFunction(() => !document.querySelector("#active-panel").hidden, { timeout: 12_000 });
    activeSessionId = await popup.$eval("#active-session", (element) => element.textContent.trim());
    await popup.close().catch(() => undefined);
    await taskPage.bringToFront();
    await taskPage.waitForSelector("#cui-trace-recorder-indicator", { timeout: 10_000 });
    return { session_id: activeSessionId };
  }

  async function stopRecorder(payload) {
    if (!activeSessionId) throw new Error("No recorder session is active");
    if (episodeGuard) throw new Error("Disarm the episode guard immediately after validation before recorder UI operations");
    const sessionId = activeSessionId;
    const popup = await openActionPopup();
    await popup.waitForFunction(() => !document.querySelector("#active-panel").hidden, { timeout: 10_000 });
    await popup.click("#stop");
    await popup.waitForFunction(() => !document.querySelector("#idle-panel").hidden, { timeout: 15_000 });
    await popup.close().catch(() => undefined);

    const control = await browser.newPage();
    await control.goto(`chrome-extension://${extensionId}/dashboard/dashboard.html`, {
      waitUntil: "domcontentloaded",
      timeout: 15_000,
    });
    const session = await control.evaluate(async (id) => {
      const response = await chrome.runtime.sendMessage({ type: "RECORDER_GET_SESSION", sessionId: id });
      if (!response?.ok) throw new Error(response?.error || "Could not read recorder session");
      if (!globalThis.CUIRecorderCore?.sanitizeSessionForExport) {
        throw new Error("Recorder export sanitizer is unavailable");
      }
      return CUIRecorderCore.sanitizeSessionForExport(response.data);
    }, sessionId);
    await control.close();
    const tracePath = path.resolve(payload.trace_path);
    await fs.mkdir(path.dirname(tracePath), { recursive: true });
    await fs.writeFile(tracePath, `${JSON.stringify(session, null, 2)}\n`, "utf8");
    activeSessionId = undefined;
    return {
      session_id: session.metadata.session_id,
      status: session.metadata.status,
      event_count: session.metadata.event_count,
    };
  }

  async function setRecorderStep(payload) {
    if (!activeSessionId) throw new Error("No recorder session is active");
    const stepId = payload.step_id == null ? null : String(payload.step_id);
    if (stepId !== null && !/^step-[0-9]{3}$/.test(stepId)) {
      throw new Error("step_id is not a bounded opaque framework-step token");
    }
    const worker = await poll(async () => {
      const workers = await extension.workers();
      if (workers.length > 1) throw new Error(`Recorder exposed ${workers.length} service workers`);
      return workers[0];
    }, { timeout: 10_000, label: "recorder service worker" });
    const response = await worker.evaluate(async (nextStepId) => {
      // A service worker cannot send RECORDER_SET_STEP to its own onMessage
      // listener: runtime.sendMessage excludes the sender context. Execute
      // the listener's exact serialized write path inside that worker instead.
      if (
        typeof enqueueLifecycle !== "function"
        || typeof enqueueWrite !== "function"
        || typeof setActiveStep !== "function"
      ) throw new Error("Recorder background step API is unavailable");
      return enqueueLifecycle(() => enqueueWrite(() => setActiveStep(nextStepId)));
    }, stepId);
    if (!response || response.step_id !== stepId) {
      throw new Error("Recorder did not acknowledge the requested step token");
    }
    return { step_id: response.step_id };
  }

  async function dispatch(request) {
    switch (request.operation) {
      case "start_recorder":
        return startRecorder(request.payload || {});
      case "set_recorder_step":
        return setRecorderStep(request.payload || {});
      case "pin_post_setup_environment":
        return pinPostSetupEnvironment();
      case "stop_recorder":
        return stopRecorder(request.payload || {});
      case "arm_episode_guard":
        return armEpisodeGuard(request.payload || {});
      case "assert_episode_guard":
        return assertEpisodeGuard();
      case "disarm_episode_guard":
        return disarmEpisodeGuard();
      case "shutdown":
        await disarmEpisodeGuard();
        if (activeSessionId) {
          try {
            await stopRecorder({ trace_path: path.join(args.profile, "interrupted_trace.json") });
          } catch (error) {
            fail(`Recorder cleanup failed: ${error.message}`);
          }
        }
        await browser.close();
        return { shutting_down: true };
      default:
        throw new Error(`Unknown operation ${request.operation}`);
    }
  }

  try {
    browser = await puppeteer.launch({
      executablePath: path.resolve(args.chrome),
      headless: false,
      userDataDir: path.resolve(args.profile),
      enableExtensions: true,
      defaultViewport: { width, height, deviceScaleFactor: 1 },
      protocolTimeout: 60_000,
      env: { ...process.env, LANG: "en_US.UTF-8", LC_ALL: "en_US.UTF-8", TZ: PINNED_TIMEZONE },
      args: [
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-background-networking",
        "--disable-component-update",
        ...CAPTURE_STABILITY_SWITCHES,
        `--disable-features=${CAPTURE_STABILITY_DISABLED_FEATURE}`,
        "--high-dpi-support=1",
        "--force-device-scale-factor=1",
        `--lang=${PINNED_LOCALE}`,
        `--window-size=${width},${height}`,
      ],
    });
    const captureStabilityGuard = await verifyCaptureStabilityCommandLine(browser);
    const installResult = await browser.installExtension(path.resolve(args.extension));
    const installed = await browser.extensions();
    extensionId = installResult || [...installed.entries()]
      .find(([_id, candidate]) => candidate.name === "CUI Trace Recorder")?.[0];
    extension = extensionId ? installed.get(extensionId) : undefined;
    if (!extension) throw new Error("CUI Trace Recorder did not install in the owned Chrome profile");
    taskPage = await establishSingleTaskPage(browser);
    await taskPage.setViewport({ width, height, deviceScaleFactor: 1 });
    const pinnedEnvironment = await pinAndVerifyTaskEnvironment(taskPage);
    taskEnvironmentSession = pinnedEnvironment.session;
    const marker = `about:blank#cui-${args["attempt-id"]}`;
    await taskPage.goto(marker);
    await taskPage.bringToFront();
    const observedEnvironment = await taskPage.evaluate(() => ({
      locale: navigator.language,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    }));
    if (
      observedEnvironment.locale !== PINNED_LOCALE
      || observedEnvironment.timezone !== PINNED_TIMEZONE
    ) {
      throw new Error(
        `Pinned browser environment did not survive navigation: locale=${observedEnvironment.locale} timezone=${observedEnvironment.timezone}`,
      );
    }
    send({
      type: "ready",
      ok: true,
      cdp_url: browser.wsEndpoint(),
      browser_version: await browser.version(),
      extension_id: extensionId,
      task_marker: marker,
      locale: observedEnvironment.locale,
      timezone: observedEnvironment.timezone,
      environment_guard: {
        requested_locale: PINNED_LOCALE,
        requested_timezone: PINNED_TIMEZONE,
        observed_locale: observedEnvironment.locale,
        observed_timezone: observedEnvironment.timezone,
        ...captureStabilityGuard,
        verified: true,
      },
    });

    const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
    for await (const line of lines) {
      if (!line.trim()) continue;
      let request;
      try {
        request = JSON.parse(line);
        const data = await dispatch(request);
        send({ id: request.id, ok: true, ...data });
        if (request.operation === "shutdown") break;
      } catch (error) {
        send({ id: request?.id, ok: false, error: `${error.name}: ${error.message}` });
      }
    }
  } catch (error) {
    fail(`${error.stack || error}`);
    if (browser) await browser.close().catch(() => undefined);
    process.exitCode = 2;
  }
}
