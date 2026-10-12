# FP-Agent analyses (archived)

Three analyses of the public FP-Agent corpus (upstream repository
`https://github.com/ethanbwang/fp-agent`, commit `e614e2f`; data on OSF at
`https://osf.io/j6b5p/`). The code that produced them is in
[`../../fp_agent/`](../../fp_agent/README.md). The FP-Agent data itself is not
redistributed here; only aggregate results are.

## 1. Released-model reproduction: `fp_agent_classifier_eval.json`

Inference only, with the three released all-class XGBoost models on the authors'
random split (6,175 train, 1,544 test, 8 classes). No retraining. Run on 2026-07-27
under Python 3.13.14, outside the hash-locked research environment.

| Model | Accuracy | Macro-F1 |
|---|---:|---:|
| Browser fingerprint (418 features) | 0.8212 | 0.8223 |
| Behavioral (50 features) | 0.9994 | 0.9994 |
| Combined (468 features) | 1.0000 | 1.0000 |

Caveat: these scores re-execute a within-corpus split. They do not measure unseen
agents or deployment distributions.

## 2. Leave-one-agent-family-out: `fp_agent_loafo.json`, `fp_agent_loafo.csv`

A binary agent-versus-human XGBoost model is trained from scratch per fold on the
released feature vectors, holding out one of the 7 agent families each time, for
each of 3 feature sets: 21 folds. Run under Python 3.14.5, outside the hash-locked
environment.

- Browser features: mean balanced accuracy 0.9286, worst fold 0.5000. With Manus
  held out, recall is 0.0 although AUROC is 0.9174: the ranking transfers, the
  threshold does not.
- Behavioral and combined features: balanced accuracy at least 0.9968 in every fold.
- The same 109 human test rows are reused in every fold, so the folds are not
  independent.

The CSV has one compact row per feature set and held-out family.

## 3. 23-feature retrospective: `fp_agent_retrospective.json`

The FP-Agent raw events are recomputed into the same 23-feature allowlist the
detector library uses, then evaluated with a purged 3-fold leave-one-task-out
logistic model (primary), an XGBoost model (secondary), a 7-fold agent-family
holdout, task and source confound controls, a 1,000-draw within-task permutation
test and group and single-feature ablations. 7,728 sessions (546 human, 7,182
agent), seed 20260811, run in an isolated hash-locked Python 3.12.3 environment.

| Primary result | Value |
|---|---:|
| Pooled balanced accuracy | 0.6001 |
| Pooled AUROC | 0.6358 |
| Pooled macro-F1 | 0.5746 |
| Flight-booking held out: BA / AUROC | 0.500 / 0.998 |
| Forums held out: BA / AUROC | 0.497 / 0.829 |
| Shopping held out: BA / AUROC | 0.853 / 0.967 |

Read this as a cross-task threshold and calibration transfer failure, not as
chance-level ranking. Caveats:

- Only 3 tasks. The reported "95% interval" for balanced accuracy is the min/max over
  the 3 folds (0.497 to 0.853), not a population confidence interval.
- The pooled AUROC mixes three separately calibrated fold models.
- AUPRC 0.958 is inflated by the 92.9% agent prevalence.
- The participant/trial-group purge leaves only 79, 212 and 37 training rows.
- Focus is never observed: FP-Agent records no focus events, so `missing_focus=1`
  and `focus_transition_count=0` for every row.
- The permutation p-value 0.000999 is the minimum possible with 1,000 draws.
- The same features predict the task (balanced accuracy 0.866) and the agent family
  (0.974): strong task and source confounds.
- Dropping the keyboard features raises balanced accuracy by 0.181.

`fp_agent_retrospective_status.json` records the SHA-256 of the result file.
`figures/figure_manifest.json` binds the result file and the seven figures in
`figures/` by SHA-256; those hashes still match. Paths recorded inside it use the
original layout (`results/fp_agent/...`, `experiments/fp_agent/...`); see
[`../LEGACY_PATHS.json`](../LEGACY_PATHS.json) and
[`../../PROVENANCE.md`](../../PROVENANCE.md).

## Redaction

In `fp_agent_classifier_eval.json` (`/dataset/source`) and `fp_agent_loafo.json`
(`/source/feature_vectors_source`), the OSF view-only share link the runs recorded was
replaced with the public project URL `https://osf.io/j6b5p/`. Nothing else in these
files changed. No file in this repository binds their previous hashes.

The headline numbers above have entries in [`../INDEX.json`](../INDEX.json) (sections
`fp_agent_released_models`, `fp_agent_loafo` and `fp_agent_retrospective`).
