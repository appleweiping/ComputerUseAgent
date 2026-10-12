const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const core = require("../../app/src/core.js");

const CONTENT_PATH = path.resolve(__dirname, "../../app/src/content.js");

function createHarness(options = {}) {
  const documentListeners = new Map();
  const windowListeners = new Map();
  const sentMessages = [];
  let runtimeListener = null;
  let uuidIndex = 0;
  let monotonicNow = 1000;
  let mutationCallback = null;
  // Every attached shadow root, open or closed, keyed by host: the fake
  // chrome.dom.openOrClosedShadowRoot reads it, page code cannot.
  const hostedRoots = new Map();

  class ShadowRoot {
    constructor(host, mode) {
      this.host = host;
      this.mode = mode;
      this.activeElement = null;
    }
  }

  class Element {
    constructor(tagName = "div", options = {}) {
      this.tagName = String(tagName).toUpperCase();
      this.id = options.id || "";
      this.classList = options.classes || [];
      this.attributes = { ...(options.attributes || {}) };
      this.style = {};
      this.scrollLeft = options.scrollLeft || 0;
      this.scrollTop = options.scrollTop || 0;
      this.form = options.form || null;
      this.privateTarget = Boolean(options.privateTarget);
      this.parentElement = options.parent || null;
      this.rootNode = options.root || null;
      this.assignedSlot = options.slot || null;
      this.shadowRoot = null;
      this.listeners = new Map();
    }

    appendChild() {}
    remove() {}
    addEventListener(type, listener) {
      const rows = this.listeners.get(type) || [];
      rows.push(listener);
      this.listeners.set(type, rows);
    }
    dispatch(type) {
      for (const listener of this.listeners.get(type) || []) listener({ type, target: this });
    }
    querySelectorAll() { return []; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] || null; }
    attachShadow({ mode }) {
      const root = new ShadowRoot(this, mode);
      hostedRoots.set(this, root);
      if (mode === "open") this.shadowRoot = root;
      return root;
    }
    getRootNode() { return this.rootNode || document; }
    matches(selector) {
      return (
        selector === "[data-recorder-private]" &&
        (this.privateTarget || Object.hasOwn(this.attributes, "data-recorder-private"))
      );
    }
  }

  class HTMLInputElement extends Element {
    constructor(type = "text", options = {}) {
      super("input", options);
      this.type = type;
    }
  }

  function addListener(registry, type, listener) {
    const existing = registry.get(type) || [];
    existing.push(listener);
    registry.set(type, existing);
  }

  const documentElement = new Element("html");
  const document = {
    activeElement: null,
    documentElement,
    fullscreenElement: null,
    visibilityState: "visible",
    addEventListener(type, listener) { addListener(documentListeners, type, listener); },
    createElement(tagName) { return new Element(tagName); },
    getElementById() { return null; }
  };
  const window = {
    addEventListener(type, listener) { addListener(windowListeners, type, listener); },
    devicePixelRatio: 1.5,
    innerHeight: 720,
    innerWidth: 1280,
    location: {
      href: "https://example.test/task?participant=private#answer",
      origin: "https://example.test"
    },
    scrollX: 20,
    scrollY: 40
  };
  const chrome = {
    ...(options.withoutChromeDom
      ? {}
      : {
          dom: {
            openOrClosedShadowRoot(element) {
              if (options.chromeDomThrows) throw new TypeError("not an HTMLElement");
              return hostedRoots.get(element) || null;
            }
          }
        }),
    runtime: {
      onMessage: {
        addListener(listener) { runtimeListener = listener; }
      },
      async sendMessage(message) {
        if (message.type === "RECORDER_GET_STATE") {
          return { ok: true, data: { activeSession: options.restoredSession || null } };
        }
        sentMessages.push(message);
        if (message.type === "RECORDER_EVENT_BATCH" && options.failEventBatch) {
          return { ok: false, error: "simulated quota failure" };
        }
        if (message.type === "RECORDER_FRAME_DISCOVERED" && options.frameRefreshPromise) {
          return options.frameRefreshPromise;
        }
        return { ok: true };
      }
    }
  };

  const context = {
    CUIRecorderCore: core,
    Date,
    Element,
    HTMLInputElement,
    MutationObserver: class MutationObserver {
      constructor(callback) { mutationCallback = callback; }
      observe() {}
    },
    URL,
    chrome,
    console,
    crypto: {
      ...crypto.webcrypto,
      randomUUID() {
        uuidIndex += 1;
        return `00000000-0000-4000-8000-${String(uuidIndex).padStart(12, "0")}`;
      }
    },
    document,
    navigator: { onLine: true },
    performance: { now: () => monotonicNow },
    setTimeout() { return 1; },
    clearTimeout() {},
    window
  };
  vm.runInNewContext(fs.readFileSync(CONTENT_PATH, "utf8"), context, {
    filename: CONTENT_PATH
  });

  function activate(overrides = {}) {
    let response = null;
    const keepOpen = runtimeListener(
      {
        type: "RECORDER_SESSION_STARTED",
        session: {
          session_id: "session-1",
          scope_origin: "https://example.test",
          raw_key_capture: false,
          include_query: false,
          ...overrides
        }
      },
      {},
      (value) => { response = value; }
    );
    assert.equal(keepOpen, false);
    assert.equal(response?.ok, true);
  }

  function baseEvent(type, overrides = {}) {
    return {
      type,
      target: new Element("button", { id: "run" }),
      altKey: false,
      ctrlKey: false,
      metaKey: false,
      shiftKey: false,
      isTrusted: true,
      ...overrides
    };
  }

  function dispatch(registry, type, overrides = {}, advanceMs = 100) {
    monotonicNow += advanceMs;
    const event = baseEvent(type, overrides);
    for (const listener of registry.get(type) || []) listener(event);
  }

  async function flush() {
    const response = await new Promise((resolve) => {
      const keepOpen = runtimeListener(
        { type: "RECORDER_FLUSH" },
        {},
        resolve
      );
      assert.equal(keepOpen, true);
    });
    assert.equal(response?.ok, true);
    return sentMessages
      .filter((message) => message.type === "RECORDER_EVENT_BATCH")
      .flatMap((message) => message.events);
  }

  return {
    Element,
    HTMLInputElement,
    ShadowRoot,
    activate,
    dispatchDocument(type, overrides, advanceMs) { dispatch(documentListeners, type, overrides, advanceMs); },
    dispatchWindow(type, overrides, advanceMs) { dispatch(windowListeners, type, overrides, advanceMs); },
    runtimeMessage(message) {
      return new Promise((resolve) => {
        const keepOpen = runtimeListener(message, {}, resolve);
        if (!keepOpen) resolve({ keepOpen });
      });
    },
    document,
    documentListeners,
    flush,
    notifyAddedNode(node) {
      assert(mutationCallback, "MutationObserver was not registered");
      mutationCallback([{ addedNodes: [node] }]);
    },
    async stop() {
      return new Promise((resolve) => {
        const keepOpen = runtimeListener(
          { type: "RECORDER_SESSION_STOPPED" },
          {},
          resolve
        );
        assert.equal(keepOpen, true);
      });
    },
    sentMessages,
    windowListeners
  };
}

test("content capture registers the expanded privacy-safe event surface", () => {
  const harness = createHarness();
  const documentEvents = [
    "pointermove", "pointerdown", "pointerup", "pointercancel", "pointerover",
    "pointerout", "pointerenter", "pointerleave", "gotpointercapture",
    "lostpointercapture", "click", "dblclick", "auxclick", "contextmenu",
    "wheel", "scroll", "keydown", "keyup", "beforeinput", "input",
    "compositionstart", "compositionupdate", "compositionend", "copy", "cut",
    "paste", "dragstart", "drag", "dragenter", "dragover", "dragleave", "drop",
    "dragend", "touchstart", "touchmove", "touchend", "touchcancel", "submit",
    "reset", "change", "select", "selectionchange", "focusin", "focusout",
    "invalid", "toggle", "beforetoggle", "cancel", "close", "visibilitychange",
    "fullscreenchange", "fullscreenerror"
  ];
  const windowEvents = [
    "resize", "pageshow", "pagehide", "online", "offline", "hashchange", "popstate"
  ];
  for (const type of documentEvents) {
    assert.equal(harness.documentListeners.has(type), true, `missing document listener: ${type}`);
  }
  for (const type of windowEvents) {
    assert.equal(harness.windowListeners.has(type), true, `missing window listener: ${type}`);
  }
});

test("content capture emits rich metadata without reading or retaining user contents", async () => {
  const harness = createHarness();
  harness.activate();
  const input = new harness.HTMLInputElement("text", { id: "answer" });

  harness.dispatchDocument("keyup", {
    target: input,
    key: "q",
    repeat: false,
    location: 0,
    isComposing: false
  });
  harness.dispatchDocument("compositionupdate", {
    target: input,
    data: "COMPOSITION_CONTENT_MUST_NOT_SURVIVE",
    isComposing: true
  });
  harness.dispatchDocument("paste", {
    target: input,
    clipboardData: {
      types: ["text/plain", "CLIPBOARD_CONTENT_MUST_NOT_SURVIVE"],
      items: { length: 2 },
      files: { length: 0 },
      getData() { throw new Error("clipboard contents must never be read"); }
    }
  });
  harness.dispatchDocument("drop", {
    clientX: 100,
    clientY: 200,
    dataTransfer: {
      types: ["text/html", "TRANSFER_CONTENT_MUST_NOT_SURVIVE"],
      items: { length: 2 },
      files: { length: 1 },
      effectAllowed: "copyMove",
      dropEffect: "copy",
      getData() { throw new Error("dataTransfer contents must never be read"); }
    }
  });

  const form = new harness.Element("form");
  form.method = "post";
  form.action = "https://example.test/submit?answer=private";
  form.elements = { length: 4 };
  harness.dispatchDocument("submit", {
    target: form,
    submitter: new harness.Element("button")
  });
  harness.dispatchDocument("touchstart", {
    target: new harness.Element("canvas"),
    touches: [{ identifier: 7, clientX: 32, clientY: 64, radiusX: 3, radiusY: 4, force: 0.5 }],
    changedTouches: [{ identifier: 7, clientX: 32, clientY: 64, radiusX: 3, radiusY: 4, force: 0.5 }]
  });
  harness.document.fullscreenElement = new harness.Element("video");
  harness.dispatchDocument("fullscreenchange", { target: harness.document.documentElement });
  harness.dispatchWindow("resize", { target: harness.document.documentElement });

  const events = await harness.flush();
  const byType = Object.fromEntries(events.map((event) => [event.type, event]));
  assert.equal(byType.keyup.key, null);
  assert.equal(byType.keyup.key_class, "letter");
  assert.deepEqual(byType.keyup.keyboard, {
    repeat: false,
    location: 0,
    is_composing: false,
    hold_ms: null
  });
  assert.equal(byType.compositionupdate.input.data_length, 36);
  assert.deepEqual(byType.paste.clipboard, {
    types: ["text/plain", "other"],
    item_count: 2,
    file_count: 0
  });
  assert.deepEqual(byType.drop.transfer, {
    types: ["text/html", "other"],
    item_count: 2,
    file_count: 1,
    effect_allowed: "copymove",
    drop_effect: "copy"
  });
  assert.equal(byType.submit.form.action, "https://example.test/submit");
  assert.equal(byType.submit.form.control_count, 4);
  assert.equal(byType.touchstart.pointer.pointer_type, "touch");
  assert.deepEqual(byType.touchstart.touch, { active_count: 1, changed_count: 1 });
  assert.equal(byType.fullscreenchange.document_state.fullscreen, true);
  assert.deepEqual(byType.resize.viewport, {
    width: 1280,
    height: 720,
    device_pixel_ratio: 1.5
  });
  assert.equal(events.every((event) => event.url === "https://example.test/task"), true);

  const serialized = JSON.stringify(events);
  for (const forbidden of [
    "COMPOSITION_CONTENT_MUST_NOT_SURVIVE",
    "CLIPBOARD_CONTENT_MUST_NOT_SURVIVE",
    "TRANSFER_CONTENT_MUST_NOT_SURVIVE"
  ]) {
    assert.equal(serialized.includes(forbidden), false);
  }
});

test("content capture strips input, clipboard, and drag metadata on private targets", async () => {
  const harness = createHarness();
  harness.activate();
  const password = new harness.HTMLInputElement("password", {
    id: "account-secret",
    classes: ["private-field"]
  });
  harness.dispatchDocument("compositionupdate", {
    target: password,
    data: "PRIVATE_TEXT",
    isComposing: true
  });
  harness.dispatchDocument("paste", {
    target: password,
    clipboardData: {
      types: ["text/plain"],
      items: { length: 1 },
      files: { length: 0 }
    }
  });
  harness.dispatchDocument("drop", {
    target: password,
    dataTransfer: {
      types: ["text/plain"],
      items: { length: 1 },
      files: { length: 0 },
      effectAllowed: "copy",
      dropEffect: "copy"
    }
  });

  const events = await harness.flush();
  for (const event of events.filter((item) => item.type !== "navigation")) {
    assert.equal(event.target.password_like, true);
    assert.equal(event.target.selector_hint, "input");
  }
  assert.equal(events.find((event) => event.type === "compositionupdate").input, null);
  assert.equal(events.find((event) => event.type === "paste").clipboard, null);
  assert.equal(events.find((event) => event.type === "drop").transfer, null);
  assert.equal(JSON.stringify(events).includes("PRIVATE_TEXT"), false);
});

test("content script resumes an active session after service-worker recovery", async () => {
  const harness = createHarness({
    restoredSession: {
      session_id: "restored-session",
      scope_origin: "https://example.test",
      raw_key_capture: false,
      include_query: false,
      current_document_epoch: 4,
      current_step_id: "step-7"
    }
  });
  await new Promise((resolve) => setImmediate(resolve));
  harness.dispatchDocument("click");
  const events = await harness.flush();
  assert.equal(events[0].type, "navigation");
  assert.equal(events.every((event) => event.document_epoch === 4), true);
  assert.equal(events.every((event) => event.step_id === "step-7"), true);
});

test("event-time steps survive pending batches and dynamic iframes request scoped reinjection", async () => {
  const harness = createHarness();
  harness.activate({ current_step_id: "step-01" });
  harness.dispatchDocument("click");
  const stepResponse = await new Promise((resolve) => {
    const keepOpen = harness.sentMessages.length;
    // Runtime control messages are synchronous in the content script; use the
    // public activation helper to exercise the same update path without
    // exposing recorder internals to the page fixture.
    harness.activate({ current_step_id: "step-02" });
    resolve(keepOpen);
  });
  assert(Number.isInteger(stepResponse));
  harness.dispatchDocument("click");
  const iframe = new harness.Element("iframe");
  harness.notifyAddedNode(iframe);
  iframe.dispatch("load");
  const events = await harness.flush();
  assert.equal(events.at(-2).step_id, "step-01");
  assert.equal(events.at(-1).step_id, "step-02");
  const refreshes = harness.sentMessages.filter(
    (message) => message.type === "RECORDER_FRAME_DISCOVERED"
  );
  assert.equal(refreshes.length, 2);
  assert.equal(refreshes.every((message) => message.sessionId === "session-1"), true);
  assert.equal(refreshes.every((message) => !Object.hasOwn(message, "url")), true);
});

test("dynamic iframe refresh coalesces a load signal that overlaps discovery", async () => {
  let releaseFirstRefresh;
  const frameRefreshPromise = new Promise((resolve) => {
    releaseFirstRefresh = () => resolve({ ok: true });
  });
  const harness = createHarness({ frameRefreshPromise });
  harness.activate();
  const iframe = new harness.Element("iframe");
  harness.notifyAddedNode(iframe);
  iframe.dispatch("load");

  assert.equal(
    harness.sentMessages.filter((message) => message.type === "RECORDER_FRAME_DISCOVERED").length,
    1
  );
  releaseFirstRefresh();
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));

  const refreshes = harness.sentMessages.filter(
    (message) => message.type === "RECORDER_FRAME_DISCOVERED"
  );
  assert.equal(refreshes.length, 2);
  assert.equal(refreshes.every((message) => message.sessionId === "session-1"), true);
});

test("key hold uses a transient exact key identity and persists only numeric duration", async () => {
  const harness = createHarness();
  harness.activate();
  const input = new harness.HTMLInputElement("text", { id: "answer" });
  harness.dispatchDocument("keydown", {
    target: input,
    key: "q",
    code: "KeyQ",
    repeat: false,
    location: 0,
    isComposing: false
  });
  harness.dispatchDocument("keyup", {
    target: input,
    key: "q",
    code: "KeyQ",
    repeat: false,
    location: 0,
    isComposing: false
  });
  const events = await harness.flush();
  const keyEvents = events.filter((event) => ["keydown", "keyup"].includes(event.type));
  assert.equal(keyEvents[0].keyboard.hold_ms, null);
  assert.equal(keyEvents[1].keyboard.hold_ms, 100);
  assert.equal(keyEvents.every((event) => !Object.hasOwn(event.keyboard, "code")), true);
  assert.equal(keyEvents.every((event) => event.key === null), true);
});

test("failed final flush reports the exact dropped batch before clearing it", async () => {
  const harness = createHarness({ failEventBatch: true });
  harness.activate();
  harness.dispatchDocument("click");
  const response = await harness.stop();
  assert.equal(response.ok, false);
  assert.match(response.error, /quota failure/);
  const report = harness.sentMessages.find((message) => message.type === "RECORDER_REPORT_DROPPED");
  assert.equal(report.sessionId, "session-1");
  assert.equal(report.count, 2);
});

test("password, autocomplete-password, and data-recorder-private keys drop class, modifiers, and timing", async () => {
  const harness = createHarness();
  harness.activate();
  const targets = [
    new harness.HTMLInputElement("password", { id: "pw" }),
    new harness.HTMLInputElement("text", { id: "otp", attributes: { autocomplete: "section-a current-password" } }),
    new harness.HTMLInputElement("text", { id: "note", privateTarget: true })
  ];
  for (const target of targets) {
    for (const type of ["keydown", "keyup"]) {
      harness.dispatchDocument(type, {
        target,
        key: "Z",
        code: "KeyZ",
        shiftKey: true,
        repeat: false,
        location: 0,
        isComposing: false
      });
    }
  }
  const events = (await harness.flush()).filter((event) => ["keydown", "keyup"].includes(event.type));
  assert.equal(events.length, 6);
  for (const event of events) {
    assert.equal(event.target.password_like, true);
    assert.equal(event.key, "<redacted>");
    assert.equal(event.key_class, null);
    assert.deepEqual(event.keyboard, { repeat: null, location: null, is_composing: false, hold_ms: null });
    assert.deepEqual(event.modifiers, { alt: null, ctrl: null, meta: null, shift: null });
  }
});

// Composed events reach the document-level listeners retargeted to the
// outermost shadow host. composedPath() reaches into open roots only, so a
// closed root is modelled by a path that stops at its host.
function shadowPrivacyScenarios(harness) {
  const { Element, HTMLInputElement } = harness;
  const scenarios = [];

  const openHost = new Element("login-box", { id: "open-host" });
  const openRoot = openHost.attachShadow({ mode: "open" });
  const openPassword = new HTMLInputElement("password", { id: "open-pw", root: openRoot });
  openRoot.activeElement = openPassword;
  scenarios.push({ name: "password in an open root", target: openHost, path: [openPassword, openRoot, openHost] });

  const closedHost = new Element("login-box", { id: "closed-host" });
  const closedRoot = closedHost.attachShadow({ mode: "closed" });
  const closedPassword = new HTMLInputElement("password", { id: "closed-pw", root: closedRoot });
  closedRoot.activeElement = closedPassword;
  scenarios.push({ name: "password in a closed root", target: closedHost, path: [closedHost] });

  const outerHost = new Element("app-shell", { id: "outer" });
  const outerRoot = outerHost.attachShadow({ mode: "closed" });
  const innerHost = new Element("login-box", { id: "inner", root: outerRoot });
  const innerRoot = innerHost.attachShadow({ mode: "closed" });
  const nestedPassword = new HTMLInputElement("text", {
    id: "nested-pw",
    root: innerRoot,
    attributes: { autocomplete: "new-password" }
  });
  outerRoot.activeElement = innerHost;
  innerRoot.activeElement = nestedPassword;
  scenarios.push({ name: "autocomplete password in nested closed roots", target: outerHost, path: [outerHost] });

  const noteHost = new Element("note-box", { id: "note-host" });
  const noteRoot = noteHost.attachShadow({ mode: "open" });
  const wrapper = new Element("div", { root: noteRoot, attributes: { "data-recorder-private": "" } });
  const note = new HTMLInputElement("text", { id: "note", root: noteRoot, parent: wrapper });
  noteRoot.activeElement = note;
  scenarios.push({ name: "private marker inside a shadow root", target: noteHost, path: [note, wrapper, noteRoot, noteHost] });

  const region = new Element("section", { attributes: { "data-recorder-private": "" } });
  const regionHost = new Element("note-box", { id: "region-host", parent: region });
  const regionRoot = regionHost.attachShadow({ mode: "closed" });
  const regionField = new HTMLInputElement("text", { id: "region-field", root: regionRoot });
  regionRoot.activeElement = regionField;
  scenarios.push({ name: "private marker around a closed host", target: regionHost, path: [regionHost, region] });

  const frameHost = new Element("field-frame", { id: "frame-host" });
  const frameRoot = frameHost.attachShadow({ mode: "open" });
  const privateSlotRegion = new Element("div", { root: frameRoot, attributes: { "data-recorder-private": "" } });
  const slot = new Element("slot", { root: frameRoot, parent: privateSlotRegion });
  const slotted = new HTMLInputElement("text", { id: "slotted", parent: frameHost, slot });
  scenarios.push({
    name: "light-DOM field slotted into a private shadow region",
    target: slotted,
    path: [slotted, slot, privateSlotRegion, frameRoot, frameHost]
  });

  const opaqueHost = new Element("opaque-box", { id: "opaque" });
  opaqueHost.attachShadow({ mode: "closed" });
  scenarios.push({ name: "unresolvable closed root (fails closed)", target: opaqueHost, path: [opaqueHost] });
  return scenarios;
}

for (const rawKeyCapture of [false, true]) {
  test(`shadow-DOM password fields and private regions stay private (raw_key_capture=${rawKeyCapture})`, async () => {
    const harness = createHarness();
    harness.activate({ raw_key_capture: rawKeyCapture });
    const scenarios = shadowPrivacyScenarios(harness);
    for (const scenario of scenarios) {
      for (const type of ["keydown", "keyup", "beforeinput", "input", "compositionupdate", "paste"]) {
        harness.dispatchDocument(type, {
          target: scenario.target,
          composedPath: () => scenario.path,
          key: "Z",
          code: "KeyZ",
          shiftKey: true,
          repeat: false,
          location: 0,
          isComposing: false,
          inputType: "insertText",
          data: "SHADOW_SECRET",
          clipboardData: { types: ["text/plain"], items: { length: 1 }, files: { length: 0 } }
        });
      }
    }
    const events = (await harness.flush()).filter((event) => event.type !== "navigation");
    assert.equal(events.length, scenarios.length * 6);
    events.forEach((event, index) => {
      const scenario = scenarios[Math.floor(index / 6)].name;
      assert.equal(event.target.password_like, true, `${scenario}: ${event.type} was not private`);
      assert.equal(event.target.selector_hint, event.target.tag, `${scenario}: selector hint kept id/classes`);
      assert.equal(event.input, null, `${scenario}: ${event.type} kept input metadata`);
      assert.equal(event.clipboard, null, `${scenario}: ${event.type} kept clipboard metadata`);
      if (event.type === "keydown" || event.type === "keyup") {
        assert.equal(event.key, "<redacted>", `${scenario}: key identity recorded`);
        assert.equal(event.key_class, null, `${scenario}: key class recorded`);
        assert.deepEqual(event.keyboard, { repeat: null, location: null, is_composing: false, hold_ms: null });
        assert.deepEqual(event.modifiers, { alt: null, ctrl: null, meta: null, shift: null });
      }
    });
    // Every resolvable scenario is attributed to the inner field, not the host.
    const resolvedTags = new Set(events.slice(0, (scenarios.length - 1) * 6).map((event) => event.target.tag));
    assert.deepEqual([...resolvedTags], ["input"]);
    assert.equal(JSON.stringify(events).includes("SHADOW_SECRET"), false);
  });
}

test("shadow-DOM resolution does not redact ordinary shadow targets", async () => {
  const harness = createHarness();
  harness.activate();
  const { Element, HTMLInputElement, document } = harness;

  // A focused shadow host with nothing focused inside it is the real target.
  const widget = new Element("color-dial", { id: "dial" });
  widget.attachShadow({ mode: "closed" });
  document.activeElement = widget;
  harness.dispatchDocument("keydown", { target: widget, composedPath: () => [widget], key: "q" });

  // An ordinary text field in an open root keeps the default-mode key class.
  const formHost = new Element("search-box", { id: "search" });
  const formRoot = formHost.attachShadow({ mode: "open" });
  const query = new HTMLInputElement("text", { id: "query", root: formRoot });
  formRoot.activeElement = query;
  harness.dispatchDocument("keydown", { target: formHost, composedPath: () => [formHost], key: "q" });

  // Pointer events use the hit-tested inner target and do not follow focus,
  // even when a password field inside the same root has focus.
  const loginHost = new Element("login-box", { id: "login" });
  const loginRoot = loginHost.attachShadow({ mode: "open" });
  const button = new Element("button", { id: "go", root: loginRoot });
  loginRoot.activeElement = new HTMLInputElement("password", { root: loginRoot });
  harness.dispatchDocument("click", {
    target: loginHost,
    composedPath: () => [button, loginRoot, loginHost],
    clientX: 4,
    clientY: 8
  });
  // A throwing composedPath falls back to the retargeted event target.
  harness.dispatchDocument("click", {
    target: button,
    composedPath: () => {
      throw new Error("detached event");
    }
  });

  const events = (await harness.flush()).filter((event) => event.type !== "navigation");
  assert.deepEqual(
    events.map((event) => [event.type, event.target.selector_hint, event.target.password_like, event.key_class]),
    [
      ["keydown", "color-dial#dial", false, "letter"],
      ["keydown", "input#query", false, "letter"],
      ["click", "button#go", false, null],
      ["click", "button#go", false, null]
    ]
  );
});

for (const variant of [{ withoutChromeDom: true }, { chromeDomThrows: true }]) {
  test(`open shadow roots are followed without composedPath (${Object.keys(variant)[0]})`, async () => {
    const harness = createHarness(variant);
    harness.activate({ raw_key_capture: true });
    const host = new harness.Element("login-box", { id: "host" });
    const root = host.attachShadow({ mode: "open" });
    root.activeElement = new harness.HTMLInputElement("password", { id: "pw", root });
    harness.dispatchDocument("keydown", { target: host, key: "Z" });
    const [event] = (await harness.flush()).filter((item) => item.type === "keydown");
    assert.equal(event.target.tag, "input");
    assert.equal(event.target.password_like, true);
    assert.equal(event.key, "<redacted>");
  });
}

test("content state restore uses the background's reduced projection only", async () => {
  const harness = createHarness({
    restoredSession: {
      session_id: "restored",
      scope_origin: "https://example.test",
      raw_key_capture: false,
      include_query: false,
      current_document_epoch: 0,
      current_step_id: null
    }
  });
  await new Promise((resolve) => setImmediate(resolve));
  harness.dispatchDocument("click");
  const events = await harness.flush();
  assert.equal(events.length, 2);
  assert.equal(harness.sentMessages.every((message) => message.sessionId === "restored"), true);
});

test("pointer, drag, touch, and scroll streams are sampled; discrete events are not", async () => {
  const harness = createHarness();
  harness.activate();
  for (const type of ["pointermove", "dragover", "touchmove", "scroll"]) {
    harness.dispatchDocument(type, {}, 200);
    harness.dispatchDocument(type, {}, 10);
  }
  harness.dispatchDocument("drag", {}, 200);
  harness.dispatchDocument("click", {}, 1);
  harness.dispatchDocument("click", {}, 1);
  const events = (await harness.flush()).filter((event) => event.type !== "navigation");
  const counts = events.reduce((rows, event) => ({ ...rows, [event.type]: (rows[event.type] || 0) + 1 }), {});
  assert.deepEqual(counts, { pointermove: 1, dragover: 1, touchmove: 1, scroll: 1, drag: 1, click: 2 });
});

test("wheel and scroll capture deltas and element or window offsets", async () => {
  const harness = createHarness();
  harness.activate();
  harness.dispatchDocument("wheel", { clientX: 5, clientY: 6, deltaX: 1.5, deltaY: -120, deltaMode: 0 });
  harness.dispatchDocument("scroll", { target: new harness.Element("div", { scrollLeft: 30, scrollTop: 400 }) }, 200);
  harness.dispatchDocument("scroll", { target: {} }, 200);
  const events = await harness.flush();
  const wheel = events.find((event) => event.type === "wheel");
  assert.deepEqual(wheel.wheel, { delta_x: 1.5, delta_y: -120, delta_mode: 0 });
  const scrolls = events.filter((event) => event.type === "scroll").map((event) => event.scroll);
  assert.deepEqual(scrolls, [{ x: 30, y: 400 }, { x: 20, y: 40 }]);
});

test("a full batch flushes immediately and events outside the scoped origin are ignored", async () => {
  const harness = createHarness();
  harness.activate();
  for (let index = 0; index < 79; index += 1) harness.dispatchDocument("click");
  await new Promise((resolve) => setImmediate(resolve));
  const batches = harness.sentMessages.filter((message) => message.type === "RECORDER_EVENT_BATCH");
  assert.equal(batches.length, 1);
  assert.equal(batches[0].events.length, 80);

  const outside = createHarness();
  outside.activate({ scope_origin: "https://elsewhere.test" });
  outside.dispatchDocument("click");
  assert.deepEqual(await outside.flush(), []);
  const invalid = createHarness();
  invalid.activate({ scope_origin: "not a url" });
  invalid.dispatchDocument("click");
  assert.deepEqual(await invalid.flush(), []);
});

test("step changes, pagehide flushes, and a clean stop are honoured", async () => {
  const harness = createHarness();
  harness.activate();
  const step = await harness.runtimeMessage({ type: "RECORDER_STEP_CHANGED", stepId: "step-09" });
  assert.equal(step.ok, true);
  harness.dispatchDocument("click");
  harness.dispatchWindow("pagehide", { target: harness.document.documentElement });
  await new Promise((resolve) => setImmediate(resolve));
  const flushed = harness.sentMessages.filter((message) => message.type === "RECORDER_EVENT_BATCH").flatMap((message) => message.events);
  assert.deepEqual(flushed.map((event) => event.type), ["navigation", "click", "pagehide"]);
  assert.equal(flushed.at(-1).step_id, "step-09");

  const stopped = await harness.stop();
  assert.equal(stopped.ok, true);
  const before = harness.sentMessages.length;
  harness.dispatchDocument("click");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(harness.sentMessages.length, before, "events after stop must not be sent");
  const ignored = await harness.runtimeMessage({ type: "RECORDER_UNKNOWN" });
  assert.deepEqual(ignored, { keepOpen: false });
  const inactiveStep = await harness.runtimeMessage({ type: "RECORDER_STEP_CHANGED", stepId: "late" });
  assert.equal(inactiveStep.ok, true);
});

test("form reset, lifecycle, and nested iframe discovery are captured without content", async () => {
  const harness = createHarness();
  harness.activate();
  const form = new harness.Element("form");
  harness.dispatchDocument("reset", { target: form });
  harness.dispatchDocument("toggle", { target: new harness.Element("details") });
  harness.dispatchWindow("online", { target: harness.document.documentElement });
  const container = new harness.Element("div");
  const nested = new harness.Element("iframe");
  container.querySelectorAll = (selector) => (selector === "iframe" ? [nested] : []);
  harness.notifyAddedNode(container);
  const events = await harness.flush();
  const reset = events.find((event) => event.type === "reset");
  assert.equal(reset.form.control_count, 0);
  assert.equal(reset.form.action, "https://example.test/task");
  assert.equal(events.find((event) => event.type === "online").document_state.online, true);
  assert.equal(events.some((event) => event.type === "toggle"), true);
  assert.equal(
    harness.sentMessages.filter((message) => message.type === "RECORDER_FRAME_DISCOVERED").length,
    1
  );
});
