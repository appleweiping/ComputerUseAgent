# ScienceBoard matched-trace harness (archived research)

This directory is the harness for collecting the same three official
ScienceBoard tasks from one self-human operator, `AllInOne/AIOAgent`, and
`SeeAct`, recorded by the X11 guest recorder in
`src/computer_use_agent/capture/x11/`. It is an execution and evidence
pipeline, not a result. **The formal count is 0/27: the harness has never run
on a VM.** Formal execution stays blocked until the external VM, guest
calibration, model entitlement, human-research determination, task-selection
confirmation and a newly sealed run capsule genuinely exist.

The earlier NetLogo/PhET browser batch is retained only as a
`scripted_automation_control`; it is not ScienceBoard or reasoning-agent data.

## Pinned protocol

`tasks/manifest.json` fixes seed `20260808`, snapshot `sci_bench`, Ubuntu 22.04,
1920×1080, five official task steps, and 27 planned cells:

- KAlgebra A-01: store the conjugate of `1+i` in `p`;
- Celestia A-01: set Julian date `2400000`;
- GrassGIS A-01: load `countries@PERMANENT` from natural-earth;
- actors `human`, `aio` (`AllInOne`), and `seeact`, three repetitions each.

ScienceBoard is pinned to commit `c8d5010bdba32afc5dfb16472873bed148c39a45`.
Local task copies are canonical-JSON hash-bound to the pinned upstream files.
The manifest records the task choice as
`researcher-selected, pending independent confirmation`; this is a blocking
preflight gate. Only the exact status `researcher-selected, independently
confirmed` passes, and it may be recorded only after an independent reviewer
has actually confirmed the selection.

Every protocol pin (commit, OpenAI endpoint, VM archive SHA-256, evaluator
sources, guest bundle name, guest paths, phase-timeout margins) has one home:
`worker_contract.py`. `tests/test_pins.py` fails if a copy drifts.

Official source: <https://github.com/OS-Copilot/ScienceBoard>

## Execution boundary

ScienceBoard is a host-side VM benchmark. `VManager` loads `desktop_env`, owns
the `.vmx`, restores the snapshot, and communicates with the guest. AIOAgent,
SeeAct, the task object and evaluation therefore run on the **host**; the guest
only records.

The `host_worker.py` CLI/service keeps one official object graph alive for a
whole session. `worker_contract.py` owns the closed v3 schemas, the protocol
pins and the authenticated state chain, `runtime_doctor.py` the read-only
source/dependency/guest-bundle checks, and `official_runtime.py` the pinned
`Tester`/VM bridge. The v3 `worker_source_sha256` field is a canonical
aggregate over these four files (`HOST_WORKER_SOURCE_FILES`).

The lifecycle is:

1. construct `Tester` for exactly one task and one community;
2. call the same task's `init()`; for a `VTask` this restores `sci_bench` and
   runs `/home/user/server/reset.sh`;
3. only after reset, deploy the guest bundle and start the X11 recorder;
4. call that task's `predict()` for AIO/SeeAct, or wait for the bound manual
   stop signal for a human run;
5. take the final screenshot through the official manager;
6. cooperatively stop the X11 recorder;
7. call `eval(stop_type, stop_args)` on the same task instance;
8. bind `official/result.out`, `official/traj.jsonl`, the evaluator source
   hash, the stop arguments and the normalized validator result;
9. fetch the finalized trace atomically, then tear down the manager.

Starting a recorder before `Task.init()` is invalid: `VTask._init()` restores
the VM snapshot and resets the guest, destroying that recorder. A separate
validator CLI is also invalid because upstream exposes none; the official
evaluator is `Task.eval()` on the live task.

The worker's source doctor verifies exact AST signatures and SHA-256 values for
the pinned `Task`, `VTask`, `VManager`, `Tester`, `Log`, `AllInOne`, `SeeAct`,
model, agent and application evaluator files without importing them, starting
a VM or calling a model.

### Security-minimal DesktopEnv boundary

The host environment deliberately does **not** install the upstream
`desktop-env` distribution: `desktop-env==0.1.22` forces a vulnerable
`requests` range, and `desktop-env==1.0.2` resolves to a 227-package graph with
constraint-locked vulnerable packages. Instead, `runtime_compat/desktop_env`
implements the exact local-VM surface consumed by the hash-pinned
`sci/vm/vmanager.py`:

| Pinned `VManager` use | Compatibility member |
|---|---|
| construct the supplied VMX | `DesktopEnv(...)` |
| expose the guest HTTP client | `controller` |
| restore `sci_bench` | `snapshot_name`, `_revert_to_snapshot()` |
| restart after restore | `_start_emulator()` |
| stop the VM at teardown | `close()` |

It uses shell-free `vmrun` argv, bounded subprocess and network calls, an
existing VMX only, and a private non-loopback IPv4 guest-controller address.
`runtime_compat/provenance.json` records the PyPI `desktop-env==0.1.22` wheel
SHA-256 and three upstream reference-file hashes. `RUNTIME_COMPAT_HASHES` in
`runtime_doctor.py` pins the LF bytes of every compatibility file; any drift
is a hard failure.

`vmrun` is found by one rule shared by the preflight and the runtime
(`runtime_compat/desktop_env/vmware.py: discover_vmrun`): an explicit
`VMRUN_PATH` (which must name an existing file), otherwise `vmrun` on `PATH`.
No install directory or drive letter is guessed.

Install the eight-package host graph from the hash lock (Windows shown; on
Linux use `.runtime/scienceboard-host/bin/python`):

```bash
uv venv .runtime/scienceboard-host --python 3.12
uv pip install --python .runtime/scienceboard-host/Scripts/python.exe \
  --require-hashes -r research/scienceboard/requirements-host.lock
```

The lock (generated before the repository restructure, hence its header path)
must pass `pip-audit --strict --no-deps --disable-pip` before formal use.

Pinned upstream source locations behind the adapter (commit `c8d5010b`; the
doctor records the measured AST line numbers):

| Official file | Pinned lines | Contract used |
|---|---:|---|
| `main.py` | 124–129 | `AllInOne(AIOAgent)` and `SeeAct(PlannerAgent, GrounderAgent)` construction |
| `sci/base/community.py` | 54–145 | official `AllInOne` and `SeeAct` action loops |
| `sci/base/task.py` | 213–262 | `Task.init()` |
| `sci/base/task.py` | 323–341 | `Task.predict()` returns `(stop_type, stop_args)` |
| `sci/base/task.py` | 343–396 | same-task `Task.eval(stop_type, stop_args)` lifecycle |
| `sci/vm/vtask.py` | 40–48 | snapshot restore plus `/home/user/server/reset.sh` |
| `sci/vm/vmanager.py` | 90–95, 238–260 | lazy host `desktop_env`, revert, enter, and teardown |
| `sci/Tester.py` | 239–267, 347–366 | one-file task loading and official manager/task construction |
| `sci/base/log.py` | 81–106, 351–455 | `traj.jsonl`, `result.out`, trajectory save, result write |

## Worker protocol, timeouts and failure semantics

`driver.example.json` is deliberately non-runnable. A private copy under
`CUA_PRIVATE_ROOT` must replace the host-Python placeholder with the
interpreter that holds `requirements-host.lock`.

The driver speaks shell-free argv to a single localhost worker. A 48-byte
session nonce is stored in the private session directory; the state file
stores only its SHA-256. Every request carries a unique request ID and the
previous state hash. The worker rejects a wrong nonce, stale or tampered state,
a replayed phase or an out-of-order command. The state chain is:

```text
prepared → initialized → capturing → acted → screenshot_captured
         → capture_stopped → evaluated → finalized
```

Timeouts are layered so that no layer kills a peer that is still legitimately
working:

- the worker gives a connected client `WORKER_IO_TIMEOUT_SECONDS` (30 s) to
  deliver its request line, so a silent client cannot wedge the single-client
  worker;
- `host_worker.py phase --timeout T` waits for the response for
  `T + PHASE_RESPONSE_MARGIN_SECONDS` (15 s), because the phase itself may use
  its whole budget `T`;
- each driver command's `timeout_seconds` must be at least
  `T + 2 × 15 s`; preflight rejects a driver that violates this (the actor
  phases resolve `{timeout_seconds}` from the manifest: 300 s human, 480 s
  agent).

Provider errors and zero-action timeouts stay in the experimental denominator.
If the recorder finalizes cleanly with zero events, the bundle is complete:
analysis receives `trace_empty=true` plus all missing-modality flags, and the
public summary accepts `trace_event_count == 0`. A failed reset, malformed
trace, missing screenshot, wrong source hash, missing `result.out` or broken
phase chain is infrastructure-incomplete and never counts as a complete bundle.

## Guest recorder bundle

The guest runs exactly one file, `computer-use-agent-x11-recorder.pyz`, a
deterministic standard-library zipapp built by
`computer_use_agent.capture.x11.bundle`. It contains the import-light package
root, the trace core (`computer_use_agent/trace/*`) and the X11 capture modules
(`computer_use_agent/capture/x11/*`); members are sorted, stored, timestamped
1980-01-01 and LF-normalized, so two builds have the same SHA-256.

```bash
python -m computer_use_agent.capture.x11.bundle --output <dir-or-file.pyz>
```

For each session the orchestrator builds the bundle into the private session
directory and binds its SHA-256 into the immutable worker spec
(`guest_bundle_hashes`). After `Task.init()` the worker sends it through the
guest file API as base64, reads the transport back, and the guest's own
`/usr/bin/python3` decodes it, verifies the SHA-256 and installs it atomically.
The worker then runs `python3 <bundle> start-recorder ...`; the controller
starts the recorder from the same archive (`python3 <bundle> record ...`), and
`python3 <bundle> stop-recorder ...` stops it cooperatively.

The guest commands are:

- `doctor`: Ubuntu release, X11 RECORD/XTEST, screen resolution and private
  guest-path checks;
- `start-recorder` / `stop-recorder`: start the privacy-minimised recorder and
  stop it with clean-terminal verification;
- `record`: the recorder process (`record --doctor` checks DISPLAY,
  python-xlib, RECORD and XTEST without recording);
- `calibration-probe`: the Tk calibration target;
- `calibrate`: evaluate calibration evidence (normally run on the host).

**Guest dependencies must be baked into the `sci_bench` snapshot.**
`Task.init()` reverts the VM to the snapshot before every session, so anything
installed into the running guest afterwards is discarded. The snapshot needs
the system Python 3.10 of Ubuntu 22.04, `python-xlib==0.33` (the `x11` extra)
and Tk (`python3-tk`) for the calibration probe. The bundle itself needs only
the standard library and runs on Python 3.10 (`tests/capture/test_bundle.py`
checks this; set `CUA_GUEST_PYTHON` to a 3.10 interpreter to also run it).

Guest runtime data stay under `/home/user`, by default:

```text
/home/user/.local/share/computer-use-agent/sessions
/home/user/.local/share/computer-use-agent/control
```

`SCIENCEBOARD_GUEST_ROOT`, `SCIENCEBOARD_AIO_LAUNCHER`,
`SCIENCEBOARD_SEEACT_LAUNCHER` and `SCIENCEBOARD_VALIDATOR_LAUNCHER` belong to
the retired guest-side driver and are rejected by preflight and orchestration.

The X11 adapter records only monotonic/wall time, coarse key class and hold,
pointer/button/wheel, a bounded application identifier, focus transitions and
screen dimensions. It never stores typed text, key symbols or codes, clipboard,
window titles, filenames, document values or process commands. Human capture
requires an explicit per-session self-consent binding; it is not inferred from
`actor=human`.

## Calibration

Calibration proves recorder behavior on the guest configuration; it is not a
task result. Run two phases in the guest, each with the recorder in one
terminal and the probe in another, then copy the traces to the host:

```bash
# guest, phase 1 (XTest) — then phase 2 with --actor human --consent-confirmed / --mode human
python3 computer-use-agent-x11-recorder.pyz record --actor xtest --controller-confirmed \
  --task-id scienceboard-x11-calibration-v1 --protocol-version scienceboard-x11-calibration-v1 \
  --executor-version python-xlib-0.33 --output <guest trace path> --control-file <stop file>
python3 computer-use-agent-x11-recorder.pyz calibration-probe --mode xtest --confirm-calibration

# host
python -m computer_use_agent.capture.x11 calibrate --dry-run          # evidence template
python -m computer_use_agent.capture.x11 calibrate \
  --evidence <CUA_PRIVATE_ROOT>/scienceboard/calibration/evidence.json \
  --output <CUA_PRIVATE_ROOT>/scienceboard/calibration/certificate.json
```

Per phase the certificate checks the X11 capture adapter, a complete terminal
record, a positive duration, the five input types `keydown`, `keyup`,
`pointermove`, `pointerdown` and `pointerup`, unique event IDs, monotonic
timestamps, the post-stop negative control (event count unchanged after stop),
the calibration task/protocol and actor bindings, and the privacy field scan.
Across phases it requires distinct traces, hashes and session IDs and the same
guest binding. **Focus changes are not part of calibration.** Preflight
recomputes the certificate from the evidence and rejects any mismatch.

## Private storage and VM provenance

Every private input and output of the harness lives under `CUA_PRIVATE_ROOT`:
an absolute directory outside this repository, outside any Git work tree and
outside `research/evidence` (the rule is shared with
`computer_use_agent.study.policy`; there is no drive-letter rule). That covers
raw session batches (default `$CUA_PRIVATE_ROOT/scienceboard/formal/<UTC>`),
the private driver, calibration evidence and certificate, the VM provenance
certificate, analysis inputs/outputs and detector datasets. Preflight measures
free space on that volume (at least 100 GiB).

Hash the downloaded official VM archive once (never downloads, extracts or
starts anything) and point `SCIENCEBOARD_VM_PROVENANCE` at the certificate:

```bash
python -m research.scienceboard.vm_provenance --vm-zip <downloaded VM zip> \
  --output <CUA_PRIVATE_ROOT>/scienceboard/vm-provenance.json
```

## Read-only checks and dry run

These commands start no VM, model or human session and write nothing:

```bash
python -m research.scienceboard.preflight --json --allow-unready
python -m research.scienceboard.orchestrate --dry-run
python -m research.scienceboard.orchestrate --actor aio --dry-run
python -m research.scienceboard.analysis --dry-run
```

The dry run needs neither `CUA_PRIVATE_ROOT` nor the release capsule: it loads
the hash-bound manifest, checks it against the matrix in
`research/configs/formal-study.json`, and prints the deterministic 27-cell
interleaved schedule with the preflight report. `--allow-unready` makes the
preflight exit 0 while still listing every blocking check.

## Formal execution (currently blocked)

A formal run additionally needs a **sealed** run capsule
(`--capsule`, verified by `computer_use_agent.study.policy.load_binding`). The
archived `research/archive/release-capsule-2026-08.json` is historical and
cannot be sealed, so `--confirm-formal-run` fails closed until a new capsule
exists. The remaining gates are: the confirmed task-selection status, the
private root with at least 100 GiB free, `vmrun`, an existing `.vmx`
(`VM_PATH`), verified VM archive provenance, the `sci_bench` snapshot, a clean
pinned ScienceBoard checkout with exact tasks, evaluators and signatures, a
passing calibration certificate, a private v3 driver with valid timeout
margins, `gpt-5.5` entitlement and key, and (for human cells) a recorded
human-research determination plus the operator's per-session action.

The host environment variables are listed in `.env.example` in this
directory. The OpenAI Chat Completions endpoint is fixed to
`https://api.openai.com/v1/chat/completions`; a custom or fallback endpoint is
rejected by preflight, orchestration and the host worker. No API key is
written to a worker spec, command line, log, trace or Git file.

```bash
python -m research.scienceboard.orchestrate --confirm-formal-run --capsule <sealed capsule>
```

During a human cell the orchestrator waits while the operator performs the
task. From a second host terminal, the same operator records the official stop
decision (normally `DONE`) for that session directory:

```bash
<host python> research/scienceboard/host_worker.py signal-manual-stop \
  --spec <session dir>/worker_spec.json \
  --state <session dir>/worker_state.json \
  --nonce-file <session dir>/.worker-nonce \
  --stop-type DONE --confirm-self-action
```

The signal is accepted only for the nonce-bound human session while its actor
phase is active; it cannot start a human run or stand in for the operator's UI
actions. `--actor` remains a diagnostic/recovery filter, not a substitute for
the predeclared interleaved 27-cell matrix.

## Evidence contract

Every complete attempt, whether the task passes or fails, contains:

```text
task.json
worker_spec.json
worker_state.json
trace.jsonl
final_state.png
validator_result.json
official/result.out
official/traj.jsonl
run.log
run_manifest.json
```

(plus `.manual-stop.json` for human cells and the `guest_bundle/` the session
deployed). `scienceboard-validator-result-v2` binds the session and task, the
official stop type and arguments, the exact ScienceBoard commit, the canonical
task hash, the evaluator module and source SHA-256, the `result.out` value and
hash, and the trajectory hash and size. Raw action trajectories and human
event sequences stay private and never reach the detector feature matrix or
the public summary.

## Publication and detector hand-off

`publication.py` revalidates a complete private batch and writes the
path-free public summary to `research/evidence/scienceboard_formal/summary.json`.
`detector_bridge.py` revalidates the 27 private bundles, the study ledger, the
sealed config/capsule snapshots, the artifact hashes and the private
determination record, and emits allowlisted, path-free feature records only.
Importing it registers the `scienceboard` provenance validator with
`computer_use_agent.detector.provenance`; the detector rejects a dataset whose
provenance stage has no registered validator.

```bash
python -m research.scienceboard.publication --input-dir <CUA_PRIVATE_ROOT>/scienceboard/formal/<batch>
python -m research.scienceboard.detector_bridge \
  --input-dir <CUA_PRIVATE_ROOT>/scienceboard/formal/<batch> \
  --output <CUA_PRIVATE_ROOT>/detector/scienceboard-dataset.json
python -m computer_use_agent.detector.pipeline \
  --dataset <CUA_PRIVATE_ROOT>/detector/scienceboard-dataset.json \
  --output-dir <CUA_PRIVATE_ROOT>/detector/scienceboard-analysis \
  --provenance-module research.scienceboard.detector_bridge
```

(`computer-use-agent analyze` accepts the same `--provenance-module`.) The
result is a single-participant, three-task pipeline probe, not population
performance or a deployable detector claim. `analysis.py` remains a
descriptive and exploratory human/AIO/SeeAct three-class diagnostic, not a
second detector.

## Tests

```bash
python -B -m pytest research/scienceboard tests/capture tests/trace/test_trace_io.py
```

The suite covers the source-signature probes, a no-model fake lifecycle, the
socket worker with its I/O and response timeouts, nonce/replay/order/state
tampering, single-bundle deployment and guest-side hash verification, bundle
determinism and Python 3.10 compatibility, recorder privacy, zero-event
complete bundles, evaluator/result/trajectory binding, deterministic
scheduling, the dry run and preflight commands, private-root gating,
calibration gates, pin drift and grouped analysis splits. It never starts
VMware or calls a provider.
