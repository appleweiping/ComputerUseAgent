const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const core = require("../../app/src/core.js");
const { FakeElement, FakeFragment, el } = require("./helpers/fake-dom.js");

const DASHBOARD_PATH = path.resolve(__dirname, "../../app/dashboard/dashboard.js");
const DASHBOARD_SOURCE = fs.readFileSync(DASHBOARD_PATH, "utf8");

function sessionCardFragment() {
  return new FakeFragment([
    el("article", { className: "session-card" }, [
      el("div", { className: "session-main" }, [
        el("div", { className: "session-title-row" }, [el("h2"), el("span", { className: "actor-chip" })]),
        el("p", { className: "session-id" }),
        el("dl")
      ]),
      el("div", { className: "actions" }, [
        el("button", { dataset: { action: "inspect" }, text: "Inspect" }),
        el("button", { dataset: { format: "json" }, text: "JSON" }),
        el("button", { dataset: { format: "jsonl" }, text: "JSONL" }),
        el("button", { dataset: { format: "csv" }, text: "CSV" }),
        el("button", { dataset: { format: "json-v1" }, text: "Legacy V1 JSON" }),
        el("button", { dataset: { format: "xml" }, text: "Unsupported" }),
        el("button", { dataset: { action: "delete" }, className: "delete", text: "Delete" })
      ]),
      el("pre", { className: "event-preview", hidden: true })
    ])
  ]);
}

function metadataFor(sessionId, overrides = {}) {
  return {
    schema_version: "cui-trace-v2",
    session_id: sessionId,
    task_id: "task-1",
    actor_type: "agent",
    executor: "puppeteer",
    executor_version: "25.4.0",
    raw_key_capture: false,
    started_at: "2026-10-11T00:00:00.000Z",
    status: "complete",
    event_count: 2,
    tab_id: 7,
    known_step_ids: ["step-01"],
    ...overrides
  };
}

function eventsFor(count) {
  return Array.from({ length: count }, (_value, index) => ({
    schema_version: "cui-trace-v2",
    event_id: `e-${index}`,
    type: "click",
    event_seq: index + 1,
    epoch_ms: 1000 + index,
    t_session_ms: index,
    pointer: { x: index, y: 0 },
    target: { tag: "button" }
  }));
}

async function settle() {
  for (let index = 0; index < 5; index += 1) await new Promise((resolve) => setImmediate(resolve));
}

async function loadDashboard(options = {}) {
  let sessions = options.sessions ?? [metadataFor("12345678-aaaa"), metadataFor("87654321-bbbb", { status: "interrupted", capture_error: "tab closed", raw_key_capture: true })];
  const messages = [];
  const downloads = [];
  const objectUrls = [];
  const revoked = [];
  const confirms = [];
  const body = new FakeElement("body");
  const elements = {
    sessions: new FakeElement("section", { id: "sessions" }),
    message: new FakeElement("p", { id: "message" }),
    refresh: new FakeElement("button", { id: "refresh" }),
    "session-template": Object.assign(new FakeElement("template"), {
      content: { cloneNode: () => sessionCardFragment() }
    })
  };
  const responders = {
    RECORDER_LIST_SESSIONS: () => ({ ok: true, data: sessions }),
    RECORDER_GET_SESSION: (message) => ({
      ok: true,
      data: {
        metadata: sessions.find((row) => row.session_id === message.sessionId),
        summary: { event_count: options.eventCount ?? 2 },
        events: eventsFor(options.eventCount ?? 2)
      }
    }),
    RECORDER_DELETE_SESSION: (message) => {
      sessions = sessions.filter((row) => row.session_id !== message.sessionId);
      return { ok: true, data: { deleted: message.sessionId } };
    },
    ...(options.responders || {})
  };
  const document = {
    body,
    getElementById: (id) => elements[id] || null,
    createElement(tagName) {
      const element = new FakeElement(tagName);
      if (tagName === "a") {
        element.click = () => downloads.push({ name: element.download, href: element.href, hidden: element.hidden });
      }
      return element;
    }
  };
  const context = {
    Blob,
    CUIRecorderCore: core,
    URL: {
      createObjectURL(blob) {
        objectUrls.push(blob);
        return `blob:fake/${objectUrls.length}`;
      },
      revokeObjectURL(url) { revoked.push(url); }
    },
    chrome: {
      runtime: {
        async sendMessage(message) {
          messages.push(structuredClone(message));
          return responders[message.type]?.(message);
        }
      }
    },
    confirm(text) {
      confirms.push(text);
      return options.confirmResult !== false;
    },
    console,
    document,
    setTimeout(callback) { callback(); return 1; }
  };
  vm.runInNewContext(DASHBOARD_SOURCE, context, { filename: DASHBOARD_PATH });
  await settle();
  const cards = () => elements.sessions.querySelectorAll("article");
  return {
    body,
    cards,
    confirms,
    downloads,
    elements,
    messages,
    objectUrls,
    revoked,
    async clickIn(card, selector) {
      await card.querySelector(selector).click();
      await settle();
    }
  };
}

test("the dashboard lists stored sessions with sanitized metadata rows", async () => {
  const dashboard = await loadDashboard();
  const cards = dashboard.cards();
  assert.equal(cards.length, 2);
  assert.equal(cards[0].dataset.sessionId, "12345678-aaaa");
  assert.equal(cards[0].querySelector("h2").textContent, "task-1");
  assert.equal(cards[0].querySelector(".actor-chip").textContent, "agent");
  const secondRows = cards[1].querySelector("dl").textContent;
  assert.match(secondRows, /Capture note/);
  assert.match(secondRows, /tab closed/);
  assert.match(secondRows, /raw keys/);
  assert.equal(dashboard.elements.message.textContent, "2 sessions stored locally.");
});

test("an empty store renders a hint without using innerHTML", async () => {
  const dashboard = await loadDashboard({ sessions: [] });
  assert.equal(dashboard.cards().length, 0);
  assert.match(dashboard.elements.sessions.textContent, /No sessions yet/);
  assert.equal(dashboard.elements.message.textContent, "");
  const single = await loadDashboard({ sessions: [metadataFor("only-one")] });
  assert.equal(single.elements.message.textContent, "1 session stored locally.");
});

test("exports are sanitized, named per format, and handed to the browser", async () => {
  const dashboard = await loadDashboard();
  const card = dashboard.cards()[0];
  const expected = [
    ["json", "cui-trace-task-1-agent-12345678.json", "application/json"],
    ["jsonl", "cui-trace-task-1-agent-12345678.jsonl", "application/x-ndjson"],
    ["csv", "cui-trace-task-1-agent-12345678.csv", "text/csv"],
    ["json-v1", "cui-trace-task-1-agent-12345678-legacy-v1.json", "application/json"]
  ];
  for (const [format, name, type] of expected) {
    await dashboard.clickIn(card, `[data-format="${format}"]`);
    const download = dashboard.downloads.at(-1);
    assert.equal(download.name, name);
    assert.equal(download.hidden, true);
    const blob = dashboard.objectUrls.at(-1);
    assert.equal(blob.type, type);
    const text = await blob.text();
    assert.equal(text.includes("tab_id"), false, format);
    assert.equal(text.includes("known_step_ids"), false, format);
    assert.match(dashboard.elements.message.textContent, /handed it to the browser's download flow/);
  }
  const json = JSON.parse(await dashboard.objectUrls[0].text());
  assert.equal(json.metadata.session_id, "12345678-aaaa");
  assert.equal(json.events.length, 2);
  const legacy = JSON.parse(await dashboard.objectUrls[3].text());
  assert.equal(legacy.metadata.schema_version, "cui-trace-v1");
  assert.equal(dashboard.revoked.length, 4);
  assert.equal(dashboard.body.children.length, 0, "temporary anchors are removed");

  await dashboard.clickIn(card, '[data-format="xml"]');
  assert.equal(dashboard.elements.message.textContent, "Unsupported export format: xml");
});

test("inspect toggles a sanitized, sampled preview", async () => {
  const dashboard = await loadDashboard({ eventCount: 14 });
  const card = dashboard.cards()[0];
  const preview = card.querySelector(".event-preview");
  await dashboard.clickIn(card, '[data-action="inspect"]');
  assert.equal(preview.hidden, false);
  const shown = JSON.parse(preview.textContent);
  assert.equal("tab_id" in shown.metadata, false);
  assert.equal(shown.event_sample.length, 11);
  assert.deepEqual(shown.event_sample[5], { omitted_event_count: 4 });
  await dashboard.clickIn(card, '[data-action="inspect"]');
  assert.equal(preview.hidden, true);

  const small = await loadDashboard({ eventCount: 3 });
  const smallCard = small.cards()[0];
  await small.clickIn(smallCard, '[data-action="inspect"]');
  assert.equal(JSON.parse(smallCard.querySelector(".event-preview").textContent).event_sample.length, 3);
});

test("delete asks for confirmation, then removes the card", async () => {
  const declined = await loadDashboard({ confirmResult: false });
  await declined.clickIn(declined.cards()[0], '[data-action="delete"]');
  assert.equal(declined.messages.some((message) => message.type === "RECORDER_DELETE_SESSION"), false);
  assert.equal(declined.confirms.length, 1);

  const accepted = await loadDashboard();
  await accepted.clickIn(accepted.cards()[0], '[data-action="delete"]');
  const deletes = accepted.messages.filter((message) => message.type === "RECORDER_DELETE_SESSION");
  assert.deepEqual(deletes.map((message) => message.sessionId), ["12345678-aaaa"]);
  assert.equal(accepted.cards().length, 1);
  assert.equal(accepted.elements.message.textContent, "1 session stored locally.");
});

test("request failures are shown and clicks outside buttons are ignored", async () => {
  const failing = await loadDashboard({
    responders: { RECORDER_GET_SESSION: () => ({ ok: false, error: "Session not found" }) }
  });
  await failing.clickIn(failing.cards()[0], '[data-format="json"]');
  assert.equal(failing.elements.message.textContent, "Session not found");
  const before = failing.messages.length;
  await failing.cards()[0].querySelector("h2").click();
  assert.equal(failing.messages.length, before);

  const unavailable = await loadDashboard({ responders: { RECORDER_LIST_SESSIONS: () => undefined } });
  assert.equal(unavailable.elements.message.textContent, "Recorder request failed");
  await unavailable.elements.refresh.click();
  await settle();
  assert.equal(unavailable.messages.filter((message) => message.type === "RECORDER_LIST_SESSIONS").length, 2);
});
