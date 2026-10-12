# Contributing to ComputerUseAgent

Thanks for your interest in improving ComputerUseAgent. This guide covers the
development setup on Windows and Linux, the checks a pull request must pass,
and the data rules that apply to every contribution.

## License of contributions

The project is licensed under the [MIT License](LICENSE). By submitting a
contribution you agree that it is licensed under the same MIT License
(inbound = outbound) and that you have the right to submit it. Do not add code,
data or assets whose license does not allow that; see
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

## Data rules (read first)

- **Never commit raw human traces**, typed text, screenshots of private pages,
  credentials, browser profiles, VM images or machine-specific absolute paths.
  Use synthetic data (`examples/traces/`, `tests/fixtures/`) in tests and
  bug reports.
- Keep private data under `CUA_PRIVATE_ROOT`, a directory outside the checkout.
- Do not edit archived evidence under `research/evidence/` or
  `research/literature/generated/`; those bytes are bound by
  `research/evidence/MANIFEST.sha256`. A correction is a separate, documented,
  re-hashed change.
- Changes to what the recorder captures, stores or exports need a matching
  update to `docs/extension/privacy.md` and a CHANGELOG entry.

## Prerequisites

- Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/) 0.6 or newer
- Node.js 22.13 or newer with npm (extension development and E2E only).
  ESLint 10, which the default `uv run nox` (`node` session) and the
  pre-commit `eslint` hook run, needs 22.13; the unit tests and E2E alone
  work on 22.12.
- Git; Chrome or Chromium for manual extension testing

## Set up

The commands are the same in PowerShell and in a POSIX shell.

```
git clone https://github.com/appleweiping/ComputerUseAgent.git
cd ComputerUseAgent
uv sync                      # .venv with the package (editable) and the dev tools from uv.lock
uv run pre-commit install    # optional: run the hooks on every commit
npm --prefix extension ci    # extension tooling (downloads Chrome for Testing for the E2E suite)
```

`uv sync` installs the `dev` dependency group (tests, lint, nox, pre-commit,
audit and build tools) at the exact versions in `uv.lock`. Add
`--extra research` to work on `research/`.

## Run the checks

[nox](https://nox.thea.codes/) runs the lint, type-check, test, build,
evidence and docs checks, each in its own environment built from `uv.lock`,
identically on Windows and Linux. actionlint, CodeQL and the dependency audits
run only in GitHub Actions (see [docs/ci.md](docs/ci.md)).

```
uv run nox                  # lint, typecheck, tests (3.12 and 3.13), node
uv run nox -l               # list all sessions
uv run nox -s fmt           # apply ruff fixes and formatting
uv run nox -s tests-3.13 -- tests/trace -k privacy   # pass pytest arguments after --
uv run nox -s research      # research tests that need only public data
uv run nox -s build         # wheel, sdist, extension ZIP, version and SBOM checks
uv run nox -s e2e           # headful browser E2E (on Linux it uses xvfb-run when no display is set)
```

Pass a pytest subset to `tests-3.13`, as above. `tests-3.12` is the full
coverage run that CI gates on (`fail_under = 75`); given pytest arguments, it
still prints the coverage report but skips the gate.

Without nox:

```
uv run pytest                              # product tests (tests/)
uv run mypy                                # strict type check of src/computer_use_agent
uv run ruff check .                        # lint
uv run ruff format --check .               # formatting check (no changes written)
npm --prefix extension run lint
npm --prefix extension test
npm --prefix extension run test:coverage
```

Research tests run with `uv run pytest research -m "not requires_private_data
and not requires_large_artifacts and not requires_network"`. Tests that need
private data, the large literature artifacts, the network or an X11 display
carry the matching pytest marker and are deselected in CI.

## Pull requests

1. Open an issue first for anything larger than a small fix, so the approach
   can be agreed before you invest time.
2. Keep each pull request focused; separate refactoring from behavior changes.
3. Add or update tests. CI must pass on Ubuntu and Windows (see
   [docs/ci.md](docs/ci.md)); the Python branch-coverage gate is 75%.
4. Add a CHANGELOG entry under `[Unreleased]` for user-visible changes.
5. Do not change a wire format (`cui-trace-v2`, the detector dataset and
   analysis schemas, `chrome.storage` keys) without versioning it; the `cui-`
   prefix is the historical wire namespace and stays as is.
6. Do not bump versions in a feature pull request; releases bump
   `pyproject.toml`, `extension/app/manifest.json`, `extension/package.json` and
   `CITATION.cff` together (see [docs/releasing.md](docs/releasing.md)).
7. Changes to privacy handling, the extension message API, evidence
   eligibility or the feature allowlist need a review that focuses on that
   risk; say so in the pull-request description.
8. Pin every GitHub Action in the one-line form
   `uses: owner/repo@<40-character SHA> # vX.Y.Z`;
   `tools/ci/check_actions_pinned.py` fails closed on any other spelling of a
   `uses` key. Dependabot ignores `ruff`, which is pinned in both
   `pyproject.toml` and `.pre-commit-config.yaml`; bump the two together by hand
   (the steps are in the comment above the pin in `pyproject.toml`).

Security problems are not reported through pull requests or issues; see
[SECURITY.md](SECURITY.md). Participation is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md).
