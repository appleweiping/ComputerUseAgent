# Trace schema

Current schema identifier: `cui-trace-v2` (TraceEnvelopeV2).

The JSON Schemas are packaged with the Python library and are the single source
of truth:

- [`src/computer_use_agent/schemas/cui-trace-v2.schema.json`](../../src/computer_use_agent/schemas/cui-trace-v2.schema.json)
  (closed, current)
- [`src/computer_use_agent/schemas/cui-trace-v1.schema.json`](../../src/computer_use_agent/schemas/cui-trace-v1.schema.json)
  (legacy compatibility)

Non-human documentation samples:
[`examples/traces/minimal-session-v2.json`](../../examples/traces/minimal-session-v2.json)
and [`examples/traces/minimal-session.json`](../../examples/traces/minimal-session.json).
Validate a file with
`computer-use-agent validate --schema cui-trace-v2 --input <file>`.

New captures use V2. V1→V2 migration is deterministic, and a V2 session can be
projected back to the strict V1 shape for a legacy consumer; the projection
removes the five V2 ordering fields, the V2 lifecycle counters and the numeric
key-hold duration. The narrowly specified `legacyStandalone*` V1 branch exists
only for a retained 2026-08-03 standalone-mirror fixture.

## Session metadata

- `session_id`: random UUID;
- `actor_type`: `human` or `agent` (a label, never a feature);
- `actor_label`, `participant_code`, `pair_id`: pseudonymous labels;
- `task_id`, `protocol_version`: immutable task and protocol identifiers;
- `executor`, `executor_version`: how the run was driven;
- `capture_adapter` (`browser_mv3`, or `browser_standalone_mirror` for the
  playground), `environment_scope`, `source_class` (`human_self`,
  `cui_agent`, `scripted_automation_control`, `synthetic_coverage`);
- `recorder_version` (from the manifest), `disclosure_version`,
  `retention_policy`;
- `environment`: `<platform> / Chromium`, where the platform comes from
  `navigator.userAgentData.platform` (falling back to `navigator.platform`);
- `scope_origin`: the only origin captured;
- `raw_key_capture`, `include_query`: privacy-mode flags (the public V2 schema
  requires both to be `false`);
- `consent_confirmed`, ISO 8601 `started_at`/`stopped_at`;
- `status`: `recording`, `complete`, `incomplete` or `interrupted`, with a
  bounded `capture_error` for non-complete sessions;
- `event_count`, `chunk_count`, `timing_model=session-global-monotonic-v1`,
  `event_seq_last`, `document_epoch_count`;
- explicit `dropped_event_count`, `rejected_event_count`,
  `flush_failure_count` and `quota_rejection_count`.

## Event record

Every V2 event carries the V1 measurements plus five envelope fields assigned
by the service worker:

- `event_seq`: contiguous, one-based order;
- `document_epoch`: nondecreasing navigation/reload generation;
- `t_session_ms`: strictly increasing session time;
- `t_document_ms`: the page's relative clock, which may reset on navigation;
- `step_id`: an optional opaque token (`^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`)
  joining an externally stored action sidecar. It must not contain action
  text, and the worker accepts it only if the step was registered through
  `RECORDER_SET_STEP`.

The measurement fields:

- `event_id`, `type`, `epoch_ms`, navigation-relative `monotonic_ms`;
- query- and fragment-free `url`;
- `viewport` size and device pixel ratio;
- optional `pointer` (coordinates, buttons, pointer type and id, contact
  size, pressure, tilt, twist, primary flag), `wheel` deltas and mode, or
  `scroll` offsets;
- `key`, `key_class`, and `keyboard` (`repeat`, `location`, `is_composing`,
  `hold_ms`). The content script pairs keydown/keyup through an in-memory
  key identity that is never persisted;
- `modifiers` (`alt`, `ctrl`, `meta`, `shift`);
- `input` (input type, inserted-character count, composition flag), never a
  value;
- `clipboard` and drag `transfer` MIME categories and counts, never contents;
- `touch` counts, `form` method, query-free action, control count and
  submitter tag, and `document_state` (fullscreen, online);
- `target` tag, role, input type, selector hint and `password_like`. Inside an
  open shadow root, `target` describes the innermost element
  (`composedPath()[0]`), not the shadow host; key, input, composition and
  clipboard events follow focus into open and closed shadow roots, and an
  event whose focused element cannot be resolved is recorded as private (see
  [privacy](privacy.md#private-targets));
- `is_trusted` (diagnostic only) and `visibility_state`.

### Private targets (schema change in 0.5.0)

On key events whose `target.password_like` is `true`, recorder 0.5.0 writes
`key: "<redacted>"`, `key_class: null`, `keyboard.repeat: null`,
`keyboard.location: null`, `keyboard.hold_ms: null` and `null` for all four
modifier flags. Both schemas were widened to accept `null` for
`keyboard.repeat`, `keyboard.location` and the modifier flags; required fields
are unchanged, so every earlier trace still validates. Consumers that compute
features from these fields must treat `null` as "not recorded". See
[privacy](privacy.md#private-targets).

## Captured event surface

The content script registers 59 browser event listeners plus the
recorder-generated `navigation` record:

- pointer and activation: `pointermove`, `pointerdown`, `pointerup`,
  `pointercancel`, `pointerover`, `pointerout`, `pointerenter`,
  `pointerleave`, `gotpointercapture`, `lostpointercapture`, `click`,
  `dblclick`, `auxclick`, `contextmenu`;
- keyboard and editing: `keydown`, `keyup`, `beforeinput`, `input`,
  `compositionstart`, `compositionupdate`, `compositionend`;
- clipboard and drag: `copy`, `cut`, `paste`, `dragstart`, `drag`,
  `dragenter`, `dragover`, `dragleave`, `drop`, `dragend`;
- movement and touch: `wheel`, `scroll`, `touchstart`, `touchmove`,
  `touchend`, `touchcancel`;
- form and focus: `submit`, `reset`, `change`, `select`, `selectionchange`,
  `focusin`, `focusout`, `invalid`;
- controls and lifecycle: `toggle`, `beforetoggle`, `cancel`, `close`,
  `visibilitychange`, `fullscreenchange`, `fullscreenerror` (on `document`),
  and `resize`, `pageshow`, `pagehide`, `online`, `offline`, `hashchange`,
  `popstate` (on `window`).

A supported type need not appear in every trace; see
[event coverage](event-coverage.md).

The service worker treats content-script messages as untrusted. Before storage
it applies the same field table that capture used (one implementation in
`extension/app/src/core.js`), bounds every value, drops unknown fields,
re-sanitizes URLs and re-applies the session's key-privacy mode and the
private-target rules. Batches are accepted only from the tab that owns the
active session.

## Export formats

- **JSON**: the complete nested session and summary (`cui-trace-v2`).
- **JSONL**: a session record followed by one event per line.
- **CSV**: a flattened convenience view; it drops nested fields and is not a
  lossless archive.
- **Legacy V1 JSON**: the explicit V1 compatibility projection described above.

Use JSON or JSONL as the canonical research copy. The browser-internal
`tab_id`, current step, known step ids and other lifecycle bookkeeping are
removed from every export. Storage keys, chunking and deletion are described
in the [local data store](data-store.md).
