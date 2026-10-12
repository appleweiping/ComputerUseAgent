# Codex Desktop MiniWoB exploratory cases

This directory ran nine bounded exploratory cases: three repetitions each of
`click-checkboxes`, `enter-text` and `choose-list`. The runner launches an
isolated Chrome profile with the real MV3 recorder and the pinned official
BrowserGym MiniWoB validator. The only permitted interaction surfaces were the
Codex in-app browser and Codex Computer Use. No model API, framework adapter,
scripted controller, manual rescue or human-participant data is used.

The protocol is [`research/configs/codex-case.json`](../configs/codex-case.json);
its bytes are unchanged, so it still matches the SHA-256 recorded for it in the
archived `source_manifest.json`. Each attempt kept its raw trace and setup
metadata only in a private root outside the repository. The archived bundles in
`research/evidence/codex_case_blocked/` contain the closed `cui-codex-case-v1`
record, synthetic-task screenshots, hashes, bounded action labels and recorder
and validator checks.

## Observed 2026-08-11 outcome

Both permitted interaction surfaces failed closed. The in-app browser could not
host the runner-launched Chrome profile with the unpacked MV3 recorder. Computer
Use returned the fresh-profile task window, but state capture rejected that same
window because of a reproducible ownership-binding error after fresh selection,
handle rehydration and a JavaScript-kernel reset. Chrome and Chrome Dev
reproduced the condition.

All nine planned cells therefore have immutable `blocked` bundles. Each bundle
records a real fresh-profile official task initialization, recorder startup and
shutdown in the private cache, a public initial screenshot, zero Codex task
actions, no reward, no eligible recorder trace, and no scripted or manual
substitution. `codex_case_summary.json` is the deterministic matrix summary and
`surface_preflight.json` the shared failure evidence (both in
`research/evidence/codex_case_blocked/`).

These cases are not a formal benchmark and are not comparable to the historical
Browser-Use, Skyvern or Stagehand pilots. They establish only what happened for
this Codex Desktop session on these nine synthetic tasks.

## Validate the archived matrix

From the repository root (read-only; this is also the `codex-case` stage of
`python -m research.reproduce`):

```
python -B -m research.codex_case.summarize --check
python -B -m pytest research/codex_case
```

The archived case records store screenshot locations as `repository_path` values
in the source repository's layout (`results/codex_case/...`). The evidence bytes
are never edited; `summarize.resolve_recorded_path` maps those paths onto the
`--results` directory (default `research/evidence/codex_case_blocked`).
Writing a summary needs `--output` outside `research/evidence`.

## Running a case (historical procedure)

The runner is frozen against recorder 0.4.0, the `recorder.version` pinned in
the protocol config. Every case binds the archived 0.4.0 source manifest
(`research/evidence/codex_case_blocked/source_manifest.json`) and the runner
never writes a new one, so it refuses to start (exit status 2, error code
`recorder_version_mismatch`) unless `extension/app/manifest.json` declares that
version. The live extension is 0.5.0, so in this repository the only supported
commands are `summarize --check` and pytest (see
[Validate the archived matrix](#validate-the-archived-matrix)). The procedure
below is kept as a record of how the archived cases were produced.

The runner needed the pinned BrowserGym environment, Chrome and a usable Codex
interaction surface. `--results` is required and must be a new directory inside
the repository but outside `research/evidence` (public artifacts are recorded by
repository-relative path); the runner refuses the archived evidence directory
and never overwrites an existing case bundle.

```
python -m research.codex_case.runner --task click-checkboxes --repetition 1 \
    --results <new results directory> \
    --private-root <private directory outside the repository> \
    --miniwob-root <verified MiniWoB source root>
```

When a permitted surface is usable, the runner prints `codex_case_ready`; the
operator then interacts through Codex and sends one JSON acknowledgement with the
observed action count and concise visible action labels. Task failure is a
retained scientific outcome; recorder, integrity or validator failure is an
error.

After a separately recorded surface preflight establishes that neither permitted
surface can act, `--record-blocked` initializes the real task and recorder only
long enough to create auditable zero-action evidence. It does not interact with
or validate the task, and it never converts setup events into an eligible trace.
