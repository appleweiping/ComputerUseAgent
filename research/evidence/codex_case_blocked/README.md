# Codex Desktop case: 9/9 blocked (archived)

Nine exploratory cells were planned: Codex Desktop on three official MiniWoB tasks
(`click-checkboxes`, `enter-text`, `choose-list`), three repetitions each, recorded
by the unpacked MV3 recorder. **All 9 were blocked** on 2026-08-11, after real task
initialization and before any agent action:

- The Codex in-app browser offered no supported way to launch the Chrome profile
  that carries the unpacked recorder (`mv3_extension_surface_unavailable`).
- The computer-control surface rejected the one fresh Chrome window it returned as
  not belonging to its own app (`window_binding_ownership_failure`), after four
  recovery attempts.

No scripted control (CDP, Playwright, Puppeteer) or human action was substituted, no
manual rescue was attempted, and no recorder trace was eligible for analysis. These
cases are not a framework benchmark, a formal study, a matched human-agent study, or
evidence about computer-use agents in general.

## Files

| File | Contents |
|---|---|
| `codex_case_summary.json` | The 9 attempts, their status (`blocked`), failure code, action count (0) and the SHA-256 of each `case.json` |
| `surface_preflight.json` | The two blocked interaction surfaces and the recovery steps tried |
| `source_manifest.json` | Provenance of the configuration, runner and task sources at the time of the run |
| `attempts/<case>/case.json` | One record per cell; binds its `initial.png`, `verification.json`, `source_manifest.json` and `surface_preflight.json` by SHA-256 |
| `attempts/<case>/initial.png` | The initialized MiniWoB task before any action |
| `attempts/<case>/verification.json` | Outcome checks for the cell |

All these internal hashes still match. `case.json` records the locations of the bound
files as `results/codex_case/...`, the original path of this directory; code that
resolves them must translate them through [`../LEGACY_PATHS.json`](../LEGACY_PATHS.json).
`source_manifest.json` records source paths in the original layout (`configs/`,
`experiments/`); see [`../../PROVENANCE.md`](../../PROVENANCE.md). The planned
and blocked counts have entries in [`../INDEX.json`](../INDEX.json), section
`formal_studies`.
