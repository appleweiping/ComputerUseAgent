# Recorder extension E2E

`extension/test/e2e/recorder.e2e.mjs` launches Chrome in a new, disposable
profile through Puppeteer, installs the real unpacked extension from
`extension/app`, serves two loopback-only fixture origins (`127.0.0.1`, random
ports) and drives the toolbar popup, content capture, MV3 storage and the
dashboard. It never touches the user's normal Chrome profile.

## Running it

```bash
cd extension
npm ci
npx puppeteer browsers install chrome   # only if npm skipped Puppeteer's download
npm run e2e
```

`npm ci` normally downloads the Chrome for Testing build pinned by Puppeteer
25.4.0 (151.0.7922.47) into Puppeteer's cache (`PUPPETEER_CACHE_DIR` moves it).
If no managed browser is present the harness falls back to an installed Google
Chrome or Chromium and records that in the run manifest.

By default Chrome runs with a visible window. On a Linux CI runner without a
display use Xvfb:

```bash
CUA_E2E_CI_NO_SANDBOX=1 CUA_E2E_RESULT_DIR="$RUNNER_TEMP/e2e" xvfb-run -a npm run e2e
```

For local runs without a window, `CUA_E2E_HEADLESS=1` uses Chrome's new
headless mode, which supports extensions; all 15 cases pass that way with
Chrome for Testing 151 (the toolbar popup is opened through the DevTools
protocol, so no window is needed). In headless mode the `chrome://settings`
page that T10 opens logs one page error of its own; errors from such
browser-internal pages are kept in `browser_ui_errors`, separate from
`browser_errors`.

### Environment variables (all optional)

| Variable | Effect | Default |
|---|---|---|
| `CUA_E2E_CHROME` | Chrome/Chromium executable to use | Puppeteer's Chrome for Testing, else an installed Chrome |
| `CUA_E2E_RESULT_DIR` | Bundle output directory; its name must contain `e2e` and it must not be inside `research/evidence` | `<OS temp>/cua-recorder-e2e-results` |
| `CUA_E2E_RUNTIME_DIR` | Parent of the disposable profile (deleted after the run) | `<OS temp>/cua-recorder-e2e-runtime` |
| `CUA_E2E_CASES` | Comma-separated case IDs to run, e.g. `T01,T02,T03`; the rest are reported as skipped | all cases |
| `CUA_E2E_CI_NO_SANDBOX` | Exactly `1` adds `--no-sandbox --disable-setuid-sandbox`; any other value, including a generic `CI=true`, keeps Chromium's sandbox | sandbox on |
| `CUA_E2E_HEADLESS` | Exactly `1` runs Chrome's new headless mode | visible window |

The older `CUI_E2E_*` names are no longer read. The run manifest records the
effective sandbox and headless modes. Some restricted environments (containers,
locked-down agent shells) cannot start Chromium's sandbox; Chrome then exits
immediately with code 3, and only there should `CUA_E2E_CI_NO_SANDBOX=1` be
set. The fixtures are loopback-only.

## Cases

| ID | Checks |
|---|---|
| T00 | Isolated bootstrap; manifest name, version, permissions (no `downloads`); browser major >= `minimum_chrome_version`; empty storage |
| T01 | Consent gate blocks start; disclosure page shows the versioned template; popup footer shows the manifest version |
| T02 | Real input across the event surface; V2 envelope invariants; privacy sentinels absent; private-field key events have `key_class`, keyboard detail and modifiers set to `null` |
| T03 | Dashboard inspect; JSON, JSONL and CSV exports confirmed through CDP `Browser.setDownloadBehavior` + `Browser.downloadWillBegin`/`downloadProgress`; delete verified |
| T04 | Same-origin reload resumes capture in the same session |
| T05 | Cross-origin navigation interrupts and clears the indicators |
| T06 | Closing the recording tab interrupts without stranding the active state |
| T07 | MV3 service-worker restart during active capture |
| T08 | Storage rollover beyond one 500-event chunk |
| T09 | Raw-key mode double confirmation; private-field rules still apply |
| T10 | Protected browser pages cannot be recorded |
| T11 | Event-time step ids survive a delayed content batch |
| T12 | Dynamic same-origin iframe capture, all-frame final flush, cross-origin frame excluded |
| T13 | Content scripts cannot read extension storage; nothing is appended after stop |
| T14 | Content-script senders are refused `LIST`, `GET`, `DELETE`, `START`, `STOP` and `SET_STEP` and receive only the reduced state view |

T07 and T10 report `skipped` (not `passed`) if the browser does not expose the
needed target. Browser startup recovery is covered by a unit test, not by an
E2E case.

## Result bundle format

The harness writes these files into `CUA_E2E_RESULT_DIR`. Every path recorded
inside the bundle is relative to the bundle root and uses `/` separators, so
the directory can be moved or uploaded as a CI artifact
(`artifact_path_base: "bundle_root"`). The schema identifiers keep the
historical `cui-` namespace.

| File | Content |
|---|---|
| `validation_summary.json` | `schema_version: "cui-recorder-extension-e2e-summary-v1"`, `artifact_path_base`, `recorder_version`, `overall_status` (`passed`/`failed`), `claim_boundary`, `counts` {`total`, `passed`, `failed`, `skipped`}, `cases` [{`id`, `name`, `status` (`passed`/`failed`/`skipped`), `started_at`, `finished_at`, `details` or `error`/`stack`}], `browser_errors` [{`at`, `page`, `kind`, `message`}] from the extension pages and fixtures, `browser_ui_errors` (same shape) from Chrome's own `chrome://` pages, `generated_artifacts` (sorted bundle-relative paths, including the files below) |
| `run_manifest.json` | `schema_version: "cui-recorder-extension-e2e-run-v1"`, `artifact_path_base`, `started_at`, `finished_at`, `runner`, `repository_commit`, `repository_status_before_run`, `node_version`, `platform`, `puppeteer_version`, `reproducibility` (sha256 of the runner, launch policy, `package.json`, `package-lock.json`), `browser_runtime` {`product_version`, `major_version`, `headless`, `executable_name`, `executable_bytes`, `executable_sha256`, `provenance`, `puppeteer_managed_default_used`, `sandbox_mode`, `ci_no_sandbox_enabled`}, `extension` {`id`, `name`, `version`, `minimum_chrome_version`, `permissions`, `source_tree_sha256`, `source_file_count`}, `isolation` {`user_chrome_profile_used`, `user_chrome_process_touched`, `unique_profile_under_configured_runtime`, `loopback_fixture_only`, `fixture_origins`}, `actor_claim` {`actor_type`, `actor_label`, `source_class`, `cui_framework_claimed`, `human_data_claimed`, `comparison_claimed`}, `fatal_error` (`null` or {`message`, `stack`}) |
| `exports_manifest.json` | `exports` [{`format` (`json`/`jsonl`/`csv`), `file` (bundle-relative, under `exports/`), `bytes`, `sha256`, `cdp_guid`, `cdp_completed` (`true`), `cdp_received_bytes`}], `browser_download_events` [{`at`, `method`, `params` {`guid`, `state`, `total_bytes`, `received_bytes`, `suggested_filename`}}] |
| `storage_lifecycle_summary.json` | `sessions` [per-case storage, chunk and delete assertions for T02, T03 and T08, without typed content] |
| `event_coverage_summary.json` | `schema_version: "cui-recorder-e2e-event-coverage-v1"`, `source_trace`, `supported_browser_event_types`, `recorder_generated_event_types`, `naturally_observed_in_fixture`, `synthetically_exercised_in_fixture`, `recorder_generated` (each {`event_count`, `types`}), `interpretation_warning` |
| `agent_extension_e2e_trace.json` | The sanitized T02 session as exported (`cui-trace-v2`), `source_class=synthetic_coverage` |
| `E2E_REPORT.md` | Human-readable summary and case table |
| `screenshots/*.png` | Popup, page indicator, dashboard, interruption, rollover, raw-mode warning and protected-page UI |
| `exports/cui-trace-*.{json,jsonl,csv}` | The dashboard exports from T03 |

Compared with the retained 0.4.0 bundle, a 0.5.0 bundle has bundle-relative
paths (0.4.0 recorded repository-relative `results/week2_recorder/...` paths),
15 cases instead of 14, no `chrome_download_id` in `exports` (there is no
`downloads` permission), and the additional fields `artifact_path_base`,
`recorder_version`, `browser_ui_errors`, `browser_runtime.major_version`, `browser_runtime.headless`,
`extension.minimum_chrome_version`, `extension.permissions` and
`exports[].cdp_received_bytes`.

`tools/ci/validate_e2e_bundle.py` checks a bundle in CI (all 15 cases passed,
zero browser errors outside `browser_ui_errors`, version, permissions and
Chrome floor bound to `extension/app/manifest.json`, exports present with
matching size and sha256, isolation and claim attestations). Bundles from current versions are CI artifacts and are not
committed.

## Retained evidence

`research/evidence/recorder_e2e_v0.4.0/` keeps the 0.4.0 bundle from
2026-08-09 (14/14 cases, 0 browser errors, Chrome 151.0.7922.108 with an
isolated profile, Puppeteer 25.4.0, Node 22.21.1, win32-x64). Its bytes are
archival and verified only by `research/evidence/MANIFEST.sha256`; it is not
re-validated against the current source.

Every automated recording uses the coarse `actor_type=agent` label and
`source_class=synthetic_coverage`. A bundle is extension QA, not an agent
framework run, human evidence, or a detector result.
