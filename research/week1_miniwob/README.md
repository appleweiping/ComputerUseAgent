# Week 1 MiniWoB harness (frozen)

This directory preserves the historical common evaluation contract for
Browser-Use, Skyvern and Stagehand on three official BrowserGym MiniWoB tasks
(3 frameworks x 3 tasks x 3 repetitions = 27 planned attempts). The formal
matrix completed **0/27** attempts. The two retained failed launches are in
`research/evidence/week1_miniwob_prefix_failures/`; the earlier public-site
pilots are in `research/evidence/pilots/` and are documented under
[`research/pilots/`](../pilots/).

## Status: frozen, cannot be launched

The harness is kept for provenance and its tests still run, but it cannot run
an experiment from this repository, for two independent reasons:

1. **Recorder pin.** The harness was reviewed against recorder **0.4.0** only.
   The pin is `EXPECTED_RECORDER_VERSION = "0.4.0"` in
   [`adapters/adapter_contract.py`](adapters/adapter_contract.py) and
   `recorder.minimum_version` in the frozen
   [`benchmark_manifest.json`](benchmark_manifest.json). The extension in
   `extension/app` is newer, so the runner preflight (`recorder-extension`)
   and every adapter self-test fail closed. `adapters/node/browser_host.mjs`
   also drives 0.4.0 service-worker internals (`enqueueLifecycle`,
   `enqueueWrite`, `setActiveStep`) that later recorder versions do not
   promise to keep.
2. **Stagehand retirement.** The frozen protocol includes Stagehand 3.7.1,
   which has no adapter (see
   [`adapters/archive/stagehand-3.7.1/`](adapters/archive/stagehand-3.7.1/README.md)).
   The runner refuses a formal 27-cell execution. Filtered Browser-Use or
   Skyvern runs would only ever be labelled `partial-debug-evidence`.

No replacement framework or re-pinned harness is implemented. Re-running this
study would need a new reviewed manifest, adapters re-validated against the
current recorder, and separately labelled evidence.

## What still works

Run everything from the repository root:

```
# Plan the 27 cells and run the read-only preflight (no files are written)
python -B -m research.week1_miniwob.runner --dry-run

# Plan one filtered cell
python -B -m research.week1_miniwob.runner --framework browser-use --task click-checkboxes --repetition 1 --dry-run

# Preflight only; exits 0 even when not ready
python -B -m research.week1_miniwob.runner --preflight --allow-unready

# Rebuild a summary from retained attempt records
python -B -m research.week1_miniwob.aggregate --input-dir <attempts-dir>

# Tests (no network, no browser)
python -B -m pytest research/week1_miniwob
```

`--dry-run` prints the plan and the preflight and exits 0. For the unfiltered
canonical matrix it also tries to verify the formal-study binding
(`research/configs/formal-study.json` plus the archived release capsule
`research/archive/release-capsule-2026-08.json`). The capsule cannot be
sealed after the Stagehand retirement, so a dry run reports the failure as
`formal_study_binding_error` instead of exiting nonzero. `--preflight` and
`--resume` still fail closed on it.

A live run requires `--output-dir`, and the runner refuses any directory
inside `research/evidence`, which is archival and read-only. The runner refuses
to create attempt artifacts when preflight fails: a plan or a missing
dependency is not an experiment.

## Legacy paths in the frozen manifest

`benchmark_manifest.json` is protocol data. The SHA-256 of its canonical JSON
(`51e92176...`) identifies the protocol and is written into every planned
attempt (the archived pre-fix failures were planned from an earlier manifest
revision, `04536ea8...`). Because that identity depends on the exact content,
the manifest still records the source repository's layout: `recorder` for the
extension and `experiments/week1/adapters/dispatch.py` for the adapter
dispatcher.
[`checkout.py`](checkout.py) maps those recorded paths onto this checkout
(`extension/app`, `research/week1_miniwob/adapters/dispatch.py`) when a file has
to be located. It never rewrites the manifest or the evidence.

## Module boundaries

- `runner.py` owns CLI parsing, preflight, attempt orchestration, immutable
  record writing and aggregation.
- `checkout.py` maps recorded legacy paths, checks the recorder pin, and keeps
  output out of `research/evidence`.
- `evidence_projection.py` owns the privacy whitelist that converts private
  adapter output into the exact public evidence schema.
- `runner_process.py` owns private stderr retention and the killable
  per-attempt subprocess lifecycle. `runner.py` keeps its historical helper
  names as compatibility boundaries for existing callers and tests.
- `protocol.py` is the schema and matrix contract; adapters import their public
  contract from `adapters/common.py`.
- `adapters/common.py` owns request checks, readiness policy, formal-attempt
  orchestration, CLI behavior and the import/patch facade.
- `adapters/adapter_contract.py` owns the immutable protocol constants (including
  the recorder pin and `ACTIVE_FRAMEWORKS`) plus the shared `AdapterError` and
  `FrameworkRun` types.
- `adapters/browser_lifecycle.py` owns the Chrome/recorder process, the official
  MiniWoB connection, episode guards and the authenticated step-alignment
  sidecar.
- `adapters/artifact_validation.py` owns dependency-freeze evidence, private
  artifact descriptors, trace privacy/integrity checks and bounded PNG decoding.
- `adapters/evidence_sanitization.py` owns the recursive publication whitelist
  and action-history minimization.

Adapter setup, isolated environments and framework-specific guards are
described in [`adapters/README.md`](adapters/README.md).

## Private storage

All private material stays outside the repository:

| Variable | Purpose |
|---|---|
| `CUA_PRIVATE_ROOT` | Default home of the provider certificate (`week1_miniwob/provider_entitlement.json`) |
| `CUI_WEEK1_PRIVATE_ROOT` | Harness private root: traces, screenshots, Chrome profiles, framework state and the default adapter environments (historical name) |
| `CUI_WEEK1_ADAPTER_ENV_ROOT` | Optional separate root for the isolated framework environments |
| `CUI_WEEK1_MINIWOB_ROOT` | Optional location of the pinned MiniWoB source |
| `CUI_WEEK1_PROVIDER_CERT` | Optional explicit provider-certificate path |
| `CUI_WEEK1_CHROME_EXECUTABLE` | Chrome executable (the manifest pins Chrome 151.0.7922.108) |

## GPT-5.5 entitlement certificate

Before any paid browser attempt, the historical protocol required one small,
tool-free Responses API certificate in private storage:

```
python -B -m research.week1_miniwob.provider_probe
python -B -m research.week1_miniwob.provider_probe --validate
```

By default the certificate is written to
`$CUA_PRIVATE_ROOT/week1_miniwob/provider_entitlement.json`; `--output` or
`CUI_WEEK1_PROVIDER_CERT` select another file. Every certificate path must
resolve outside the repository. The probe refuses to overwrite an existing
certificate.

The probe opens no browser and exposes no tools. It records only a hash of the
response ID, the resolved model, latency, token counts, a cost computed from
the pinned official GPT-5.5 token prices and a 24-hour expiry. The response
content, the key, organization or project identifiers and the private path are
not published. A non-OpenAI `OPENAI_BASE_URL` is rejected because official
OpenAI pricing and model identity cannot establish a third-party proxy's cost
or entitlement. A passing certificate is readiness evidence only, never a
MiniWoB result. Model and price source:
<https://developers.openai.com/api/docs/models/gpt-5.5>.

Formal attempts fail before browser launch unless their start time is inside
the certificate's validity window. The request uses the `gpt-5.5` alias, and
the protocol requires it to resolve exactly to `gpt-5.5-2026-04-23`; an alias
update is protocol drift. Attempt records keep the safe certificate summary,
and the 27-cell aggregate rejects provider, resolved-model or official-price
drift.
