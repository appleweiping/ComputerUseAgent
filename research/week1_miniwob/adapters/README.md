# Week 1 framework adapters

> The Week 1 harness is frozen against recorder 0.4.0 and cannot be launched
> from this repository; see [`../README.md`](../README.md). This page documents
> how the adapters were built and how their guards work.

The active fail-closed bridges are Browser-Use 0.11.5 and Skyvern 1.0.47
(`ACTIVE_FRAMEWORKS` in `adapter_contract.py`). Stagehand 3.7.1 remains in the
frozen protocol as a historical cell only; it has no adapter and every
readiness check rejects it. No bridge contains task-specific actions, and no
failed agent is replaced with Playwright, Puppeteer or a hand-written solution.

## One official browser target

`node/browser_host.mjs` starts a fresh Google Chrome profile, loads the real
Manifest V3 recorder and exposes Chrome on a private loopback CDP endpoint.
The Python orchestrator attaches BrowserGym 0.14.1 to that browser, calls the
official MiniWoB task `setup()` and resolves the exact page target ID and URL.
The selected framework must use that same target. A central sticky host guard
is armed after the recorder popup closes. It records regular-page target
creation and destruction, main-frame navigation and reload, and
execution-context replacement; immediately before the reward it rechecks the
target, loader, document token, time origin, MiniWoB episode start, viewport,
DPR, locale and timezone. Framework-local one-second polling is a secondary
check. Neither guard generates input.

After execution, BrowserGym's official task `validate()` supplies the reward.
The recorder trace, final screenshot, generated task setup and validator info,
dependency freezes, Chrome profile and framework stderr stay under
`CUI_WEEK1_PRIVATE_ROOT`; the public attempt JSON contains only bounded,
path-free artifact descriptors. Formal trace QA covers schema, session and
event counts, unique IDs, monotonic timestamps, privacy mode, page scope,
observed viewport and at least one trusted non-navigation event.

The MiniWoB files are pinned to commit
`7fd85d71a4b60325c6585396ec4f48377d049838`. Setup verifies repository-pinned
task and static-asset tree hashes. The managed loopback server byte-checks the
task pages and their shared `core`/`common` assets before BrowserGym setup, then
closes after the attempt. An explicit `MINIWOB_URL` override is accepted only
when the same resources are reachable and byte-identical.

## Isolated environments

Use Python 3.11-3.13 and keep every environment outside the repository. Run the
setup modules from the repository root:

```
# CUI_WEEK1_PRIVATE_ROOT: a private directory outside the repository
python -m research.week1_miniwob.adapters.setup_environments --framework all
python -m research.week1_miniwob.adapters.setup_miniwob
```

`--dry-run` previews paths. The setup creates three environments:

- `browser-use`: BrowserGym/Playwright 1.44 plus Browser-Use 0.11.5;
- `orchestrator`: BrowserGym/Playwright 1.44 for Skyvern task setup and reward;
- `skyvern-local`: `skyvern[local]` 1.0.47 plus Playwright 1.62, with no
  BrowserGym installation.

Each environment receives a sorted private `pip freeze --all --local` snapshot
and a SHA-256 sidecar. Preflight compares the live graph with that snapshot; an
attempt exposes only a path-free descriptor. To snapshot existing private
environments without reinstalling them:

```
python -m research.week1_miniwob.adapters.setup_environments --framework all --snapshot-existing --skip-node
```

The Skyvern split is required: its local extra needs Playwright newer than
1.46, while BrowserGym 0.14.1 pins Playwright 1.44. The bridge starts
`Skyvern.local(use_in_memory_db=True)` in the isolated process and attaches it
to the orchestrator-owned external CDP browser. No Skyvern service URL or
Skyvern API key is used. On Windows the launcher forces UTF-8 because Skyvern
1.0.47 otherwise reads one packaged JavaScript asset with the active legacy
code page. Its working directory, temp files, logs and cache are forced below
the private root.

`dispatch.py` selects the isolated interpreter for a framework and starts the
adapter as a module (`python -m research.week1_miniwob.adapters.<adapter>`) with
the repository root as the working directory, so no adapter manipulates
`sys.path`. The standalone `skyvern_local_bridge.py` script runs in the
`skyvern-local` environment.

Set only local runtime paths and the model credential, for example
`CUI_WEEK1_CHROME_EXECUTABLE` (path to a Chrome executable) and `OPENAI_API_KEY`
(set locally, never committed). The manifest pins Chrome `151.0.7922.108`. A
different installed product version fails preflight rather than mixing browser
versions in one matrix.

## Framework-specific guards

Browser-Use has no switch for a redundant `Runtime.runIfWaitingForDebugger`
call when attaching to an already-running external browser. The adapter applies
an in-memory compatibility layer only after exact version, module path,
signature and source-SHA256 checks pass for the reviewed Browser-Use/CDP
methods. Installed packages are never edited. Browser-Use output is redirected
to private stderr so protocol stdout remains one JSON object.

Skyvern uses an embedded in-memory client, the same external CDP target,
`max_steps=30` and a separate process watchdog for the eight-minute cap. The
watchdog is authoritative because Skyvern's local ASGI request can remain open
for the background run. Skyvern 1.0.47's pinned GPT-5.5 registry requires
temperature 1 and its `LLMConfig` has no model-seed field; the attempt artifact
reports both exceptions. The task seed is still applied by BrowserGym. The
completion-token cap is 8192.

Stagehand 3.7.1's AI SDK 5 dependency graph had 18 affected dependency nodes in
npm 11.17.0 JSON and 20 in the historical GitHub Node 20 console report for run
31345287584 (recorded in the frozen manifest's `formal_security` block). The
audit-clean provider-utils 4.x line changes the provider/tool contract and was
not substituted with an unverified override. Its lock and bridge were never part
of this repository; [`archive/stagehand-3.7.1/README.md`](archive/stagehand-3.7.1/README.md)
records their hashes. Active setup, dispatch and formal execution exclude
Stagehand, and no replacement framework exists.

## Action-step alignment

Browser-Use marks the recorder **before** each framework step using the reviewed
`Agent.run(on_step_start=...)` hook (the retired Stagehand bridge used its
`callbacks.prepareStep` hook). Each hook sends only a generated
framework-neutral token such as `step-003` through a per-attempt authenticated
`127.0.0.1` sidecar. The sidecar accepts one exact next token at a time, calls
the BrowserHost recorder RPC and rejects extra fields; instructions, action
text, selectors, page content and credentials cannot cross this boundary. The
token is cleared before official validation so validator-side events are not
attributed to the agent's last step. Public evidence reports hook and host
counts, the rejection count, aligned-event coverage and whether the counts
agree, but never the bearer token or loopback port. Keeping the token neutral
also prevents a detector from learning the framework name through this field.

Skyvern 1.0.47 is different: the exact source-hashed public `run_task` signature
has no reliable pre-action callback. Its complete session remains a valid
formal matrix outcome, but it is explicitly `not_supported` for step-level
analysis. Artifact polling after a run is not used to invent timestamps.
Consequently, `analysis_eligible` requires a supported hook, at least one
acknowledged step, matching framework and host counts, no rejected markers, a
successful clear and at least one naturally observed event carrying that step
token.

## Preflight and execution

These checks call no model:

```
python research/week1_miniwob/adapters/dispatch.py browser-use --self-test
python research/week1_miniwob/adapters/dispatch.py skyvern --self-test
python -B -m research.week1_miniwob.runner --preflight --allow-unready
```

In this repository they report `recorder-extension` as failed because of the
0.4.0 pin. Skyvern's self-test constructs and closes the embedded client. The
one-time preflight has a separate 150-second budget and does not consume an
attempt's 480-second task window. It may write logs and cache only in the
private framework-state directory and verifies that no repository file changed.
All checks report credential presence only, never the value.

The full runner sends one attempt JSON object on stdin to an adapter. Logs go to
private stderr, while stdout contains one `cui-week1-adapter-result-v1` object
with the compact action structure, official reward, recorder completion, exact
browser provenance and private-safe artifact descriptors. Missing dependencies,
target drift, an invalid model, action-cap violations, recorder failures and
validator failures are explicit errors.

The frozen manifest records the adapter command as
`["{python}", "experiments/week1/adapters/dispatch.py", "<framework>"]`; the
runner maps that recorded path onto `research/week1_miniwob/adapters/dispatch.py`
(see `../checkout.py`). Custom bridge overrides remain supported for partial
debugging only, as JSON argv arrays (never shell strings), through
`CUI_WEEK1_BROWSER_USE_ADAPTER` and `CUI_WEEK1_SKYVERN_ADAPTER`, for example
`["python","path/to/custom_adapter.py"]`.
