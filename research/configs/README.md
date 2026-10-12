# Research study configuration

The files here use JSON syntax (which is also valid YAML 1.2), so they can be
inspected and validated with the Python standard library. None of them
contains a credential, a private path, a VM path, a browser profile or a raw
human trace; secrets are only ever referenced as `env:NAME`.

| File | Read by | Status |
|---|---|---|
| `formal-study.json` | `computer_use_agent.study.policy` (callers pass this path, or set `CUA_FORMAL_STUDY_CONFIG`) | Historical formal protocol; fail-closed draft |
| `retrospective-study.json` | `research/fp_agent/validate_config.py` | Active, non-formal retrospective policy |
| `codex-case.json` | `research/codex_case/` | Exploratory Codex Desktop case protocol |

## `formal-study.json`

The formal-study policy of the 2026-08 snapshot: the US$200 budget stop rule,
the 3x3x3 Week 1 MiniWoB and ScienceBoard matrices (27 cells each), the
pinned provider-entitlement probe, the privacy boundaries, the prohibited
detector features and the closed `human_research_policy`.

- **Validation.** `computer_use_agent.study.policy.validate_config` checks the
  structure and the fail-closed invariants (fallback disabled, tools disabled,
  matrices that expand to the planned counts, prohibited leakage features,
  `env:` secret references, no inline keys). The exact pinned values are data:
  `tests/study/test_policy.py` checks them against a table of JSON pointers.
- **Paths.** Each stage's `manifest` is relative to this file
  (`../week1_miniwob/benchmark_manifest.json`,
  `../scienceboard/tasks/manifest.json`); `stage_manifest()` resolves them.
  The package never derives a path from the repository root.
- **Human data.** `determination_status` is `not_recorded`, so every
  formal-research and detector-training decision is `false` and self-human
  traces remain private engineering QA. To change that, an operator records a
  human-research determination under their own institution's process, stores
  the written record under `CUA_PRIVATE_ROOT` (an absolute directory outside
  every Git work tree and outside `research/evidence`), points
  `CUA_HUMAN_RESEARCH_DETERMINATION_RECORD` at it, and sets
  `determination_scope` and `determination_record_sha256` (the record's
  SHA-256) before any decision may become `true`. The record must match the
  hash every time it is used.
- **Formal runs.** A formal (non-dry-run) execution additionally requires an
  explicit, sealed run capsule whose immutable inputs are re-hashed. No such
  capsule exists: the 2026-08 capsule in `research/archive/` is historical and
  cannot be sealed after the Stagehand retirement, so formal execution stays
  blocked. Dry runs need no capsule.

The previous file name was `configs/formal-study.yaml`. Its original bytes
(SHA-256 `1a69495e...ca6ee`) are recorded in the archived capsule and in
`retrospective-study.json`; this copy was de-personalized (neutral
human-research wording, `CUA_*` environment names, relative manifest paths, a
neutral project id, and the renamed gate `task_selection_recorded` and key
`research_training_eligible_before_determination`) without changing any
budget, matrix, pin or decision value.

## Environment variables

| Variable | Meaning |
|---|---|
| `CUA_FORMAL_STUDY_CONFIG` | Default formal-study config path when none is passed |
| `CUA_PRIVATE_ROOT` | Absolute private storage root outside the repository |
| `CUA_HUMAN_RESEARCH_DETERMINATION_RECORD` | Private determination record (under `CUA_PRIVATE_ROOT`) |
| `CUI_WEEK1_PROVIDER_CERT` | Week 1 provider-entitlement certificate (historical wire name) |
| `OPENAI_API_KEY` | Provider key for the archived harnesses |
