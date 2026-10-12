# Privacy, consent, and research-use guardrails

The recorder collects behavioural data about people. Treat it as research
instrumentation, not an analytics widget. This page states what is recorded,
what is not, and what earlier versions got wrong.

## What is recorded

For each event in the scoped tab: event type, wall-clock and page-relative
timestamps, the session ordering fields, the query-free page URL, viewport
size, pointer coordinates and buttons (sampled for moves), wheel and scroll
offsets, modifier flags, focus and form lifecycle events, minimal target
metadata (tag, role, input type, a short selector hint built from the element
id and up to three class names), document visibility, and `is_trusted` as a
diagnostic. Session metadata holds the labels typed into the popup (actor,
task, participant code, pair id, protocol and executor details).

## What is not recorded

- **Typed characters.** In the default privacy-safe mode a key event stores a
  key *class* (`letter`, `digit`, `punctuation`, `whitespace`, `other`,
  `control`) and keeps the key value only for control keys such as `Enter`,
  `Tab`, arrows or `F1`–`F12`. `KeyboardEvent.code` is never stored.
- **Input values.** `beforeinput`/`input`/composition events store only the
  input type, the number of inserted characters and the composition flag.
- **Clipboard and drag contents.** Only MIME categories (unknown types become
  `other`) and item/file counts.
- **URL queries and fragments.** Always dropped. The `include_query` session
  flag exists in the data model but 0.5.0 has no UI for it: the popup always
  sends `false`, and the public V2 schema requires `false`.
- Page text, form values, file names, cookies, screenshots, audio and video.

## Private targets

A target is private when it is an `<input type="password">`, has an
`autocomplete` token `current-password` or `new-password` (token lists such as
`section-a current-password` are recognized), or sits inside an element with
the `data-recorder-private` attribute. As of **0.5.0**, a key event on a
private target stores:

| Field | Value |
|---|---|
| `key` | the constant `"<redacted>"` |
| `key_class` | `null` |
| `keyboard.repeat`, `keyboard.location`, `keyboard.hold_ms` | `null` |
| `modifiers.alt/ctrl/meta/shift` | `null` |
| `target.selector_hint` | the tag name only (no id or classes) |
| `input`, `clipboard`, `transfer` (any event type) | `null` |

These rules apply in raw-key mode as well, and the service worker re-applies
them to every stored event, so a compromised page cannot add the detail back.

**Shadow DOM.** Events are classified on the innermost element the page can
see (`composedPath()[0]`), not on the shadow host the event was retargeted to,
so `target` describes the element inside an open shadow root. Key, input,
composition and clipboard events also follow focus down through open and
closed shadow roots (closed roots through `chrome.dom.openOrClosedShadowRoot`),
and the `data-recorder-private` check walks up through slots and shadow hosts.
When focus sits in a shadow root but cannot be followed to an element, the
event is recorded as private.

**What still remains:** the fact, type and timestamps of each key, input and
focus event on a private field. The number of key events and their timing
therefore still reveal the approximate length and typing rhythm of what was
typed. Ask participants not to type real credentials while recording, and use
test accounts in task designs.

### Security fix in 0.5.0: what 0.4.0 leaked

Recorder 0.4.0 replaced the key on private targets with `"<redacted>"` but kept
the **key class**, the **key-hold duration** (`keyboard.hold_ms`), the
**auto-repeat** flag, the **physical key location** and the **modifier flags**.
Its documentation nevertheless said that password fields were "always
redacted". From a 0.4.0 trace one can recover the character-class pattern of a
password (for example letter, letter, digit, punctuation), its length, and
per-key timing. The archived 0.4.0 E2E trace shows this: all 70 key events on
its synthetic password and private fields carry a key class, and 35 carry a
hold duration (the typed text there was a fixed test sentinel, not a real
password).

If you hold 0.4.0 traces, treat `key_class`, `keyboard` and `modifiers` of
events whose `target.password_like` is `true` as sensitive and strip them
before sharing. The 0.5.0 JSON Schemas accept `null` for those fields, so
stripped traces still validate.

### Shadow-DOM fields before the 0.5.0 fix

Recorder 0.4.0, and 0.5.0 ZIPs or unpacked copies built before the shadow-DOM
fix, classified events inside a shadow root on the shadow host. A password
field inside a web component was therefore not recognized as private: in
raw-key mode the actual keys typed into it were stored in plain text, and in
the default mode its key class, modifier flags, hold time and the length of
each input event were stored. Affected traces show key or input events whose
`target.tag` is a custom element (a name containing `-`) with
`target.password_like` `false`. Treat such events as sensitive, or delete the
traces.

## Raw-key mode

The popup offers a restricted raw-key mode that stores the typed key value for
non-private targets. It needs a second confirmation, is recorded in the session
metadata (`raw_key_capture: true`), and such sessions do not validate against
the public V2 schema, which requires `raw_key_capture: false`. Use it only with
explicit protocol approval and never publish raw-key traces.

## Storage, access, and messaging

- Data stays in `chrome.storage.local`, restricted to trusted extension
  contexts; web pages and content scripts cannot read it.
- Content scripts can only send events for the tab that owns the active
  session and read a reduced state view; they cannot list, read, delete, start
  or stop sessions (see [message authority](index.md#message-authority)).
- Leaving the scoped origin or closing the recording tab ends capture; a final
  flush that cannot be confirmed marks the session `incomplete`.
- Research exports remove the browser-internal `tab_id` and other internal
  lifecycle fields.

## Disclosure template

`extension/app/privacy/disclosure.html` is a neutral template that the popup
links to before consent. It lists what is and is not recorded and contains
bracketed placeholders (`[task website]`, purpose, retention, review body,
contact). Operators must replace them with wording approved by their own
ethics or institutional review process before recording anyone other than
themselves, and must not present the page as an approval.

Each session stores `disclosure_version`. After editing the page, change the
version in three places that a unit test keeps equal: the
`<meta name="cua-disclosure-version">` tag and the visible version in
`disclosure.html`, and `DISCLOSURE_VERSION` in `popup/popup.js`. The template
shipped with 0.5.0 is `2026-10-11-template-v2`; 0.4.0 used `2026-08-03-v1`
with institution-specific wording.

## Before recording anyone else

Get a documented determination from the review body that applies to you
before recruiting participants or collecting data. Do not self-declare an
exemption. Until that determination exists, limit use to your own sessions,
synthetic fixtures and automated agent runs; those are engineering checks, not
a human-subject dataset.

## Data handling

- Use pseudonymous participant codes and keep any identity key separately.
- Encrypt and access-control raw exports; set a concrete retention date.
- Inspect every export in the dashboard before sharing it.
- Publish aggregate features or deliberately sanitized examples only.
- Never commit consent forms, identity keys, raw human traces or raw-key traces.

## Modeling leakage

Exclude actor labels, executor metadata, `source_class`, `is_trusted`, task
identifiers, validation outcomes, file names, collection order and other
direct source signals from model inputs. Hold out whole agent families, task
families and environments; otherwise a classifier learns the collection
pipeline rather than behaviour.
