const sessionsContainer = document.getElementById("sessions");
const template = document.getElementById("session-template");
const message = document.getElementById("message");

async function request(payload) {
  const response = await chrome.runtime.sendMessage(payload);
  if (!response?.ok) throw new Error(response?.error || "Recorder request failed");
  return response.data;
}

// Hands the export to the browser's normal download flow through a temporary
// object URL. The extension does not hold the `downloads` permission, so it
// cannot observe whether the browser finished saving the file; the status
// message therefore says "handed to the browser", not "saved".
function download(name, content, type) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.hidden = true;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function populateDefinitionList(container, metadata) {
  const rows = [
    ["Started", new Date(metadata.started_at).toLocaleString()],
    ["Status", metadata.status],
    ["Events", metadata.event_count],
    ["Executor", `${metadata.executor} ${metadata.executor_version}`],
    ["Mode", metadata.raw_key_capture ? "raw keys" : "privacy-safe"],
    ...(metadata.capture_error ? [["Capture note", metadata.capture_error]] : [])
  ];
  container.replaceChildren();
  for (const [term, value] of rows) {
    const row = document.createElement("div");
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = term;
    dd.textContent = String(value ?? "");
    row.append(dt, dd);
    container.appendChild(row);
  }
}

const EXPORT_FORMATS = {
  json: { suffix: ".json", type: "application/json", label: "JSON" },
  jsonl: { suffix: ".jsonl", type: "application/x-ndjson", label: "JSONL" },
  csv: { suffix: ".csv", type: "text/csv", label: "CSV" },
  "json-v1": { suffix: "-legacy-v1.json", type: "application/json", label: "legacy V1 JSON" }
};

function serializeExport(publicSession, format) {
  if (format === "jsonl") return CUIRecorderCore.toJsonl(publicSession);
  if (format === "csv") return CUIRecorderCore.toCsv(publicSession);
  if (format === "json-v1") return JSON.stringify(CUIRecorderCore.toV1Session(publicSession), null, 2);
  return JSON.stringify(publicSession, null, 2);
}

async function exportSession(sessionId, format) {
  const spec = EXPORT_FORMATS[format];
  if (!spec) throw new Error(`Unsupported export format: ${format}`);
  message.textContent = "Preparing export…";
  const session = await request({ type: "RECORDER_GET_SESSION", sessionId });
  const publicSession = CUIRecorderCore.sanitizeSessionForExport(session);
  const base = `cui-trace-${session.metadata.task_id}-${session.metadata.actor_type}-${sessionId.slice(0, 8)}`;
  download(`${base}${spec.suffix}`, serializeExport(publicSession, format), spec.type);
  message.textContent = `Prepared ${spec.label} export and handed it to the browser's download flow.`;
}

async function inspectSession(card, sessionId) {
  const preview = card.querySelector(".event-preview");
  if (!preview.hidden) {
    preview.hidden = true;
    return;
  }
  const session = CUIRecorderCore.sanitizeSessionForExport(
    await request({ type: "RECORDER_GET_SESSION", sessionId })
  );
  const sample = session.events.length <= 10
    ? session.events
    : [...session.events.slice(0, 5), { omitted_event_count: session.events.length - 10 }, ...session.events.slice(-5)];
  preview.textContent = JSON.stringify({ metadata: session.metadata, summary: session.summary, event_sample: sample }, null, 2);
  preview.hidden = false;
}

async function render() {
  message.textContent = "Loading local sessions…";
  const sessions = await request({ type: "RECORDER_LIST_SESSIONS" });
  sessionsContainer.replaceChildren();
  if (!sessions.length) {
    const empty = document.createElement("p");
    empty.textContent = "No sessions yet. Start a recording from the toolbar popup.";
    sessionsContainer.appendChild(empty);
    message.textContent = "";
    return;
  }

  for (const metadata of sessions) {
    const fragment = template.content.cloneNode(true);
    const card = fragment.querySelector("article");
    card.dataset.sessionId = metadata.session_id;
    fragment.querySelector("h2").textContent = metadata.task_id;
    fragment.querySelector(".actor-chip").textContent = metadata.actor_type;
    fragment.querySelector(".session-id").textContent = metadata.session_id;
    populateDefinitionList(fragment.querySelector("dl"), metadata);
    sessionsContainer.appendChild(fragment);
  }
  message.textContent = `${sessions.length} session${sessions.length === 1 ? "" : "s"} stored locally.`;
}

async function handleSessionAction(event) {
  const button = event.target.closest("button");
  if (!button) return;
  const card = button.closest("article");
  const sessionId = card.dataset.sessionId;
  try {
    if (button.dataset.format) await exportSession(sessionId, button.dataset.format);
    if (button.dataset.action === "inspect") await inspectSession(card, sessionId);
    if (button.dataset.action === "delete") {
      if (!confirm("Permanently delete this local trace?")) return;
      await request({ type: "RECORDER_DELETE_SESSION", sessionId });
      await render();
    }
  } catch (error) {
    message.textContent = error.message;
  }
}

async function safeRender() {
  try {
    await render();
  } catch (error) {
    message.textContent = error.message;
  }
}

sessionsContainer.addEventListener("click", handleSessionAction);
document.getElementById("refresh").addEventListener("click", safeRender);
void safeRender();
