# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). From 0.5.0 the
Python package, the Chrome extension and `CITATION.cff` share one version;
wire formats are versioned independently through their `schema_version`.
Archived research evidence is never rewritten to make a later interpretation
look contemporaneous.

## [Unreleased]

## [0.5.0] - 2026-10-11

The research artifact `cu-detector-research-artifact` (snapshot `437070f`,
2026-08-18) is restructured into the maintained project **ComputerUseAgent**.
This release is breaking in package name, CLI and repository layout, which is
a minor bump while the project is 0.x.

### Security

- **Password and private fields no longer leak keystroke detail.** In 0.4.0 the
  recorder replaced the key itself with `<redacted>` on password fields,
  `autocomplete=*-password` fields and elements inside
  `[data-recorder-private]`, but it still stored the key class (for example
  `letter` or `digit`), the modifier flags and the keystroke detail
  (`keyboard.hold_ms`, `keyboard.repeat`, `keyboard.location`), although the
  documentation said such fields were "always redacted". The archived 0.4.0 E2E
  trace shows all 70 private-field key events with a key class and 35 with a
  hold time. From 0.5.0 the key, the key class, the modifier flags and that
  detail are all dropped for those targets, in default and in restricted
  raw-key mode, and the service worker re-applies the rule to every stored
  event. The existence, count and timestamps of key events on private fields
  are still recorded (see `docs/extension/privacy.md`). Traces recorded with
  0.4.0 on such fields contain the key class sequence and timing; treat them as
  sensitive and delete them if that is not acceptable.
- **Fields inside shadow DOM are classified on the real element.** 0.4.0
  classified every event on the shadow host it was retargeted to, so a password
  field inside a web component was not recognized as private: in raw-key mode
  its typed keys were stored in plain text, and in the default mode its key
  class, modifier flags, hold time and input lengths were stored. 0.5.0
  classifies events on `composedPath()[0]`, makes key, input, composition and
  clipboard events follow focus into open and closed shadow roots, honours
  `data-recorder-private` across slots and shadow hosts, and records an event
  as private when its focused element cannot be resolved. Affected 0.4.0
  traces show key or input events whose `target.tag` is a custom element with
  `password_like: false` (see `docs/extension/privacy.md`).
- **Extension message API hardened.** Privileged messages (start, stop, set
  step, list, get and delete sessions) are accepted only from the extension's
  own pages; content scripts in web pages may send only event batches, drop
  reports, frame discovery and a reduced state query, and the session-start
  notice pushed to content scripts carries only that reduced view (no actor,
  participant or task details). A rejected or failed storage write can no
  longer poison the write queue for later requests, and deleting a session no
  longer reads the whole extension store.
- **Removed the unused `downloads` permission.** The 0.5.0 permissions are
  `activeTab`, `scripting`, `storage` and `unlimitedStorage`, plus the optional
  per-origin host permission. Exports keep working; the E2E suite confirms them
  through the Chrome DevTools Protocol instead of `chrome.downloads`.
- **`minimum_chrome_version` raised from 102 to 151**, the Chrome for Testing
  build the E2E suite runs (Chrome 102 was declared but never tested).
- The extension disclosure is a neutral operator template (version
  `2026-10-11-template-v2`) instead of institution-specific wording.

### Changed

- New names: Python distribution and console script `computer-use-agent`,
  import package `computer_use_agent` (src layout), extension
  "ComputerUseAgent Trace Recorder", environment-variable prefix `CUA_`.
  Wire-format identifiers keep their historical `cui-` namespace
  (`cui-trace-v2`, `cui-detector-dataset-v2`, the `chrome.storage` keys), so
  existing traces, sessions in Chrome profiles and archived evidence stay valid.
- Repository layout: the installable package lives in `src/computer_use_agent/`
  (JSON Schemas ship as package data), the extension in `extension/` (load
  `extension/app` unpacked), example traces in `examples/traces/`, and all
  research harnesses, analyses, literature and evidence in `research/`
  (not part of the wheel). `research/PROVENANCE.md` maps every original path.
- One version source: `pyproject.toml`, `extension/app/manifest.json`,
  `extension/package.json` and `CITATION.cff` must agree
  (`tools/ci/check_versions.py`); the popup reads the version from the manifest.
- Private storage is configured with `CUA_PRIVATE_ROOT` (must resolve outside
  the repository and outside `research/evidence`) instead of a fixed drive
  letter; the formal-study configuration is an explicit argument or
  `CUA_FORMAL_STUDY_CONFIG`.
- The human-research determination record is configured with
  `CUA_HUMAN_RESEARCH_DETERMINATION_RECORD`, and the extension disclosure is a
  neutral template that operators fill with their own approved wording.
- `cui-trace-v1` and `cui-trace-v2` accept `null` for `keyboard.repeat`,
  `keyboard.location` and the four modifier flags (meaning "not recorded"), a
  backward-compatible widening required by the private-field fix.
- The recorder E2E suite has 15 cases (T00-T14; T14 checks in a real browser
  that content scripts are refused privileged messages) and its environment
  variables are renamed from `CUI_E2E_*` to `CUA_E2E_*`. Results go to the OS
  temporary directory by default.
- The ScienceBoard guest receives a single deterministic standard-library
  zipapp (`computer-use-agent-x11-recorder.pyz`) instead of five loose files.
- Archived research evidence moved to `research/evidence/`, is indexed by
  `research/evidence/INDEX.json` and verified against
  `research/evidence/MANIFEST.sha256`; it is never re-validated against current
  source. Its bytes are unchanged except for three minimal redactions: the OSF
  view-only capability link in two FP-Agent result files became the public
  project URL `https://osf.io/j6b5p/`, and a personal name in the simulator run
  manifest became a role. No archived manifest binds those files.
- The two large literature files (citation graph and assertions) are no longer
  committed; `python -m research.literature.fetch_large` downloads and verifies
  them.

### Fixed

- CSV export no longer prefixes negative numbers with the spreadsheet formula
  guard (`-12.5` was exported as text `'-12.5`); strings, `NaN` and infinities
  that start with `=`, `+`, `-`, `@`, a tab or a carriage return are still
  guarded.

### Added

- `computer-use-agent` CLI with `validate`, `extract`, `analyze` (`--profile
  smoke|default`), `verify-bundle`, `view`, `schema list|show` and `doctor`.
- Provenance-validator registry (`computer_use_agent.detector.provenance`):
  datasets that carry provenance without a registered validator fail closed.
- Industrial tooling: `uv.lock`, `noxfile.py` sessions for Windows and Linux,
  pre-commit hooks, ruff and mypy (`--strict` for the package), a branch
  coverage gate, ESLint and a Node coverage gate for the extension, extension
  icons and a deterministic extension ZIP that also carries the repository's
  `LICENSE` and `NOTICE`.
- GitHub Actions: CI (lint, type check, Python 3.12/3.13 on Ubuntu and
  Windows, research tests, extension tests, evidence and docs checks, build),
  a headful recorder E2E job under Xvfb, CodeQL, dependency audits and a
  tag-driven release with SBOM, checksums and build-provenance attestations.
  Every action is pinned to a commit SHA.
- Governance: `SECURITY.md` (private vulnerability reporting),
  `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, issue and pull-request templates,
  `CODEOWNERS`, Dependabot.

### Removed

- The public-mirror export and audit, the claim scanner, status hashing,
  release-capsule tooling and their workflows; the frozen release capsule is
  kept at `research/archive/release-capsule-2026-08.json` as a historical,
  unvalidated record.
- Internship deliverables, slides and report drafts (the predecessor repository
  remains the archive; see `research/PROVENANCE.md`), the broken
  `demo/trace.html` page and the committed SBOM (now generated per release).
- Stagehand-coupled tests and imports in the archived Week 1 harness, which is
  documented as frozen against recorder 0.4.0.

## [0.4.0] - 2026-08-18

Historical research snapshot (`cu-detector-research-artifact`, commit
`437070f`), condensed.

- Recorder 0.4.0, the first TraceEnvelopeV2 release: contiguous `event_seq`,
  navigation generation and strictly increasing session time assigned in the
  service worker; deterministic V1-to-V2 import and V2-to-V1 projection;
  atomic metadata commits, storage-failure counters, a 100,000-event
  per-session ceiling and a 250-session ceiling that fails closed.
- 59 DOM event types plus a recorder-generated navigation record, sampled
  pointer/drag/touch/scroll streams, key classes instead of typed characters by
  default, URL queries and fragments dropped, JSON/JSONL/CSV exports.
- Retained clean-profile Chromium E2E evidence: 14/14 cases, 0 browser errors,
  527 records (now in `research/evidence/recorder_e2e_v0.4.0/`).
- Standard-library detector pipeline with 23 behavioral features, strict
  leave-one-task-out evaluation, nested Platt calibration and a self-verifying
  result bundle; it has run only on synthetic data and always reports
  `formal_claim_permitted=false`.
- Research evidence: the FP-Agent retrospective and leave-one-family-out
  probes, three agent pilots, the blocked formal MiniWoB, ScienceBoard and Codex
  matrices (no human traces collected), auxiliary-data checks and a 68-item
  literature corpus. Stagehand 3.7.1 was retired from active use.
- Known issue fixed in 0.5.0: key class and keystroke timing were retained on
  password and private fields (see 0.5.0, Security).

[Unreleased]: https://github.com/appleweiping/ComputerUseAgent/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/appleweiping/ComputerUseAgent/releases/tag/v0.5.0
[0.4.0]: https://github.com/appleweiping/cu-detector-research-artifact/tree/437070f519d4bd40e9e4e46bf19fcca52b4b7557
