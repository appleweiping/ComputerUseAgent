# research/

This directory keeps the research provenance behind ComputerUseAgent: archived agent
harnesses, pilots, data analyses, the literature corpus and the recorded evidence. It
is **not** part of the `computer-use-agent` wheel. It is importable as the `research`
package only from a checkout of this repository (run commands from the repository
root).

Where it came from, and every path that was not imported, is recorded in
[PROVENANCE.md](PROVENANCE.md).

| Path | Contents |
|---|---|
| `configs/` | Study configurations (formal study, retrospective study, Codex case) |
| `archive/` | Frozen historical inputs that are not validated (the 2026-08 release capsule) |
| `scienceboard/` | ScienceBoard VM harness (archived; never run on a VM) |
| `week1_miniwob/` | Week 1 MiniWoB harness (frozen against recorder 0.4.0) |
| `pilots/` | Browser-Use, Skyvern and Stagehand pilots (not benchmarks) |
| `codex_case/` | Codex Desktop case study (all cells blocked) |
| `scientific_simulators/` | NetLogo/PhET scripted-control harness (Node) |
| `fp_agent/` | FP-Agent retrospective and leave-one-family-out analyses |
| `auxiliary_data/` | Auxiliary public-dataset sampling checks |
| `literature/` | Literature corpus: `sources/`, `snapshots/`, `generated/`, `archive/`, build scripts and tests |
| `evidence/` | Recorded results, archived as imported (three minimal redactions, see `evidence/README.md`), indexed by `INDEX.json` and verified against `MANIFEST.sha256` |

## Running the research tests

Sync the research extra from `uv.lock`, then run pytest on this directory explicitly
(the default `pytest` run covers only `tests/`). Without uv, activate a virtual
environment and use `python -m pip install -e ".[research]"` and
`python -B -m pytest research`.

```
uv sync --extra research
uv run pytest research -m "not requires_private_data and not requires_large_artifacts and not requires_network"
uv run pytest research/fp_agent      # one area
uv run nox -s research               # the CI session (also runs the notebook checks and node tests)
node --test research/scientific_simulators/*.test.mjs
```

The markers `requires_private_data`, `requires_large_artifacts`, `requires_network` and
`requires_x11` are registered for tests that need private data, the large literature
artifacts, the network or an X11 display. The FP-Agent private-corpus tests carry
`requires_private_data` and skip when `CUA_PRIVATE_ROOT` is unset; the citation-graph
tests carry `requires_large_artifacts` and skip until
`python -m research.literature.fetch_large` has fetched the large files. Tests that need
the `research` extra (numpy, scikit-learn, xgboost) skip with a reason when it is not
installed. CI deselects the marked tests with
`-m "not requires_private_data and not requires_large_artifacts and not requires_network"`.

## Reproducing research stages

`research/reproduce.py` is a small dispatcher. Each stage runs one per-area entry
point (`python -m research.<area>.<module>`, `node` or `npm`):

```
python -B -m research.reproduce --dry-run                  # list every stage, check its entry point, run nothing
python -B -m research.reproduce --stage literature         # run one stage
python -B -m research.reproduce --stage codex-case -- ARGS # extra arguments after -- are appended
```

The stages are `week1`, `scienceboard`, `codex-case`, `skyvern-audit`,
`simulator-validators`, `simulator-plan`, `retrospective-config`, `literature`,
`citation-graph` and `recorder-e2e`. The `recorder-e2e` stage runs
`npm --prefix extension run e2e`, which opens a browser window (use `xvfb-run -a` on
Linux); every other stage is read-only or a dry run.

The former `scripts/doctor.py` was removed. Environment checks are
`computer-use-agent doctor`; harness readiness is
`python -B -m research.week1_miniwob.runner --preflight --allow-unready` and
`python -B -m research.scienceboard.preflight --allow-unready`.
`node --test research/scientific_simulators/*.test.mjs` needs no `npm install`.

Private outputs of every harness go under `CUA_PRIVATE_ROOT`, which must resolve
outside the repository and outside `research/evidence`; no harness writes into
`research/evidence`.

Evidence files under `evidence/` are archival. Never rewrite them in place; paths
recorded inside them use the source repository's layout and are translated through
`evidence/LEGACY_PATHS.json` (see also the map in `PROVENANCE.md`). After any change
under `evidence/` or `literature/generated/`, regenerate and check the manifest with
`python tools/ci/verify_evidence.py --write` and `python tools/ci/verify_evidence.py`.
