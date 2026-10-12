"use strict";

// Standalone matched-task playground. It mirrors the extension's capture path
// through the shipped normalization core (../app/src/core.js) without the
// extension, storage, or any network access. Serve the extension/ directory and
// open /playground/ (see docs/extension/index.md).

const instruments = [
  { id: "spectrascope", name: "SpectraScope", category: "spectroscopy", description: "Visible-spectrum acquisition and peak analysis.", icon: "Sp" },
  { id: "microview", name: "MicroView", category: "imaging", description: "High-resolution microscopy capture.", icon: "Mv" },
  { id: "thermosense", name: "ThermoSense", category: "sensing", description: "Calibrated temperature time series.", icon: "Ts" },
  { id: "rfspectra", name: "RF Spectra", category: "spectroscopy", description: "Radio-frequency sweep analysis.", icon: "RF" },
  { id: "nanocam", name: "NanoCam", category: "imaging", description: "Low-noise sample imaging.", icon: "Nc" },
  { id: "fieldprobe", name: "FieldProbe", category: "sensing", description: "Three-axis field measurement.", icon: "Fp" }
];

const TASK_ID = "playground-matched-task-v1";
const RUN_CODE = "PG-DEMO";
const POINTER_INTERVAL_MS = 50;

const state = {
  active: false,
  startedAt: null,
  events: [],
  selected: null,
  actorLabel: "playground-run-01",
  lastPointerAt: null
};

const catalog = document.getElementById("catalog");
const search = document.getElementById("catalog-search");
const category = document.getElementById("category");
const configuration = document.getElementById("configuration");
const success = document.getElementById("success");
const preview = document.getElementById("event-preview");

// Same private-target rule as extension/app/src/content.js.
function descriptor(target) {
  const element = target instanceof Element ? target : document.documentElement;
  const type = element instanceof HTMLInputElement ? element.type.toLowerCase() : "";
  const autocomplete = (element.getAttribute?.("autocomplete") || "").toLowerCase().split(/\s+/);
  return {
    tag: element.tagName,
    role: element.getAttribute?.("role") || "",
    inputType: type,
    id: element.id || "",
    classes: element.classList ? Array.from(element.classList).slice(0, 3) : [],
    passwordLike:
      type === "password" ||
      autocomplete.includes("current-password") ||
      autocomplete.includes("new-password") ||
      Boolean(element.closest?.("[data-recorder-private]"))
  };
}

function capture(event) {
  if (!state.active) return;
  if (event.type === "pointermove") {
    if (state.lastPointerAt !== null && performance.now() - state.lastPointerAt < POINTER_INTERVAL_MS) return;
    state.lastPointerAt = performance.now();
  }
  const raw = {
    eventId: crypto.randomUUID(),
    type: event.type,
    epochMs: Date.now(),
    monotonicMs: performance.now(),
    url: window.location.href,
    viewportWidth: window.innerWidth,
    viewportHeight: window.innerHeight,
    devicePixelRatio: window.devicePixelRatio,
    target: descriptor(event.target),
    altKey: event.altKey,
    ctrlKey: event.ctrlKey,
    metaKey: event.metaKey,
    shiftKey: event.shiftKey,
    isTrusted: event.isTrusted,
    visibilityState: document.visibilityState
  };
  if (event.type.startsWith("pointer") || event.type === "click") {
    raw.pointer = {
      x: event.clientX,
      y: event.clientY,
      button: event.button,
      buttons: event.buttons,
      pointerType: event.pointerType || "mouse"
    };
  }
  if (event.type === "keydown") {
    raw.key = event.key;
    raw.keyboard = { repeat: event.repeat, location: event.location, isComposing: event.isComposing, holdMs: null };
  }
  if (event.type === "beforeinput") {
    raw.input = { inputType: event.inputType, dataLength: event.data?.length || 0, isComposing: event.isComposing };
  }
  state.events.push(CUIRecorderCore.normalizeEvent(raw, { rawKeyCapture: false, includeQuery: false }));
  updateEvidence();
}

function updateEvidence() {
  document.getElementById("event-count").textContent = `${state.events.length} events`;
  preview.textContent = state.events.slice(-5).map((event) => JSON.stringify(event, null, 2)).join("\n\n");
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderCatalog() {
  const term = search.value.trim().toLowerCase();
  const selectedCategory = category.value;
  const visible = instruments.filter((item) =>
    (!term || `${item.name} ${item.description}`.toLowerCase().includes(term)) &&
    (selectedCategory === "all" || item.category === selectedCategory)
  );
  document.getElementById("result-count").textContent = `${visible.length} result${visible.length === 1 ? "" : "s"}`;
  catalog.replaceChildren(
    ...visible.map((item) => {
      const article = element("article", "instrument");
      const body = element("div");
      body.append(element("h4", "", item.name), element("p", "", item.description));
      const open = element("button", "", "Open");
      open.dataset.id = item.id;
      article.append(element("span", "instrument-icon", item.icon), body, open);
      return article;
    })
  );
}

function selectInstrument(id) {
  state.selected = instruments.find((item) => item.id === id) || null;
  if (!state.selected) return;
  configuration.hidden = false;
  document.getElementById("selected-name").textContent = state.selected.name;
  document.getElementById("selected-description").textContent = state.selected.description;
  configuration.scrollIntoView({ behavior: "smooth", block: "center" });
}

function startRecording() {
  state.active = true;
  state.startedAt = new Date().toISOString();
  state.events = [];
  state.lastPointerAt = null;
  state.actorLabel = document.getElementById("actor-label").value || "playground-run-01";
  document.querySelector(".live-state").classList.add("active");
  document.getElementById("record-state").textContent = "Mirrored recorder active";
  document.getElementById("start-recording").textContent = "Recording";
  document.getElementById("start-recording").disabled = true;
  updateEvidence();
}

function resetTask() {
  search.value = "";
  category.value = "all";
  state.selected = null;
  configuration.hidden = true;
  success.hidden = true;
  document.getElementById("run-form").reset();
  document.getElementById("session-export").textContent = "";
  renderCatalog();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function finishTask(event) {
  event.preventDefault();
  const code = document.getElementById("run-code").value;
  const replications = document.getElementById("replications").value;
  const passed = state.selected?.id === "spectrascope" && code === RUN_CODE && replications === "3";
  if (!passed) {
    alert("Validator did not pass. Check the instrument, run code, and replications.");
    return;
  }
  state.active = false;
  document.querySelector(".live-state").classList.remove("active");
  document.getElementById("record-state").textContent = "Trace kept in page memory";
  const session = buildSession();
  const summary = session.summary;
  document.getElementById("success-summary").textContent =
    `SpectraScope · ${RUN_CODE} · 3 replications · ${state.actorLabel}`;
  document.getElementById("summary-grid").replaceChildren(
    ...[
      [summary.event_count, "sanitized events"],
      [summary.by_type.keydown || 0, "keydown events"],
      [summary.by_type.pointermove || 0, "pointer moves"],
      [`${Math.round(summary.duration_ms / 100) / 10}s`, "observed duration"]
    ].map(([value, label]) => {
      const cell = element("div");
      cell.append(element("strong", "", String(value)), element("span", "", label));
      return cell;
    })
  );
  window.__CUA_PLAYGROUND_SESSION__ = session;
  document.getElementById("session-export").textContent = JSON.stringify(session);
  success.hidden = false;
  success.scrollIntoView({ behavior: "smooth", block: "center" });
}

function platformLabel() {
  return navigator.userAgentData?.platform || navigator.platform || "unknown-platform";
}

// A cui-trace-v2 session marked as a standalone mirror (not extension storage).
function buildSession() {
  const stoppedAt = new Date().toISOString();
  const legacy = {
    metadata: {
      schema_version: CUIRecorderCore.LEGACY_SCHEMA_VERSION,
      session_id: crypto.randomUUID(),
      actor_type: "agent",
      actor_label: state.actorLabel.slice(0, 80),
      participant_code: "self-test",
      pair_id: "unpaired",
      task_id: TASK_ID,
      protocol_version: TASK_ID,
      executor: "manual-playground",
      executor_version: "not-recorded",
      recorder_version: "playground",
      environment: `${platformLabel()} / standalone playground`.slice(0, 512),
      capture_adapter: "browser_standalone_mirror",
      environment_scope: "playground",
      source_class: "synthetic_coverage",
      raw_key_capture: false,
      include_query: false,
      scope_origin: window.location.origin,
      consent_confirmed: true,
      disclosure_version: "playground-not-applicable",
      retention_policy: "page-memory-only",
      notes: "Standalone playground mirror using extension/app/src/core.js; not extension storage and not a human trace.",
      started_at: state.startedAt || stoppedAt,
      stopped_at: stoppedAt,
      status: "complete",
      capture_error: null,
      event_count: state.events.length,
      chunk_count: 0
    },
    summary: CUIRecorderCore.summarizeEvents(state.events),
    events: state.events
  };
  return CUIRecorderCore.migrateSessionV1ToV2(legacy);
}

function downloadTrace() {
  const session = window.__CUA_PLAYGROUND_SESSION__ || buildSession();
  const url = URL.createObjectURL(new Blob([JSON.stringify(session, null, 2)], { type: "application/json" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "playground-demo-trace.json";
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

["pointermove", "pointerdown", "pointerup", "click", "keydown", "beforeinput", "change", "focusin", "focusout"].forEach((type) =>
  document.addEventListener(type, capture, { capture: true, passive: true })
);
search.addEventListener("input", renderCatalog);
category.addEventListener("change", renderCatalog);
catalog.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-id]");
  if (button) selectInstrument(button.dataset.id);
});
document.getElementById("start-recording").addEventListener("click", startRecording);
document.getElementById("reset-task").addEventListener("click", resetTask);
document.getElementById("run-form").addEventListener("submit", finishTask);
document.getElementById("download-trace").addEventListener("click", downloadTrace);
renderCatalog();
