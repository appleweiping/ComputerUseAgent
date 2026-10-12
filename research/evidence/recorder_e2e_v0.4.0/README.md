# Recorder E2E run, extension 0.4.0 (archived)

This directory is the retained end-to-end run of the Chrome MV3 recorder at version
**0.4.0**, made on **2026-08-09** (UTC). Puppeteer loaded the unpacked extension into
system Chrome with a fresh, disposable profile and drove it through 14 cases on
deterministic loopback fixtures (`127.0.0.1` only). All **14/14** cases passed, with
**0 browser errors**.

It is engineering QA of the recorder. The coarse actor field is `agent` and the
source class is `synthetic_coverage`. It is not a human trace, a run of a
computer-use agent framework, a human-versus-agent comparison or a detector result.
It describes 0.4.0 only. CI reruns the same cases on the current version, and those
bundles are CI artifacts that are not committed here.

## Environment

| Item | Value | Source |
|---|---|---|
| Extension | CUI Trace Recorder 0.4.0, 32 source files | `run_manifest.json` `/extension` |
| Browser | Chrome 151.0.7922.108 (system Chrome, not Chrome for Testing), default sandbox | `/browser_runtime` |
| Driver | Puppeteer 25.4.0 | `/puppeteer_version` |
| Node | 22.21.1 | `/node_version` |
| Platform | win32-x64 | `/platform` |
| Isolation | New profile; the user's Chrome profile and processes were not touched | `/isolation` |

**Source identity.** The run hashed the extension directory as
`fa5cfc61516551f3756166b6cd8c5f764d4f1305ed3704d1dcd6f8d571e0bfc0` (32 files). During
the restructure this hash was recomputed with the runner's own directory-hash
algorithm over `recorder/` at commit `437070f` of the original public repository, and
it matches. That tree is the 0.4.0 extension imported into `extension/app/`.

The manifest also cites `repository_commit` `621ad8ac7a609b6db9e1f675284c25f10521df67`.
That commit exists only in the private source repository. It is not in the public
history of the original repository or in this repository, so use the source-tree hash,
not the commit, to identify the tested code.

## Cases

| ID | Case |
|---|---|
| T00 | Isolated bootstrap, manifest and fresh storage |
| T01 | Consent gate and versioned disclosure (`2026-08-03-v1`) |
| T02 | Real-action broad-event capture and privacy invariants (the main session, 527 records) |
| T03 | Dashboard inspect, JSON/JSONL/CSV export and delete |
| T04 | Same-origin reload resumes capture |
| T05 | Cross-origin navigation interrupts capture and clears the indicators |
| T06 | Closing the recording tab interrupts without stranding active state |
| T07 | MV3 service-worker restart during active capture |
| T08 | Storage rollover beyond one 500-event chunk (1,405 events in chunks of 500, 500, 405) |
| T09 | Raw-key double confirmation and the private-field invariant |
| T10 | A protected browser page cannot be recorded |
| T11 | An event-time step boundary survives one delayed content batch |
| T12 | Dynamic same-origin iframe capture and the all-frame final flush |
| T13 | Trusted-only storage and a post-stop negative control |

Per-case details are in `validation_summary.json` and, in human-readable form, in
`E2E_REPORT.md` (the report the runner generated; kept as generated).

What the suite does not cover:

- **Browser-start recovery.** T07 restarts the service worker during capture. No case
  restarts the browser; in 0.4.0 the `onStartup` path was covered only by a unit test.
- **Password-field timing.** T09 verified that raw key values are redacted on private
  fields. It did not check the key class or keystroke timing, which 0.4.0 still
  retained on password fields.
- **Downloads.** T03 confirmed the exports through `chrome.downloads` (0.4.0 still had
  the `downloads` permission) and the CDP download events.

## Records and event-type coverage

The T02 session stored **527 records**:

| Kind | Records | Types |
|---|---:|---:|
| Naturally observed fixture events | 516 | 35 |
| Deliberately dispatched synthetic coverage events | 10 | 10 |
| Recorder-generated `navigation` record | 1 | 1 |

The recorder supports 59 DOM event types plus the recorder-generated `navigation`
record. The session covers **42 of the 59 types**: 35 natural, 10 synthetic, with 3
(`change`, `input`, `pointercancel`) in both sets. The **17 types that never occur** are:

`beforetoggle`, `cancel`, `close`, `fullscreenchange`, `fullscreenerror`,
`gotpointercapture`, `hashchange`, `lostpointercapture`, `offline`, `online`,
`pagehide`, `pageshow`, `popstate`, `select`, `touchcancel`, `touchmove`,
`visibilitychange`.

Their listeners exist in the 0.4.0 source, but this run does not show that they
capture anything. `is_trusted` is diagnostic only: the natural/synthetic split
describes this fixture and must not be used as an actor label.

## Files

| File | Contents |
|---|---|
| `run_manifest.json` | Runtime, browser binary hash, extension identity and source-tree hash, isolation flags, actor claim |
| `validation_summary.json` | The 14 case outcomes, counts, browser errors (none) and the list of generated artifacts |
| `event_coverage_summary.json` | Supported, naturally observed, synthetically exercised and recorder-generated types, kept separate |
| `storage_lifecycle_summary.json` | Chunking, delete and rollover assertions, without typed content |
| `exports_manifest.json` | Bytes and SHA-256 of the three dashboard exports, and the CDP download events |
| `exports/` | The privacy-safe JSON, JSONL and CSV exports of the T02 session |
| `agent_extension_e2e_trace.json` | The sanitized T02 trace (`cui-trace-v2`); the file name is historical |
| `screenshots/` | Disclosure, popup, page indicator, dashboard, interruption, rollover, raw-mode warning and protected-page screens |
| `E2E_REPORT.md` | The generated run report |

The hashes in `exports_manifest.json` and `E2E_REPORT.md` still match the files in
`exports/`. Paths recorded inside these files start with
`results/week2_recorder/extension_e2e/`, the original location of this directory;
[`../LEGACY_PATHS.json`](../LEGACY_PATHS.json) maps them here. The screenshots show the
historical popup branding and the 2026-08-03 pilot disclosure text. They are kept
unedited, as described in [`../README.md`](../README.md).

The headline numbers in this README (cases, records, types, versions, hashes) have
entries in [`../INDEX.json`](../INDEX.json), section `recorder_e2e_v0.4.0`, which
`tools/ci/verify_evidence.py` checks against these files.
