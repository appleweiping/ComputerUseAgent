# Local trace and result viewer

The viewer makes captured traces and detector results easier to inspect
without sending raw traces to a browser. Python validates the selected files
and builds a closed projection that contains only event type, modality,
document epoch, relative session time, aggregate counts, terminal status and a
redacted validator outcome. Raw keys, typed text, URLs, selectors,
coordinates, paths, session IDs, participant codes, action text and wall-clock
timestamps are never served.

The HTTP server binds to `127.0.0.1` only, rejects requests whose `Host`
header is not the loopback address (a DNS-rebinding guard), exposes fixed
read-only routes, sends no CORS header, disables caching, uses a restrictive
Content Security Policy and serves its three static files with a fixed
media-type map. There is no upload endpoint and no third-party script, font
or network request.

## Start

```sh
computer-use-agent view \
    --trace human=private/human-trace.json \
    --trace agent=private/agent-trace.json \
    --validator agent=private/agent-validator.json \
    --result out/run1/result.json \
    --port 0
```

- `--trace NAME=PATH` is repeatable; up to eight traces can be loaded. Browser
  exports (`.json`, `.jsonl`) and X11 traces (`.jsonl`) are accepted.
- `--validator NAME=PATH` attaches a validator outcome to the trace with the
  same name.
- `--result` is optional and accepts only `cui-detector-analysis-v2`
  (`result.json` from `computer-use-agent analyze`).
- `--port 0` picks a free loopback port (the default is 8765). The URL is
  printed on start; `--open-browser` opens it. Stop with Ctrl+C.

`python -m computer_use_agent.viewer.serve` accepts the same options.

## What the views show

- **Sessions**: role, task reference, capture adapter, terminal or validator
  outcome, event count and duration. For browser traces the duration is
  measured from session start (the largest `t_session_ms`), like the detector
  features and X11 traces; the extension's own `summary.duration_ms` stays the
  first-to-last event span. Task references use the detector's
  aliasing: unique source task IDs in sorted order become `task-01`,
  `task-02`, ... so the same task set gets the same aliases as a detector
  result.
- **Pair comparison**: two selected sessions side by side, by modality.
- **Event timelines**: relative timing and modality marks. Coordinates and
  key identities are omitted. Timelines longer than 2,500 events are sampled
  deterministically (uniformly over the whole session including the final
  event, keeping the first event of every type; at most 2,500 shown), and the
  full event count is still shown.
- **Detector diagnostics** (with `--result`): the held-out aggregate metrics,
  the task-identification and label-permutation controls, the task-cluster
  bootstrap 95% intervals, and the task-internal descriptive statistics
  (per task alias: `n`, mean and median for each class plus the
  human-minus-agent Hedges g for every feature). The aggregate trajectory
  artifact is shown only as its SHA-256 binding; the viewer never loads an
  event sequence from it. Predictions and model coefficients are not shown,
  and the viewer never fits or scores a model.

The viewer is a QA tool. A complete timeline shows that the recorder captured
a session; it is not evidence that a detector generalizes. Synthetic results
stay labelled as pipeline checks.
