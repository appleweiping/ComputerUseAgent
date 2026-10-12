# FP-Agent retrospective analyses

Three retrospective analyses of the public FP-Agent corpus
([ethanbwang/fp-agent](https://github.com/ethanbwang/fp-agent), data on the OSF
project <https://osf.io/j6b5p/>). None of them is a validated detector, and
none supports a claim about future agents, other corpora or deployment.

| Analysis | Module | Archived result |
|---|---|---|
| Released-classifier inference on the authors' random split | `evaluate_fp_agent_classifiers.py` | `research/evidence/fp_agent/fp_agent_classifier_eval.json` |
| Leave-one-agent-family-out (LOAFO) on the released features | `leave_one_family_out.py` | `research/evidence/fp_agent/fp_agent_loafo.{json,csv}` |
| Raw-event 23-feature retrospective (primary: purged leave-one-task-out) | `run_retrospective.py` and the modules it drives | `research/evidence/fp_agent/fp_agent_retrospective.json`, `figures/` |

The archived results are hash-bound evidence. A rerun never overwrites them:
every command writes to its own output directory, and any output path under
`research/evidence/` is refused. Compare a rerun with the archived file instead.

## Inputs and locations

Nothing private is looked up relative to the checkout.

| Input | Flag | Environment default |
|---|---|---|
| Private data root: `feature_vectors.json`, `train_test_split.npz`, `raw_dataset.json`, the derived `features_23.jsonl` cache and its ledger | `--data-root DIR` | `$CUA_PRIVATE_ROOT/fp_agent` |
| Upstream FP-Agent git checkout at commit `e614e2f0f935211e22d856195d56e91fa88a7b16` | `--upstream-repo DIR` | `$CUA_FP_AGENT_UPSTREAM_REPO` |
| Aggregate outputs | `--output` / `--output-json` / `--output-csv` / `--output-dir` | `<data-root>/outputs/` |

The data root must resolve outside the repository checkout: the corpus is
restricted third-party data and the derived cache is row-level. The released
files are verified against the byte counts and SHA-256 values in
`raw_artifacts_manifest.json` before they are used. The split file is a NumPy
archive with object arrays, so it is only unpickled after that verification.

The OSF node is not publicly listed. The upstream README publishes a share link
that grants read access. `official_range_download.py` reads that link from the
upstream checkout at run time and forwards its access query to the OSF API
only. The link is never logged, written to the download ledger or written to an
output. New outputs name the project as `https://osf.io/j6b5p/`.

## Environments

The analyses import `computer_use_agent` (feature extraction and the feature
contract), so the checkout must be importable in the analysis environment. Run
every command from the repository root.

- Development environment: `uv sync --extra research` provides numpy,
  scikit-learn, xgboost and ijson.
- Locked reproduction environment (how the archived retrospective result was
  produced; Python 3.12):

  ```
  uv venv --python 3.12 .venv-fp
  uv pip install --python .venv-fp --require-hashes --only-binary=:all: --no-deps -r research/fp_agent/requirements.lock
  uv pip install --python .venv-fp --no-deps -e .
  ```

- Notebook environment (adds pandas, seaborn, matplotlib and the Jupyter stack):
  the same commands with `research/fp_agent/notebook-requirements.lock`.

xgboost is imported lazily, only by the functions that train or load XGBoost
models. The LOAFO data-handling tests therefore run without xgboost, and four
of the five run without numpy or scikit-learn at all; the metric test skips with
a reason when those are absent (`tests/test_optional_dependencies.py` checks
both cases in a fresh interpreter).

## Commands

```
export CUA_PRIVATE_ROOT=/absolute/path/outside/the/checkout     # PowerShell: $env:CUA_PRIVATE_ROOT = "..."
export CUA_FP_AGENT_UPSTREAM_REPO=/absolute/path/to/fp-agent
git clone https://github.com/ethanbwang/fp-agent.git "$CUA_FP_AGENT_UPSTREAM_REPO"
git -C "$CUA_FP_AGENT_UPSTREAM_REPO" checkout e614e2f0f935211e22d856195d56e91fa88a7b16

# Released-classifier inference (needs feature_vectors.json and train_test_split.npz)
python -m research.fp_agent.evaluate_fp_agent_classifiers

# Leave-one-agent-family-out
python -m research.fp_agent.leave_one_family_out --dry-run
python -m research.fp_agent.leave_one_family_out

# Raw-event retrospective: download, extract, evaluate, then execute the notebook
python -m research.fp_agent.run_retrospective --download-raw --rebuild-features

# Validate the retrospective policy config
python -m research.fp_agent.validate_config
```

Each command also accepts the flags in the table above in place of the
environment variables. The old spellings `--repo` and `--data-dir` remain as
aliases.

## Released-classifier inference

An inference-only reproduction of the three released all-class XGBoost
classifiers on the authors' released random split (1,544 test examples, 8
classes). It does not retrain the models and does not test out-of-distribution
or future-agent generalization. The archived run used Python 3.13.14, outside
the lock.

| Features | Accuracy | Macro-F1 |
|---|---:|---:|
| Browser fingerprint | 0.8212 | 0.8223 |
| Behavioral | 0.9994 | 0.9994 |
| Combined | 1.0000 | 1.0000 |

The result records the upstream commit, model, feature-vector and split SHA-256
values, runtime versions, per-class metrics and confusion matrices.

## Leave-one-agent-family-out probe

After removing one named agent family from training, can a binary classifier
trained on the other six families and humans recognize the unseen family? The
run uses the released `feature_vectors.json` (SHA-256
`54aa9eb6e74a509b86a3504f97e1f2c6fee99467c1ac935b92a5385e9e1cf641`). Exact
duplicate combined vectors are collapsed within each family. Humans are split
by the dataset's opaque participant token (52 train groups, 14 test groups, 109
test rows), so no participant group is on both sides. Source identifiers and
row-level predictions are not exported. The archived run used Python 3.14.5,
outside the lock.

| Features | Mean balanced accuracy | Worst-family balanced accuracy | Mean unseen-family recall | Worst-family recall | Human specificity |
|---|---:|---:|---:|---:|---:|
| Browser fingerprint | 0.9286 | 0.5000 | 0.8571 | 0.0000 | 1.0000 |
| Behavioral | 0.9991 | 0.9968 | 0.9982 | 0.9936 | 1.0000 |
| Combined | 0.9992 | 0.9968 | 0.9984 | 0.9936 | 1.0000 |

The browser-only classifier labels every held-out Manus row as human at the
fixed 0.5 threshold even though its AUROC is 0.9174: ranking quality does not
guarantee a usable threshold. Agent family is entangled with software,
browser/OS setup, model and collection period; only three website tasks are
represented; rows are repeated attempts rather than an independent sample; and
the same human test rows are reused in all seven folds. The aggregates are
descriptive, not population estimates.

## Raw-event 23-feature retrospective

`retrospective_features.py` streams the upstream `raw_dataset.json` one trial at
a time and recomputes exactly the shared 23-feature allowlist of
`computer_use_agent.features`. Inter-key latency is keydown-to-keydown.
FP-Agent records no focus events, so `focus_transition_count=0` and
`missing_focus=1` for every row. Element IDs, typed keys, request headers and
source metadata never enter the derived matrix or the result.

`official_range_download.py` uses eight bounded Range workers, validates every
`206 Content-Range` and byte count, retries with exponential backoff, keeps a
chunk ledger in the data root, and installs `raw_dataset.json` only after the
whole-file SHA-256 matches. Redirects are limited to `osf.io`,
`files.osf.io` and `storage.googleapis.com`.

`retrospective_evaluation.py` runs the prespecified protocol (seed 20260811):
3-fold leave-one-task-out logistic regression as the primary analysis. Every
participant or agent-trial group seen in the held-out task is purged from outer
training. Scaling and the nested, group-disjoint out-of-fold Platt calibration
stay inside the reduced training set; `Protocol.calibration_splits` (default 3)
sets the calibration folds and `Protocol.calibration_bins` (default 10) the ECE
and calibration-curve bins (`--calibration-splits`, `--calibration-bins`).
Controls: 2,000 whole-task bootstrap draws, 1,000 group-label permutations
stratified by observed task coverage, XGBoost as a secondary sensitivity
analysis, seven agent-family holdout folds, task and source encoding probes,
and grouped and single-feature ablations. Failed folds stay in the denominator.

The archived run (Python 3.12.3 from `requirements.lock`) extracted 7,728
sessions (546 human, 7,182 agent). Of 1,811 participant/trial groups, 1,721
span more than one task, so the purge leaves only 79, 212 and 37 training rows
in the three task folds.

| Primary metric | Value |
|---|---:|
| Balanced accuracy | 0.6001 (range over the 3 task folds 0.4974-0.8527) |
| AUROC | 0.6358 |
| Macro-F1 | 0.5746 |
| AUPRC | 0.9584 (inflated by 92.9% agent prevalence) |
| Brier / ECE | 0.1141 / 0.1376 |

Per task: Flight-booking BA 0.5000 / AUROC 0.998; Forums BA 0.4974 / AUROC
0.829; Shopping BA 0.8527 / AUROC 0.967. Read this as a cross-task threshold
and calibration transfer failure, not as chance-level ranking; the pooled
AUROC mixes three separately calibrated models. The "95% interval" is the
whole-task bootstrap over only three units. The permutation p-value 0.000999 is
the minimum possible with 1,000 draws and does not remove task or source
confounding: the same features predict the task at balanced accuracy 0.8662 and
the agent family at 0.9744. Dropping the keyboard group raises balanced
accuracy by 0.181. Secondary XGBoost reached balanced accuracy 0.6893; the
seven-family logistic probe 0.9130 inside this corpus. None of these is
evidence of future-agent, deployment or cross-corpus generalization.

## Notebook

`notebooks/retrospective.py` is a jupytext percent-format source with the fixed
section order `tl;dr -> Context & Methods -> Data -> Results -> Takeaways`. It
refuses to run unless the result and its status file are complete and
hash-bound. It reads the result from `CUA_FP_RESULT_DIR` and writes figures to
`CUA_FP_FIGURE_DIR` (both set by the driver); it never writes into
`research/evidence`.

```
python -m research.fp_agent.execute_notebook --check      # verify the tracked bindings
python -m research.fp_agent.execute_notebook               # re-execute in a clean kernel
```

The executed `.ipynb` and the regenerated figures are untracked build outputs,
written to `--output-dir` (a new temporary directory by default). The tracked
`notebooks/retrospective.execution.json` (schema `cui-notebook-execution-v2`)
binds the notebook source, the archived result and status, the archived figure
manifest and all seven archived figures by SHA-256 and byte count, plus the
notebook lock. `--check` verifies those bindings and does not need an executed
notebook. Archived evidence records its original `results/...` and
`experiments/...` paths; they are translated through a small legacy-path map
(`paths.resolve_recorded_path`), never rewritten.

Executing against the archived result is a reproduction check: it fails unless
every regenerated figure is byte-identical to the archived one, and only then
rewrites the tracked manifest. The current manifest records such a clean-kernel
re-execution in the locked notebook environment (Python 3.12.3): 10 of 10 code
cells, no errors, all seven figures byte-identical. It also keeps a summary of
the original 2026-08-11 execution under `original_execution`, which later
reruns carry forward. Executing against another `--result-dir` (for example a
new run's output directory) writes the manifest next to that run's outputs.

## Tests

```
python -B -m pytest research/fp_agent
```

Tests that need the private corpus are marked `requires_private_data` and skip
cleanly when `CUA_PRIVATE_ROOT` (or the named file) is absent; the raw-corpus
hash check is also marked `slow`.
