# Skyvern repeated public-task pilot

## Outcome

The final offline audit passed: **9/9 planned runs completed, 9/9 passed,
9 unique task/repetition cells, 9 unique provider run IDs, and 0 runs with
unreported cost**. The retained pilot batch reported **$0.432419**. Including the
smoke tests and the separately audited canceled attempt, total Skyvern cost was
**$0.509600**.

This is a small, repeated reproduction of Skyvern on three public, read-only
navigation-and-extraction tasks. It is evidence that the pinned runtime could
execute this protocol; it is not an official Skyvern benchmark score and should
not be generalized to arbitrary websites or agents.

## Protocol

Each task starts from the listed URL. The prompt names a visible link but does not
reveal the destination URL. Skyvern must click the link, wait for the
destination and return a structured value.

| Task ID | Start page | Required visible action | Target and extraction |
|---|---|---|---|
| `example_follow_more_information` | `https://example.com` | Follow the information link | Exact URL `https://www.iana.org/help/example-domains`; H1 |
| `iana_follow_root_zone` | `https://www.iana.org/` | Follow “Root Zone Management” | Exact URL `https://www.iana.org/domains/root`; H1 |
| `hn_follow_newest` | `https://news.ycombinator.com/news` | Follow the top “new” link | Exact URL `https://news.ycombinator.com/newest`; first story title |

The retained batch used three repetitions per task, `skyvern-2.0`, a seven-step
limit, a fresh self-hosted browser run for each repetition and a pilot-batch cost
cap of $2.50.

A run passes only if all six checks pass:

1. Skyvern reports `completed`.
2. Step evidence contains the supplied start URL.
3. Step evidence contains the exact target URL.
4. Step evidence records a click action.
5. The requested structured output field is present.
6. The output matches deterministic DOM evidence: an independent post-run fetch
   for the two stable pages, or Skyvern's timestamped run DOM captured before
   completion or structured output for the live Hacker News page.

The runner returns nonzero for any failed or missing planned run. It also stops
starting new paid runs if a completed run has no auditable cost or once
cumulative reported cost reaches the configured cap.

## Results

| Task | Successful / completed | Success rate | Median duration | Median steps | Reported cost |
|---|---:|---:|---:|---:|---:|
| Example Domain → information | 3 / 3 | 100% | 207.28 s | 3 | $0.1090965 |
| IANA → Root Zone Management | 3 / 3 | 100% | 278.87 s | 3 | $0.1204905 |
| Hacker News → newest | 3 / 3 | 100% | 335.44 s | 3 | $0.2028320 |
| **Overall** | **9 / 9** | **100%** | **278.87 s** | **3** | **$0.4324190** |

Pilot cost is calculated from Skyvern PostgreSQL `workflow_run_blocks.llm_cost`
plus child `steps.step_cost`, with any recorded proxy or CAPTCHA cost. It has not
been independently reconciled to the model provider's invoice.

Pre-pilot diagnostics cost $0.075239: an authentication/configuration failure
cost $0; a `skyvern-1.0` strict-validation failure cost $0.040425; and the
successful `skyvern-2.0` smoke test cost $0.034814. A legacy-runner attempt
started after a host validator exception and was immediately canceled; it cost
$0.001942. Total diagnostic overhead is therefore $0.077181. Smoke and canceled
attempts are excluded from the retained-batch success rate. See
`research/evidence/pilots/skyvern/diagnostics.json`.

Cost accounting across the two paid online historical pilots:

| Scope | Reported cost |
|---|---:|
| Skyvern retained 9-run pilot | $0.432419 |
| Skyvern smoke diagnostics | $0.075239 |
| Skyvern canceled diagnostic attempt | $0.001942 |
| **Skyvern total** | **$0.509600** |
| Browser-Use pilot + validation smoke | $0.495014 |
| **Browser-Use + Skyvern combined** | **$1.004614** |

The Browser-Use number is read from
`research/evidence/pilots/browser_use/aggregate.json` and
[its README](../browser_use/README.md). The combined amount is below the shared
$5.00 ceiling.

## Pinned environment

- Official repository: <https://github.com/Skyvern-AI/skyvern>
- Reviewed source checkout: `8e3b40ae072c8d954879f26460b710367db723cb`
- Executed runtime version: `1.0.47`
- Executed Skyvern image:
  `public.ecr.aws/skyvern/skyvern@sha256:ff9ae1aa20f27d90115614f98fc0a796dcb86ee218aedde9f035cd9c2a89b95b`
- PostgreSQL image:
  `postgres@sha256:f1341c01408dc7278e9d365ed4f860cd3f87dd16b4464ac326fc0f422083a579`
- Docker Engine: `29.4.1`
- Historical engine/model configuration: `skyvern-2.0` / `OPENAI_GPT5_5`;
  database-observed model `gpt-5.5`

The published image has no Git metadata or source-commit label. Therefore the
reviewed source checkout commit is not presented as the image's source commit;
the immutable image digest is the executable runtime pin. See
`research/evidence/pilots/skyvern/environment.json` for the complete scope
statement.

## Offline audit (read-only)

Recompute the aggregate from the archived sanitized runs and compare it with the
archived `aggregate.json`, without starting Skyvern or calling any API:

```
python -m research.pilots.skyvern.runner \
    --audit-existing research/evidence/pilots/skyvern/sanitized_runs.json \
    --repetitions 3 --engine skyvern-2.0 --max-steps 7 --cost-cap-usd 2.5
```

The audit writes nothing. It exits 0 only when every planned run passed and the
recomputed aggregate matches the existing one (host- and time-dependent fields
such as `completed_at` and `python_version` are ignored). `--write` rewrites the
runs file and its `aggregate.json` in place instead; it is refused for files in
`research/evidence`, which is archival.

Unit tests: `python -m pytest research/pilots/skyvern`.

## Live reproduction (paid, network)

Prerequisites: Docker, Python 3.11 or newer, an authorized model credential in
the current process environment, the compatible endpoint setting
`OPENAI_API_BASE` or `OPENAI_BASE_URL`, and `CUA_PRIVATE_ROOT` naming a private
directory outside the repository. The scripts do not write any credential to a
public file. From the repository root, in PowerShell (the Python command is one
line):

```
research/pilots/skyvern/docker_start.ps1
python -m research.pilots.skyvern.runner --repetitions 3 --engine skyvern-2.0 --max-steps 7 --cost-cap-usd 2.5 --timeout-seconds 600 --output-dir <new results directory outside research/evidence>
research/pilots/skyvern/docker_stop.ps1
```

`docker_start.ps1` keeps the database, artifacts, videos, HAR files, logs,
downloads, browser sessions, credential vault and the generated local Skyvern
credentials in `$CUA_PRIVATE_ROOT/pilots/skyvern`; the runner reads
`.skyvern/credentials.toml` from there by default (`--credentials-file`
overrides it). `docker_stop.ps1` removes the two containers and their network
and leaves the private directory in place. If a host-side publication step is
interrupted after Skyvern has already completed, `--resume-existing <runs file>`
continues without repeating completed cells.

## Files

- `runner.py`: task submission, polling, evidence extraction, validators, cost
  gate, aggregation and the offline audit.
- `test_runner.py`: offline tests for complete, duplicate and missing run
  matrices, unreported costs, URL matching, output parsing and the read-only
  audit.
- `docker_start.ps1` / `docker_stop.ps1`: the pinned local deployment lifecycle.
- `research/evidence/pilots/skyvern/`: `sanitized_runs.json` (one
  publication-safe record per retained run), `aggregate.json` (metrics and
  acceptance flags), `environment.json` (runtime and host metadata) and
  `diagnostics.json` (setup and smoke outcomes, separate from the retained runs).

The public artifacts contain no model API key, local Skyvern token, cookie,
browser profile, complete page body, raw browser trace, provider endpoint origin
or private data.

## Limitations

- The three micro-tasks are much narrower than realistic multi-step workflows.
- Two target pages are relatively stable; Hacker News “newest” is live and its
  first story can change. HN output is therefore checked against the timestamped
  browser DOM captured before structured extraction, with its SHA-256 kept in the
  sanitized run record; the title is not a permanent benchmark label.
- Costs are application-reported values, not provider-invoice reconciliation.
- The official image did not disclose its source commit.
- The source metadata clone is pinned, but the working-tree checkout could not be
  fully materialized because GitHub blob transport repeatedly reset. This does
  not affect the digest-pinned container that executed the retained batch.

## Official references

- [Skyvern repository and self-host instructions](https://github.com/Skyvern-AI/skyvern)
- [Skyvern self-hosted LLM configuration](https://www.skyvern.com/docs/self-hosted/llm-configuration)
