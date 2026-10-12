const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("../../app/src/core.js");

function baseEvent(overrides = {}) {
  return {
    eventId: "evt-1",
    type: "keydown",
    epochMs: 1000,
    monotonicMs: 10,
    url: "https://example.test/task?participant=secret#answer",
    viewportWidth: 1280,
    viewportHeight: 720,
    devicePixelRatio: 1.25,
    target: { tag: "input", id: "answer", inputType: "text", classes: ["field"] },
    key: "a",
    isTrusted: true,
    ...overrides
  };
}

test("privacy-safe mode removes printable keys and URL query data", () => {
  const event = core.normalizeEvent(baseEvent(), { rawKeyCapture: false });
  assert.equal(event.key, null);
  assert.equal(event.key_class, "letter");
  assert.equal(event.url, "https://example.test/task");
});

test("control keys remain useful in privacy-safe mode", () => {
  const event = core.normalizeEvent(baseEvent({ key: "Enter" }), { rawKeyCapture: false });
  assert.equal(event.key, "Enter");
  assert.equal(event.key_class, "control");
});

test("keyup events use the same privacy policy and retain only keyboard metadata", () => {
  const printable = core.normalizeEvent(
    baseEvent({
      type: "keyup",
      key: "q",
      keyboard: { repeat: true, location: 2, isComposing: true, holdMs: 37.1256 }
    }),
    { rawKeyCapture: false }
  );
  assert.equal(printable.key, null);
  assert.equal(printable.key_class, "letter");
  assert.deepEqual(printable.keyboard, {
    repeat: true,
    location: 2,
    is_composing: true,
    hold_ms: 37.126
  });

  const control = core.normalizeEvent(baseEvent({ type: "keyup", key: "Escape" }), {
    rawKeyCapture: false
  });
  assert.equal(control.key, "Escape");
  assert.equal(control.key_class, "control");

  printable.key = "must-not-survive";
  assert.equal(
    core.sanitizeNormalizedEvent(printable, { rawKeyCapture: false }).key,
    null
  );
});

test("rich interaction metadata is bounded and never stores transfer contents", () => {
  const normalized = core.normalizeEvent(
    baseEvent({
      type: "drop",
      pointer: {
        x: 20.25,
        y: 30.5,
        button: 0,
        buttons: 0,
        pointerType: "pen",
        pointerId: 12,
        width: 4.5,
        height: 6.25,
        pressure: 2,
        tangentialPressure: -2,
        tiltX: 120,
        tiltY: -120,
        twist: 400,
        isPrimary: true
      },
      input: { inputType: "insertFromDrop", dataLength: 27, isComposing: false },
      clipboard: {
        types: ["text/plain", "private clipboard phrase", "Files", "text/plain"],
        itemCount: 4,
        fileCount: 1,
        rawText: "must-not-survive"
      },
      transfer: {
        types: ["text/html", "private transfer phrase", "Files"],
        itemCount: 3,
        fileCount: 1,
        effectAllowed: "copyMove",
        dropEffect: "move",
        data: "must-not-survive"
      },
      touch: { activeCount: 2, changedCount: 1 },
      form: {
        method: "POST",
        action: "https://example.test/submit?answer=secret#fragment",
        controlCount: 5,
        hasSubmitter: true,
        submitterTag: "BUTTON",
        values: ["must-not-survive"]
      },
      documentState: { fullscreen: true, online: false }
    })
  );

  assert.deepEqual(normalized.clipboard, {
    types: ["text/plain", "other", "files"],
    item_count: 4,
    file_count: 1
  });
  assert.deepEqual(normalized.transfer, {
    types: ["text/html", "other", "files"],
    item_count: 3,
    file_count: 1,
    effect_allowed: "copymove",
    drop_effect: "move"
  });
  assert.equal(normalized.form.action, "https://example.test/submit");
  assert.equal(normalized.form.method, "post");
  assert.equal(normalized.pointer.pressure, 1);
  assert.equal(normalized.pointer.tangential_pressure, -1);
  assert.equal(normalized.pointer.tilt_x, 90);
  assert.equal(normalized.pointer.tilt_y, -90);
  assert.equal(normalized.pointer.twist, 359);
  assert.deepEqual(normalized.touch, { active_count: 2, changed_count: 1 });
  assert.deepEqual(normalized.document_state, { fullscreen: true, online: false });

  normalized.clipboard.raw_text = "must-not-survive";
  normalized.transfer.data = "must-not-survive";
  normalized.form.values = ["must-not-survive"];
  normalized.input.data = "must-not-survive";
  const sanitized = core.sanitizeNormalizedEvent(normalized);
  assert.equal(JSON.stringify(sanitized).includes("must-not-survive"), false);
});

test("password input type independently strips content-derived metadata", () => {
  const normalized = core.normalizeEvent(
    baseEvent({
      type: "paste",
      target: { tag: "input", inputType: "text" },
      input: { inputType: "insertFromPaste", dataLength: 8 },
      clipboard: { types: ["text/plain"], itemCount: 1, fileCount: 0 },
      transfer: { types: ["text/plain"], itemCount: 1, fileCount: 0 }
    })
  );
  normalized.target.input_type = "password";
  normalized.target.password_like = false;
  const sanitized = core.sanitizeNormalizedEvent(normalized);
  assert.equal(sanitized.target.password_like, true);
  assert.equal(sanitized.input, null);
  assert.equal(sanitized.clipboard, null);
  assert.equal(sanitized.transfer, null);
});

test("password-like targets are always redacted", () => {
  const event = core.normalizeEvent(
    baseEvent({
      target: { tag: "input", id: "account-123", classes: ["secret"], inputType: "password", passwordLike: true },
      key: "x",
      input: { inputType: "insertText", dataLength: 12 }
    }),
    { rawKeyCapture: true }
  );
  assert.equal(event.key, "<redacted>");
  assert.equal(event.input, null);
  assert.equal(event.target.password_like, true);
  assert.equal(event.target.selector_hint, "input");
});

test("background sanitizer removes input metadata from password-like targets", () => {
  const normalized = core.normalizeEvent(
    baseEvent({
      type: "beforeinput",
      target: { tag: "input", inputType: "password", passwordLike: true },
      input: { inputType: "insertText", dataLength: 12 }
    }),
    { rawKeyCapture: true }
  );
  normalized.input = { input_type: "insertText", data_length: 12 };
  const event = core.sanitizeNormalizedEvent(normalized, { rawKeyCapture: true });
  assert.equal(event.input, null);
  assert.equal(event.target.password_like, true);
});

test("background sanitizer re-enforces the metadata privacy mode", () => {
  const normalized = core.normalizeEvent(baseEvent(), { rawKeyCapture: true, includeQuery: true });
  normalized.unexpected_page_text = "must not be stored";
  const event = core.sanitizeNormalizedEvent(normalized, { rawKeyCapture: false, includeQuery: false });
  assert.equal(event.key, null);
  assert.equal(event.url, "https://example.test/task");
  assert.equal("unexpected_page_text" in event, false);
  assert.throws(
    () => core.sanitizeNormalizedEvent({ ...normalized, schema_version: "unknown" }),
    /schema_version/
  );
});

test("non-web URLs are never retained", () => {
  assert.equal(core.sanitizeUrl("javascript:alert(document.cookie)"), "unavailable");
  assert.equal(core.sanitizeUrl("chrome://settings"), "unavailable");
});

test("summary counts event types and pointer distance", () => {
  const summary = core.summarizeEvents([
    { type: "pointermove", epoch_ms: 1000, pointer: { x: 0, y: 0 } },
    { type: "pointermove", epoch_ms: 1100, pointer: { x: 3, y: 4 } },
    { type: "click", epoch_ms: 1200, pointer: { x: 3, y: 4 } }
  ]);
  assert.equal(summary.event_count, 3);
  assert.equal(summary.duration_ms, 200);
  assert.equal(summary.pointer_distance_px, 5);
  assert.deepEqual(summary.by_type, { pointermove: 2, click: 1 });
});

test("summary duration uses timestamp bounds rather than array order or truthiness", () => {
  const summary = core.summarizeEvents([
    { type: "click", epoch_ms: 100 },
    { type: "click", epoch_ms: 0 },
    { type: "click", epoch_ms: 50 }
  ]);
  assert.equal(summary.duration_ms, 100);
});

test("session input requires actor, task, and affirmative consent", () => {
  assert.throws(() => core.validateSessionInput({ actorType: "agent", taskId: "demo", consentConfirmed: false }), /consent/);
  const metadata = core.validateSessionInput({
    actorType: "agent",
    actorLabel: "agent-test",
    pairId: "pair-001",
    taskId: "demo",
    protocolVersion: "demo-v1",
    scopeOrigin: "https://example.test/path?q=1",
    consentConfirmed: true,
    disclosureVersion: "2026-08-03-v1"
  });
  assert.equal(metadata.actor_type, "agent");
  assert.equal(metadata.pair_id, "pair-001");
  assert.equal(metadata.protocol_version, "demo-v1");
  assert.equal(metadata.capture_adapter, "browser_mv3");
  assert.equal(metadata.environment_scope, "active_origin");
  assert.equal(metadata.source_class, "cui_agent");
  assert.equal(metadata.disclosure_version, "2026-08-03-v1");
  assert.equal(metadata.scope_origin, "https://example.test");
  assert.throws(
    () => core.validateSessionInput({ actorType: "agent", taskId: "demo", scopeOrigin: "chrome://settings", consentConfirmed: true }),
    /scopeOrigin/
  );
});

test("session source class is explicit, bounded, and backward compatible", () => {
  const shared = {
    taskId: "demo",
    scopeOrigin: "https://example.test",
    consentConfirmed: true
  };
  const human = core.validateSessionInput({ actorType: "human", ...shared });
  assert.equal(human.source_class, "human_self");
  const control = core.validateSessionInput({
    actorType: "agent",
    sourceClass: "scripted_automation_control",
    captureAdapter: "browser_mv3",
    environmentScope: "loopback_fixture",
    ...shared
  });
  assert.equal(control.source_class, "scripted_automation_control");
  assert.equal(control.capture_adapter, "browser_mv3");
  assert.equal(control.environment_scope, "loopback_fixture");
  assert.throws(
    () => core.validateSessionInput({ actorType: "agent", sourceClass: "human", ...shared }),
    /sourceClass/
  );
});

test("CSV export neutralizes spreadsheet formulas", () => {
  const csv = core.toCsv({
    metadata: { session_id: "s1", actor_type: "agent", actor_label: "=HYPERLINK(1)", task_id: "demo" },
    events: [{ type: "click", target: {} }]
  });
  assert.match(csv, /"'=HYPERLINK\(1\)"/);
});

test("CSV export keeps negative numbers numeric while still guarding strings", () => {
  const csv = core.toCsv({
    metadata: { session_id: "s1", actor_type: "agent", task_id: "-demo" },
    events: [
      { type: "pointerout", url: "https://x/", pointer: { x: -12.5, y: 3 }, key: "-", target: {} },
      { type: "pointerout", pointer: { x: -Infinity, y: Number.NaN }, key: "+1", target: {} }
    ]
  });
  const [, first, second] = csv.split("\r\n");
  assert.match(first, /,"https:\/\/x\/","-12\.5","3","'-",/);
  assert.match(first, /,"'-demo",/);
  assert.match(second, /,"'-Infinity","NaN","'\+1",/);
});

test("JSONL exporter cannot have its record type overwritten", () => {
  const lines = core
    .toJsonl({ metadata: { session_id: "s1" }, summary: {}, events: [{ record_type: "session", type: "click" }] })
    .split("\n")
    .map(JSON.parse);
  assert.equal(lines[0].record_type, "session");
  assert.equal(lines[1].record_type, "event");
});

test("research exports remove browser-internal tab identifiers", () => {
  const session = {
    metadata: {
      session_id: "s1",
      actor_type: "agent",
      task_id: "demo",
      tab_id: 42,
      known_step_ids: ["step-01"]
    },
    summary: { event_count: 1 },
    events: [{ event_id: "e1", type: "click" }]
  };
  const sanitized = core.sanitizeSessionForExport(session);
  assert.equal("tab_id" in sanitized.metadata, false);
  assert.equal("known_step_ids" in sanitized.metadata, false);
  assert.equal(session.metadata.tab_id, 42);
  const header = JSON.parse(core.toJsonl(session).split("\n")[0]);
  assert.equal("tab_id" in header.session, false);
});

test("V1 migration is deterministic, navigation-aware, and reversibly exportable", () => {
  const first = core.normalizeEvent(baseEvent({ eventId: "e-1", type: "click", epochMs: 1100, monotonicMs: 900 }));
  const navigation = core.normalizeEvent(baseEvent({ eventId: "e-2", type: "navigation", epochMs: 1200, monotonicMs: 2 }));
  const source = {
    metadata: {
      schema_version: "cui-trace-v1",
      session_id: "session-1",
      started_at: "1970-01-01T00:00:01.000Z",
      raw_key_capture: false,
      include_query: false,
      event_count: 2,
      chunk_count: 1
    },
    summary: {},
    events: [first, navigation]
  };
  const migrated = core.migrateSessionV1ToV2(source);
  assert.deepEqual(migrated.events.map((event) => event.event_seq), [1, 2]);
  assert.deepEqual(migrated.events.map((event) => event.document_epoch), [0, 1]);
  assert.deepEqual(migrated.events.map((event) => event.t_document_ms), [900, 2]);
  assert.ok(migrated.events[1].t_session_ms > migrated.events[0].t_session_ms);
  assert.equal(core.assertV2EventStream(migrated.events), true);
  assert.deepEqual(core.migrateSessionV1ToV2(source), migrated);

  const v1 = core.toV1Session(migrated);
  assert.equal(v1.metadata.schema_version, "cui-trace-v1");
  assert.equal(v1.events.every((event) => event.schema_version === "cui-trace-v1"), true);
  assert.equal(v1.events.some((event) => "t_session_ms" in event), false);
  assert.equal(v1.events.every((event) => !event.keyboard || !("hold_ms" in event.keyboard)), true);
  assert.equal(core.parseTrace(JSON.stringify(v1)).metadata.schema_version, "cui-trace-v2");
});

test("V2 integrity rejects duplicate ids, sequence gaps, and clock regressions", () => {
  const migrated = core.migrateSessionV1ToV2({
    metadata: {
      schema_version: "cui-trace-v1",
      session_id: "session-1",
      started_at: "1970-01-01T00:00:01.000Z",
      raw_key_capture: false,
      include_query: false,
      event_count: 2,
      chunk_count: 1
    },
    summary: {},
    events: [
      core.normalizeEvent(baseEvent({ eventId: "e-1", epochMs: 1000 })),
      core.normalizeEvent(baseEvent({ eventId: "e-2", epochMs: 1001 }))
    ]
  });
  const sequenceGap = structuredClone(migrated.events);
  sequenceGap[1].event_seq = 3;
  assert.throws(() => core.assertV2EventStream(sequenceGap), /contiguous/);
  const duplicate = structuredClone(migrated.events);
  duplicate[1].event_id = duplicate[0].event_id;
  assert.throws(() => core.assertV2EventStream(duplicate), /unique/);
  const regression = structuredClone(migrated.events);
  regression[1].t_session_ms = regression[0].t_session_ms;
  assert.throws(() => core.assertV2EventStream(regression), /strictly increasing/);
});

test("100k-event V1 migration preserves every event and global order", { timeout: 30000 }, () => {
  const template = core.normalizeEvent(baseEvent({ type: "pointermove", key: null }));
  const events = Array.from({ length: 100000 }, (_unused, index) => ({
    ...template,
    event_id: `stress-${index}`,
    epoch_ms: 1000 + index,
    monotonic_ms: index
  }));
  const migrated = core.migrateSessionV1ToV2({
    metadata: {
      schema_version: "cui-trace-v1",
      session_id: "stress-session",
      started_at: "1970-01-01T00:00:01.000Z",
      raw_key_capture: false,
      include_query: false,
      event_count: events.length,
      chunk_count: 200
    },
    summary: {},
    events
  });
  assert.equal(migrated.events.length, 100000);
  assert.equal(migrated.events.at(-1).event_seq, 100000);
  assert.equal(migrated.events.at(-1).t_session_ms, 99999);
  assert.equal(core.assertV2EventStream(migrated.events), true);
});

test("password-field keydown stores key_class=null and drops repeat, location, hold_ms, and modifiers", () => {
  const raw = baseEvent({
    type: "keydown",
    key: "Q",
    shiftKey: true,
    target: { tag: "input", id: "pw", inputType: "password", passwordLike: true },
    keyboard: { repeat: true, location: 1, isComposing: false, holdMs: 41 }
  });
  for (const rawKeyCapture of [false, true]) {
    const event = core.normalizeEvent(raw, { rawKeyCapture });
    assert.equal(event.key, "<redacted>");
    assert.equal(event.key_class, null);
    assert.deepEqual(event.keyboard, { repeat: null, location: null, is_composing: false, hold_ms: null });
    assert.deepEqual(event.modifiers, { alt: null, ctrl: null, meta: null, shift: null });
    assert.equal(event.target.password_like, true);
    assert.equal(event.target.selector_hint, "input");
  }
  const keyup = core.normalizeEvent({ ...raw, type: "keyup" });
  assert.equal(keyup.key_class, null);
  assert.equal(keyup.keyboard.hold_ms, null);
});

test("an input type of password alone marks the target private at capture time", () => {
  const event = core.normalizeEvent(
    baseEvent({
      key: "7",
      target: { tag: "input", id: "secret-id", classes: ["pin"], inputType: "password" },
      keyboard: { repeat: false, location: 0, isComposing: false, holdMs: 12 }
    })
  );
  assert.equal(event.target.password_like, true);
  assert.equal(event.target.selector_hint, "input");
  assert.equal(event.key_class, null);
  assert.equal(event.keyboard.hold_ms, null);
});

test("the background sanitizer strips forged key detail from private targets", () => {
  const forged = core.normalizeEvent(baseEvent({ keyboard: { repeat: false, location: 0, holdMs: 5 } }));
  forged.target.password_like = true;
  forged.key = "a";
  forged.key_class = "letter";
  forged.keyboard = { repeat: true, location: 2, is_composing: true, hold_ms: 99 };
  forged.modifiers = { alt: true, ctrl: false, meta: false, shift: true };
  const event = core.sanitizeNormalizedEvent(forged, { rawKeyCapture: true });
  assert.equal(event.key, "<redacted>");
  assert.equal(event.key_class, null);
  assert.deepEqual(event.keyboard, { repeat: null, location: null, is_composing: true, hold_ms: null });
  assert.deepEqual(event.modifiers, { alt: null, ctrl: null, meta: null, shift: null });
});

test("non-key events on private targets keep their modifier flags", () => {
  const click = core.normalizeEvent(
    baseEvent({ type: "click", key: undefined, shiftKey: true, target: { tag: "input", passwordLike: true } })
  );
  assert.deepEqual(click.modifiers, { alt: false, ctrl: false, meta: false, shift: true });
  assert.equal(click.key, null);
  assert.equal(click.keyboard, null);
});

test("private-target key events survive the V2 envelope and the V1 projection", () => {
  const event = core.normalizeEvent(
    baseEvent({
      eventId: "pw-1",
      target: { tag: "input", passwordLike: true },
      keyboard: { repeat: false, location: 0, isComposing: false, holdMs: 30 }
    })
  );
  const v2 = core.enrichEventV2(event, { eventSeq: 1, documentEpoch: 0, tSessionMs: 1, tDocumentMs: 10 });
  assert.deepEqual(v2.keyboard, { repeat: null, location: null, is_composing: false, hold_ms: null });
  const v1 = core.toV1Session({ metadata: { schema_version: "cui-trace-v2" }, summary: {}, events: [v2] });
  assert.deepEqual(v1.events[0].keyboard, { repeat: null, location: null, is_composing: false });
  assert.equal(v1.events[0].key_class, null);
});

test("capture normalization and background sanitization share one idempotent field table", () => {
  const raws = [
    baseEvent({ keyboard: { repeat: true, location: 3, isComposing: true, holdMs: 12.34567 } }),
    baseEvent({ type: "keyup", key: "F5", keyboard: { repeat: false, location: 0, holdMs: 7 } }),
    baseEvent({
      type: "drop",
      pointer: { x: 1.23456, y: -2, button: 0, buttons: 1, pointerType: "pen", pressure: 0.5 },
      transfer: { types: ["text/plain", "custom"], itemCount: 2, fileCount: 0, effectAllowed: "all", dropEffect: "copy" },
      clipboard: { types: ["Files"], itemCount: 1, fileCount: 1 },
      input: { inputType: "insertFromDrop", dataLength: 4, isComposing: false },
      touch: { activeCount: 1, changedCount: 1 },
      form: { method: "GET", action: "https://example.test/a?b=c", controlCount: 2, hasSubmitter: false },
      documentState: { fullscreen: false, online: true },
      wheel: { deltaX: 1, deltaY: -3.25, deltaMode: 0 },
      scroll: { x: 5, y: 600 }
    }),
    baseEvent({ type: "keydown", target: { tag: "input", passwordLike: true }, keyboard: { repeat: true } })
  ];
  for (const raw of raws) {
    for (const options of [{}, { rawKeyCapture: true }]) {
      const normalized = core.normalizeEvent(raw, options);
      assert.deepEqual(core.sanitizeNormalizedEvent(normalized, options), normalized);
    }
  }
  const missingId = core.normalizeEvent({ type: "click" });
  assert.equal(missingId.event_id, null);
  assert.throws(() => core.sanitizeNormalizedEvent(missingId), /event_id is required/);
});

test("the shared step-id pattern accepts opaque tokens only", () => {
  assert.equal(core.STEP_ID_PATTERN.test("step-01:a.b_c"), true);
  for (const value of ["", "-leading", "has space", "x".repeat(129), "step/1"]) {
    assert.equal(core.STEP_ID_PATTERN.test(value), false, value);
  }
});
