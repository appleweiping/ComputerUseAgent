# Scripted scientific-simulator controls, recorder 0.2.1 (archived)

> **Correction (2026-08-08):** These nine historical runs were produced by a fixed Puppeteer/CDP script. They are `scripted_automation_control` recorder-compatibility evidence—not reasoning CUI-agent runs, ScienceBoard runs, or detector evidence. The official ScienceBoard matrix remains **0/27 collected**. Raw evidence is unchanged; see [`correction_manifest.json`](correction_manifest.json).

Historical engineering-control result: **9/9 scripted runs passed their task-state checks across 3 browser simulators, capturing 908 sanitized browser events**. The run window was 2026-08-07 13:58:06–14:01:23 UTC, using an isolated Chrome profile and CUI Trace Recorder 0.2.1.

## Aggregate results

| Task | Passed | Events/run, mean (range) | Recorded event span/run, mean (range) | Validator oracle |
|---|---:|---:|---:|---|
| `netlogo-ca-rule90-v1` | 3/3 | 116.00 (116–116) | 4,720.67 ms (4,359–4,907) | Exact Rule 90 terminal state plus SHA-256 of all 33,153 patch bits |
| `phet-bce-ammonia-balance-v1` | 3/3 | 122.33 (122–123) | 5,968.00 ms (5,888–6,076) | Exact simulator identity, coefficients, atom counts, and element labels |
| `phet-pdl-annotated-single-launch-v1` | 3/3 | 64.33 (63–65) | 4,642.00 ms (4,467–4,942) | Exact annotation controls, one-sample count, and updated field-card state |

Machine-readable totals are in [`aggregate_metrics.json`](aggregate_metrics.json); all release checks are `true` in [`validation_summary.json`](validation_summary.json).

The recorded event span is `last event epoch_ms - first event epoch_ms` from
the sanitized trace. It is not the agent's full wall-clock task duration, which
also includes setup, page loading, validation, screenshots, and recorder
start/stop work.

## Browser error review

All nine runs recorded **zero unexpected console or page errors**. The six
PhET runs recorded 12 allowlisted errors in total (two per run) under policy
`phet-google-analytics-connection-closed-v1`: the exact console message
`Failed to load resource: net::ERR_CONNECTION_CLOSED` from source origin
`https://www.google-analytics.com` and path `/g/collect`. The allowance applies
only when the simulator origin is `https://phet.colorado.edu`; every other
console or page error is fatal. The three NetLogo runs had zero allowlisted
errors.

## Evidence map

- [`run_manifest.json`](run_manifest.json): timing, task list, browser/extension identity, upstream response digests, and source provenance. Its `mentor_selection_status` value was redacted during the restructure (a person's name was replaced with the word `mentor`); nothing else in the file changed, and no other file binds its hash.
- [`aggregate_metrics.json`](aggregate_metrics.json): overall and per-task pass, event, recorded-event-span, pointer-distance, trusted-input, and event-type metrics.
- [`validation_summary.json`](validation_summary.json): nine release-level checks, including real extension start/stop, trusted inputs, privacy-safe export, post-stop negative control, and no human data.
- [`runs/`](runs/): nine run directories. Every run contains `run_result.json`, `validator_result.json`, `sanitized_trace.json`, `simulator-recording.png`, and `simulator-final-state.png`. The first screenshot is taken after the task validator passes while recording is still active; the second is taken after the recorder stops. They document the validated terminal state and recorder lifecycle, not a task-state before/after pair. Each task's `rep-01` also includes recorder-metadata and dashboard screenshots.

Representative evidence:

| Task | Run record | Scientific-state validation | Final state | Recorder/dashboard proof |
|---|---|---|---|---|
| NetLogo Rule 90 | [`run_result.json`](runs/netlogo-ca-rule90-v1/rep-01/run_result.json) | [`validator_result.json`](runs/netlogo-ca-rule90-v1/rep-01/validator_result.json) | [`simulator-final-state.png`](runs/netlogo-ca-rule90-v1/rep-01/simulator-final-state.png) | [`recorder-agent-metadata.png`](runs/netlogo-ca-rule90-v1/rep-01/recorder-agent-metadata.png) / [`dashboard-inspect.png`](runs/netlogo-ca-rule90-v1/rep-01/dashboard-inspect.png) |
| PhET chemical balance | [`run_result.json`](runs/phet-bce-ammonia-balance-v1/rep-01/run_result.json) | [`validator_result.json`](runs/phet-bce-ammonia-balance-v1/rep-01/validator_result.json) | [`simulator-final-state.png`](runs/phet-bce-ammonia-balance-v1/rep-01/simulator-final-state.png) | [`recorder-agent-metadata.png`](runs/phet-bce-ammonia-balance-v1/rep-01/recorder-agent-metadata.png) / [`dashboard-inspect.png`](runs/phet-bce-ammonia-balance-v1/rep-01/dashboard-inspect.png) |
| PhET projectile launch | [`run_result.json`](runs/phet-pdl-annotated-single-launch-v1/rep-01/run_result.json) | [`validator_result.json`](runs/phet-pdl-annotated-single-launch-v1/rep-01/validator_result.json) | [`simulator-final-state.png`](runs/phet-pdl-annotated-single-launch-v1/rep-01/simulator-final-state.png) | [`recorder-agent-metadata.png`](runs/phet-pdl-annotated-single-launch-v1/rep-01/recorder-agent-metadata.png) / [`dashboard-inspect.png`](runs/phet-pdl-annotated-single-launch-v1/rep-01/dashboard-inspect.png) |

## Historical control provenance

| Component | Fixed identity / SHA-256 |
|---|---|
| Repository source | Git base commit `185e283b0d0eb2e38941c733b37a0ff9978bd76e` (in the private source repository only; not in any public history); the manifest records the non-clean snapshot and exact content hashes below |
| Runner | `d784b6cb1cf60f161a436636671bd4c60132fd3c370a421cab6cb94ea87239d4` |
| Runner policy | `97bda078ebc1713075ebf2d5bb2d5d72a09e69d1552e1a123fb4fdbea02bba61` |
| Validator | `c3ecb1dc190b75114e4d625ca3fb9658b99ee7beb181e613a957da671d0592e9` |
| Recorder extension | version 0.2.1; directory SHA-256 `da2db7deadcd7cba2c45d7b12b9715025a272247c546a1fcdacfe7e576651d86` across 25 files |
| Chrome executable | `Chrome/150.0.7871.187`; SHA-256 `115a87374083a409eb4b3d0204cbaea8a0e27eeb264cb0b9d3e4c19c378f54d1` |
| NetLogo task manifest | `282d8c32a81c4f46b27b3e0ce70c5f5600f96f88537086a1952d6ecedc85e6a1` |
| PhET chemical-balance task manifest | `53e2f97cbe994feeb2482286e1e0ecdc93bcd5230c1384aa70db35fc1995cb33` |
| PhET projectile task manifest | `eeb6294b83899f1cee2913ac655d1fa62fc08574dd9fecfbbb6e63f3273f0929` |

Upstream simulator hashes are documented with their task/oracle definitions in the [harness README](../../scientific_simulators/README.md) (section "Pinned upstream bytes"), which also describes how the historical control was run. Paths recorded inside these files use the original layout (`results/week3_scientific_simulators/...`, `experiments/scientific_simulators/...`); see [`../LEGACY_PATHS.json`](../LEGACY_PATHS.json) and [`../../PROVENANCE.md`](../../PROVENANCE.md). The task manifest hashes above are those of the task files at run time; the current task files under `research/scientific_simulators/tasks/` may differ.

## Interpretation boundary

These results establish only that the recorder and deterministic validators worked with a fixed script on this provisional browser task set. They contain neither human nor reasoning-agent evidence. The task set was provisional and its final selection was never confirmed. A separate ScienceBoard-native X11 harness exists under `research/scienceboard/`, but it has not passed real-VM calibration or produced any of the planned 27 formal sessions.
