# ComputerUseAgent Trace Recorder (browser extension)

The ComputerUseAgent Trace Recorder is a Chrome Manifest V3 extension that,
after explicit consent, records privacy-reduced interaction traces from one
browser tab so that human and computer-use-agent runs of the same task can be
compared. Traces stay in the browser's local extension storage until the user
deletes them; exporting writes a copy and leaves the stored trace in place.
The extension makes no network requests.

The extension version lives in one place, `extension/app/manifest.json`
(0.5.0 at the time of writing); the popup footer reads it at run time and
`npm run version:check` keeps `extension/package.json`, `pyproject.toml` and
`CITATION.cff` in step with it.

Wire-format identifiers keep their historical `cui-` namespace (`cui-trace-v2`,
the storage keys `cui-recorder-root`, `cui-session-*`, `cui-events-*`, export
file names `cui-trace-*`). They are unchanged on purpose: renaming them would
orphan sessions already stored in users' profiles and break archived evidence.

## What is captured

- 59 DOM event types (52 listeners on `document`, 7 on `window`) plus a
  recorder-generated `navigation` record per document. The exact list is
  [`extension/event_coverage.json`](../../extension/event_coverage.json); see
  [event coverage](event-coverage.md) for what was actually observed in a run.
- `pointermove`, `drag`/`dragover` and `touchmove` are sampled at most every
  50 ms, `scroll` at most every 100 ms.
- The content script flushes a batch at 80 events or after 1 s.
- Every event is re-sanitized by the service worker before it is stored, and
  stored in chunks of 500 events in `chrome.storage.local`.
- Exports: JSON and JSONL (`cui-trace-v2`), CSV (spreadsheet-formula cells are
  escaped) and an explicit legacy V1 JSON projection.

What is and is not recorded, field by field, is in [privacy](privacy.md) and
the [trace schema](data-schema.md).

## Limits

| Limit | Value | Behaviour when reached |
|---|---:|---|
| Stored sessions | 250 | Starting a new session fails; nothing is evicted |
| Events per session | 100,000 | Further events are rejected and counted in `quota_rejection_count` |
| Approximate size per session | 64 MiB (UTF-16 length of the stored JSON) | Further events are rejected and counted |
| Total extension storage | 512 MiB | New sessions and further events are refused |
| Events per message batch | 1,000 | The excess is rejected and counted |
| Events per storage chunk | 500 | A new chunk is started |
| Distinct step ids per session | 512 | `RECORDER_SET_STEP` fails |

## Permissions (0.5.0)

| Permission | Why |
|---|---|
| `activeTab` | Inject into the tab the user is on, only after a deliberate toolbar click |
| `scripting` | Inject `src/core.js` and `src/content.js` into that tab and its same-origin frames |
| `storage`, `unlimitedStorage` | Keep traces locally in bounded chunks (see limits above) |
| optional `http://*/*`, `https://*/*` | Requested per origin, only when the user ticks *Task-origin permission* in the popup (for reloads and iframes); never granted globally |

There is no `downloads` permission. Version 0.4.0 declared it but never used
it; 0.5.0 removes it. Exports are handed to the browser's normal download flow
through a temporary object URL, so the extension cannot observe whether the
browser finished saving the file. The dashboard says "handed to the browser",
not "saved".

Recorder storage is restricted to trusted extension contexts
(`storage.local.setAccessLevel("TRUSTED_CONTEXTS")`), so content scripts and
pages cannot read it.

## Message authority

The service worker classifies every `runtime.onMessage` sender using the
fields Chrome fills in (`sender.id`, `sender.origin`/`sender.url`,
`sender.tab`):

| Sender | Accepted messages |
|---|---|
| Extension page (popup, dashboard, any `chrome-extension://<id>/` page, also when opened in a tab) | `RECORDER_GET_STATE` (full state), `RECORDER_START`, `RECORDER_STOP`, `RECORDER_SET_STEP`, `RECORDER_LIST_SESSIONS`, `RECORDER_GET_SESSION`, `RECORDER_DELETE_SESSION` |
| Content script (has a tab and a web origin) | `RECORDER_EVENT_BATCH`, `RECORDER_REPORT_DROPPED`, `RECORDER_FRAME_DISCOVERED`, and a reduced `RECORDER_GET_STATE` |
| Anything else (other extensions, opaque origins, no tab) | nothing |

The reduced state for a content script contains only the session id, status,
scope origin, privacy-mode flags, document epoch and current step id, and only
for the tab that owns the active session and a frame of its scoped origin. The
same reduced view is the only session data pushed to content scripts; actor,
participant, task, executor and notes never enter a web-page renderer.

The rule keys on the sender's origin rather than on the absence of
`sender.tab`: the dashboard is opened in a tab (`options_page`), so its
messages carry `sender.tab` too, while a content script always reports the web
page's origin.

## Chrome version

`minimum_chrome_version` is **151**: the Chrome for Testing build pinned by
Puppeteer 25.4.0 (`151.0.7922.47`), which is what the E2E job runs. Older Chrome
versions were never tested, and the manifest no longer claims them (0.4.0
declared 102 without testing it). `npm run version:check` fails if the floor is
raised above the Chrome for Testing build that the installed Puppeteer pins. If
Puppeteer is upgraded, either keep an E2E run on Chrome 151
(`npx @puppeteer/browsers install chrome@151.0.7922.47` and `CUA_E2E_CHROME`) or
raise the floor.

## Install (unpacked)

```bash
git clone https://github.com/appleweiping/ComputerUseAgent.git
cd ComputerUseAgent
```

1. Open `chrome://extensions` and turn on **Developer mode**.
2. Choose **Load unpacked** and select `extension/app`.
3. Pin **ComputerUseAgent Trace Recorder** to the toolbar.

A release ZIP (`computer-use-agent-recorder-<version>.zip`, built by
`npm --prefix extension run package`) has `manifest.json` at its root, next to
the repository's `LICENSE` and `NOTICE`; extract it to a folder and load that
folder the same way.

## Recording a session

1. Open the task page (an `http` or `https` page; browser pages such as
   `chrome://` cannot be recorded).
2. Open the popup. Choose the actor (human or computer-use agent) and the task
   ID. **Advanced metadata and privacy settings** holds the participant code,
   pair ID, protocol version, executor details, source class, the optional
   task-origin permission and the key-capture mode.
3. Read the linked disclosure, tick the consent box and press **Start
   recording**. A red indicator appears on the page and the toolbar badge shows
   `REC`.
4. Stop as soon as the task ends. Inspect, export or delete the session from
   **Sessions & exports** (the dashboard).

The disclosure page (`extension/app/privacy/disclosure.html`) is a neutral
**operator template**: bracketed fields must be replaced with wording approved
by the operator's own ethics or institutional review before anyone other than
the operator is recorded. See [privacy](privacy.md#disclosure-template).

### Known capture gaps

- After a same-origin navigation or reload, nothing is captured until Chrome
  reports `status=complete` for the tab and the content script is injected
  again.
- Navigating the recording tab to another origin ends the session as
  `interrupted`; closing the tab does the same.
- Cross-origin iframes are not captured unless that origin was granted through
  the task-origin permission.
- Browser UI (toolbar, omnibox, dialogs), OS-level input and native
  applications are out of scope; the X11 capture path covers desktop sessions.
- A browser restart finalizes a still-active session as `interrupted`.

## Matched human/agent runs

Use the same task version, browser build, viewport, starting state, allowed
tools, completion validator and time limit for each paired run, and record the
executor and its version. Never use `actor_type`, `actor_label`, `executor`,
`source_class`, `is_trusted`, task IDs or other provenance fields as detector
features; they are labels and would leak the answer.

## Development

From `extension/` (Node >= 22.12; ESLint 10 itself needs Node >= 22.13):

```bash
npm ci
npm run lint            # ESLint flat config (eslint.config.js)
npm test                # unit tests, launch-policy tests, version-check tests
npm run test:coverage   # unit tests with the coverage gate on app/**
npm run version:check   # manifest == package.json == pyproject == CITATION
npm run package         # dist/computer-use-agent-recorder-<version>.zip
npm run e2e             # real-browser E2E, see e2e.md
```

The coverage gate is lines >= 85 %, branches >= 78 %, functions >= 85 % over
`app/**/*.js` (0.5.0 measured 94.6 % / 83.2 % / 88.7 %). `npm run package`
(`scripts/package.mjs`) runs `tools/package_extension.py` with the first of
`$PYTHON` (used alone when set), `python`, `python3` or, on Windows, `py -3`
that is Python 3.10 or newer; the packager needs only the standard library.
It packages only the runtime files (`manifest.json`, `src/`, `popup/`,
`dashboard/`, `privacy/`, `icons/`) plus the repository's `LICENSE` and
`NOTICE` (18 entries for 0.5.0), fails if `extension/app` contains anything
else, and produces a byte-identical ZIP on every run. The icons are generated
by `extension/scripts/make_icons.py` (Pillow, development only).

## Playground

`extension/playground/` is a standalone matched-task page that runs the
extension's own normalization core without the extension. Serve the
`extension/` directory (so `../app/src/core.js` resolves) on loopback only:

```bash
python -m http.server 8765 --bind 127.0.0.1 --directory extension
```

Then open <http://127.0.0.1:8765/playground/>. It is a synthetic QA fixture,
not evidence of a human-versus-agent study; its download is a `cui-trace-v2`
session marked `capture_adapter=browser_standalone_mirror` and
`source_class=synthetic_coverage`.

## Research harnesses pinned to 0.4.0

The archived Week 1 MiniWoB harness under `research/week1_miniwob/` checks for
recorder 0.4.0 and evaluates service-worker globals directly. It is frozen and
refuses the 0.5.0 extension by design; it is kept for provenance only.

## Related documents

- [Privacy and consent](privacy.md)
- [Trace schema](data-schema.md)
- [Local data store](data-store.md)
- [Event coverage](event-coverage.md)
- [End-to-end tests](e2e.md)
