// Identifier of the participant-facing wording in privacy/disclosure.html. It
// is stored with every session. Operators who edit the disclosure text must
// change this value and the matching <meta name="cua-disclosure-version"> tag.
const DISCLOSURE_VERSION = "2026-10-11-template-v2";

const elements = Object.fromEntries(
  [
    "status-chip", "idle-panel", "active-panel", "actor-type", "participant-code",
    "task-id", "pair-id", "protocol-version", "actor-label", "executor",
    "executor-version", "capture-adapter", "environment-scope", "source-class",
    "consent", "scope", "error", "start", "stop", "active-session",
    "active-actor", "active-task", "active-events", "open-dashboard",
    "persist-origin-permission", "recorder-version"
  ].map((id) => [id, document.getElementById(id)])
);

let activeTab = null;
let refreshTimer = null;

// The manifest is the single source of the extension version.
function recorderVersion() {
  return chrome.runtime.getManifest().version;
}

// navigator.platform is frozen/deprecated in Chromium; prefer User-Agent
// Client Hints and fall back only when they are unavailable.
function platformLabel() {
  const hinted = navigator.userAgentData?.platform;
  if (typeof hinted === "string" && hinted.trim()) return hinted.trim();
  return navigator.platform || "unknown-platform";
}

async function request(message) {
  const response = await chrome.runtime.sendMessage(message);
  if (!response?.ok) throw new Error(response?.error || "Recorder request failed");
  return response.data;
}

function setError(message = "") {
  elements.error.textContent = message;
}

function actorDefaults() {
  const agent = elements["actor-type"].value === "agent";
  elements["participant-code"].value = agent ? "agent-run-01" : "self-run-01";
  elements["actor-label"].value = agent ? "agent-run-01" : "self-run-01";
  elements.executor.value = agent ? "unspecified-agent" : "manual";
  elements["source-class"].value = agent ? "cui_agent" : "human_self";
}

function showState(activeSession) {
  const active = Boolean(activeSession);
  elements["idle-panel"].hidden = active;
  elements["active-panel"].hidden = !active;
  elements["status-chip"].textContent = active ? "Recording" : "Idle";
  elements["status-chip"].className = `chip ${active ? "active" : "idle"}`;
  if (active) {
    elements["active-session"].textContent = activeSession.session_id;
    elements["active-actor"].textContent = `${activeSession.actor_type} | ${activeSession.actor_label}`;
    elements["active-task"].textContent = activeSession.task_id;
    elements["active-events"].textContent = String(activeSession.event_count || 0);
    if (!refreshTimer) refreshTimer = setInterval(refresh, 1000);
  } else if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
}

async function refresh() {
  try {
    const state = await request({ type: "RECORDER_GET_STATE" });
    showState(state.activeSession);
  } catch (error) {
    setError(error.message);
  }
}

async function startRecording() {
  setError();
  if (!elements.consent.checked) {
    setError("Affirmative consent must be confirmed before recording.");
    return;
  }
  let url;
  try {
    url = new URL(activeTab?.url);
  } catch (_error) {
    setError("The active tab is not a recordable web page.");
    return;
  }
  const keyMode = document.querySelector('input[name="key-mode"]:checked').value;
  if (
    keyMode === "raw" &&
    !window.confirm("Raw-key mode can capture typed text. Continue only with explicit protocol approval and consent.")
  ) {
    return;
  }
  try {
    if (elements["persist-origin-permission"].checked) {
      const originPattern = `${url.origin}/*`;
      const granted = await chrome.permissions.request({ origins: [originPattern] });
      if (!granted) throw new Error("Task-origin permission was not granted; disable the option or approve this exact origin.");
    }
    const session = await request({
      type: "RECORDER_START",
      input: {
        tabId: activeTab.id,
        actorType: elements["actor-type"].value,
        participantCode: elements["participant-code"].value,
        pairId: elements["pair-id"].value,
        taskId: elements["task-id"].value,
        protocolVersion: elements["protocol-version"].value,
        actorLabel: elements["actor-label"].value,
        executor: elements.executor.value,
        executorVersion: elements["executor-version"].value,
        captureAdapter: elements["capture-adapter"].value,
        environmentScope: elements["environment-scope"].value,
        sourceClass: elements["source-class"].value,
        recorderVersion: recorderVersion(),
        environment: `${platformLabel()} / Chromium`,
        rawKeyCapture: keyMode === "raw",
        includeQuery: false,
        scopeOrigin: url.origin,
        consentConfirmed: true,
        disclosureVersion: DISCLOSURE_VERSION,
        retentionPolicy: "local-until-export-or-delete"
      }
    });
    showState(session);
  } catch (error) {
    setError(error.message);
  }
}

async function stopRecording() {
  setError();
  try {
    const session = await request({ type: "RECORDER_STOP" });
    showState(null);
    elements.consent.checked = false;
    elements.scope.textContent =
      session.status === "complete"
        ? `Saved ${session.event_count} events locally. Export or delete from Sessions.`
        : `Stopped with incomplete capture: ${session.capture_error || "final flush was not confirmed"}. Inspect before use.`;
  } catch (error) {
    setError(error.message);
  }
}

async function initialize() {
  elements["recorder-version"].textContent = `v${recorderVersion()}`;
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  activeTab = tabs[0];
  try {
    const url = new URL(activeTab.url);
    const recordable = /^https?:$/.test(url.protocol);
    elements.scope.textContent = recordable
      ? `Capture scope: ${url.origin} only (query strings excluded)`
      : "This protected page cannot be recorded. Open the task website first.";
    elements.start.disabled = !recordable;
  } catch (_error) {
    elements.scope.textContent = "This protected page cannot be recorded. Open the task website first.";
    elements.start.disabled = true;
  }
  await refresh();
}

elements["actor-type"].addEventListener("change", actorDefaults);
elements.start.addEventListener("click", startRecording);
elements.stop.addEventListener("click", stopRecording);
elements["open-dashboard"].addEventListener("click", () => chrome.runtime.openOptionsPage());
void initialize();
