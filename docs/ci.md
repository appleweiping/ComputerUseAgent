# Continuous integration and supply chain

Almost everything CI runs can be reproduced locally with `uv run nox` (see
[CONTRIBUTING.md](../CONTRIBUTING.md) and
[Running the checks locally](#running-the-checks-locally)). The exceptions run
only in GitHub Actions: actionlint, the non-blocking `mypy research` step, the
wheel/sdist content assertions in the `build` job, CodeQL and the `audit.yml`
jobs. The `build` job's twice-built, byte-compared extension ZIP has a local
equivalent in `tests/tools/test_package_extension.py`
(`test_two_builds_are_byte_identical`). All workflows live in
`.github/workflows/`, default to `permissions: contents: read`, cancel
superseded runs through a `concurrency` group, set a `timeout-minutes` on every
job, check out with `persist-credentials: false`, and contain no
repository-name guards, so forks run the same checks.

## Workflows

### `ci.yml` (every push to `main` and every pull request)

| Job | Runner | What it runs |
|---|---|---|
| `lint` | ubuntu | `ruff check`, `ruff format --check`, `tools/ci/check_actions_pinned.py`, `tools/ci/scan_secrets.py`, actionlint (release archive verified against a fixed SHA-256), ESLint for `extension/` |
| `typecheck` | ubuntu, Python 3.12 | `mypy` (strict, `src/computer_use_agent`); a second, non-blocking step type-checks `research/` |
| `test-python` | {ubuntu, windows} x {3.12, 3.13} | `uv sync --locked --group test` and `pytest`; on ubuntu/3.12 under `coverage run` with the branch-coverage gate (`fail_under = 75`) and `coverage.xml` uploaded as the `coverage-python` artifact. On ubuntu, a Python 3.10 interpreter is installed with `uv python install 3.10` and exported as `CUA_GUEST_PYTHON`, so the X11 guest bundle is smoke-tested under the guest's Python (Ubuntu 22.04 ships 3.10) |
| `test-research` | {ubuntu, windows}, 3.12 | `uv sync --locked --group test --extra research`, `pytest research -m "not requires_private_data and not requires_large_artifacts and not requires_network"`, the notebook hash-binding checks (`python -m research.fp_agent.execute_notebook --check`, `python -m research.auxiliary_data.execute_notebook --check`), the retrospective policy check (`python -m research.fp_agent.validate_config`) and the scientific-simulator `node --test` suite |
| `test-node` | {ubuntu, windows}, Node 22 | in `extension/`: `npm ci`, `npm test`, `npm run test:coverage` (Node coverage gate), `npm run version:check` |
| `evidence` | ubuntu | `tools/ci/verify_evidence.py`: every file under `research/evidence/` (and the literature `generated/` set) matches `research/evidence/MANIFEST.sha256` |
| `docs` | ubuntu | `tools/ci/check_readme.py`: README links, scripted README commands and headline numbers against `research/evidence/INDEX.json` |
| `build` | ubuntu | `tools/ci/check_versions.py`, `uv build` (wheel and sdist), `twine check --strict`, wheel/sdist content assertions (the sdist must not contain `research/`, `extension/`, `tests/`, `tools/` or `.github/`), the extension ZIP built twice and compared byte for byte, `tools/ci/supply_chain.py --check`; uploads `dist/` |

Branch protection matches check runs by the job's `name:`, not its job ID, so
the required checks are listed under
[Repository settings](#repository-settings-one-time) by those names.

### `e2e.yml` (pull requests and pushes touching `extension/`, nightly, manual)

`e2e-linux` runs the headful recorder E2E suite on Ubuntu with Puppeteer's
Chrome for Testing (cached by `package-lock.json` hash):

```
xvfb-run -a npm run e2e        # in extension/
# env: CUA_E2E_CI_NO_SANDBOX=1  CUA_E2E_RESULT_DIR=$RUNNER_TEMP/e2e
```

`CUA_E2E_CI_NO_SANDBOX=1` disables Chrome's sandbox only on the CI runner,
whose kernel blocks the unprivileged user namespaces the sandbox needs. The
bundle is uploaded as the `e2e-bundle-linux` artifact (also on failure) and
then checked by `tools/ci/validate_e2e_bundle.py`, which requires:

- all bundle files (`run_manifest.json`, `validation_summary.json`,
  `storage_lifecycle_summary.json`, `exports_manifest.json`,
  `event_coverage_summary.json`, `agent_extension_e2e_trace.json`,
  `E2E_REPORT.md`);
- 15 of 15 cases passed (T00-T14), none failed or skipped;
- zero recorded browser errors (errors from Chrome's own `chrome://` pages are
  kept apart in `browser_ui_errors` and do not count);
- `run_manifest.extension.version` equal to `extension/app/manifest.json`, and,
  where the bundle records them, `extension.permissions` equal to the
  manifest's permissions and `browser_runtime.major_version` at least
  `minimum_chrome_version`;
- the isolation and claim-boundary attestations, every listed artifact present
  inside the bundle, and every export matching its recorded size and SHA-256.

The validator ignores fields it does not know, so the harness can add
diagnostics without breaking the gate. The same validator accepts the archived
0.4.0 bundle (14 cases, T00-T13): `python tools/ci/validate_e2e_bundle.py
research/evidence/recorder_e2e_v0.4.0 --expected-version 0.4.0 --expected-cases 14`.

`e2e-windows` runs the same suite on `windows-latest` on the nightly schedule
and on manual dispatch only. It is informational (`continue-on-error`) and never
required.

### `codeql.yml` (pushes to `main`, pull requests, weekly, manual)

CodeQL `security-extended` queries for `python`, `javascript-typescript` and
`actions` (the workflows themselves), with `build-mode: none`. Only this job
has `security-events: write`.

### `audit.yml` (weekly, manual, and pull requests that change dependency files)

| Job | What it runs |
|---|---|
| `python` | `uv export --locked --all-extras --all-groups` piped to `pip-audit --strict --disable-pip --require-hashes`: every package in `uv.lock`, including the research and notebook extras and the dev tools |
| `npm` | `npm audit --audit-level=high` in `extension/` (development dependencies included, because CI executes them) |
| `research-locks` | informational: `pip-audit` on each hash-locked research graph listed by `tools/ci/supply_chain.py --print-python-audit-targets`, each in isolation, and `npm audit` on research lockfiles. These graphs are frozen provenance, so findings are reported as warning annotations and a job summary, not auto-fixed, and never block or turn the check red |
| `dependency-review` | pull requests only: `actions/dependency-review-action`, failing on new dependencies with high-severity advisories |

### `release.yml` (tags `vX.Y.Z`; manual dispatch is a dry run)

See [releasing.md](releasing.md). The `build` job checks versions against the
tag, builds the wheel, sdist and extension ZIP, generates the CycloneDX SBOM and
`SHA256SUMS`, and uploads them as the `release-dist` artifact. On a tag push,
`github-release` (the only job with `contents: write`, `id-token: write` and
`attestations: write`) verifies the checksums, attests build provenance for
every file in `SHA256SUMS` and creates a **draft** GitHub Release. The `pypi`
job is skipped unless the repository variable `PUBLISH_TO_PYPI` is `true`; it
then publishes the wheel and sdist through PyPI trusted publishing from the
protected `pypi` environment.

## Repository settings (one-time)

These settings live in GitHub, not in the repository, so a new or transferred
repository starts without them. Apply them before the repository is announced.

1. **Private vulnerability reporting.** [SECURITY.md](../SECURITY.md),
   [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md) and the issue templates all send
   reporters to `/security/advisories/new`, which works only when this is on
   (it is off by default for repositories owned by a user account):

   ```
   gh api -X PUT repos/appleweiping/ComputerUseAgent/private-vulnerability-reporting
   gh api repos/appleweiping/ComputerUseAgent/private-vulnerability-reporting
   ```

   The second command must report `"enabled": true`.

2. **Branch protection on `main`.** Require these check runs. GitHub names a
   check run after the job's `name:` (with the matrix values filled in), not
   after its job ID:

   | Job ID | Required check runs |
   |---|---|
   | `lint` | `Lint` |
   | `typecheck` | `Type check` |
   | `test-python` | `Python 3.12 (ubuntu-latest)`, `Python 3.13 (ubuntu-latest)`, `Python 3.12 (windows-latest)`, `Python 3.13 (windows-latest)` |
   | `test-research` | `Research (ubuntu-latest)`, `Research (windows-latest)` |
   | `test-node` | `Extension (ubuntu-latest)`, `Extension (windows-latest)` |
   | `evidence` | `Evidence integrity` |
   | `docs` | `Docs` |
   | `build` | `Build and package` |

   If a job's `name:` or matrix changes, update this table and the
   branch-protection settings together; renaming a job silently drops its
   required check.

3. **Optional, free on public repositories:** Dependabot alerts, and secret
   scanning with push protection.

   ```
   gh api -X PUT repos/appleweiping/ComputerUseAgent/vulnerability-alerts
   gh api -X PATCH repos/appleweiping/ComputerUseAgent -F "security_and_analysis[secret_scanning][status]=enabled" -F "security_and_analysis[secret_scanning_push_protection][status]=enabled"
   ```

## Supply-chain controls

- **Pinned Actions.** Every `uses:` is a 40-character commit SHA followed by a
  version comment, for example
  `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1`.
  `tools/ci/check_actions_pinned.py` enforces this in CI and in pre-commit. It
  reads only the one-line `uses: owner/repo@<sha>` form and fails closed on any
  other spelling of a `uses` key (flow mappings, quoted keys, a value on the
  next line, block scalars, tags, anchors or aliases). Pinned versions were at least about two weeks old when chosen, and Dependabot
  waits seven days (`cooldown`) before proposing a newer release.
- **Locked Python graph.** `uv.lock` pins every package with SHA-256 hashes from
  the official PyPI index; CI installs with `uv sync --locked`, which fails if
  the lock no longer matches `pyproject.toml`. `tools/ci/supply_chain.py`
  additionally rejects any locked package from another index or without a
  hash, and any exact pin in `pyproject.toml` that the lock does not honor.
  ruff and mypy are pinned exactly in the `lint` group, and the ruff
  pre-commit hook revision matches that pin.
- **Locked npm graph.** `extension/package-lock.json`, installed with `npm ci`.
  `tools/ci/supply_chain.py` rejects any npm lock entry (here or under
  `research/`) that does not resolve from `https://registry.npmjs.org/`.
- **Research locks.** The hash locks and exact-pin files beside each research
  harness are frozen provenance. Dependabot never touches `research/**`; the
  audit workflow reports on them separately.
- **SBOM.** `tools/ci/supply_chain.py --write` builds a CycloneDX 1.6 SBOM from
  `uv.lock`, `extension/package-lock.json` and the research manifests. The root
  component is `pkg:pypi/computer-use-agent@<version>` (read from
  `pyproject.toml`), each component records its source manifests, npm
  components carry their SHA-512 integrity, and the document has no timestamp
  or serial number, so identical inputs give identical bytes. It is validated
  offline against the schema bundled with the locked CycloneDX library,
  generated at release time and attached to the release; it is not committed.
- **Secret scan.** `tools/ci/scan_secrets.py` scans every file Git would track
  (including archived evidence) for provider tokens, private keys, OSF
  view-only capability links and high-entropy credential assignments, and
  prints only the file, line and rule, never the value.
- **Dependabot** (`.github/dependabot.yml`) proposes weekly grouped minor and
  patch updates for `uv` at `/`, npm at `/extension` and GitHub Actions. It
  ignores `ruff`, which is pinned twice (the `lint` group and the ruff
  pre-commit hook revision) and is bumped by hand in both places; the steps are
  in the comment above the pin in `pyproject.toml`.

## Coverage

- Python: branch coverage of `computer_use_agent` with `fail_under = 75`. The
  guest-only X11 modules `capture/x11/recorder.py`, `guest_controller.py` and
  `calibration_probe.py` are omitted: they run under the guest's system Python
  inside the ScienceBoard VM and are exercised only there. The floor rises by
  5 points per minor release toward 85.
- Extension: `npm run test:coverage` applies Node's built-in coverage
  thresholds to `extension/app`.

## Running the checks locally

```
uv run nox                    # lint, typecheck, tests-3.12, tests-3.13, node
uv run nox -s research build evidence docs
uv run nox -s e2e             # opens a browser window (xvfb-run on headless Linux)
uv run pre-commit run --all-files
python tools/ci/check_versions.py
python tools/ci/supply_chain.py --check        # needs the dev group: uv run ...
```

On Windows run the same commands in PowerShell; nox, uv and the `tools/ci`
scripts do not depend on a POSIX shell.

actionlint, the `mypy research` step, the wheel/sdist content assertions,
CodeQL and the dependency audits are not wrapped in a nox session. To run one
of them by hand, copy its command from the workflow file (`ci.yml`,
`codeql.yml` or `audit.yml`).
