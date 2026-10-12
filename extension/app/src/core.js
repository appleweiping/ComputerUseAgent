(function initRecorderCore(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.CUIRecorderCore = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function recorderCoreFactory() {
  "use strict";

  // V1 remains a supported import/export format. New extension and X11
  // captures use V2, whose envelope adds a session-global ordering clock
  // without removing any V1 measurement fields.
  const LEGACY_SCHEMA_VERSION = "cui-trace-v1";
  const CURRENT_SCHEMA_VERSION = "cui-trace-v2";
  const SCHEMA_VERSION = LEGACY_SCHEMA_VERSION;
  const SUPPORTED_SCHEMA_VERSIONS = new Set([
    LEGACY_SCHEMA_VERSION,
    CURRENT_SCHEMA_VERSION
  ]);
  const V2_EVENT_FIELDS = new Set([
    "event_seq",
    "document_epoch",
    "t_session_ms",
    "t_document_ms",
    "step_id"
  ]);
  const V2_METADATA_FIELDS = new Set([
    "timing_model",
    "event_seq_last",
    "document_epoch_count",
    "dropped_event_count",
    "rejected_event_count",
    "flush_failure_count",
    "quota_rejection_count"
  ]);
  // Opaque step token shared by the content script, background worker and exports.
  const STEP_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
  const KEY_EVENT_TYPES = new Set(["keydown", "keyup"]);
  const KEY_CLASS_VALUES = new Set(["control", "whitespace", "letter", "digit", "punctuation", "other"]);
  const SAFE_TRANSFER_TYPES = new Set([
    "application/json",
    "application/pdf",
    "application/xml",
    "application/zip",
    "files",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/svg+xml",
    "image/webp",
    "text",
    "text/csv",
    "text/html",
    "text/plain",
    "text/rtf",
    "text/uri-list"
  ]);
  const EFFECT_ALLOWED_VALUES = new Set([
    "all",
    "copy",
    "copylink",
    "copymove",
    "link",
    "linkmove",
    "move",
    "none",
    "uninitialized"
  ]);
  const DROP_EFFECT_VALUES = new Set(["copy", "link", "move", "none"]);
  const FORM_METHOD_VALUES = new Set(["dialog", "get", "post"]);
  const SOURCE_CLASS_VALUES = new Set([
    "human_self",
    "cui_agent",
    "scripted_automation_control",
    "synthetic_coverage"
  ]);
  const SPECIAL_KEYS = new Set([
    "Alt",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "ArrowUp",
    "Backspace",
    "CapsLock",
    "Control",
    "Delete",
    "End",
    "Enter",
    "Escape",
    "Home",
    "Insert",
    "Meta",
    "PageDown",
    "PageUp",
    "Shift",
    "Tab"
  ]);

  function clampNumber(value, min, max, fallback = 0) {
    const number = Number(value);
    if (!Number.isFinite(number)) return fallback;
    return Math.min(max, Math.max(min, number));
  }

  function safeToken(value, maxLength = 80) {
    return String(value || "")
      .replace(/[^a-zA-Z0-9_.:-]/g, "_")
      .slice(0, maxLength);
  }

  function safeStepId(value) {
    const raw = String(value || "").trim();
    return STEP_ID_PATTERN.test(raw) ? raw : null;
  }

  function boundedNumberOrNull(value, min, max, precision = 0) {
    if (value === null || value === undefined || value === "") return null;
    const number = Number(value);
    if (!Number.isFinite(number)) return null;
    const multiplier = 10 ** precision;
    return Math.round(clampNumber(number, min, max, 0) * multiplier) / multiplier;
  }

  function safeTransferTypeList(value, maxItems = 16) {
    if (!Array.isArray(value)) return [];
    return [...new Set(
      value
        .slice(0, maxItems)
        .map((item) => String(item || "").trim().toLowerCase())
        .map((item) => (SAFE_TRANSFER_TYPES.has(item) ? item : "other"))
    )];
  }

  function safeEnum(value, allowedValues) {
    const normalized = String(value || "").trim().toLowerCase();
    return allowedValues.has(normalized) ? normalized : null;
  }

  function optionalBoolean(value) {
    return typeof value === "boolean" ? value : null;
  }

  function sanitizeUrl(value, includeQuery = false) {
    try {
      const parsed = new URL(String(value));
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return "unavailable";
      const base = `${parsed.origin}${parsed.pathname}`;
      return includeQuery ? `${base}${parsed.search}` : base;
    } catch (_error) {
      return "unavailable";
    }
  }

  function classifyKey(key) {
    const value = String(key || "");
    if (SPECIAL_KEYS.has(value) || /^F\d{1,2}$/.test(value)) return "control";
    if (value === " ") return "whitespace";
    if (/^[a-zA-Z]$/.test(value)) return "letter";
    if (/^[0-9]$/.test(value)) return "digit";
    if (value.length === 1) return "punctuation";
    return "other";
  }

  function describeTarget(target) {
    const source = target || {};
    const tag = safeToken(source.tag || "unknown", 24).toLowerCase();
    const passwordLike = Boolean(source.passwordLike);
    const id = passwordLike ? "" : safeToken(source.id, 48);
    const classList = !passwordLike && Array.isArray(source.classes)
      ? source.classes.slice(0, 3).map((item) => safeToken(item, 32)).filter(Boolean)
      : [];
    const hint = [tag, id ? `#${id}` : "", ...classList.map((name) => `.${name}`)].join("");
    return {
      tag,
      role: safeToken(source.role, 40) || null,
      input_type: safeToken(source.inputType, 24).toLowerCase() || null,
      selector_hint: hint.slice(0, 120),
      password_like: passwordLike
    };
  }

  function isAllowedControlKey(value) {
    const key = String(value || "");
    return SPECIAL_KEYS.has(key) || /^F\d{1,2}$/.test(key);
  }

  // Keyboard detail for a private target (password field, autocomplete
  // current-password/new-password, or anything inside [data-recorder-private]).
  // The key identity, key class, auto-repeat flag, physical key location and
  // hold duration are not recorded; only the IME-composition flag remains.
  function privateKeyboard(isComposing) {
    return { repeat: null, location: null, is_composing: Boolean(isComposing), hold_ms: null };
  }

  // Modifier state on a private-target key event would reveal which
  // characters were typed with Shift/AltGr, so it is not recorded either.
  const PRIVATE_MODIFIERS = Object.freeze({ alt: null, ctrl: null, meta: null, shift: null });

  // The single field table shared by capture-time normalization
  // (normalizeEvent) and the background re-sanitization (sanitizeNormalizedEvent).
  // `source` uses the exported snake_case field names; every value is treated
  // as untrusted and bounded again, so the function is idempotent.
  function sanitizeEventFields(source, eventId, options) {
    const type = safeToken(source.type, 40).toLowerCase() || "unknown";
    const isKeyEvent = KEY_EVENT_TYPES.has(type);
    const sourceTarget = source.target || {};
    const sourceInputType = safeToken(sourceTarget.input_type, 24).toLowerCase();
    const passwordLike = Boolean(sourceTarget.password_like) || sourceInputType === "password";
    const tag = safeToken(sourceTarget.tag || "unknown", 24).toLowerCase();
    const keyClass = isKeyEvent && !passwordLike && KEY_CLASS_VALUES.has(source.key_class)
      ? source.key_class
      : null;
    const rawKeyAllowed = Boolean(options.rawKeyCapture) && !passwordLike;
    let key = null;
    if (isKeyEvent) {
      if (passwordLike) key = "<redacted>";
      else if (rawKeyAllowed) key = String(source.key || "").slice(0, 24);
      else if (keyClass === "control" && isAllowedControlKey(source.key)) key = String(source.key).slice(0, 24);
    }
    let keyboard = null;
    if (isKeyEvent && source.keyboard) {
      keyboard = passwordLike
        ? privateKeyboard(source.keyboard.is_composing)
        : {
            repeat: Boolean(source.keyboard.repeat),
            location: Math.round(clampNumber(source.keyboard.location, 0, 3, 0)),
            is_composing: Boolean(source.keyboard.is_composing),
            hold_ms: boundedNumberOrNull(source.keyboard.hold_ms, 0, 600000, 3)
          };
    }
    const modifiers = isKeyEvent && passwordLike
      ? { ...PRIVATE_MODIFIERS }
      : {
          alt: Boolean(source.modifiers?.alt),
          ctrl: Boolean(source.modifiers?.ctrl),
          meta: Boolean(source.modifiers?.meta),
          shift: Boolean(source.modifiers?.shift)
        };

    const selectorHint = passwordLike
      ? tag
      : String(sourceTarget.selector_hint || tag)
          .replace(/[^a-zA-Z0-9_.:#-]/g, "_")
          .slice(0, 120);

    return {
      schema_version: source.schema_version,
      event_id: eventId,
      type,
      epoch_ms: Math.round(clampNumber(source.epoch_ms, 0, Number.MAX_SAFE_INTEGER, Date.now())),
      monotonic_ms: Math.round(clampNumber(source.monotonic_ms, 0, Number.MAX_SAFE_INTEGER, 0) * 1000) / 1000,
      url: sanitizeUrl(source.url, Boolean(options.includeQuery)),
      viewport: {
        width: Math.round(clampNumber(source.viewport?.width, 0, 100000, 0)),
        height: Math.round(clampNumber(source.viewport?.height, 0, 100000, 0)),
        device_pixel_ratio: Math.round(clampNumber(source.viewport?.device_pixel_ratio, 0.1, 20, 1) * 1000) / 1000
      },
      pointer: source.pointer
        ? {
            x: Math.round(clampNumber(source.pointer.x, -100000, 100000, 0) * 1000) / 1000,
            y: Math.round(clampNumber(source.pointer.y, -100000, 100000, 0) * 1000) / 1000,
            button: Math.round(clampNumber(source.pointer.button, -1, 8, -1)),
            buttons: Math.round(clampNumber(source.pointer.buttons, 0, 31, 0)),
            pointer_type: safeToken(source.pointer.pointer_type, 24) || null,
            pointer_id: boundedNumberOrNull(source.pointer.pointer_id, 0, 1000000),
            width: boundedNumberOrNull(source.pointer.width, 0, 100000, 3),
            height: boundedNumberOrNull(source.pointer.height, 0, 100000, 3),
            pressure: boundedNumberOrNull(source.pointer.pressure, 0, 1, 4),
            tangential_pressure: boundedNumberOrNull(
              source.pointer.tangential_pressure,
              -1,
              1,
              4
            ),
            tilt_x: boundedNumberOrNull(source.pointer.tilt_x, -90, 90),
            tilt_y: boundedNumberOrNull(source.pointer.tilt_y, -90, 90),
            twist: boundedNumberOrNull(source.pointer.twist, 0, 359),
            is_primary: optionalBoolean(source.pointer.is_primary)
          }
        : null,
      wheel: source.wheel
        ? {
            delta_x: Math.round(clampNumber(source.wheel.delta_x, -100000, 100000, 0) * 1000) / 1000,
            delta_y: Math.round(clampNumber(source.wheel.delta_y, -100000, 100000, 0) * 1000) / 1000,
            delta_mode: Math.round(clampNumber(source.wheel.delta_mode, 0, 2, 0))
          }
        : null,
      scroll: source.scroll
        ? {
            x: Math.round(clampNumber(source.scroll.x, 0, 10000000, 0)),
            y: Math.round(clampNumber(source.scroll.y, 0, 10000000, 0))
          }
        : null,
      key,
      key_class: keyClass,
      keyboard,
      modifiers,
      input: source.input && !passwordLike
        ? {
            input_type: safeToken(source.input.input_type, 40) || null,
            data_length: Math.round(clampNumber(source.input.data_length, 0, 1000000, 0)),
            is_composing: Boolean(source.input.is_composing)
          }
        : null,
      clipboard: source.clipboard && !passwordLike
        ? {
            types: safeTransferTypeList(source.clipboard.types),
            item_count: Math.round(clampNumber(source.clipboard.item_count, 0, 1000, 0)),
            file_count: Math.round(clampNumber(source.clipboard.file_count, 0, 1000, 0))
          }
        : null,
      transfer: source.transfer && !passwordLike
        ? {
            types: safeTransferTypeList(source.transfer.types),
            item_count: Math.round(clampNumber(source.transfer.item_count, 0, 1000, 0)),
            file_count: Math.round(clampNumber(source.transfer.file_count, 0, 1000, 0)),
            effect_allowed: safeEnum(source.transfer.effect_allowed, EFFECT_ALLOWED_VALUES),
            drop_effect: safeEnum(source.transfer.drop_effect, DROP_EFFECT_VALUES)
          }
        : null,
      touch: source.touch
        ? {
            active_count: Math.round(clampNumber(source.touch.active_count, 0, 100, 0)),
            changed_count: Math.round(clampNumber(source.touch.changed_count, 0, 100, 0))
          }
        : null,
      form: source.form
        ? {
            method: safeEnum(source.form.method, FORM_METHOD_VALUES),
            action: sanitizeUrl(source.form.action, false),
            control_count: Math.round(clampNumber(source.form.control_count, 0, 100000, 0)),
            has_submitter: Boolean(source.form.has_submitter),
            submitter_tag: safeToken(source.form.submitter_tag, 24).toLowerCase() || null
          }
        : null,
      document_state: source.document_state
        ? {
            fullscreen: optionalBoolean(source.document_state.fullscreen),
            online: optionalBoolean(source.document_state.online)
          }
        : null,
      target: {
        tag,
        role: safeToken(sourceTarget.role, 40) || null,
        input_type: sourceInputType || null,
        selector_hint: selectorHint,
        password_like: passwordLike
      },
      is_trusted: typeof source.is_trusted === "boolean" ? source.is_trusted : null,
      visibility_state: safeToken(source.visibility_state, 24) || null
    };
  }

  function sanitizeNormalizedEvent(candidate, options = {}) {
    const source = candidate || {};
    if (!SUPPORTED_SCHEMA_VERSIONS.has(source.schema_version)) {
      throw new Error(
        `event schema_version must be ${LEGACY_SCHEMA_VERSION} or ${CURRENT_SCHEMA_VERSION}`
      );
    }
    const eventId = safeToken(source.event_id, 64);
    if (!eventId) throw new Error("event_id is required");
    const sanitized = sanitizeEventFields(source, eventId, options);
    if (source.schema_version === CURRENT_SCHEMA_VERSION) {
      sanitized.event_seq = Math.round(clampNumber(source.event_seq, 1, Number.MAX_SAFE_INTEGER, 1));
      sanitized.document_epoch = Math.round(
        clampNumber(source.document_epoch, 0, Number.MAX_SAFE_INTEGER, 0)
      );
      sanitized.t_session_ms = Math.round(
        clampNumber(source.t_session_ms, 0, Number.MAX_SAFE_INTEGER, 0) * 1000
      ) / 1000;
      sanitized.t_document_ms = Math.round(
        clampNumber(
          source.t_document_ms,
          0,
          Number.MAX_SAFE_INTEGER,
          sanitized.monotonic_ms
        ) * 1000
      ) / 1000;
      sanitized.step_id = safeStepId(source.step_id);
    }
    return sanitized;
  }

  function enrichEventV2(candidate, envelope) {
    const source = sanitizeNormalizedEvent(candidate, envelope || {});
    const context = envelope || {};
    const eventSeq = Math.round(clampNumber(context.eventSeq, 1, Number.MAX_SAFE_INTEGER, 1));
    const documentEpoch = Math.round(
      clampNumber(context.documentEpoch, 0, Number.MAX_SAFE_INTEGER, 0)
    );
    const tDocumentMs = Math.round(
      clampNumber(
        context.tDocumentMs,
        0,
        Number.MAX_SAFE_INTEGER,
        source.t_document_ms ?? source.monotonic_ms
      ) * 1000
    ) / 1000;
    const tSessionMs = Math.round(
      clampNumber(context.tSessionMs, 0, Number.MAX_SAFE_INTEGER, 0) * 1000
    ) / 1000;
    return {
      ...source,
      schema_version: CURRENT_SCHEMA_VERSION,
      event_seq: eventSeq,
      document_epoch: documentEpoch,
      t_session_ms: tSessionMs,
      t_document_ms: tDocumentMs,
      step_id: safeStepId(context.stepId ?? source.step_id)
    };
  }

  // Project one raw capture record (camelCase, produced by content.js or the
  // playground) onto the exported snake_case field names. No value is trusted
  // here; sanitizeEventFields bounds every field afterwards.
  function rawEventToCandidate(raw, type) {
    const pointer = raw.pointer;
    const keyboard = raw.keyboard;
    return {
      schema_version: SCHEMA_VERSION,
      type,
      epoch_ms: raw.epochMs,
      monotonic_ms: raw.monotonicMs,
      url: raw.url,
      viewport: {
        width: raw.viewportWidth,
        height: raw.viewportHeight,
        device_pixel_ratio: raw.devicePixelRatio
      },
      pointer: pointer
        ? {
            x: pointer.x,
            y: pointer.y,
            button: pointer.button,
            buttons: pointer.buttons,
            pointer_type: pointer.pointerType,
            pointer_id: pointer.pointerId,
            width: pointer.width,
            height: pointer.height,
            pressure: pointer.pressure,
            tangential_pressure: pointer.tangentialPressure,
            tilt_x: pointer.tiltX,
            tilt_y: pointer.tiltY,
            twist: pointer.twist,
            is_primary: pointer.isPrimary
          }
        : null,
      wheel: raw.wheel
        ? { delta_x: raw.wheel.deltaX, delta_y: raw.wheel.deltaY, delta_mode: raw.wheel.deltaMode }
        : null,
      scroll: raw.scroll ? { x: raw.scroll.x, y: raw.scroll.y } : null,
      key: raw.key,
      key_class: KEY_EVENT_TYPES.has(type) ? classifyKey(raw.key) : null,
      keyboard: keyboard
        ? {
            repeat: keyboard.repeat,
            location: keyboard.location,
            is_composing: keyboard.isComposing,
            hold_ms: keyboard.holdMs
          }
        : null,
      modifiers: {
        alt: raw.altKey,
        ctrl: raw.ctrlKey,
        meta: raw.metaKey,
        shift: raw.shiftKey
      },
      input: raw.input
        ? {
            input_type: raw.input.inputType,
            data_length: raw.input.dataLength,
            is_composing: raw.input.isComposing
          }
        : null,
      clipboard: raw.clipboard
        ? {
            types: raw.clipboard.types,
            item_count: raw.clipboard.itemCount,
            file_count: raw.clipboard.fileCount
          }
        : null,
      transfer: raw.transfer
        ? {
            types: raw.transfer.types,
            item_count: raw.transfer.itemCount,
            file_count: raw.transfer.fileCount,
            effect_allowed: raw.transfer.effectAllowed,
            drop_effect: raw.transfer.dropEffect
          }
        : null,
      touch: raw.touch
        ? { active_count: raw.touch.activeCount, changed_count: raw.touch.changedCount }
        : null,
      form: raw.form
        ? {
            method: raw.form.method,
            action: raw.form.action,
            control_count: raw.form.controlCount,
            has_submitter: raw.form.hasSubmitter,
            submitter_tag: raw.form.submitterTag
          }
        : null,
      document_state: raw.documentState
        ? { fullscreen: raw.documentState.fullscreen, online: raw.documentState.online }
        : null,
      target: describeTarget(raw.target),
      is_trusted: raw.isTrusted,
      visibility_state: raw.visibilityState
    };
  }

  function normalizeEvent(rawEvent, options = {}) {
    const raw = rawEvent || {};
    const type = safeToken(raw.type, 40).toLowerCase() || "unknown";
    const eventId = safeToken(raw.eventId, 64) || null;
    return sanitizeEventFields(rawEventToCandidate(raw, type), eventId, options);
  }

  function summarizeEvents(events) {
    const list = Array.isArray(events) ? events : [];
    const byType = {};
    let pointerDistance = 0;
    let previousPointer = null;
    let first = null;
    let last = null;
    for (const event of list) {
      const type = String(event.type || "unknown");
      byType[type] = (byType[type] || 0) + 1;
      const sessionTimestamp = Number(event?.t_session_ms);
      const timestamp = Number.isFinite(sessionTimestamp)
        ? sessionTimestamp
        : Number(event?.epoch_ms);
      if (Number.isFinite(timestamp)) {
        first = first === null ? timestamp : Math.min(first, timestamp);
        last = last === null ? timestamp : Math.max(last, timestamp);
      }
      if (event.pointer && Number.isFinite(event.pointer.x) && Number.isFinite(event.pointer.y)) {
        if (previousPointer) {
          pointerDistance += Math.hypot(event.pointer.x - previousPointer.x, event.pointer.y - previousPointer.y);
        }
        previousPointer = event.pointer;
      }
    }
    return {
      event_count: list.length,
      duration_ms: first !== null && last !== null ? Math.max(0, last - first) : 0,
      pointer_distance_px: Math.round(pointerDistance * 100) / 100,
      by_type: byType
    };
  }

  function validateSessionInput(input) {
    const source = input || {};
    const actorType = source.actorType === "agent" ? "agent" : source.actorType === "human" ? "human" : null;
    if (!actorType) throw new Error("actorType must be human or agent");
    const taskId = safeToken(source.taskId, 80);
    if (!taskId) throw new Error("taskId is required");
    if (!source.consentConfirmed) throw new Error("affirmative consent is required");
    const scopeOrigin = sanitizeUrl(source.scopeOrigin || "unavailable");
    if (scopeOrigin === "unavailable") throw new Error("scopeOrigin must be an http(s) origin");
    const requestedSourceClass = safeToken(source.sourceClass, 60).toLowerCase();
    const sourceClass = requestedSourceClass || (actorType === "human" ? "human_self" : "cui_agent");
    if (!SOURCE_CLASS_VALUES.has(sourceClass)) {
      throw new Error(`sourceClass must be one of: ${[...SOURCE_CLASS_VALUES].join(", ")}`);
    }
    return {
      actor_type: actorType,
      actor_label: safeToken(source.actorLabel, 80) || (actorType === "human" ? "human-participant" : "unspecified-agent"),
      participant_code: safeToken(source.participantCode, 80) || "self-test",
      pair_id: safeToken(source.pairId, 80) || "unpaired",
      task_id: taskId,
      protocol_version: safeToken(source.protocolVersion, 80) || "not-recorded",
      executor: safeToken(source.executor, 100) || "unspecified",
      executor_version: safeToken(source.executorVersion, 60) || "not-recorded",
      recorder_version: safeToken(source.recorderVersion, 24) || "not-recorded",
      environment: safeToken(source.environment, 100) || "browser",
      capture_adapter: safeToken(source.captureAdapter, 60).toLowerCase() || "browser_mv3",
      environment_scope: safeToken(source.environmentScope, 80).toLowerCase() || "active_origin",
      source_class: sourceClass,
      raw_key_capture: Boolean(source.rawKeyCapture),
      include_query: Boolean(source.includeQuery),
      scope_origin: new URL(scopeOrigin).origin,
      consent_confirmed: true,
      disclosure_version: safeToken(source.disclosureVersion, 40) || "not-recorded",
      retention_policy: safeToken(source.retentionPolicy, 80) || "local-until-export-or-delete",
      notes: String(source.notes || "").slice(0, 500)
    };
  }

  function sanitizeSessionForExport(session) {
    const source = session || {};
    const metadata = { ...(source.metadata || {}) };
    delete metadata.tab_id;
    delete metadata.current_document_epoch;
    delete metadata.last_t_session_ms;
    delete metadata.last_event_document_epoch;
    delete metadata.started_epoch_ms;
    delete metadata.current_step_id;
    delete metadata.known_step_ids;
    delete metadata.approx_event_bytes;
    return {
      metadata,
      summary: { ...(source.summary || {}) },
      events: Array.isArray(source.events) ? source.events : []
    };
  }

  function migrateSessionV1ToV2(session) {
    const source = sanitizeSessionForExport(session);
    if (source.metadata.schema_version === CURRENT_SCHEMA_VERSION) {
      const events = source.events.map((event) => sanitizeNormalizedEvent(event, {
        rawKeyCapture: Boolean(source.metadata.raw_key_capture),
        includeQuery: Boolean(source.metadata.include_query)
      }));
      assertV2EventStream(events);
      return {
        metadata: { ...source.metadata },
        summary: summarizeEvents(events),
        events
      };
    }
    if (source.metadata.schema_version !== LEGACY_SCHEMA_VERSION) {
      throw new Error("Only cui-trace-v1 and cui-trace-v2 sessions are supported");
    }
    const startEpoch = Number.isFinite(Date.parse(source.metadata.started_at || ""))
      ? Date.parse(source.metadata.started_at)
      : Number(source.events[0]?.epoch_ms || 0);
    let documentEpoch = 0;
    let previousDocumentTime = null;
    let previousSessionTime = -0.001;
    const events = source.events.map((candidate, index) => {
      const clean = sanitizeNormalizedEvent(candidate, {
        rawKeyCapture: Boolean(source.metadata.raw_key_capture),
        includeQuery: Boolean(source.metadata.include_query)
      });
      const documentTime = Number(clean.monotonic_ms) || 0;
      if (
        index > 0 &&
        (clean.type === "navigation" ||
          (previousDocumentTime !== null && documentTime < previousDocumentTime))
      ) {
        documentEpoch += 1;
      }
      const wallRelative = Math.max(0, Number(clean.epoch_ms) - startEpoch);
      const sessionTime = Math.max(previousSessionTime + 0.001, wallRelative);
      previousDocumentTime = documentTime;
      previousSessionTime = sessionTime;
      return enrichEventV2(clean, {
        rawKeyCapture: Boolean(source.metadata.raw_key_capture),
        includeQuery: Boolean(source.metadata.include_query),
        eventSeq: index + 1,
        documentEpoch,
        tSessionMs: sessionTime,
        tDocumentMs: documentTime,
        stepId: clean.step_id
      });
    });
    const metadata = {
      ...source.metadata,
      schema_version: CURRENT_SCHEMA_VERSION,
      timing_model: "session-global-monotonic-v1",
      event_seq_last: events.length,
      document_epoch_count: events.length ? documentEpoch + 1 : 0,
      dropped_event_count: 0,
      rejected_event_count: 0,
      flush_failure_count: 0,
      quota_rejection_count: 0,
      event_count: events.length
    };
    return { metadata, summary: summarizeEvents(events), events };
  }

  function assertV2EventStream(events) {
    const list = Array.isArray(events) ? events : [];
    let previousTime = -Infinity;
    let previousEpoch = -1;
    const ids = new Set();
    for (let index = 0; index < list.length; index += 1) {
      const event = list[index] || {};
      if (event.schema_version !== CURRENT_SCHEMA_VERSION) {
        throw new Error(`event ${index + 1} is not ${CURRENT_SCHEMA_VERSION}`);
      }
      if (event.event_seq !== index + 1) throw new Error("V2 event_seq must be contiguous");
      if (ids.has(event.event_id)) throw new Error("V2 event_id values must be unique");
      ids.add(event.event_id);
      if (!Number.isFinite(event.t_session_ms) || event.t_session_ms <= previousTime) {
        throw new Error("V2 t_session_ms must be strictly increasing");
      }
      if (!Number.isInteger(event.document_epoch) || event.document_epoch < previousEpoch) {
        throw new Error("V2 document_epoch must be nondecreasing");
      }
      if (!Number.isFinite(event.t_document_ms) || event.t_document_ms < 0) {
        throw new Error("V2 t_document_ms must be nonnegative");
      }
      previousTime = event.t_session_ms;
      previousEpoch = event.document_epoch;
    }
    return true;
  }

  function toV1Session(session) {
    const source = sanitizeSessionForExport(session);
    const metadata = { ...source.metadata, schema_version: LEGACY_SCHEMA_VERSION };
    for (const field of V2_METADATA_FIELDS) delete metadata[field];
    const events = source.events.map((candidate) => {
      const event = { ...candidate, schema_version: LEGACY_SCHEMA_VERSION };
      for (const field of V2_EVENT_FIELDS) delete event[field];
      const projected = sanitizeNormalizedEvent(event, {
        rawKeyCapture: Boolean(metadata.raw_key_capture),
        includeQuery: Boolean(metadata.include_query)
      });
      if (projected.keyboard) delete projected.keyboard.hold_ms;
      return projected;
    });
    metadata.event_count = events.length;
    return { metadata, summary: summarizeEvents(events), events };
  }

  function parseTrace(serialized, options = {}) {
    const text = String(serialized || "");
    let session;
    if (text.trimStart().startsWith("{")) {
      try {
        const payload = JSON.parse(text);
        if (payload.metadata && Array.isArray(payload.events)) session = payload;
      } catch (_jsonError) {
        // JSONL is handled below.
      }
    }
    if (!session) {
      const records = text.split(/\r?\n/).filter((line) => line.trim()).map(JSON.parse);
      const header = records.find((record) => record.record_type === "session");
      if (!header) throw new Error("Trace has no session record");
      const metadata = header.metadata || header.session || {};
      const events = records
        .filter((record) => record.record_type === "event")
        .map(({ record_type: _recordType, ...event }) => event);
      const terminal = records.find((record) => record.record_type === "session_end") || {};
      session = {
        metadata: { ...metadata, status: terminal.status ?? metadata.status },
        summary: header.summary || summarizeEvents(events),
        events
      };
    }
    const clean = sanitizeSessionForExport(session);
    return options.migrateToV2 === false || clean.metadata.schema_version === CURRENT_SCHEMA_VERSION
      ? clean
      : migrateSessionV1ToV2(clean);
  }

  function toJsonl(session) {
    const publicSession = sanitizeSessionForExport(session);
    const header = {
      record_type: "session",
      schema_version: publicSession.metadata.schema_version || LEGACY_SCHEMA_VERSION,
      session: publicSession.metadata,
      summary: publicSession.summary
    };
    return [JSON.stringify(header), ...publicSession.events.map((event) => JSON.stringify({ ...event, record_type: "event" }))].join("\n");
  }

  // Spreadsheet-formula guard. A finite number (for example a negative pointer
  // coordinate outside the viewport) cannot start a formula, so it keeps its
  // numeric text; strings, objects and NaN/Infinity stay guarded.
  function csvCell(value) {
    let serialized = value == null ? "" : typeof value === "object" ? JSON.stringify(value) : String(value);
    const finiteNumber = typeof value === "number" && Number.isFinite(value);
    if (!finiteNumber && /^[=+\-@\t\r]/.test(serialized)) serialized = `'${serialized}`;
    return `"${serialized.replace(/"/g, '""')}"`;
  }

  function toCsv(session) {
    const columns = [
      "session_id",
      "actor_type",
      "actor_label",
      "task_id",
      "event_id",
      "type",
      "epoch_ms",
      "monotonic_ms",
      "event_seq",
      "document_epoch",
      "t_session_ms",
      "t_document_ms",
      "step_id",
      "url",
      "x",
      "y",
      "key",
      "key_class",
      "target_tag",
      "target_role",
      "is_trusted"
    ];
    const metadata = session.metadata || {};
    const rows = (session.events || []).map((event) => [
      metadata.session_id,
      metadata.actor_type,
      metadata.actor_label,
      metadata.task_id,
      event.event_id,
      event.type,
      event.epoch_ms,
      event.monotonic_ms,
      event.event_seq,
      event.document_epoch,
      event.t_session_ms,
      event.t_document_ms,
      event.step_id,
      event.url,
      event.pointer?.x,
      event.pointer?.y,
      event.key,
      event.key_class,
      event.target?.tag,
      event.target?.role,
      event.is_trusted
    ]);
    return [columns.map(csvCell).join(","), ...rows.map((row) => row.map(csvCell).join(","))].join("\r\n");
  }

  return {
    CURRENT_SCHEMA_VERSION,
    LEGACY_SCHEMA_VERSION,
    SCHEMA_VERSION,
    STEP_ID_PATTERN,
    assertV2EventStream,
    classifyKey,
    describeTarget,
    enrichEventV2,
    migrateSessionV1ToV2,
    normalizeEvent,
    parseTrace,
    sanitizeSessionForExport,
    sanitizeNormalizedEvent,
    sanitizeUrl,
    summarizeEvents,
    toCsv,
    toJsonl,
    toV1Session,
    validateSessionInput
  };
});
