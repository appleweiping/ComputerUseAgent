# Browser-Use repeated public-task pilot

This directory contains a retained historical Browser-Use pilot. It uses
`browser-use==0.11.5` with `gpt-5.5` on three public, read-only micro-tasks.
Each task starts from a disclosed public URL, requires a visible-link click to an
undisclosed target URL, and extracts a value from the live destination DOM. The
installed release maps to
[official tag 0.11.5](https://github.com/browser-use/browser-use/releases/tag/0.11.5),
commit `eed197835cb6e5023716ddbad5bb11975c856232`.

These are controlled micro-tasks: not an official Browser-Use Benchmark score and
not evidence of general web-agent performance.

## Protocol

- Three tasks, three independent repetitions per task.
- A new empty Chromium user-data directory for every run.
- Loopback CDP traffic bypasses the Windows system proxy via
  `NO_PROXY=127.0.0.1,localhost`; without this, the local `/json/version`
  request can receive an empty proxy-generated 502 response.
- Headless browser; DOM accessibility observations; no vision and no LLM judge.
- Deterministic validation of the start URL, target URL, recorded click action,
  live DOM and final answer.
- Browser-Use received a USD 2.50 allocation within a combined USD 5.00
  Browser-Use/Skyvern ceiling. The pilot-run ceiling was USD 2.40, leaving
  USD 0.10 for the one-task validation smoke run. No new run starts after the
  reported cumulative pilot-run cost reaches its ceiling.
- If a run cannot report cost, the runner starts no further paid work because
  the remaining budget cannot be established safely.

## Reproduce

This is a paid, network-dependent run. From the repository root, with
`OPENAI_API_KEY` set in a separate virtual environment:

```
python -m pip install -r research/pilots/browser_use/requirements.txt
python -m research.pilots.browser_use.runner --repetitions 3 --model gpt-5.5 \
    --cost-cap-usd 2.4 --output-dir <new results directory>
```

`--output-dir` is required and must not be inside `research/evidence`, which is
archival and read-only; a directory outside the repository (for example under
`$CUA_PRIVATE_ROOT`) is best. The runner writes `sanitized_runs.json` and
`aggregate.json` there and keeps browser profiles, dynamic page content and
provider error details under its `_private/` subdirectory, which must never be
published. The runner exits nonzero if any completed run fails its
deterministic validator.

## Historical pilot result (2026-07-27 UTC)

- Experiment ID: `20260727T081143Z`
- Completed: **9/9 runs**
- Deterministic validator pass: **9/9 runs** (54/54 individual checks)
- Recorded visible-link click: **9/9 runs**
- Median duration: **34.19 seconds**
- Median agent steps: **3**
- Pilot-batch reported cost: **USD 0.436519**
- One-task validation smoke cost: **USD 0.058495**
- Total Browser-Use reported cost including validation: **USD 0.495014**

Per-task pilot results:

| Task | Passed | Median duration | Median steps | Reported cost |
|---|---:|---:|---:|---:|
| Example.com → visible information link | 3/3 | 71.13 s | 4 | USD 0.153735 |
| IANA homepage → Root Zone Management | 3/3 | 34.19 s | 3 | USD 0.112167 |
| Hacker News → new/newest | 3/3 | 29.49 s | 3 | USD 0.170617 |

## Artifacts

- `research/evidence/pilots/browser_use/sanitized_runs.json`: one
  publication-safe record per run.
- `research/evidence/pilots/browser_use/aggregate.json`: task-level and overall
  success, median duration and steps, and reported cost.

No full page body, cookie, browser profile, API key or raw agent trace appears in
the sanitized JSON.
