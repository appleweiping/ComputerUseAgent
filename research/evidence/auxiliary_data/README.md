# Auxiliary public datasets (archived)

Aggregate-only checks on three public datasets, run on 2026-08-11. The code is in
[`../../auxiliary_data/`](../../auxiliary_data/). Rows were read through HTTP range
requests and processed in memory: no raw or sampled rows, row-level identities or
timestamp values are kept here. None of these datasets is combined with FP-Agent, fed
to the 23-feature timing model, or reported as a detector result.

| Track | File | What it is |
|---|---|---|
| MolmoWeb | `molmoweb_metrics.json` | Domain-held-out source classifier on ordered action-name 1-3 grams, 10,000 human and 10,000 synthetic trajectories |
| CaptchaSolve30k | `captchasolve30k_metrics.json` | Mouse-only aggregate distribution calibration of a 5,000-row stratified sample |
| Aalto 136M Keystrokes | `aalto_136m_metrics.json` | Physical-keyboard participant-level aggregate distribution calibration of a 5,000-participant sample |

**MolmoWeb.** The action n-gram classifier reaches balanced accuracy 0.8167 on held-out
domains, against 0.5 for the domain-only baseline (0.5 by construction, because every
held-out domain is unseen and maps to the same all-zero vector). Read the 0.8167 as
likely label leakage, not as a behavioral signal: all 108,254 audited synthetic rows
carry an `action_timestamp` field and none of the human rows carry any timestamp
field. The timestamps were discarded and not used as features, but their presence
marks the two sources as differently produced. The track status records this as
`complete_with_timestamp_absence_check_failed`.

**CaptchaSolve30k and Aalto.** These are sampling-fidelity checks only: each compares a
seeded, stratified sample against its population on aggregate distributions.

## Other files

| File | Contents |
|---|---|
| `summary.json` | Status of the three tracks and the hard boundaries above |
| `sampling_ledgers.json` | Sampling algorithm, seeds, population and sample counts, and digests of the selected identities |
| `source_access_manifest.json` | Pinned sources, the members and byte counts read, and their hashes |
| `auxiliary_calibration.svg`, `auxiliary_calibration.png` | The calibration figure |
| `notebook_execution.json` | Execution record of the analysis notebook: environment, seeds, and SHA-256 of its inputs (the metrics files above) and outputs (the figure) |

The input and output hashes in `notebook_execution.json` still match the files here.
It also binds the notebook source and an executed notebook by their original paths
(`experiments/auxiliary_data/...`); the executed notebook was never committed. Paths
recorded as `results/auxiliary_data/...` map here through
[`../LEGACY_PATHS.json`](../LEGACY_PATHS.json).

The MolmoWeb, CaptchaSolve30k and Aalto claims have entries in
[`../INDEX.json`](../INDEX.json), section `auxiliary`.
