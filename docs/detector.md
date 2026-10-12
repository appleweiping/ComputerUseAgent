# Detector analysis pipeline

`computer_use_agent.detector` is an exploratory, leakage-controlled analysis of
session-level human-versus-agent separation under strict leave-one-task-out
(LOTO) evaluation. It is analysis infrastructure, **not a validated detector**:
it has only run on synthetic fixtures, every result carries
`formal_claim_permitted: false`, and no deployable model is ever produced.

The library is standard-library Python apart from `jsonschema` (used only to
validate files) and is fully deterministic for a given configuration.

## Contract and leakage boundary

- The model matrix is selected by the exact 23-name `FEATURE_ALLOWLIST`
  (`computer_use_agent.features.FEATURE_NAMES`), grouped for ablations by
  `FEATURE_GROUPS`: timing 7, pointer 6, keyboard 3, focus_and_mix 3,
  missingness 4. Drift tests keep the allowlist, the groups and the
  `cui-detector-dataset-v2` schema identical.
- Labels, task groups and provenance are stored beside the vector, never in it.
  Twenty forbidden name tokens (actor, framework, task, URL, selector, UUID,
  session, absolute time, software versions, validator, trust, action text,
  instruction, executor, participant, ...) and 39 leakage fields can never
  enter the matrix; an unexpected feature fails the run.
- The dataset gate fails closed unless there are at least three tasks, exactly
  the `human` and `agent` classes, both classes in every task, and at least three
  records per task. This is a structural gate, not evidence that the sample is
  scientifically sufficient.

## Stages

1. `extract.py` validates browser or X11 traces with the shared trace reader,
   extracts the reviewed feature vector and emits path-free records. It keeps a
   source SHA-256 and an opaque record reference, never a path or session UUID.
2. `model.py` provides deterministic full-batch logistic regression, Platt
   calibration and training-fold standardization.
3. `evaluation.py` fits each LOTO fold, calibrates from nested task-held-out
   training scores and evaluates the untouched outer task.
4. Controls: a 3-fold task-identification negative control, within-task label
   permutation, task-cluster bootstrap intervals, calibration/Brier metrics, and
   5 modality-group plus 23 single-feature ablations.
5. `reporting.py` adds task-internal `n`, mean, median and human-minus-agent
   Hedges g for every feature. Source task names are replaced by `task-01`,
   `task-02`, ... in sorted order (`sorted_source_task_to_ordinal_v1`).
6. `bundle.py` writes the result bundle described below.

Fold coefficients are stability diagnostics, not causal effects.

## Command line

All commands are subcommands of the `computer-use-agent` console script (also
`python -m computer_use_agent`).

### Extract a dataset from traces

Create an extraction manifest outside the repository. Trace paths may be
absolute or relative to the manifest; no path is copied into the dataset.

```json
{
  "schema_version": "cui-detector-extraction-manifest-v1",
  "traces": [
    {
      "trace": "traces/session-001.json",
      "label": "human",
      "task_group": "miniwob.click-checkboxes",
      "agent_family": null,
      "evidence_class": "external_public",
      "participant_scope": "public_dataset"
    }
  ]
}
```

```sh
computer-use-agent extract --manifest my-manifest.json --output dataset.json
```

The dataset is written atomically (a temporary file in the same directory is
renamed over the destination), so a failed run never leaves a partial file.

### Validate a dataset

```sh
computer-use-agent validate --schema cui-detector-dataset-v2 --input dataset.json
```

For `cui-detector-dataset-v2`, `validate` checks the JSON Schema, the safety
rules, duplicate records (`record_ref` or `source_sha256`), formal rows without
provenance, and, for a dataset without provenance, any stored audit (which must
match a recomputed one). The leave-one-task-out gate (at least three tasks,
both classes in every task, at least three records per task) is enforced by
`analyze`; `validate` applies it only while recomputing a stored audit.
Provenance-bound datasets are checked by their stage validator at analysis
time.

### Analyze and verify

```sh
computer-use-agent analyze --dataset examples/detector/synthetic-dataset.json \
    --output-dir out/run1 --profile smoke
computer-use-agent verify-bundle out/run1
```

| Profile   | Epochs | Bootstrap draws | Permutations | Wall time on 18 records |
|-----------|-------:|----------------:|-------------:|-------------------------|
| `default` | 500    | 1,000           | 100          | about 2 minutes         |
| `smoke`   | 60     | 200             | 5            | about 15 seconds        |

`smoke` is an engineering check of every code path; its numbers must never be
compared with a `default` run. `--seed`, `--epochs`, `--bootstrap` and
`--permutations` override the chosen profile, and the effective values are
recorded in `result.json` under `configuration`.

`examples/detector/synthetic-dataset.json` is an 18-record synthetic dataset
(3 tasks x 2 classes x 3 repetitions) generated by `tests/fixtures/synthetic.py`.
Any analysis of it is labelled "Synthetic pipeline check only".

The output directory must not exist, so artifacts from different runs are
never mixed. A successful run writes exactly six files:

- `result.json`: the `cui-detector-analysis-v2` result (canonical JSON);
- `trajectory_projection.json`: task-by-class aggregate feature profiles for
  plotting, bound into `result.json` by SHA-256 (no record identifiers,
  paths, raw feature rows or event sequences);
- `data_card.md` and `model_card.md`;
- `MANIFEST.sha256`: hashes of the four files above;
- `detector-result-bundle.zip`: a byte-reproducible ZIP of all of them.

`verify-bundle` re-checks the hashes, the canonical JSON encoding, the
reporting contract, the trajectory binding and the ZIP read-back.

The historical module entry points keep working:
`python -m computer_use_agent.detector.extract` and
`python -m computer_use_agent.detector.pipeline` (same options as `analyze`).

## Formal data, provenance and human data

The generic extraction manifest rejects `formal_trace` records, and the
pipeline rejects formal records that arrive without provenance. Formal data
enter only through a dataset `provenance` object whose `source_stage` has a
registered validator:

```python
from computer_use_agent.detector.provenance import register_provenance_validator


def validate(payload):  # schema_version, records, audit, provenance
    ...  # raise ValueError to reject


register_provenance_validator("my-stage", validate)
```

- A dataset whose `source_stage` has no registered validator **fails closed**.
- The core package never imports research code. Make a stage's validator
  available with `--provenance-module NAME` (repeatable) on `analyze`, or
  import the module before calling `run_pipeline`. The archived ScienceBoard
  harness registers its validator when `research.scienceboard.detector_bridge`
  is imported.
- Optional hooks: `analysis_provenance(payload)` returns the publication-safe
  binding stored as `source_provenance` in the result (otherwise a generic
  binding with the stage, the dataset hash and a SHA-256 of the provenance is
  stored), and `human_data_policy(payload)` returns the policy used to
  re-authorize formal human records in every evaluation fold.
- A validator only accepts provenance. Formal human rows additionally require
  a `human_data_policy` hook that authorizes them (for example one returning
  `ConfigHumanDataPolicy(config)`); without the hook they are denied
  (`DENY_FORMAL_HUMAN_DATA`), even when the validator accepted the dataset.

Human data are governed by an injectable
`computer_use_agent.study.policy.HumanDataPolicy`. The default denies formal
human data. `ConfigHumanDataPolicy` authorizes them only when a formal-study
config records a human-research determination whose private record (named by
`CUA_HUMAN_RESEARCH_DETERMINATION_RECORD`, stored under `CUA_PRIVATE_ROOT`
outside every Git work tree) matches the recorded SHA-256. Human QA traces
cannot be promoted by editing a manifest field.

## Interpretation

LOTO asks whether a learned behavioral boundary transfers to another observed
task. A high task-identification control score means the features encode the
task, which weakens any human-versus-agent interpretation. The permutation
test checks the observed score against a task-preserving null. Brier score and
the calibration curve assess the probabilities, and the task-cluster bootstrap
shows uncertainty at the task level (with three tasks it has little
resolution). No result from this package supports a population-level or
deployable detector claim.
