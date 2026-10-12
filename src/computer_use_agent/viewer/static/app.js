"use strict";

const MODALITY_COLORS = {
  pointer: "var(--pointer)",
  keyboard: "var(--keyboard)",
  focus: "var(--focus)",
  scroll: "var(--scroll)",
  touch: "var(--touch)",
  other: "var(--other)",
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function number(value, digits = 3) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: digits }).format(value);
}

function duration(ms) {
  if (ms >= 60000) return `${number(ms / 60000, 2)} min`;
  if (ms >= 1000) return `${number(ms / 1000, 2)} s`;
  return `${number(ms, 1)} ms`;
}

function addDefinition(list, term, value) {
  list.append(element("dt", "", term), element("dd", "", value));
}

function renderSessions(traces) {
  const container = document.querySelector("#session-cards");
  container.replaceChildren();
  document.querySelector("#session-count").textContent = `${traces.length} local projection${traces.length === 1 ? "" : "s"}`;
  for (const trace of traces) {
    const card = element("article", "session-card");
    card.append(element("h3", "", trace.name));
    const status = element("span", `status${trace.status === "complete" ? "" : " error"}`, trace.status);
    card.append(status);
    const list = element("dl");
    addDefinition(list, "Role", trace.role);
    addDefinition(list, "Task ref", trace.task_ref);
    addDefinition(list, "Adapter", trace.capture_adapter);
    addDefinition(list, "Events", number(trace.event_count, 0));
    addDefinition(list, "Duration", duration(trace.duration_ms));
    if (trace.validator) {
      const outcome = trace.validator.passed === null ? (trace.validator.status || "unknown") : (trace.validator.passed ? "pass" : "fail");
      addDefinition(list, "Validator", outcome);
    }
    card.append(list);
    const reason = trace.failure_reason || (trace.validator && trace.validator.failure_reason);
    if (reason) card.append(element("p", "failure", reason));
    container.append(card);
  }
}

function populateSelectors(traces) {
  const left = document.querySelector("#left-session");
  const right = document.querySelector("#right-session");
  for (const select of [left, right]) {
    select.replaceChildren();
    traces.forEach((trace, index) => {
      const option = element("option", "", trace.name);
      option.value = String(index);
      select.append(option);
    });
  }
  right.value = String(Math.min(1, traces.length - 1));
  return { left, right };
}

function comparisonRow(container, label, left, right) {
  container.append(
    element("div", "comparison-label", label),
    element("div", "", left),
    element("div", "", right),
  );
}

function renderComparison(traces, leftIndex, rightIndex) {
  const left = traces[leftIndex];
  const right = traces[rightIndex];
  const container = document.querySelector("#pair-summary");
  container.replaceChildren();
  comparisonRow(container, "Session", left.name, right.name);
  comparisonRow(container, "Role", left.role, right.role);
  comparisonRow(container, "Task ref", left.task_ref, right.task_ref);
  comparisonRow(container, "Events", number(left.event_count, 0), number(right.event_count, 0));
  comparisonRow(container, "Duration", duration(left.duration_ms), duration(right.duration_ms));
  const modalities = Object.keys(MODALITY_COLORS);
  for (const modality of modalities) {
    comparisonRow(
      container,
      `${modality} events`,
      number(left.modality_counts[modality] || 0, 0),
      number(right.modality_counts[modality] || 0, 0),
    );
  }
}

function renderLegend() {
  const legend = document.querySelector("#legend");
  legend.replaceChildren();
  for (const [modality, color] of Object.entries(MODALITY_COLORS)) {
    const item = element("span", "", modality);
    item.style.setProperty("--legend-color", color);
    legend.append(item);
  }
}

function renderTimelines(traces) {
  const container = document.querySelector("#timelines");
  container.replaceChildren();
  for (const trace of traces) {
    const panel = element("article", "timeline-panel");
    const heading = element("div", "timeline-heading");
    const samplingNote = trace.timeline_sampling === "none" ? "" : ` · showing ${number(trace.timeline_display_count, 0)}`;
    heading.append(element("strong", "", trace.name), element("span", "muted", `${number(trace.event_count, 0)} events${samplingNote} · ${duration(trace.duration_ms)}`));
    panel.append(heading);
    if (!trace.timeline.length) {
      panel.append(element("div", "timeline-empty", "No observed events"));
      container.append(panel);
      continue;
    }
    const track = element("div", "timeline-track");
    track.setAttribute("role", "img");
    track.setAttribute("aria-label", `${trace.name}: relative event timeline with ${trace.event_count} events`);
    const maximum = Math.max(trace.duration_ms, ...trace.timeline.map((event) => event.relative_ms), 1);
    for (const event of trace.timeline) {
      const mark = element("span", "event-mark");
      mark.style.left = `${Math.min(100, Math.max(0, event.relative_ms / maximum * 100))}%`;
      mark.style.setProperty("--mark-color", MODALITY_COLORS[event.modality] || MODALITY_COLORS.other);
      mark.setAttribute("aria-hidden", "true");
      mark.title = `${event.type} · ${duration(event.relative_ms)} · epoch ${event.document_epoch}`;
      track.append(mark);
    }
    panel.append(track);
    const axis = element("div", "timeline-axis");
    axis.append(element("span", "", "0"), element("span", "", duration(maximum)));
    panel.append(axis);
    container.append(panel);
  }
}

function renderDefinitionObject(selector, payload) {
  const list = document.querySelector(selector);
  list.replaceChildren();
  for (const [key, value] of Object.entries(payload || {})) {
    addDefinition(list, key.replaceAll("_", " "), number(value));
  }
}

function renderResult(result) {
  if (!result) return;
  document.querySelector("#result-section").hidden = false;
  document.querySelector("#claim-status").textContent = result.claim_status || "Exploratory analysis only.";
  const metrics = document.querySelector("#metrics");
  metrics.replaceChildren();
  for (const [key, value] of Object.entries(result.primary_metrics)) {
    if (["count", "threshold"].includes(key)) continue;
    const card = element("article", "metric");
    card.append(element("div", "value", number(value)), element("div", "label", key.replaceAll("_", " ")));
    metrics.append(card);
  }
  renderDefinitionObject("#task-control", result.task_control);
  renderDefinitionObject("#permutation-control", result.label_permutation);
  renderBootstrap(result.bootstrap_intervals_95);
  renderDescriptive(result.task_descriptive);
  renderTrajectoryBinding(result.trajectory_artifact);
}

function renderBootstrap(intervals) {
  const list = document.querySelector("#bootstrap-intervals");
  list.replaceChildren();
  for (const [metric, bounds] of Object.entries(intervals || {})) {
    if (!bounds) continue;
    addDefinition(list, metric.replaceAll("_", " "), `${number(bounds.lower)} to ${number(bounds.upper)}`);
  }
  if (!list.children.length) addDefinition(list, "intervals", "not reported");
}

function cell(row, value) {
  row.append(element("td", "", value));
}

function renderDescriptiveTask(descriptive, taskRef) {
  const body = document.querySelector("#descriptive-table tbody");
  body.replaceChildren();
  const task = descriptive.by_task[taskRef];
  if (!task) return;
  const human = (task.labels && task.labels.human) || {};
  const agent = (task.labels && task.labels.agent) || {};
  const effects = task.human_minus_agent_hedges_g || {};
  for (const feature of descriptive.feature_names || []) {
    const row = element("tr");
    row.append(element("th", "", feature));
    row.lastChild.setAttribute("scope", "row");
    const h = human[feature] || {};
    const a = agent[feature] || {};
    cell(row, number(h.n, 0));
    cell(row, number(h.mean));
    cell(row, number(h.median));
    cell(row, number(a.n, 0));
    cell(row, number(a.mean));
    cell(row, number(a.median));
    cell(row, number(effects[feature]));
    body.append(row);
  }
}

function renderDescriptive(descriptive) {
  const select = document.querySelector("#descriptive-task");
  select.replaceChildren();
  if (!descriptive || !descriptive.by_task) return;
  const tasks = Object.keys(descriptive.by_task);
  for (const taskRef of tasks) {
    const option = element("option", "", taskRef);
    option.value = taskRef;
    select.append(option);
  }
  const update = () => renderDescriptiveTask(descriptive, select.value);
  select.addEventListener("change", update);
  if (tasks.length) update();
}

function renderTrajectoryBinding(binding) {
  const note = document.querySelector("#trajectory-binding");
  if (!binding || typeof binding.sha256 !== "string") {
    note.textContent = "";
    return;
  }
  const raw = binding.privacy && binding.privacy.raw_event_sequences_embedded ? "yes" : "no";
  note.textContent = `Aggregate profile ${binding.artifact_name} is bound by SHA-256 ${binding.sha256.slice(0, 16)}… (raw event sequences embedded: ${raw}). The viewer never loads an event sequence from it.`;
}

async function start() {
  const response = await fetch("/api/state", { cache: "no-store", credentials: "same-origin" });
  if (!response.ok) throw new Error(`State request failed: ${response.status}`);
  const state = await response.json();
  if (state.schema_version !== "cui-local-view-v1" || !Array.isArray(state.traces) || !state.traces.length) {
    throw new Error("Unexpected viewer state schema");
  }
  document.querySelector("#privacy-mode").textContent = "Privacy projection · no raw payloads";
  renderSessions(state.traces);
  renderLegend();
  renderTimelines(state.traces);
  const { left, right } = populateSelectors(state.traces);
  const update = () => renderComparison(state.traces, Number(left.value), Number(right.value));
  left.addEventListener("change", update);
  right.addEventListener("change", update);
  update();
  renderResult(state.detector_result);
}

start().catch((error) => {
  document.querySelector("#privacy-mode").textContent = "Viewer failed closed";
  document.querySelector("#claim-status").textContent = error instanceof Error ? error.message : "Unable to load viewer state";
});
