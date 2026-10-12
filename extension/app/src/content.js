(function startContentRecorder() {
  "use strict";

  if (globalThis.__CUI_TRACE_RECORDER_CONTENT__) return;
  globalThis.__CUI_TRACE_RECORDER_CONTENT__ = true;

  const BATCH_SIZE = 80;
  const MAX_BATCH_AGE_MS = 1000;
  const POINTER_INTERVAL_MS = 50;
  const SCROLL_INTERVAL_MS = 100;
  const DRAG_INTERVAL_MS = 50;
  const TOUCH_INTERVAL_MS = 50;
  const MAX_TRANSIENT_KEYS = 256;
  const INDICATOR_ID = "cui-trace-recorder-indicator";

  let activeSession = null;
  let pending = [];
  let lastPointerAt = -Infinity;
  let lastScrollAt = -Infinity;
  let lastDragAt = -Infinity;
  let lastTouchAt = -Infinity;
  let flushTimer = null;
  let flushInFlight = Promise.resolve();
  let frameRefreshInFlight = false;
  let frameRefreshPending = false;
  const keyDownAt = new Map();

  function showRecordingIndicator(show) {
    document.getElementById(INDICATOR_ID)?.remove();
    if (!show || !document.documentElement) return;
    const indicator = document.createElement("div");
    indicator.id = INDICATOR_ID;
    indicator.setAttribute("role", "status");
    indicator.setAttribute("aria-live", "polite");
    indicator.textContent = "CUA RECORDER · RECORDING";
    Object.assign(indicator.style, {
      position: "fixed",
      zIndex: "2147483647",
      top: "12px",
      right: "12px",
      padding: "8px 12px",
      borderRadius: "999px",
      background: "#b42318",
      color: "#ffffff",
      font: "700 12px/1.2 system-ui, sans-serif",
      letterSpacing: ".04em",
      boxShadow: "0 4px 14px rgba(0,0,0,.25)",
      pointerEvents: "none"
    });
    document.documentElement.appendChild(indicator);
  }

  // Shadow DOM. The listeners below sit on `document`, so a composed event
  // (keyboard, input, composition, clipboard, pointer, focus, drag) raised
  // inside a shadow tree arrives with `event.target` retargeted to the
  // outermost shadow host. Classifying the host would miss an
  // <input type=password> or a [data-recorder-private] region inside the
  // shadow tree and record its keystrokes. So:
  // * every event is classified on `composedPath()[0]`, the innermost target
  //   visible from here (it already reaches into open shadow roots);
  // * events that are delivered to the focused element (FOCUS_TARGETED_TYPES)
  //   follow focus down through open *and* closed shadow roots;
  // * the private-region marker is matched across shadow boundaries;
  // * when focus cannot be followed into a shadow tree, those events fail
  //   closed and are recorded as private.
  const PRIVATE_REGION_SELECTOR = "[data-recorder-private]";
  const MAX_SHADOW_DEPTH = 32;
  const FOCUS_TARGETED_TYPES = new Set([
    "keydown",
    "keyup",
    "beforeinput",
    "input",
    "compositionstart",
    "compositionupdate",
    "compositionend",
    "copy",
    "cut",
    "paste"
  ]);

  // Open or closed author shadow root hosted by `element`, or null. Closed
  // roots are reachable only through the extension-only chrome.dom API.
  function shadowRootOf(element) {
    try {
      const root = globalThis.chrome?.dom?.openOrClosedShadowRoot?.(element);
      if (root) return root;
    } catch (_error) {
      // Not a shadow host, or the API is unavailable; fall back to open roots.
    }
    return element.shadowRoot || null;
  }

  function isFocusedInItsTree(element) {
    try {
      return element.getRootNode?.()?.activeElement === element;
    } catch (_error) {
      return false;
    }
  }

  function innermostTarget(event) {
    try {
      const first = typeof event?.composedPath === "function" ? event.composedPath()[0] : null;
      if (first instanceof Element) return first;
    } catch (_error) {
      // Synthetic or detached event: use the (retargeted) event target.
    }
    return event?.target;
  }

  // Returns the element an event is really about. `unresolvedShadow` is true
  // when a focus-targeted event came from inside a shadow tree whose focused
  // element cannot be determined; the caller then treats the target as private.
  function resolveTarget(event) {
    const innermost = innermostTarget(event);
    if (!(innermost instanceof Element)) return { element: innermost, unresolvedShadow: false };
    if (!FOCUS_TARGETED_TYPES.has(event.type)) return { element: innermost, unresolvedShadow: false };
    let element = innermost;
    for (let depth = 0; depth < MAX_SHADOW_DEPTH; depth += 1) {
      const root = shadowRootOf(element);
      if (!root) return { element, unresolvedShadow: false };
      const inner = root.activeElement;
      if (inner instanceof Element && inner !== element) {
        element = inner;
        continue;
      }
      // Nothing inside the shadow tree has focus. If the host itself is the
      // focused element it is the real target; otherwise the event came from
      // a node we cannot see.
      return { element, unresolvedShadow: !isFocusedInItsTree(element) };
    }
    return { element, unresolvedShadow: true };
  }

  // True when `element` or any ancestor in the flat tree (through slots and
  // shadow hosts) carries the [data-recorder-private] marker.
  function insidePrivateRegion(element) {
    let node = element;
    while (node) {
      if (node.matches?.(PRIVATE_REGION_SELECTOR)) return true;
      node = node.assignedSlot || node.parentElement || node.getRootNode?.()?.host || null;
    }
    return false;
  }

  function targetDescriptor(target, unresolvedShadow = false) {
    const element = target instanceof Element ? target : document.documentElement;
    const type = element instanceof HTMLInputElement ? element.type.toLowerCase() : "";
    const autocompleteTokens = (element.getAttribute?.("autocomplete") || "")
      .toLowerCase()
      .split(/\s+/)
      .filter(Boolean);
    return {
      tag: element.tagName || "unknown",
      role: element.getAttribute?.("role") || "",
      inputType: type,
      id: element.id || "",
      classes: element.classList ? Array.from(element.classList).slice(0, 3) : [],
      passwordLike:
        type === "password" ||
        autocompleteTokens.includes("current-password") ||
        autocompleteTokens.includes("new-password") ||
        Boolean(unresolvedShadow) ||
        insidePrivateRegion(element)
    };
  }

  function currentScopeMatches() {
    if (!activeSession) return false;
    if (!activeSession.scope_origin || activeSession.scope_origin === "unavailable") return true;
    try {
      return window.location.origin === new URL(activeSession.scope_origin).origin;
    } catch (_error) {
      return false;
    }
  }

  function rawBase(event, typeOverride) {
    const resolved = resolveTarget(event);
    return {
      eventId: crypto.randomUUID(),
      type: typeOverride || event.type,
      epochMs: Date.now(),
      monotonicMs: performance.now(),
      url: window.location.href,
      viewportWidth: window.innerWidth,
      viewportHeight: window.innerHeight,
      devicePixelRatio: window.devicePixelRatio,
      target: targetDescriptor(resolved.element, resolved.unresolvedShadow),
      altKey: event.altKey,
      ctrlKey: event.ctrlKey,
      metaKey: event.metaKey,
      shiftKey: event.shiftKey,
      isTrusted: event.isTrusted,
      visibilityState: document.visibilityState
    };
  }

  function queueEvent(raw) {
    if (!activeSession || !currentScopeMatches()) return;
    const normalized = CUIRecorderCore.normalizeEvent(raw, {
        rawKeyCapture: activeSession.raw_key_capture,
        includeQuery: activeSession.include_query
      });
    // These fields are treated as untrusted hints and are assigned/validated
    // again by the background worker. Keeping document time separate prevents
    // performance.now() resets after navigation from corrupting session time.
    normalized.document_epoch = Number(activeSession.current_document_epoch || 0);
    normalized.t_document_ms = normalized.monotonic_ms;
    normalized.step_id = activeSession.current_step_id || null;
    pending.push(normalized);
    if (pending.length >= BATCH_SIZE) void flush().catch(() => {});
    else scheduleFlush();
  }

  function scheduleFlush() {
    if (flushTimer) return;
    flushTimer = setTimeout(() => {
      flushTimer = null;
      void flush().catch(() => {});
    }, MAX_BATCH_AGE_MS);
  }

  async function flushOnce() {
    if (!activeSession || pending.length === 0) return;
    const events = pending.splice(0, pending.length);
    try {
      const response = await chrome.runtime.sendMessage({
        type: "RECORDER_EVENT_BATCH",
        sessionId: activeSession.session_id,
        events
      });
      if (!response?.ok) throw new Error(response?.error || "Recorder storage write failed");
    } catch (error) {
      pending.unshift(...events);
      if (activeSession) scheduleFlush();
      throw error;
    }
  }

  function flush() {
    const operation = flushInFlight.catch(() => undefined).then(flushOnce);
    flushInFlight = operation;
    return operation;
  }

  function pointerDescriptor(event, fallbackType = "mouse") {
    return {
      x: event.clientX,
      y: event.clientY,
      button: event.button,
      buttons: event.buttons,
      pointerType: event.pointerType || fallbackType,
      pointerId: event.pointerId,
      width: event.width,
      height: event.height,
      pressure: event.pressure,
      tangentialPressure: event.tangentialPressure,
      tiltX: event.tiltX,
      tiltY: event.tiltY,
      twist: event.twist,
      isPrimary: event.isPrimary
    };
  }

  function clipboardDescriptor(clipboardData) {
    if (!clipboardData) return null;
    return {
      types: Array.from(clipboardData.types || []),
      itemCount: clipboardData.items?.length || 0,
      fileCount: clipboardData.files?.length || 0
    };
  }

  function transferDescriptor(dataTransfer) {
    if (!dataTransfer) return null;
    return {
      types: Array.from(dataTransfer.types || []),
      itemCount: dataTransfer.items?.length || 0,
      fileCount: dataTransfer.files?.length || 0,
      effectAllowed: dataTransfer.effectAllowed || "",
      dropEffect: dataTransfer.dropEffect || ""
    };
  }

  function currentDocumentState() {
    return {
      fullscreen: Boolean(document.fullscreenElement),
      online:
        typeof navigator !== "undefined" && typeof navigator.onLine === "boolean"
          ? navigator.onLine
          : null
    };
  }

  function recordPointer(event) {
    if (event.type === "pointermove" && performance.now() - lastPointerAt < POINTER_INTERVAL_MS) return;
    if (event.type === "pointermove") lastPointerAt = performance.now();
    const raw = rawBase(event);
    raw.pointer = pointerDescriptor(event);
    queueEvent(raw);
  }

  function recordWheel(event) {
    const raw = rawBase(event);
    raw.pointer = pointerDescriptor(event);
    raw.wheel = { deltaX: event.deltaX, deltaY: event.deltaY, deltaMode: event.deltaMode };
    queueEvent(raw);
  }

  function recordScroll(event) {
    if (performance.now() - lastScrollAt < SCROLL_INTERVAL_MS) return;
    lastScrollAt = performance.now();
    const raw = rawBase(event, "scroll");
    const scroller = event.target instanceof Element ? event.target : window;
    raw.scroll = {
      x: Number.isFinite(scroller.scrollLeft) ? scroller.scrollLeft : window.scrollX,
      y: Number.isFinite(scroller.scrollTop) ? scroller.scrollTop : window.scrollY
    };
    queueEvent(raw);
  }

  function recordKey(event) {
    const capturedAt = performance.now();
    const keyIdentity = `${String(event.code || "")}` +
      `\u0000${String(event.key || "")}\u0000${Number(event.location || 0)}`;
    let holdMs = null;
    if (event.type === "keydown" && !event.repeat) {
      if (keyDownAt.size >= MAX_TRANSIENT_KEYS) keyDownAt.clear();
      keyDownAt.set(keyIdentity, capturedAt);
    } else if (event.type === "keyup") {
      const startedAt = keyDownAt.get(keyIdentity);
      keyDownAt.delete(keyIdentity);
      if (Number.isFinite(startedAt)) holdMs = Math.max(0, capturedAt - startedAt);
    }
    const raw = rawBase(event);
    raw.key = event.key;
    raw.keyboard = {
      repeat: event.repeat,
      location: event.location,
      isComposing: event.isComposing,
      holdMs
    };
    queueEvent(raw);
  }

  function recordInput(event) {
    const raw = rawBase(event);
    raw.input = {
      inputType: event.inputType || "unknown",
      dataLength: typeof event.data === "string" ? event.data.length : 0,
      isComposing: event.isComposing
    };
    queueEvent(raw);
  }

  function recordClipboard(event) {
    const raw = rawBase(event);
    raw.clipboard = clipboardDescriptor(event.clipboardData);
    queueEvent(raw);
  }

  function recordDrag(event) {
    if (
      (event.type === "drag" || event.type === "dragover") &&
      performance.now() - lastDragAt < DRAG_INTERVAL_MS
    ) {
      return;
    }
    if (event.type === "drag" || event.type === "dragover") lastDragAt = performance.now();
    const raw = rawBase(event);
    raw.pointer = pointerDescriptor(event);
    raw.transfer = transferDescriptor(event.dataTransfer);
    queueEvent(raw);
  }

  function recordTouch(event) {
    if (event.type === "touchmove" && performance.now() - lastTouchAt < TOUCH_INTERVAL_MS) return;
    if (event.type === "touchmove") lastTouchAt = performance.now();
    const raw = rawBase(event);
    const touches = event.touches || [];
    const changedTouches = event.changedTouches || [];
    const firstTouch = changedTouches[0] || touches[0];
    if (firstTouch) {
      raw.pointer = {
        x: firstTouch.clientX,
        y: firstTouch.clientY,
        button: -1,
        buttons: event.type === "touchend" || event.type === "touchcancel" ? 0 : 1,
        pointerType: "touch",
        pointerId: firstTouch.identifier,
        width: Number(firstTouch.radiusX || 0) * 2,
        height: Number(firstTouch.radiusY || 0) * 2,
        pressure: firstTouch.force,
        isPrimary: true
      };
    }
    raw.touch = {
      activeCount: touches.length || 0,
      changedCount: changedTouches.length || 0
    };
    queueEvent(raw);
  }

  function recordForm(event) {
    const raw = rawBase(event);
    const form = event.target?.tagName === "FORM" ? event.target : event.target?.form;
    raw.form = {
      method: form?.method || "",
      action: form?.action || window.location.href,
      controlCount: form?.elements?.length || 0,
      hasSubmitter: Boolean(event.submitter),
      submitterTag: event.submitter?.tagName || ""
    };
    queueEvent(raw);
  }

  function recordDocumentLifecycle(event) {
    const raw = rawBase(event, event.type);
    raw.documentState = currentDocumentState();
    queueEvent(raw);
  }

  function recordLifecycle(event) {
    queueEvent(rawBase(event, event.type));
  }

  function activateSession(session) {
    const sameSession = activeSession?.session_id === session?.session_id;
    activeSession = session || null;
    showRecordingIndicator(Boolean(activeSession));
    if (!activeSession || sameSession) return;
    frameRefreshPending = false;
    pending = [];
    keyDownAt.clear();
    lastPointerAt = -Infinity;
    lastScrollAt = -Infinity;
    lastDragAt = -Infinity;
    lastTouchAt = -Infinity;
    queueEvent({
      eventId: crypto.randomUUID(),
      type: "navigation",
      epochMs: Date.now(),
      monotonicMs: performance.now(),
      url: window.location.href,
      viewportWidth: window.innerWidth,
      viewportHeight: window.innerHeight,
      devicePixelRatio: window.devicePixelRatio,
      target: { tag: "document" },
      isTrusted: null,
      visibilityState: document.visibilityState
    });
  }

  function requestDynamicFrameRefresh() {
    if (!activeSession) return;
    if (frameRefreshInFlight) {
      frameRefreshPending = true;
      return;
    }
    frameRefreshInFlight = true;
    void chrome.runtime.sendMessage({
      type: "RECORDER_FRAME_DISCOVERED",
      sessionId: activeSession.session_id
    }).catch(() => {}).finally(() => {
      frameRefreshInFlight = false;
      if (frameRefreshPending) {
        frameRefreshPending = false;
        requestDynamicFrameRefresh();
      }
    });
  }

  function registerDynamicFrame(frame) {
    if (!frame || String(frame.tagName || "").toUpperCase() !== "IFRAME") return false;
    frame.addEventListener?.("load", requestDynamicFrameRefresh, { once: true });
    return true;
  }

  if (typeof MutationObserver === "function" && document.documentElement) {
    const frameObserver = new MutationObserver((records) => {
      let discovered = false;
      for (const record of records) {
        for (const node of record.addedNodes || []) {
          discovered = registerDynamicFrame(node) || discovered;
          for (const frame of node.querySelectorAll?.("iframe") || []) {
            discovered = registerDynamicFrame(frame) || discovered;
          }
        }
      }
      if (discovered) requestDynamicFrameRefresh();
    });
    frameObserver.observe(document.documentElement, { childList: true, subtree: true });
  }

  [
    "pointermove",
    "pointerdown",
    "pointerup",
    "pointercancel",
    "pointerover",
    "pointerout",
    "pointerenter",
    "pointerleave",
    "gotpointercapture",
    "lostpointercapture",
    "click",
    "dblclick",
    "auxclick",
    "contextmenu"
  ].forEach((type) =>
    document.addEventListener(type, recordPointer, { capture: true, passive: true })
  );
  document.addEventListener("wheel", recordWheel, { capture: true, passive: true });
  document.addEventListener("scroll", recordScroll, { capture: true, passive: true });
  ["keydown", "keyup"].forEach((type) =>
    document.addEventListener(type, recordKey, { capture: true, passive: true })
  );
  ["beforeinput", "input", "compositionstart", "compositionupdate", "compositionend"].forEach(
    (type) => document.addEventListener(type, recordInput, { capture: true, passive: true })
  );
  ["copy", "cut", "paste"].forEach((type) =>
    document.addEventListener(type, recordClipboard, { capture: true, passive: true })
  );
  ["dragstart", "drag", "dragenter", "dragover", "dragleave", "drop", "dragend"].forEach(
    (type) => document.addEventListener(type, recordDrag, { capture: true, passive: true })
  );
  ["touchstart", "touchmove", "touchend", "touchcancel"].forEach((type) =>
    document.addEventListener(type, recordTouch, { capture: true, passive: true })
  );
  ["submit", "reset"].forEach((type) =>
    document.addEventListener(type, recordForm, { capture: true, passive: true })
  );
  [
    "change",
    "select",
    "selectionchange",
    "focusin",
    "focusout",
    "invalid",
    "toggle",
    "beforetoggle",
    "cancel",
    "close"
  ].forEach((type) =>
    document.addEventListener(type, recordLifecycle, { capture: true, passive: true })
  );
  ["visibilitychange", "fullscreenchange", "fullscreenerror"].forEach((type) =>
    document.addEventListener(type, recordDocumentLifecycle, { capture: true, passive: true })
  );
  ["resize", "pageshow", "online", "offline", "hashchange", "popstate"].forEach((type) =>
    window.addEventListener(type, recordDocumentLifecycle, { capture: true, passive: true })
  );
  window.addEventListener(
    "pagehide",
    (event) => {
      recordDocumentLifecycle(event);
      void flush().catch(() => {});
    },
    { capture: true, passive: true }
  );

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "RECORDER_SESSION_STARTED") {
      activateSession(message.session);
      sendResponse({ ok: true });
      return false;
    }
    if (message?.type === "RECORDER_SESSION_STOPPED") {
      void flush()
        .then(() => {
          activeSession = null;
          showRecordingIndicator(false);
          pending = [];
          keyDownAt.clear();
          sendResponse({ ok: true });
        })
        .catch((error) => {
          const failedSession = activeSession;
          const droppedCount = pending.length;
          pending = [];
          keyDownAt.clear();
          const report = failedSession && droppedCount
            ? chrome.runtime.sendMessage({
                type: "RECORDER_REPORT_DROPPED",
                sessionId: failedSession.session_id,
                count: Math.min(droppedCount, 100000)
              }).catch(() => undefined)
            : Promise.resolve();
          void report.finally(() => {
            activeSession = null;
            showRecordingIndicator(false);
            sendResponse({ ok: false, error: error.message });
          });
        });
      return true;
    }
    if (message?.type === "RECORDER_FLUSH") {
      void flush()
        .then(() => sendResponse({ ok: true }))
        .catch((error) => sendResponse({ ok: false, error: error.message }));
      return true;
    }
    if (message?.type === "RECORDER_STEP_CHANGED") {
      if (activeSession) activeSession.current_step_id = message.stepId || null;
      sendResponse({ ok: true });
      return false;
    }
    return false;
  });

  chrome.runtime
    .sendMessage({ type: "RECORDER_GET_STATE" })
    .then((state) => {
      const active = state?.data?.activeSession || state?.activeSession;
      if (active) activateSession(active);
      else if (!activeSession) showRecordingIndicator(false);
    })
    .catch(() => {});
})();
