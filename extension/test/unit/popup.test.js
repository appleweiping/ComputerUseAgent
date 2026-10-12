const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { FakeElement } = require("./helpers/fake-dom.js");

const POPUP_PATH = path.resolve(__dirname, "../../app/popup/popup.js");
const POPUP_SOURCE = fs.readFileSync(POPUP_PATH, "utf8");
const DISCLOSURE_VERSION = POPUP_SOURCE.match(/const DISCLOSURE_VERSION = "([^"]+)";/)[1];
const MANIFEST_VERSION = JSON.parse(
  fs.readFileSync(path.resolve(__dirname, "../../app/manifest.json"), "utf8")
).version;

const FIELD_DEFAULTS = {
  "actor-type": "human",
  "participant-code": "self-run-01",
  "task-id": "task-v1",
  "pair-id": "unpaired",
  "protocol-version": "task-v1",
  "actor-label": "self-run-01",
  executor: "manual",
  "executor-version": "not-recorded",
  "capture-adapter": "browser_mv3",
  "environment-scope": "active_origin",
  "source-class": "human_self"
};
const OTHER_IDS = [
  "status-chip", "idle-panel", "active-panel", "consent", "scope", "error", "start", "stop",
  "active-session", "active-actor", "active-task", "active-events", "open-dashboard",
  "persist-origin-permission", "recorder-version"
];

async function settle() {
  for (let index = 0; index < 5; index += 1) await new Promise((resolve) => setImmediate(resolve));
}

async function loadPopup(options = {}) {
  const elements = {};
  for (const [id, value] of Object.entries(FIELD_DEFAULTS)) elements[id] = new FakeElement("input", { id, value });
  for (const id of OTHER_IDS) elements[id] = new FakeElement(id === "consent" ? "input" : "div", { id });
  elements["active-panel"].hidden = true;
  const messages = [];
  const permissionRequests = [];
  const intervals = new Map();
  let keyMode = options.keyMode || "safe";
  let optionsPageOpened = 0;
  const responders = {
    RECORDER_GET_STATE: () => ({ ok: true, data: { activeSession: options.activeSession || null, sessionCount: 0 } }),
    RECORDER_START: (message) => ({
      ok: true,
      data: { session_id: "session-1", actor_type: message.input.actorType, actor_label: message.input.actorLabel, task_id: message.input.taskId, event_count: 0 }
    }),
    RECORDER_STOP: () => ({ ok: true, data: { status: "complete", event_count: 3 } }),
    ...(options.responders || {})
  };
  const chrome = {
    runtime: {
      async sendMessage(message) {
        messages.push(structuredClone(message));
        const responder = responders[message.type];
        if (!responder) return { ok: false, error: "unexpected message" };
        return responder(message);
      },
      getManifest: () => ({ version: MANIFEST_VERSION }),
      openOptionsPage() { optionsPageOpened += 1; }
    },
    tabs: {
      async query() {
        return options.tabUrl === null ? [] : [{ id: 7, url: options.tabUrl ?? "https://example.test/task?q=secret" }];
      }
    },
    permissions: {
      async request(input) {
        permissionRequests.push(structuredClone(input));
        return options.permissionGranted !== false;
      }
    }
  };
  const navigator = "navigator" in options ? options.navigator : { userAgentData: { platform: "Windows" }, platform: "Win32" };
  const context = {
    chrome,
    console,
    document: {
      getElementById: (id) => elements[id] || null,
      querySelector(selector) {
        assert.equal(selector, 'input[name="key-mode"]:checked');
        return { value: keyMode };
      }
    },
    navigator,
    window: { confirm: () => options.confirmResult !== false },
    URL,
    setInterval(callback) {
      const handle = intervals.size + 1;
      intervals.set(handle, callback);
      return handle;
    },
    clearInterval(handle) { intervals.delete(handle); }
  };
  vm.runInNewContext(POPUP_SOURCE, context, { filename: POPUP_PATH });
  await settle();
  return {
    elements,
    intervals,
    messages,
    permissionRequests,
    get optionsPageOpened() { return optionsPageOpened; },
    setKeyMode(value) { keyMode = value; },
    sent(type) { return messages.filter((message) => message.type === type); },
    async click(id) { await elements[id].click(); await settle(); }
  };
}

test("popup shows the manifest version and the capture scope on a web page", async () => {
  const popup = await loadPopup();
  assert.equal(popup.elements["recorder-version"].textContent, `v${MANIFEST_VERSION}`);
  assert.equal(popup.elements.scope.textContent, "Capture scope: https://example.test only (query strings excluded)");
  assert.equal(popup.elements.start.disabled, false);
  assert.equal(popup.elements["status-chip"].textContent, "Idle");
  assert.equal(popup.elements["active-panel"].hidden, true);
  assert.equal(popup.sent("RECORDER_GET_STATE").length, 1);
});

test("protected pages and missing tabs cannot be recorded", async () => {
  for (const tabUrl of ["chrome://settings/", null, "not a url"]) {
    const popup = await loadPopup({ tabUrl });
    assert.equal(popup.elements.start.disabled, true, String(tabUrl));
    assert.match(popup.elements.scope.textContent, /protected page cannot be recorded/);
  }
  const popup = await loadPopup({ tabUrl: "not a url" });
  popup.elements.consent.checked = true;
  await popup.click("start");
  assert.equal(popup.elements.error.textContent, "The active tab is not a recordable web page.");
  assert.equal(popup.sent("RECORDER_START").length, 0);
});

test("start requires affirmative consent", async () => {
  const popup = await loadPopup();
  await popup.click("start");
  assert.match(popup.elements.error.textContent, /Affirmative consent/);
  assert.equal(popup.sent("RECORDER_START").length, 0);
});

test("start sends manifest version, client-hint platform, and the disclosure version", async () => {
  const popup = await loadPopup();
  popup.elements.consent.checked = true;
  await popup.click("start");
  const [start] = popup.sent("RECORDER_START");
  assert.equal(start.input.tabId, 7);
  assert.equal(start.input.recorderVersion, MANIFEST_VERSION);
  assert.equal(start.input.environment, "Windows / Chromium");
  assert.equal(start.input.disclosureVersion, DISCLOSURE_VERSION);
  assert.equal(start.input.scopeOrigin, "https://example.test");
  assert.equal(start.input.includeQuery, false);
  assert.equal(start.input.rawKeyCapture, false);
  assert.equal(start.input.consentConfirmed, true);
  assert.equal(popup.elements["active-panel"].hidden, false);
  assert.equal(popup.elements["idle-panel"].hidden, true);
  assert.equal(popup.elements["status-chip"].textContent, "Recording");
  assert.equal(popup.elements["active-session"].textContent, "session-1");
  assert.equal(popup.elements["active-events"].textContent, "0");
  assert.equal(popup.intervals.size, 1);
  assert.equal(popup.permissionRequests.length, 0);
});

test("platform falls back to navigator.platform, then to a neutral label", async () => {
  for (const [navigator, expected] of [
    [{ platform: "Linux x86_64" }, "Linux x86_64 / Chromium"],
    [{ userAgentData: { platform: "  " }, platform: "MacIntel" }, "MacIntel / Chromium"],
    [{}, "unknown-platform / Chromium"]
  ]) {
    const popup = await loadPopup({ navigator });
    popup.elements.consent.checked = true;
    await popup.click("start");
    assert.equal(popup.sent("RECORDER_START")[0].input.environment, expected);
  }
});

test("raw-key mode needs the second confirmation", async () => {
  const declined = await loadPopup({ keyMode: "raw", confirmResult: false });
  declined.elements.consent.checked = true;
  await declined.click("start");
  assert.equal(declined.sent("RECORDER_START").length, 0);

  const accepted = await loadPopup({ keyMode: "raw", confirmResult: true });
  accepted.elements.consent.checked = true;
  await accepted.click("start");
  assert.equal(accepted.sent("RECORDER_START")[0].input.rawKeyCapture, true);
});

test("the optional task-origin permission is requested for the exact origin only", async () => {
  const denied = await loadPopup({ permissionGranted: false });
  denied.elements.consent.checked = true;
  denied.elements["persist-origin-permission"].checked = true;
  await denied.click("start");
  assert.deepEqual(denied.permissionRequests, [{ origins: ["https://example.test/*"] }]);
  assert.match(denied.elements.error.textContent, /Task-origin permission was not granted/);
  assert.equal(denied.sent("RECORDER_START").length, 0);

  const granted = await loadPopup({ permissionGranted: true });
  granted.elements.consent.checked = true;
  granted.elements["persist-origin-permission"].checked = true;
  await granted.click("start");
  assert.equal(granted.sent("RECORDER_START").length, 1);
});

test("background errors are shown instead of a recording state", async () => {
  const popup = await loadPopup({
    responders: { RECORDER_START: () => ({ ok: false, error: "A recording session is already active" }) }
  });
  popup.elements.consent.checked = true;
  await popup.click("start");
  assert.equal(popup.elements.error.textContent, "A recording session is already active");
  assert.equal(popup.elements["active-panel"].hidden, true);

  const broken = await loadPopup({ responders: { RECORDER_GET_STATE: () => undefined } });
  assert.equal(broken.elements.error.textContent, "Recorder request failed");
});

test("stop reports complete and incomplete sessions and resets consent", async () => {
  const activeSession = { session_id: "s1", actor_type: "agent", actor_label: "agent-run-01", task_id: "t", event_count: 9 };
  const popup = await loadPopup({ activeSession });
  assert.equal(popup.elements["active-actor"].textContent, "agent | agent-run-01");
  assert.equal(popup.elements["active-events"].textContent, "9");
  popup.elements.consent.checked = true;
  await popup.click("stop");
  assert.equal(popup.elements.consent.checked, false);
  assert.equal(popup.elements.scope.textContent, "Saved 3 events locally. Export or delete from Sessions.");
  assert.equal(popup.intervals.size, 0);

  const incomplete = await loadPopup({
    activeSession,
    responders: { RECORDER_STOP: () => ({ ok: true, data: { status: "incomplete", capture_error: "frame lost" } }) }
  });
  await incomplete.click("stop");
  assert.match(incomplete.elements.scope.textContent, /incomplete capture: frame lost/);

  const unconfirmed = await loadPopup({
    activeSession,
    responders: { RECORDER_STOP: () => ({ ok: true, data: { status: "interrupted" } }) }
  });
  await unconfirmed.click("stop");
  assert.match(unconfirmed.elements.scope.textContent, /final flush was not confirmed/);

  const failing = await loadPopup({
    activeSession,
    responders: { RECORDER_STOP: () => ({ ok: false, error: "No recording session is active" }) }
  });
  await failing.click("stop");
  assert.equal(failing.elements.error.textContent, "No recording session is active");
});

test("actor defaults follow the actor type and the dashboard link opens the options page", async () => {
  const popup = await loadPopup();
  popup.elements["actor-type"].value = "agent";
  await popup.elements["actor-type"].dispatch("change");
  assert.equal(popup.elements["participant-code"].value, "agent-run-01");
  assert.equal(popup.elements.executor.value, "unspecified-agent");
  assert.equal(popup.elements["source-class"].value, "cui_agent");
  popup.elements["actor-type"].value = "human";
  await popup.elements["actor-type"].dispatch("change");
  assert.equal(popup.elements["source-class"].value, "human_self");
  await popup.click("open-dashboard");
  assert.equal(popup.optionsPageOpened, 1);
});

test("the refresh timer polls state and clears when recording ends", async () => {
  let active = { session_id: "s1", actor_type: "human", actor_label: "self", task_id: "t", event_count: 1 };
  const popup = await loadPopup({
    activeSession: active,
    responders: { RECORDER_GET_STATE: () => ({ ok: true, data: { activeSession: active } }) }
  });
  assert.equal(popup.intervals.size, 1);
  active = null;
  await [...popup.intervals.values()][0]();
  assert.equal(popup.intervals.size, 0);
  assert.equal(popup.elements["status-chip"].textContent, "Idle");
});
