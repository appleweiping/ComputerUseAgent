# Scientific simulator control

> **Scope:** a historical `scripted_automation_control` driven by fixed
> Puppeteer/CDP actions. It is not a reasoning computer-use agent and not a
> ScienceBoard experiment. The ScienceBoard matrix remains **0/27 collected**.

This directory defines a reproducible browser-simulator recorder control. Three
versioned browser tasks are executed through the real recorder extension and
judged from simulator state by an independent validator. The archived run (9/9
passing runs, recorded with recorder 0.2.1) is in
`research/evidence/scripted_simulator_control_v0.2.1/`.

## Scope and claim boundary

- This is a **scripted-automation engineering control**; it contains neither a
  human trace nor a reasoning-agent trace and is not a human-versus-agent
  comparison.
- The task set was provisional and was never confirmed for a formal study.
- This is **not a ScienceBoard-native adapter**: it does not import ScienceBoard
  tasks, run their VMware or native applications, or capture OS-level input. It is
  a browser-only baseline.

## Task and scientific oracle

| Task manifest | Pinned upstream | Requested operation | Independent pass oracle |
|---|---|---|---|
| [`netlogo-ca-rule90-v1.json`](tasks/netlogo-ca-rule90-v1.json) | NetLogo Web 2.15.0; model commit `ca8e0e1611aef188fadeebd2097b70d1e5fa4e00` | Set Rule 90, run `setup single`, then `go` to completion | `rule=90`, `row=-64`, `ticks=128`, 2,189 on-cells, 33,153 patch bits, full-state SHA-256 `669dd8381d3bdcf0d93e40170ccf3237a194e213344da0d024ec1ee5770d1a27`, and `go` stopped |
| [`phet-bce-ammonia-balance-v1.json`](tasks/phet-bce-ammonia-balance-v1.json) | PhET Balancing Chemical Equations 1.3.1 | Balance `N2 + H2 -> NH3` with the smallest whole-number coefficients | Project/version match, coefficients `[1,3,2]`, scale counts `[2,2,6,6]`, and element labels `[H,N]` |
| [`phet-pdl-annotated-single-launch-v1.json`](tasks/phet-pdl-annotated-single-launch-v1.json) | PhET Projectile Data Lab 1.0.7 | Enable path, launch-angle and launch-speed annotations; fire once | All three PDOM controls checked, sample count exactly `1`, and field card text contains `1 of 1` (a protocol-state oracle, not a physics-law claim) |

The validator is [`validator.mjs`](validator.mjs), with unit coverage in
[`validator.test.mjs`](validator.test.mjs). The console-error policy is
[`runner_policy.mjs`](runner_policy.mjs) ([tests](runner_policy.test.mjs)). The
browser/extension runner is [`run_extension_e2e.mjs`](run_extension_e2e.mjs).

The task files were edited after the archived run: two descriptive fields
(`status`, `research_boundary`) were rewritten to remove a personal name. The
archived `run_manifest.json` records the hashes of the files as executed:
`netlogo-ca-rule90-v1` `282d8c32...e6a1`, `phet-bce-ammonia-balance-v1`
`53e2f97c...cb33`, `phet-pdl-annotated-single-launch-v1` `eeb6294b...0f29`.
Actions, oracles and pinned upstream URLs are unchanged.

## Browser error policy

The runner has one narrow console-error allowance: on PhET tasks only, the exact
`Failed to load resource: net::ERR_CONNECTION_CLOSED` error from
`https://www.google-analytics.com/g/collect` is recorded separately as an
optional analytics failure. Every other console error and every page error is
fatal to the run.

## Pinned upstream bytes

These SHA-256 values were observed in the archived control; the full URLs, byte
counts, response metadata and source hashes are recorded in its
`run_manifest.json`.

| Artifact | Scripted-control SHA-256 |
|---|---|
| NetLogo Web 2.15.0 HTML | `760123f4996da8f248b4307131d5952147fb6ce37c3cbd7bac4fccfb68717c6a` |
| NetLogo CA 1D Elementary model at the pinned commit | `36c2d8020692b8d438e921255635437939eb6625b4ffd50184cce8ed34e32c27` |
| PhET Balancing Chemical Equations 1.3.1 HTML | `cbe5fef2e07f83321e88d747bc150c06326233e286c8f246aada727b2bf0053b` |
| PhET Projectile Data Lab 1.0.7 HTML | `edfef35351e386a778789e8812066114a1b99241a16d4d83d264ae37533cd37c` |

## Offline checks

From the repository root (no browser, no network, no Puppeteer needed):

```
node --test research/scientific_simulators/*.test.mjs
node research/scientific_simulators/run_extension_e2e.mjs --dry-run
```

`--dry-run` validates the task manifests and prints the run plan (output
directory, tasks, repetitions, extension name and version) without creating any
directory, fetching anything or launching a browser.

## Running the control (network, visible browser)

This directory has its own `package.json` and `package-lock.json` (Puppeteer
`25.4.0`, which provides Chrome for Testing). The runner needs Node.js
`>=22.12`, network access to the pinned simulator URLs and a display (on Linux,
`xvfb-run -a`). It creates an isolated temporary Chrome profile and never uses a
normal user profile. It is not exercised in CI. The archived run used recorder
0.2.1 and the harness was last adapted for recorder 0.4.0 without a retained
re-run. It now reads the expected extension name and version from
`extension/app/manifest.json`, but it also drives the popup form and recorder
messages, which may need updating for the current recorder.

```
npm ci --prefix research/scientific_simulators
node research/scientific_simulators/run_extension_e2e.mjs --output-dir <new or empty directory>
```

| Option | Environment variable | Default |
|---|---|---|
| `--output-dir DIR` | `CUA_SIM_E2E_RESULT_DIR` | a fresh directory under the OS temp directory |
| `--runtime-dir DIR` | `CUA_SIM_E2E_RUNTIME_DIR` | `<OS temp>/computer-use-agent/scientific-simulators/runtime` (parent of the temporary profile) |
| `--chrome PATH` | `CUA_SIM_E2E_CHROME` | Puppeteer's Chrome for Testing |
| `--reps N` | `CUA_SIM_E2E_REPS` | `3` (1-10) |
| `--tasks a,b` | `CUA_SIM_E2E_TASKS` | all three tasks |

The output directory must be new or empty, and the runner refuses any directory
inside `research/evidence` (also through links), so it can never replace or
delete the archived control. It never deletes files from the output directory.
A successful run writes 9 run results, the aggregate, the validation summary, the
run manifest, sanitized traces, validator records and screenshots there; artifact
paths inside those files are relative to the output directory. Chrome and
network-delivered bytes are re-hashed into the new manifest; compare them with
the archived control's `run_manifest.json` before claiming an exact
replication.
