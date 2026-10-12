# Agent-framework pilots (archived)

Controlled integration runs of three computer-use agent frameworks on public,
read-only micro-tasks. Browser-Use and Skyvern ran the same three tasks: follow the
information link on `example.com`, follow the Root Zone Management link on the IANA
homepage, and open the newest-stories page on Hacker News. The Stagehand smoke run
used its own variants: `example.com` to IANA's example-domains page, IANA's
example-domains page to `/domains`, and Hacker News `/news` to `/newest` (task ids
and URLs in `stagehand/sanitized_runs.json`). The prompt gives only the start URL;
the target must be reached through a visible link click, and deterministic
validators check the URL, the live DOM, the answer and the action history. These are
pilots, **not benchmark scores**, and they contain no human data.

| Framework | Version | Runs passed | Validator checks | Reported cost (USD) | Files |
|---|---|---:|---:|---:|---|
| Browser-Use | 0.11.5 | 9/9 (3 tasks x 3 repetitions) | 54/54 | 0.436519 | `browser_use/` |
| Skyvern (self-hosted, `skyvern-2.0` engine) | 1.0.47 | 9/9 (3 tasks x 3 repetitions) | 54/54 | 0.432419 | `skyvern/` |
| Stagehand | 3.7.1 | 3/3 (one pass per task) | n/a | not reported | `stagehand/` |

- Browser-Use and Skyvern ran on 2026-07-27 with the `gpt-5.5` model, a fresh browser
  profile per run and a cost cap. Costs are application-reported and not reconciled
  to provider invoices.
- Stagehand was a supplemental single-pass smoke run, not one of the three-repetition
  pilots. It was later retired from the formal MiniWoB matrix because its dependency
  graph carried 13 blocking GHSA advisories (recorded in the frozen harness manifest
  `research/week1_miniwob/benchmark_manifest.json`).
- `cost_summary.json` reconciles the pilot costs, the validation and diagnostic smoke
  costs and one canceled Skyvern attempt against the USD 5.00 cap.
- `skyvern/diagnostics.json` records the setup and smoke failures that preceded the
  pilot, and `skyvern/environment.json` records the runtime image digests.

Each directory has an `aggregate.json` (per-task and overall results) and a
`sanitized_runs.json` (one record per run: steps, action names, validator checks,
cost; no page bodies or model messages). Paths recorded inside these files that start
with `results/` map here through [`../LEGACY_PATHS.json`](../LEGACY_PATHS.json).

The framework versions, run counts and costs have entries in
[`../INDEX.json`](../INDEX.json), section `pilots`.
