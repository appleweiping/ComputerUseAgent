# Playground fixture (archived)

A 50-event example trace recorded on 2026-08-03 in the standalone recorder
playground (the "SpectraScope" matched-task form, task `week2-matched-task-v1`), plus
its validation summary and three screenshots. An in-app browser automation process
operated the page, and the playground's copy of the recorder normalization core
(`recorder/src/core.js` at the time) produced the trace.

It is a small, readable data example. It is not an extension-storage end-to-end test,
not evidence that the unpacked Chrome extension captured a reasoning agent, and not a
human-versus-agent study. For the real-extension E2E run, see
[`../../recorder_e2e_v0.4.0/`](../../recorder_e2e_v0.4.0/README.md).

| File | Contents |
|---|---|
| `agent_demo_trace.json` | The trace, `cui-trace-v1` format: 50 events over about 2.4 s; actor label `codex-in-app-browser`, participant code `self-test` |
| `validation_summary.json` | Task validator outcome, event counts by type, a privacy audit (no printable keys, no URL query or fragment, no password-like events, raw-key capture off) and the trace's SHA-256 |
| `screenshots/01_recorder_active.png`, `02_task_complete.png`, `03_trace_dashboard.png` | The playground while recording, after the task, and the trace view |

`validation_summary.json` binds `agent_demo_trace.json` by SHA-256, and the hash still
matches. A byte-identical copy of the trace is used as a test fixture at
`extension/test/fixtures/agent_demo_trace.json`.

The original `README.md` in this directory also described an older 0.3.0 E2E run
(11/11 cases, 507 natural events). That description was stale: the E2E bundle that
was actually retained is the 0.4.0 run in `recorder_e2e_v0.4.0/` (14/14 cases, 527
records).

Collecting human traces requires the operator's own human-research determination and
an approved consent and retention process. Keep raw traces outside the repository,
under the private storage root configured by `CUA_PRIVATE_ROOT`.
