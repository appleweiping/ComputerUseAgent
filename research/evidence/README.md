# Research evidence (archived)

This directory holds the archived results of the research phase that preceded
ComputerUseAgent. The files are kept as they were recorded, apart from the three
minimal redactions listed under [Changes made during the restructure](#changes-made-during-the-restructure).
They are verified only against [`MANIFEST.sha256`](MANIFEST.sha256), never against
the current source code.

The files contain configuration, task identifiers, action summaries, deterministic
validation outcomes, aggregate metrics, hashes and reported costs. They contain no
complete web page bodies, raw model messages, cookies, browser profiles, credentials
or private source datasets.

**What this evidence does not show.** There is no validated human-versus-agent
detector. No human trace has been collected by this project. The formal MiniWoB
matrix is 0/27, the formal ScienceBoard matrix is 0/27, and all 9 Codex Desktop
attempts were blocked. The only real-data detection evidence is the retrospective on
the public FP-Agent corpus in [`fp_agent/`](fp_agent/README.md), and it shows a
cross-task transfer failure with strong task and source confounds.

## Layout

| Directory | What it holds | Path in the original repository |
|---|---|---|
| [`recorder_e2e_v0.4.0/`](recorder_e2e_v0.4.0/README.md) | Isolated real-extension E2E run of recorder 0.4.0: 14/14 cases, 527 records | `results/week2_recorder/extension_e2e/` |
| [`fp_agent/`](fp_agent/README.md) | FP-Agent released-model reproduction, leave-one-agent-family-out, 23-feature retrospective and figures | `results/fp_agent/` |
| [`pilots/`](pilots/README.md) | Browser-Use and Skyvern pilots (9/9 each), Stagehand smoke run (3/3), cost reconciliation | `results/{browser_use,skyvern,stagehand}/`, `results/cost_summary.json` |
| [`week1_miniwob_prefix_failures/`](week1_miniwob_prefix_failures/README.md) | The two failed Browser-Use launches of the formal MiniWoB matrix (0/27) | `results/week1_browsergym_pre_fix_c56a7b5/` |
| [`scripted_simulator_control_v0.2.1/`](scripted_simulator_control_v0.2.1/README.md) | Nine scripted NetLogo/PhET control runs with recorder 0.2.1; the ScienceBoard correction (0/27) | `results/week3_scientific_simulators/` |
| [`codex_case_blocked/`](codex_case_blocked/README.md) | Nine blocked Codex Desktop cases on three MiniWoB tasks | `results/codex_case/` |
| [`auxiliary_data/`](auxiliary_data/README.md) | MolmoWeb, CaptchaSolve30k and Aalto sampling-fidelity and leakage checks | `results/auxiliary_data/` |
| [`archive/playground_fixture/`](archive/playground_fixture/README.md) | 50-event playground trace from the standalone recorder core, with screenshots | `results/week2_recorder/` (top level) |
| [`archive/week1_recap/`](archive/week1_recap/README.md) | Week 1 screenshots and the superseded 43-record literature dashboards | `results/week1_recap/` |

## Integrity: `MANIFEST.sha256`

[`MANIFEST.sha256`](MANIFEST.sha256) is in `sha256sum` format: one
`<sha256>  <path>` line per file, sorted by path, LF line endings, paths relative to
the repository root. It covers every file under `research/evidence/` (except the
manifest itself) and every file under `research/literature/generated/` except the
git-ignored download directory `research/literature/generated/large/`.

```
python tools/ci/verify_evidence.py            # check (the default); CI runs this
python tools/ci/verify_evidence.py --write    # regenerate after an intentional change
sha256sum -c research/evidence/MANIFEST.sha256  # equivalent hash check with coreutils
```

The check fails when a listed file is missing, when a covered file is not listed, or
when a hash differs. `.gitattributes` marks both trees `-text`, so Git never rewrites
their line endings. Only regenerate the manifest for a deliberate, documented change.

## Claims: `INDEX.json`

[`INDEX.json`](INDEX.json) lists the headline claims. Each entry has:

| Field | Meaning |
|---|---|
| `id` | Stable identifier, for example `fp_agent.retro.pooled_ba` |
| `section` | Grouping (`recorder_e2e_v0.4.0`, `fp_agent_retrospective`, `formal_studies`, ...) |
| `claim` | The statement in plain English |
| `file` | Repository-relative path of the backing file |
| `json_pointer` | RFC 6901 pointer into `file`, or `null` for a claim stated only in prose or code |
| `derive` | Optional derivation applied to the pointer target: `length`, `sum`, `union_length` (with `with` pointers) or `difference` (with `minus` pointers) |
| `value` | The exact value at the pointer after the derivation |
| `display` | Optional rounded form for prose, for example `0.6001` |
| `caveat` | How the number must be qualified |
| `note`, `text` | For `json_pointer: null`: why there is no pointer, and optional text that must occur in `file` |

`tools/ci/verify_evidence.py` resolves every pointer and fails unless it yields
`value` exactly. Most entries point into this directory. The literature counts point
into `research/literature/generated/`. Two Stagehand entries point into the frozen
MiniWoB harness manifest `research/week1_miniwob/benchmark_manifest.json`. One entry
points at the FP-Agent feature extractor, which enforces the missing-focus contract in
code. To add a claim, add an entry and run the check. Do not copy a number into
documentation without an INDEX entry behind it.

## Paths recorded inside the evidence: `LEGACY_PATHS.json`

Paths recorded inside these files use the original layout (`results/...`,
`experiments/...`, `recorder/...`, `scripts/...`). The evidence is not edited to
update them. [`LEGACY_PATHS.json`](LEGACY_PATHS.json) maps every `results/...` prefix
to its current directory (the longest matching prefix wins), and
`translate_legacy_path()` in `tools/ci/verify_evidence.py` applies it. The check
fails if any `results/...` path recorded in an evidence JSON file does not resolve
to an existing file through the map. The other prefixes are mapped in
[`../PROVENANCE.md`](../PROVENANCE.md).

## Changes made during the restructure

The source snapshot is `results/` at commit `437070f` of the original repository (see
[`../PROVENANCE.md`](../PROVENANCE.md)). Relative to it:

- **Redactions (minimal byte edits; no numeric value changed).**
  - `fp_agent/fp_agent_classifier_eval.json` at `/dataset/source` and
    `fp_agent/fp_agent_loafo.json` at `/source/feature_vectors_source`: an OSF
    view-only share link was replaced with the public project URL
    `https://osf.io/j6b5p/`.
  - `scripted_simulator_control_v0.2.1/run_manifest.json` at
    `/mentor_selection_status`: a person's name was replaced with the word
    `mentor`.

  No file inside this directory hash-binds these three files. Their old hashes were
  bound only by status and mirror files of the original repository, which were not
  imported. Each file's other bytes, including its line endings, are unchanged.
- **Documentation.** This README and the per-directory READMEs were rewritten or
  added.
- **New files.** `INDEX.json`, `LEGACY_PATHS.json` and `MANIFEST.sha256`.

Every other file is byte-identical to its source.

### Left unchanged on purpose

- Screenshots of the recorder popup (`recorder_e2e_v0.4.0/screenshots/`, the
  `recorder-agent-metadata.png` files under `scripted_simulator_control_v0.2.1/runs/`)
  show the original lab's name in the historical popup header. The disclosure
  screenshot `recorder_e2e_v0.4.0/screenshots/01-versioned-disclosure.png` shows the
  2026-08-03 pilot disclosure, which refers to the original institution's review
  process. Editing pixels is not a minimal edit, and the simulator screenshots are
  hash-bound by their `run_result.json` files.
- Old paths, old product names (`CUI Trace Recorder`) and the `cui-` wire
  identifiers inside the JSON files. `cui-` remains the wire namespace.
- `artifact_id` values such as `private-recorder-trace-...` name artifacts that were
  never published. They are opaque identifiers, not paths.

## Claim boundaries

- The recorder E2E and scripted simulator runs are engineering QA of the recorder.
  They are not human data, reasoning-agent data, paired traces or detector results.
- The Browser-Use and Skyvern results are controlled integration evidence on three
  public, read-only micro-tasks, not benchmark scores. Stagehand was a supplemental
  single-pass smoke run and was later retired from the formal matrix.
- The FP-Agent released-model results are inference on the released within-corpus
  split, not evaluation on unseen agents or deployment distributions. The
  leave-one-family-out and 23-feature results carry the caveats in `INDEX.json`.
- The auxiliary datasets are sampling-fidelity and leakage checks. They are never
  combined with FP-Agent and never reported as a detector result.
