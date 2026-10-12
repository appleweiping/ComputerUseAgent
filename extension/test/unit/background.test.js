const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const core = require("../../app/src/core.js");

const BACKGROUND_PATH = path.resolve(__dirname, "../../app/src/background.js");
const EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop";
const EXTENSION_ORIGIN = `chrome-extension://${EXTENSION_ID}`;
const TASK_URL = "https://example.test/task";

// Sender shapes that Chrome's browser process attaches to runtime messages.
function extensionPageSender(page = "popup/popup.html", tab = undefined) {
  return {
    id: EXTENSION_ID,
    url: `${EXTENSION_ORIGIN}/${page}`,
    origin: EXTENSION_ORIGIN,
    ...(tab ? { tab } : {})
  };
}

function contentSender(tabId = 7, url = TASK_URL, frameId = 0) {
  return { id: EXTENSION_ID, tab: { id: tabId }, frameId, url, origin: new URL(url).origin };
}

// Tests written against the 0.4.0 harness pass `{}` (extension page) or
// `{ tab: { id } }` (content script); fill in the identity fields Chrome adds.
function completeSender(sender) {
  if (!sender || typeof sender !== "object") return sender;
  if (Object.keys(sender).length === 0) return extensionPageSender();
  if (sender.tab && !("id" in sender) && !("url" in sender) && !("origin" in sender)) {
    return contentSender(sender.tab.id);
  }
  if (sender.tab && !("id" in sender) && typeof sender.url === "string" && !("origin" in sender)) {
    return contentSender(sender.tab.id, sender.url);
  }
  return sender;
}

function createHarness() {
  const values = new Map();
  const existingTabs = new Set([7, 8, 99]);
  const sentTabMessages = [];
  const storageSetPayloads = [];
  const storageAccessLevels = [];
  const listeners = {};
  let injectedFrameIds = [0];
  const rejectedFrameIds = new Set();
  let injectionCount = 0;
  let failNextStorageSet = false;
  let failNextInjection = false;
  let blockedChunkWrite = null;
  let reportedBytesInUse = null;
  let getKeysAvailable = true;
  const storageGetCalls = [];

  const chrome = {
    scripting: {
      async executeScript() {
        injectionCount += 1;
        if (failNextInjection) {
          failNextInjection = false;
          throw new Error("Cannot access contents of the page");
        }
        return injectedFrameIds.map((frameId) => ({ frameId, result: undefined }));
      }
    },
    storage: {
      local: {
        async setAccessLevel(input) {
          storageAccessLevels.push(structuredClone(input));
        },
        async getBytesInUse() {
          if (reportedBytesInUse !== null) return reportedBytesInUse;
          return JSON.stringify(Object.fromEntries(values)).length * 2;
        },
        async get(keys) {
          storageGetCalls.push(keys === null ? null : structuredClone(keys));
          if (keys === null || keys === undefined) {
            throw new Error("storage.local.get(null) would load the whole store");
          }
          const requested = Array.isArray(keys) ? keys : [keys];
          return Object.fromEntries(
            requested.filter((key) => values.has(key)).map((key) => [key, structuredClone(values.get(key))])
          );
        },
        async set(payload) {
          if (failNextStorageSet) {
            failNextStorageSet = false;
            throw new Error("simulated storage failure");
          }
          if (blockedChunkWrite && Object.keys(payload).some((key) => key.startsWith("cui-events-"))) {
            const gate = blockedChunkWrite;
            blockedChunkWrite = null;
            gate.reached();
            await gate.wait;
          }
          storageSetPayloads.push(structuredClone(payload));
          for (const [key, value] of Object.entries(payload)) values.set(key, structuredClone(value));
        },
        async remove(keys) {
          for (const key of Array.isArray(keys) ? keys : [keys]) values.delete(key);
        }
      }
    },
    runtime: {
      id: EXTENSION_ID,
      getURL(resource = "") { return `${EXTENSION_ORIGIN}/${String(resource).replace(/^\//, "")}`; },
      onMessage: { addListener(listener) { listeners.message = listener; } },
      onInstalled: { addListener(listener) { listeners.installed = listener; } },
      onStartup: { addListener(listener) { listeners.startup = listener; } }
    },
    tabs: {
      async get(tabId) {
        if (!existingTabs.has(tabId)) throw new Error("No tab with id");
        return { id: tabId, url: "https://example.test/task" };
      },
      async sendMessage(tabId, message, options = {}) {
        sentTabMessages.push({ tabId, message, options });
        if (!existingTabs.has(tabId)) throw new Error("Receiving end does not exist");
        if (rejectedFrameIds.has(options.frameId)) {
          throw new Error("Simulated subframe listener loss");
        }
        return { ok: true };
      },
      onUpdated: { addListener(listener) { listeners.updated = listener; } },
      onRemoved: { addListener(listener) { listeners.removed = listener; } }
    }
  };

  Object.defineProperty(chrome.storage.local, "getKeys", {
    configurable: true,
    enumerable: true,
    get() {
      return getKeysAvailable ? async () => [...values.keys()] : undefined;
    }
  });

  const context = {
    URL,
    Date,
    console,
    crypto: crypto.webcrypto,
    chrome,
    importScripts(name) {
      assert.equal(name, "core.js");
      context.CUIRecorderCore = core;
    }
  };
  vm.runInNewContext(fs.readFileSync(BACKGROUND_PATH, "utf8"), context, { filename: BACKGROUND_PATH });

  async function send(message, sender = {}) {
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error(`No response for ${message?.type}`)), 1000);
      const keepChannelOpen = listeners.message(message, completeSender(sender), (response) => {
        clearTimeout(timeout);
        resolve(response);
      });
      assert.equal(keepChannelOpen, true);
    });
  }

  async function start() {
    const response = await send({
      type: "RECORDER_START",
      input: {
        tabId: 7,
        actorType: "agent",
        taskId: "test-task",
        scopeOrigin: "https://example.test",
        consentConfirmed: true
      }
    });
    assert.equal(response.ok, true, response.error);
    return response.data;
  }

  return {
    blockNextChunkWrite() {
      let release;
      let reached;
      const wait = new Promise((resolve) => { release = resolve; });
      const reachedPromise = new Promise((resolve) => { reached = resolve; });
      blockedChunkWrite = { wait, reached };
      return { reached: reachedPromise, release };
    },
    existingTabs,
    get injectionCount() { return injectionCount; },
    setInjectedFrames(frameIds) { injectedFrameIds = [...frameIds]; },
    rejectFrameMessages(frameIds) {
      rejectedFrameIds.clear();
      for (const frameId of frameIds) rejectedFrameIds.add(frameId);
    },
    failNextInjection() { failNextInjection = true; },
    failNextSet() { failNextStorageSet = true; },
    reportBytesInUse(value) { reportedBytesInUse = value; },
    disableGetKeys() { getKeysAvailable = false; },
    storageGetCalls,
    listeners,
    sentTabMessages,
    storageSetPayloads,
    storageAccessLevels,
    send,
    start,
    values
  };
}

function normalizedClick(eventId = "evt-1") {
  return core.normalizeEvent(
    {
      eventId,
      type: "click",
      epochMs: 1000,
      monotonicMs: 10,
      url: "https://example.test/task?secret=value#fragment",
      target: { tag: "button", id: "submit" },
      pointer: { x: 20, y: 30, button: 0, buttons: 0, pointerType: "mouse" }
    },
    { includeQuery: true }
  );
}

function normalizedPrintableKey(eventId = "evt-key") {
  return core.normalizeEvent(
    {
      eventId,
      type: "keydown",
      key: "q",
      epochMs: 1010,
      monotonicMs: 20,
      url: "https://example.test/task?participant=private",
      target: { tag: "input", id: "answer" }
    },
    { rawKeyCapture: true, includeQuery: true }
  );
}

test("session lifecycle stores, reads, stops on the original tab, and deletes", async () => {
  const harness = createHarness();
  const session = await harness.start();
  const batch = await harness.send(
    {
      type: "RECORDER_EVENT_BATCH",
      sessionId: session.session_id,
      events: [normalizedClick(), normalizedPrintableKey()]
    },
    { tab: { id: 7 } }
  );
  assert.equal(batch.data.accepted, 2);
  assert.equal(batch.data.rejected, 0);
  assert.equal(batch.data.eventCount, 2);

  const stopped = await harness.send({ type: "RECORDER_STOP", tabId: 99 });
  assert.equal(stopped.ok, true);
  assert.equal(stopped.data.status, "complete");
  assert.equal(harness.sentTabMessages.at(-1).tabId, 7);

  const exported = await harness.send({ type: "RECORDER_GET_SESSION", sessionId: session.session_id });
  assert.equal(exported.data.events.length, 2);
  assert.equal(exported.data.events[0].url, "https://example.test/task");
  assert.equal(exported.data.events[1].url, "https://example.test/task");
  assert.equal(exported.data.events[1].key_class, "letter");
  assert.equal(exported.data.events[1].key, null);

  const deleted = await harness.send({ type: "RECORDER_DELETE_SESSION", sessionId: session.session_id });
  assert.equal(deleted.ok, true);
  const listed = await harness.send({ type: "RECORDER_LIST_SESSIONS" });
  assert.equal(listed.data.length, 0);
});

test("subframe flush loss marks the session incomplete instead of complete", async () => {
  const harness = createHarness();
  const session = await harness.start();
  harness.setInjectedFrames([0, 12]);
  harness.rejectFrameMessages([12]);

  const stopped = await harness.send({ type: "RECORDER_STOP", tabId: 99 });
  assert.equal(stopped.ok, true);
  assert.equal(stopped.data.session_id, session.session_id);
  assert.equal(stopped.data.status, "incomplete");
  assert.match(stopped.data.capture_error, /Final flush could not be confirmed/);
});

test("background storage re-sanitizes expanded metadata and drops injected contents", async () => {
  const harness = createHarness();
  const session = await harness.start();
  const event = core.normalizeEvent({
    eventId: "evt-rich",
    type: "drop",
    epochMs: 1100,
    monotonicMs: 30,
    url: "https://example.test/task?private=value",
    target: { tag: "div", id: "drop-zone" },
    clipboard: {
      types: ["text/plain", "private clipboard content"],
      itemCount: 2,
      fileCount: 0
    },
    transfer: {
      types: ["text/html", "private transfer content"],
      itemCount: 2,
      fileCount: 1,
      effectAllowed: "copyMove",
      dropEffect: "copy"
    }
  });
  event.clipboard.raw_text = "must-not-be-stored";
  event.transfer.data = "must-not-be-stored";
  event.unexpected_page_text = "must-not-be-stored";

  const batch = await harness.send(
    {
      type: "RECORDER_EVENT_BATCH",
      sessionId: session.session_id,
      events: [event]
    },
    { tab: { id: 7 } }
  );
  assert.equal(batch.ok, true, batch.error);
  assert.equal(batch.data.accepted, 1);

  const exported = await harness.send({
    type: "RECORDER_GET_SESSION",
    sessionId: session.session_id
  });
  const stored = exported.data.events[0];
  assert.deepEqual(stored.clipboard, {
    types: ["text/plain", "other"],
    item_count: 2,
    file_count: 0
  });
  assert.deepEqual(stored.transfer, {
    types: ["text/html", "other"],
    item_count: 2,
    file_count: 1,
    effect_allowed: "copymove",
    drop_effect: "copy"
  });
  assert.equal(stored.url, "https://example.test/task");
  assert.equal(JSON.stringify(stored).includes("must-not-be-stored"), false);
});

test("event writes reject the wrong tab and recover after a storage failure", async () => {
  const harness = createHarness();
  const session = await harness.start();
  const event = normalizedClick();

  const wrongTab = await harness.send(
    { type: "RECORDER_EVENT_BATCH", sessionId: session.session_id, events: [event] },
    { tab: { id: 8 } }
  );
  assert.equal(wrongTab.ok, false);
  assert.match(wrongTab.error, /recording tab/);

  harness.failNextSet();
  const failedWrite = await harness.send(
    { type: "RECORDER_EVENT_BATCH", sessionId: session.session_id, events: [event] },
    { tab: { id: 7 } }
  );
  assert.equal(failedWrite.ok, false);
  assert.match(failedWrite.error, /storage failure/);

  const retry = await harness.send(
    { type: "RECORDER_EVENT_BATCH", sessionId: session.session_id, events: [event] },
    { tab: { id: 7 } }
  );
  assert.equal(retry.ok, true);
  assert.equal(retry.data.eventCount, 1);

  const duplicate = await harness.send(
    { type: "RECORDER_EVENT_BATCH", sessionId: session.session_id, events: [event] },
    { tab: { id: 7 } }
  );
  assert.equal(duplicate.ok, true);
  assert.equal(duplicate.data.accepted, 0);
  assert.equal(duplicate.data.rejected, 1);
  assert.equal(duplicate.data.eventCount, 1);
});

test("cross-origin navigation and tab closure clear stranded active sessions", async () => {
  const navigationHarness = createHarness();
  const navigatedSession = await navigationHarness.start();
  await navigationHarness.listeners.updated(
    7,
    { status: "complete" },
    { id: 7, url: "https://other.test/private" }
  );
  const navigationState = await navigationHarness.send({ type: "RECORDER_GET_STATE" });
  assert.equal(navigationState.data.activeSession, null);
  const navigationExport = await navigationHarness.send({
    type: "RECORDER_GET_SESSION",
    sessionId: navigatedSession.session_id
  });
  assert.equal(navigationExport.data.metadata.status, "interrupted");
  assert.match(navigationExport.data.metadata.capture_error, /scoped origin/);

  const closeHarness = createHarness();
  const closedSession = await closeHarness.start();
  closeHarness.existingTabs.delete(7);
  await closeHarness.listeners.removed(7);
  const closeExport = await closeHarness.send({ type: "RECORDER_GET_SESSION", sessionId: closedSession.session_id });
  assert.equal(closeExport.data.metadata.status, "interrupted");
  assert.match(closeExport.data.metadata.capture_error, /tab closed/);

  const revokedHarness = createHarness();
  const revokedSession = await revokedHarness.start();
  revokedHarness.failNextInjection();
  await revokedHarness.listeners.updated(7, { status: "complete" }, { id: 7 });
  const revokedExport = await revokedHarness.send({
    type: "RECORDER_GET_SESSION",
    sessionId: revokedSession.session_id
  });
  assert.equal(revokedExport.data.metadata.status, "interrupted");
  assert.match(revokedExport.data.metadata.capture_error, /activeTab grant/);
});

test("the session limit fails closed instead of orphaning old traces", async () => {
  const harness = createHarness();
  const ids = Array.from({ length: 250 }, (_value, index) => `session-${index}`);
  harness.values.set("cui-recorder-root", { activeSessionId: null, sessionIds: ids });
  const response = await harness.send({
    type: "RECORDER_START",
    input: {
      tabId: 7,
      actorType: "agent",
      taskId: "over-limit",
      scopeOrigin: "https://example.test",
      consentConfirmed: true
    }
  });
  assert.equal(response.ok, false);
  assert.match(response.error, /session limit/);
  assert.deepEqual(harness.values.get("cui-recorder-root").sessionIds, ids);
});

test("concurrent start requests create exactly one active session", async () => {
  const harness = createHarness();
  const input = {
    tabId: 7,
    actorType: "agent",
    taskId: "concurrent-start",
    scopeOrigin: "https://example.test",
    consentConfirmed: true
  };
  const responses = await Promise.all([
    harness.send({ type: "RECORDER_START", input }),
    harness.send({ type: "RECORDER_START", input })
  ]);
  assert.equal(responses.filter((response) => response.ok).length, 1);
  assert.equal(responses.filter((response) => !response.ok).length, 1);
  assert.match(responses.find((response) => !response.ok).error, /already active/);
  const listed = await harness.send({ type: "RECORDER_LIST_SESSIONS" });
  assert.equal(listed.data.length, 1);
});

test("same-origin navigation preserves one strictly ordered V2 session", async () => {
  const harness = createHarness();
  const session = await harness.start();
  const first = normalizedClick("before-navigation");
  first.document_epoch = 0;
  first.t_document_ms = 900;
  const pagehide = normalizedClick("pagehide-flush");
  pagehide.document_epoch = 0;
  pagehide.t_document_ms = 950;
  await Promise.all([
    harness.send(
      { type: "RECORDER_EVENT_BATCH", sessionId: session.session_id, events: [first, pagehide] },
      { tab: { id: 7 } }
    ),
    harness.listeners.updated(
      7,
      { status: "complete" },
      { id: 7, url: "https://example.test/next" }
    )
  ]);
  const second = normalizedClick("after-navigation");
  second.monotonic_ms = 2;
  second.document_epoch = 1;
  second.t_document_ms = 2;
  await harness.send(
    { type: "RECORDER_EVENT_BATCH", sessionId: session.session_id, events: [second] },
    { tab: { id: 7 } }
  );

  const exported = await harness.send({
    type: "RECORDER_GET_SESSION",
    sessionId: session.session_id
  });
  const events = exported.data.events;
  assert.equal(events.map((event) => event.event_seq).join(","), "1,2,3");
  assert.equal(events.map((event) => event.document_epoch).join(","), "0,0,1");
  assert.equal(events.map((event) => event.t_document_ms).join(","), "900,950,2");
  assert.ok(events[2].t_session_ms > events[1].t_session_ms);
  assert.equal(core.assertV2EventStream(events), true);
  const stale = normalizedClick("stale-old-document");
  stale.document_epoch = 0;
  stale.t_document_ms = 999;
  const staleResponse = await harness.send(
    { type: "RECORDER_EVENT_BATCH", sessionId: session.session_id, events: [stale] },
    { tab: { id: 7 } }
  );
  assert.equal(staleResponse.data.accepted, 0);
  assert.equal(staleResponse.data.rejected, 1);
});

test("step sidecar is bounded and attached by the background authority", async () => {
  const harness = createHarness();
  const session = await harness.start();
  const step = await harness.send({
    type: "RECORDER_SET_STEP",
    stepId: "step-03"
  });
  assert.equal(step.data.step_id, "step-03");
  await harness.send(
    { type: "RECORDER_EVENT_BATCH", sessionId: session.session_id, events: [normalizedClick()] },
    { tab: { id: 7 } }
  );
  const exported = await harness.send({ type: "RECORDER_GET_SESSION", sessionId: session.session_id });
  assert.equal(exported.data.events[0].step_id, "step-03");
  const prose = await harness.send({
    type: "RECORDER_SET_STEP",
    stepId: "click the secret answer button"
  });
  assert.equal(prose.ok, false);
  assert.match(prose.error, /opaque token/);
});

test("step publication fails closed when the recording frames cannot be reached", async () => {
  const harness = createHarness();
  const session = await harness.start();
  harness.failNextInjection();
  const response = await harness.send({ type: "RECORDER_SET_STEP", stepId: "step-unreachable" });
  assert.equal(response.ok, false);
  assert.match(response.error, /Cannot access contents/);
  const metadata = harness.values.get(`cui-session-${session.session_id}`);
  assert.equal(metadata.current_step_id, "step-unreachable");
  assert.equal(metadata.known_step_ids.includes("step-unreachable"), true);
});

test("step and dropped-event metadata serialize with an in-flight event write", async () => {
  const harness = createHarness();
  const session = await harness.start();
  await harness.send({ type: "RECORDER_SET_STEP", stepId: "step-race-01" });
  const firstEvent = normalizedClick("race-1");
  firstEvent.step_id = "step-race-01";
  const gate = harness.blockNextChunkWrite();
  const batch = harness.send(
    { type: "RECORDER_EVENT_BATCH", sessionId: session.session_id, events: [firstEvent] },
    { tab: { id: 7 } }
  );
  await gate.reached;
  const step = harness.send({ type: "RECORDER_SET_STEP", stepId: "step-race-02" });
  const dropped = harness.send(
    { type: "RECORDER_REPORT_DROPPED", sessionId: session.session_id, count: 2 },
    { tab: { id: 7 } }
  );
  gate.release();
  await Promise.all([batch, step, dropped]);
  const secondEvent = normalizedClick("race-2");
  secondEvent.step_id = "step-race-02";
  await harness.send(
    { type: "RECORDER_EVENT_BATCH", sessionId: session.session_id, events: [secondEvent] },
    { tab: { id: 7 } }
  );
  const exported = await harness.send({ type: "RECORDER_GET_SESSION", sessionId: session.session_id });
  assert.equal(exported.data.metadata.event_count, 2);
  assert.equal(exported.data.metadata.event_seq_last, 2);
  assert.equal(exported.data.metadata.dropped_event_count, 2);
  assert.equal(exported.data.events[0].step_id, "step-race-01");
  assert.equal(exported.data.events[1].step_id, "step-race-02");
});

test("a delayed batch keeps its event-time step and cannot invent a step id", async () => {
  const harness = createHarness();
  const session = await harness.start();
  await harness.send({ type: "RECORDER_SET_STEP", stepId: "step-01" });
  const delayed = normalizedClick("delayed-step-01");
  delayed.step_id = "step-01";
  await harness.send({ type: "RECORDER_SET_STEP", stepId: "step-02" });
  const current = normalizedClick("current-step-02");
  current.step_id = "step-02";
  const invented = normalizedClick("invented-step");
  invented.step_id = "step-never-registered";
  const stored = await harness.send(
    {
      type: "RECORDER_EVENT_BATCH",
      sessionId: session.session_id,
      events: [delayed, current, invented]
    },
    { tab: { id: 7 } }
  );
  assert.equal(stored.data.accepted, 3);
  const exported = await harness.send({ type: "RECORDER_GET_SESSION", sessionId: session.session_id });
  assert.equal(
    exported.data.events.map((event) => event.step_id ?? "<none>").join(","),
    "step-01,step-02,<none>"
  );
});

test("dynamic frame refresh remains bound to the active tab and scoped origin", async () => {
  const harness = createHarness();
  const session = await harness.start();
  const before = harness.injectionCount;
  harness.setInjectedFrames([0, 7]);
  const refreshed = await harness.send(
    { type: "RECORDER_FRAME_DISCOVERED", sessionId: session.session_id },
    { tab: { id: 7 }, url: "https://example.test/task" }
  );
  assert.equal(refreshed.ok, true, refreshed.error);
  assert.equal(refreshed.data.ignored, false);
  assert.equal(harness.injectionCount, before + 1);
  assert.equal(
    harness.sentTabMessages.some(
      (row) => row.options.frameId === 7 && row.message.type === "RECORDER_SESSION_STARTED"
    ),
    true
  );

  const beforeOutside = harness.injectionCount;
  const outside = await harness.send(
    { type: "RECORDER_FRAME_DISCOVERED", sessionId: session.session_id },
    { tab: { id: 7 }, url: "https://outside.test/frame" }
  );
  assert.equal(outside.data.ignored, true);
  assert.equal(outside.data.reason, "outside_scope");
  assert.equal(harness.injectionCount, beforeOutside);
});

test("storage is trusted-context only and browser startup finalizes an interrupted capture", async () => {
  const harness = createHarness();
  const session = await harness.start();
  assert.deepEqual(harness.storageAccessLevels.at(-1), { accessLevel: "TRUSTED_CONTEXTS" });
  harness.listeners.startup();
  await new Promise((resolve) => setTimeout(resolve, 15));
  const exported = await harness.send({ type: "RECORDER_GET_SESSION", sessionId: session.session_id });
  assert.equal(exported.data.metadata.status, "interrupted");
  assert.match(exported.data.metadata.capture_error, /browser restarted|crash/i);
  const state = await harness.send({ type: "RECORDER_GET_STATE" });
  assert.equal(state.data.activeSession, null);
  assert.deepEqual(harness.storageAccessLevels.at(-1), { accessLevel: "TRUSTED_CONTEXTS" });
});

test("event quota fails closed and records explicit rejection metrics", async () => {
  const harness = createHarness();
  const session = await harness.start();
  const key = `cui-session-${session.session_id}`;
  const metadata = harness.values.get(key);
  metadata.event_count = 100000;
  metadata.event_seq_last = 100000;
  harness.values.set(key, metadata);
  const response = await harness.send(
    { type: "RECORDER_EVENT_BATCH", sessionId: session.session_id, events: [normalizedClick()] },
    { tab: { id: 7 } }
  );
  assert.equal(response.ok, true);
  assert.equal(response.data.accepted, 0);
  assert.equal(response.data.rejected, 1);
  const stored = harness.values.get(key);
  assert.equal(stored.rejected_event_count, 1);
  assert.equal(stored.quota_rejection_count, 1);
});

test("byte quota fails closed before unbounded local storage growth", async () => {
  const harness = createHarness();
  const session = await harness.start();
  const key = `cui-session-${session.session_id}`;
  const metadata = harness.values.get(key);
  metadata.approx_event_bytes = 64 * 1024 * 1024;
  harness.values.set(key, metadata);
  const response = await harness.send(
    { type: "RECORDER_EVENT_BATCH", sessionId: session.session_id, events: [normalizedClick("byte-limit")] },
    { tab: { id: 7 } }
  );
  assert.equal(response.data.accepted, 0);
  assert.equal(response.data.rejected, 1);
  const stored = harness.values.get(key);
  assert.equal(stored.event_count, 0);
  assert.equal(stored.rejected_event_count, 1);
  assert.equal(stored.quota_rejection_count, 1);
});

test("global byte quota blocks new sessions without deleting retained data", async () => {
  const harness = createHarness();
  harness.values.set("retained-evidence", { keep: true });
  harness.reportBytesInUse(512 * 1024 * 1024);
  const response = await harness.send({
    type: "RECORDER_START",
    input: {
      tabId: 7,
      actorType: "agent",
      taskId: "test-task",
      scopeOrigin: "https://example.test",
      consentConfirmed: true
    }
  });
  assert.equal(response.ok, false);
  assert.match(response.error, /byte quota/);
  assert.deepEqual(harness.values.get("retained-evidence"), { keep: true });
});

test("content drop reports are tab-bound, bounded, and persisted", async () => {
  const harness = createHarness();
  const session = await harness.start();
  const wrongTab = await harness.send(
    { type: "RECORDER_REPORT_DROPPED", sessionId: session.session_id, count: 3 },
    { tab: { id: 8 } }
  );
  assert.equal(wrongTab.ok, false);
  assert.match(wrongTab.error, /recording tab/);
  const accepted = await harness.send(
    { type: "RECORDER_REPORT_DROPPED", sessionId: session.session_id, count: 3 },
    { tab: { id: 7 } }
  );
  assert.equal(accepted.ok, true);
  assert.equal(accepted.data.dropped_event_count, 3);
  const invalid = await harness.send(
    { type: "RECORDER_REPORT_DROPPED", sessionId: session.session_id, count: 100001 },
    { tab: { id: 7 } }
  );
  assert.equal(invalid.ok, false);
  assert.match(invalid.error, /session bound/);
});

test("finalize commits metadata and active-root transition atomically", async () => {
  const harness = createHarness();
  const session = await harness.start();
  await harness.send({ type: "RECORDER_STOP" });
  const metadataKey = `cui-session-${session.session_id}`;
  assert.equal(
    harness.storageSetPayloads.some(
      (payload) => payload[metadataKey]?.status === "complete" &&
        payload["cui-recorder-root"]?.activeSessionId === null
    ),
    true
  );
});

test("deletion enumerates and verifies orphaned chunks beyond stale metadata", async () => {
  const harness = createHarness();
  const session = await harness.start();
  await harness.send({ type: "RECORDER_STOP" });
  const orphan = `cui-events-${session.session_id}-999`;
  harness.values.set(orphan, [{ event_id: "orphan" }]);
  const deleted = await harness.send({ type: "RECORDER_DELETE_SESSION", sessionId: session.session_id });
  assert.equal(deleted.ok, true);
  assert.equal(harness.values.has(orphan), false);
  assert.equal([...harness.values.keys()].some((key) => key.includes(session.session_id)), false);
});

test("extension upgrade interrupts an active V1 session instead of mixing schemas", async () => {
  const harness = createHarness();
  const sessionId = "legacy-active-session";
  harness.values.set("cui-recorder-root", {
    activeSessionId: sessionId,
    sessionIds: [sessionId]
  });
  harness.values.set(`cui-session-${sessionId}`, {
    schema_version: "cui-trace-v1",
    session_id: sessionId,
    status: "recording",
    tab_id: 7,
    event_count: 0,
    chunk_count: 0
  });
  harness.listeners.installed({ reason: "update", previousVersion: "0.3.0" });
  await new Promise((resolve) => setTimeout(resolve, 10));
  const metadata = harness.values.get(`cui-session-${sessionId}`);
  assert.equal(metadata.schema_version, "cui-trace-v1");
  assert.equal(metadata.status, "interrupted");
  assert.match(metadata.capture_error, /start a new V2 session/);
  assert.equal(harness.values.get("cui-recorder-root").activeSessionId, null);
});

test("a content-script (tab) sender cannot use privileged session messages", async () => {
  const harness = createHarness();
  const session = await harness.start();
  const tab = contentSender(7);
  const privileged = [
    { type: "RECORDER_LIST_SESSIONS" },
    { type: "RECORDER_GET_SESSION", sessionId: session.session_id },
    { type: "RECORDER_DELETE_SESSION", sessionId: session.session_id },
    {
      type: "RECORDER_START",
      input: {
        tabId: 7,
        actorType: "agent",
        taskId: "forged",
        scopeOrigin: "https://example.test",
        consentConfirmed: true
      }
    },
    { type: "RECORDER_STOP" },
    { type: "RECORDER_SET_STEP", stepId: "forged-step" }
  ];
  for (const message of privileged) {
    const response = await harness.send(message, tab);
    assert.equal(response.ok, false, message.type);
    assert.match(response.error, new RegExp(`${message.type} is not accepted from this sender`));
  }
  const metadata = harness.values.get(`cui-session-${session.session_id}`);
  assert.equal(metadata.status, "recording");
  assert.equal(metadata.current_step_id, null);
  assert.equal(harness.values.get("cui-recorder-root").sessionIds.length, 1);
});

test("an extension page opened in a tab keeps privileged access", async () => {
  const harness = createHarness();
  const session = await harness.start();
  const dashboardTab = extensionPageSender("dashboard/dashboard.html", { id: 12 });
  const listed = await harness.send({ type: "RECORDER_LIST_SESSIONS" }, dashboardTab);
  assert.equal(listed.ok, true, listed.error);
  assert.equal(listed.data[0].session_id, session.session_id);
  const step = await harness.send({ type: "RECORDER_SET_STEP", stepId: "step-01" }, dashboardTab);
  assert.equal(step.ok, true, step.error);
  const urlOnly = { id: EXTENSION_ID, url: `${EXTENSION_ORIGIN}/dashboard/dashboard.html`, tab: { id: 12 } };
  assert.equal((await harness.send({ type: "RECORDER_LIST_SESSIONS" }, urlOnly)).ok, true);
});

test("untrusted senders and content-only messages from pages are rejected", async () => {
  const harness = createHarness();
  const session = await harness.start();
  const batch = { type: "RECORDER_EVENT_BATCH", sessionId: session.session_id, events: [normalizedClick()] };
  const untrusted = [
    { ...contentSender(7), id: "another-extension-id" },
    { id: EXTENSION_ID, url: TASK_URL, origin: "https://example.test" },
    { ...contentSender(7), origin: "null" },
    { id: EXTENSION_ID, tab: { id: -1 }, url: TASK_URL, origin: "https://example.test" },
    { id: EXTENSION_ID, tab: { id: 7 }, url: "about:blank" },
    null
  ];
  for (const sender of untrusted) {
    for (const message of [batch, { type: "RECORDER_GET_STATE" }, { type: "RECORDER_LIST_SESSIONS" }]) {
      const response = await harness.send(message, sender);
      assert.equal(response.ok, false, `${message.type} from ${JSON.stringify(sender)}`);
      assert.match(response.error, /not accepted from this sender/);
    }
  }
  for (const type of ["RECORDER_EVENT_BATCH", "RECORDER_REPORT_DROPPED", "RECORDER_FRAME_DISCOVERED"]) {
    const response = await harness.send(
      { type, sessionId: session.session_id, events: [], count: 1 },
      extensionPageSender()
    );
    assert.equal(response.ok, false, type);
    assert.match(response.error, /not accepted from this sender/);
  }
  const unknown = await harness.send({ type: "RECORDER_EVAL" }, extensionPageSender());
  assert.equal(unknown.ok, false);
  assert.match(unknown.error, /Unknown recorder message/);
  const missingType = await harness.send(undefined, extensionPageSender());
  assert.match(missingType.error, /Unknown recorder message/);
  assert.equal(harness.values.get(`cui-session-${session.session_id}`).event_count, 0);
});

test("content scripts get only a reduced, tab- and origin-bound state projection", async () => {
  const harness = createHarness();
  const session = await harness.start();
  const full = await harness.send({ type: "RECORDER_GET_STATE" }, extensionPageSender());
  assert.equal(full.data.activeSession.task_id, "test-task");
  assert.equal(full.data.sessionCount, 1);

  const reduced = await harness.send({ type: "RECORDER_GET_STATE" }, contentSender(7));
  assert.deepEqual(Object.keys(reduced.data).sort(), ["activeSession"]);
  assert.deepEqual({ ...reduced.data.activeSession }, {
    schema_version: "cui-trace-v2",
    session_id: session.session_id,
    status: "recording",
    scope_origin: "https://example.test",
    raw_key_capture: false,
    include_query: false,
    current_document_epoch: 0,
    current_step_id: null
  });
  for (const sender of [contentSender(8), contentSender(7, "https://other.test/frame", 3)]) {
    const other = await harness.send({ type: "RECORDER_GET_STATE" }, sender);
    assert.equal(other.ok, true);
    assert.equal(other.data.activeSession, null);
  }
  const started = harness.sentTabMessages.find((row) => row.message.type === "RECORDER_SESSION_STARTED");
  assert.deepEqual(
    Object.keys(started.message.session).sort(),
    Object.keys(reduced.data.activeSession).sort()
  );
  assert.equal(JSON.stringify(started.message.session).includes("test-task"), false);

  await harness.send({ type: "RECORDER_STOP" });
  const afterStop = await harness.send({ type: "RECORDER_GET_STATE" }, contentSender(7));
  assert.equal(afterStop.data.activeSession, null);
});

test("GET_SESSION and LIST still work after a rejected write (no queue poisoning)", async () => {
  const harness = createHarness();
  const session = await harness.start();
  harness.failNextSet();
  const failed = await harness.send(
    { type: "RECORDER_EVENT_BATCH", sessionId: session.session_id, events: [normalizedClick("poison")] },
    contentSender(7)
  );
  assert.equal(failed.ok, false);
  assert.match(failed.error, /storage failure/);
  const read = await harness.send({ type: "RECORDER_GET_SESSION", sessionId: session.session_id });
  assert.equal(read.ok, true, read.error);
  assert.equal(read.data.metadata.session_id, session.session_id);
  assert.equal(read.data.metadata.flush_failure_count, 1);
  const listed = await harness.send({ type: "RECORDER_LIST_SESSIONS" });
  assert.equal(listed.ok, true);
  const stopped = await harness.send({ type: "RECORDER_STOP" });
  assert.equal(stopped.ok, true, stopped.error);
});

test("deletion never reads the whole store and derives chunk keys from chunk_count", async () => {
  const harness = createHarness();
  harness.disableGetKeys();
  const session = await harness.start();
  await harness.send({ type: "RECORDER_STOP" });
  const metadataKey = `cui-session-${session.session_id}`;
  const metadata = harness.values.get(metadataKey);
  metadata.chunk_count = 3;
  harness.values.set(metadataKey, metadata);
  for (let index = 0; index < 3; index += 1) {
    harness.values.set(`cui-events-${session.session_id}-${index}`, [{ event_id: `e-${index}` }]);
  }
  harness.values.set("cui-events-unrelated-session-0", [{ event_id: "keep" }]);
  const deleted = await harness.send({ type: "RECORDER_DELETE_SESSION", sessionId: session.session_id });
  assert.equal(deleted.ok, true, deleted.error);
  assert.equal(deleted.data.removed_key_count, 4);
  assert.equal([...harness.values.keys()].some((key) => key.includes(session.session_id)), false);
  assert.deepEqual(harness.values.get("cui-events-unrelated-session-0"), [{ event_id: "keep" }]);
  assert.equal(harness.storageGetCalls.includes(null), false);
});

test("a corrupt chunk_count cannot make deletion enumerate unbounded keys", async () => {
  const harness = createHarness();
  harness.disableGetKeys();
  const session = await harness.start();
  await harness.send({ type: "RECORDER_STOP" });
  const metadataKey = `cui-session-${session.session_id}`;
  harness.values.set(metadataKey, { ...harness.values.get(metadataKey), chunk_count: 1e9 });
  const deleted = await harness.send({ type: "RECORDER_DELETE_SESSION", sessionId: session.session_id });
  assert.equal(deleted.ok, true, deleted.error);
  assert.equal(deleted.data.removed_key_count, 1 + 201);

  harness.values.set("cui-recorder-root", { activeSessionId: null, sessionIds: ["stale-id"] });
  const stale = await harness.send({ type: "RECORDER_DELETE_SESSION", sessionId: "stale-id" });
  assert.equal(stale.ok, true, stale.error);
  assert.equal(stale.data.removed_key_count, 1);
  assert.deepEqual(harness.values.get("cui-recorder-root").sessionIds, []);
});

test("session identifiers in messages must be bounded opaque tokens", async () => {
  const harness = createHarness();
  for (const sessionId of [undefined, 42, "", "../../cui-recorder-root", "x".repeat(65), { id: "x" }]) {
    for (const type of ["RECORDER_GET_SESSION", "RECORDER_DELETE_SESSION"]) {
      const response = await harness.send({ type, sessionId });
      assert.equal(response.ok, false);
      assert.match(response.error, /bounded opaque token/);
    }
    const batch = await harness.send({ type: "RECORDER_EVENT_BATCH", sessionId, events: [] }, contentSender(7));
    assert.match(batch.error, /bounded opaque token/);
  }
});

test("private-target key events are stored without key class, modifiers, or key timing", async () => {
  const harness = createHarness();
  const session = await harness.start();
  const forged = core.normalizeEvent({
    eventId: "private-key",
    type: "keyup",
    key: "Q",
    epochMs: 1020,
    monotonicMs: 25,
    url: TASK_URL,
    shiftKey: true,
    target: { tag: "input", inputType: "password", passwordLike: true },
    keyboard: { repeat: false, location: 0, isComposing: false, holdMs: 80 }
  });
  // A compromised page could try to re-add the detail the content script dropped.
  forged.key = "Q";
  forged.key_class = "letter";
  forged.keyboard = { repeat: true, location: 1, is_composing: false, hold_ms: 80 };
  forged.modifiers = { alt: false, ctrl: false, meta: false, shift: true };
  const response = await harness.send(
    { type: "RECORDER_EVENT_BATCH", sessionId: session.session_id, events: [forged] },
    contentSender(7)
  );
  assert.equal(response.data.accepted, 1);
  const exported = await harness.send({ type: "RECORDER_GET_SESSION", sessionId: session.session_id });
  const stored = exported.data.events[0];
  assert.equal(stored.key, "<redacted>");
  assert.equal(stored.key_class, null);
  assert.deepEqual(stored.keyboard, { repeat: null, location: null, is_composing: false, hold_ms: null });
  assert.deepEqual(stored.modifiers, { alt: null, ctrl: null, meta: null, shift: null });
  assert.equal(stored.target.password_like, true);
});
