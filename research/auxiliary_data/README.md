# Auxiliary data calibration tracks

Three independent, license-scoped calibration analyses of public datasets. They
do not extend the FP-Agent training population, are never concatenated with
FP-Agent rows, and do not support a headline cross-dataset classifier.

## Locked scope

- **CaptchaSolve30k:** exactly 5,000 of the 26,469 non-touchscreen sessions
  (30,000 in total), proportionally stratified by release split and game type.
  Only duration, sample-count and tick distributions are read. The Apache-2.0
  dataset card additionally forbids re-identification, cross-source identity
  linkage, biometric profiling, behavioral authentication, surveillance and
  tracking; those restrictions are enforced here.
- **Aalto 136M Keystrokes:** exactly 5,000 of the 165,009 participants who
  report a physical keyboard, proportionally stratified by keyboard type and
  layout. Only the official participant-level aggregate metadata member (and
  the readme) is range-read from the archive. Use is research/non-commercial
  with attribution to Dhakal, Feit, Kristensson and Oulasvirta. No raw
  keystroke member or participant row is published.
- **MolmoWeb:** exactly 10,000 HumanTrajs and 10,000 SyntheticTrajs rows. The
  analysis uses only ordered action names, plus a normalized-hostname baseline,
  under five hostname-disjoint folds. Images and instructions are not read.
  Timestamp field names are audited across every released train row; timestamp
  values are discarded, and this source is never fed to the 23-feature timing
  model.

All provider revisions are pinned. Artifact byte counts and provider LFS
SHA-256 values are recorded in
`research/evidence/auxiliary_data/source_access_manifest.json`. The Aalto
provider publishes no whole-archive SHA-256, so the manifest records the exact
byte count, ETag, Last-Modified value, ZIP CRCs and locally recomputed SHA-256
values of the two members read.

## Archived results

| File under `research/evidence/auxiliary_data/` | Contents |
|---|---|
| `captchasolve30k_metrics.json`, `aalto_136m_metrics.json` | Sample-versus-population distribution calibration |
| `molmoweb_metrics.json` | Domain-held-out source-label probe and the timestamp audit |
| `summary.json`, `sampling_ledgers.json`, `source_access_manifest.json` | Track status, hash-only sampling ledgers, pinned sources and licenses |
| `auxiliary_calibration.{svg,png}` | The aggregate figure |
| `notebook_execution.json` | The original 2026-08-11 notebook execution record (historical) |

MolmoWeb result: the ordered action n-gram model separates the two sources at
balanced accuracy 0.8167 across held-out hostnames; the domain-only baseline is
0.5 by construction, because unseen hostnames map to an all-zero vector. Treat
0.8167 as a within-release source-label diagnostic with likely label leakage,
not as a detector: the timestamp audit found `action_timestamp` on all 108,254
synthetic rows and on none of the 35,971 human rows, so the two releases differ
in how they were produced and recorded. The track status follows that audit:
`complete` only when both sources are verified timestamp-free, otherwise
`complete_with_timestamp_absence_check_failed` (the archived status).
CaptchaSolve30k and Aalto are sampling-fidelity checks only and train no
classifier.

## Run

Use the research extra (`uv sync --extra research`) or the pinned
`requirements.txt`, and run from the repository root with an exact retrieval
timestamp and an output directory of your choice:

```
python -m research.auxiliary_data.run --retrieved-at-utc 2026-08-11T14:00:00Z --output-dir /tmp/auxiliary-rerun --max-workers 8
python -B -m pytest research/auxiliary_data
```

`--output-dir` is required, and the archived `research/evidence/auxiliary_data`
is refused: compare a rerun with the archived files rather than overwriting
them. The exit status is 2 when any track is blocked.

The tracks use public, read-only HTTP Range requests and never attach an
authorization header. Every request URL and every redirect hop must be HTTPS on
an allowlisted host: `huggingface.co`, subdomains of `huggingface.co` and
`hf.co` (the Hugging Face CDN and Xet storage hosts), and exactly
`userinterfaces.aalto.fi`. A redirect anywhere else, including a generic cloud
storage host, is refused before it is followed. Redirect URLs are never printed
or persisted. Requests identify themselves as
`ComputerUseAgent-research/auxiliary-data (+https://github.com/appleweiping/ComputerUseAgent)`.
Projected rows are processed in memory; only source/access manifests,
deterministic hash-only ledgers, aggregate metrics and aggregate figures are
written.

## Notebook

`notebooks/auxiliary_calibration.py` is a jupytext percent-format source with
the section order `tl;dr -> Context & Methods -> Data -> Results -> Takeaways`.
It loads only the aggregate metrics (from `CUA_AUX_RESULT_DIR`, default the
archived evidence) and writes the figure to `CUA_AUX_FIGURE_DIR` (default a
temporary directory); it never writes into `research/evidence`. Execution needs
the notebook environment from the hash-locked `notebook-requirements.lock`:

```
uv venv --python 3.12 .venv-aux-notebook
uv pip install --python .venv-aux-notebook --require-hashes --only-binary=:all: --no-deps -r research/auxiliary_data/notebook-requirements.lock
.venv-aux-notebook/bin/python -m research.auxiliary_data.execute_notebook      # Windows: .venv-aux-notebook\Scripts\python.exe
python -m research.auxiliary_data.execute_notebook --check                      # any environment
```

The executed `.ipynb` and the regenerated figures are untracked build outputs,
written to `--output-dir` (a new temporary directory by default). The tracked
`notebooks/auxiliary_calibration.execution.json` (schema
`cui-notebook-execution-v2`) binds the notebook source, the six archived
inputs, the two archived figures, the notebook lock, the fixed sampling seeds
and the archived 2026-08-11 execution record by SHA-256 and byte count.
`--check` verifies those bindings without an executed notebook. It also
cross-checks the archived record: its input, figure and lock bindings, recorded
under the original `results/...` and `experiments/...` paths and translated
through a small legacy-path map, must still match the current bytes.

Executing against the archived inputs is a reproduction check: it fails unless
both regenerated figures are byte-identical to the archived ones, and only then
rewrites the tracked manifest. The current manifest records such a clean-kernel
re-execution in the locked environment (Python 3.12.3): 5 of 5 code cells, no
errors, both figures byte-identical.

## Interpretation boundary

The MolmoWeb result is a within-release source-label diagnostic under
domain-held-out splits. It is not a population estimate, deployment detector,
future-agent test or cross-corpus result. CaptchaSolve30k and Aalto are
distribution calibration only.
