# Releasing

A release publishes one version for the Python package, the Chrome extension
ZIP and the citation metadata. Releases are cut from `main` by pushing a tag
`vX.Y.Z`; the `release.yml` workflow builds and attests the assets and opens a
**draft** GitHub Release that a maintainer reviews and publishes.

## Versioning

- Semantic Versioning, plain `MAJOR.MINOR.PATCH` (Chrome accepts only dotted
  integers in `manifest.json`, so there are no pre-release suffixes).
- While the project is 0.x, a breaking change to the CLI, the Python API or the
  repository layout bumps the minor version.
- Wire formats (`cui-trace-v2`, `cui-detector-dataset-v2`, ...) are versioned
  independently through `schema_version`; a release never changes them silently.
- The extension version must never go backwards: Chrome orders updates by
  version, and 0.4.0 already shipped.
- 1.0.0 waits for a frozen CLI, API and wire formats, a green E2E job for three
  consecutive releases, and either a preregistered, validated detector
  evaluation or an explicit narrowing of scope to "recorder plus analysis
  toolkit".

## Checklist

Before the first release (and after any repository transfer), apply the
one-time [repository settings](ci.md#repository-settings-one-time): private
vulnerability reporting, which every reporting link in the project depends on,
and the required status checks.

1. **Bump the version in all four places** in one pull request:
   - `pyproject.toml` `[project].version`
   - `extension/app/manifest.json` `version`
   - `extension/package.json` `version` (then `npm --prefix extension install
     --package-lock-only` so `package-lock.json` follows)
   - `CITATION.cff` `version` and `date-released`

   Also update the literal fallback `__version__` in
   `src/computer_use_agent/__init__.py` if it has one, and run `uv lock` so `uv.lock` records
   the new project version. Then check:

   ```
   python tools/ci/check_versions.py
   uv lock --check
   ```

2. **Update `CHANGELOG.md`**: move the `[Unreleased]` entries into a new
   `## [X.Y.Z] - YYYY-MM-DD` section and update the compare links at the bottom.
   The release workflow copies this section into the release notes and fails if
   it is missing.

3. **Run the full local gate** (or rely on CI for the merge commit):

   ```
   uv run nox
   uv run nox -s research build evidence docs
   ```

   Confirm that the latest `Extension E2E` run on `main` passed all 15 cases
   (T00-T14).

4. **Dry run** (optional): run the `Release` workflow manually from the Actions
   tab (`workflow_dispatch`). It builds every asset and uploads them as the
   `release-dist` artifact without creating a release or an attestation.

5. **Tag and push** from the merged `main` commit:

   ```
   git tag -a vX.Y.Z -m "ComputerUseAgent X.Y.Z"
   git push origin vX.Y.Z
   ```

6. **Review the draft release.** The workflow attaches:

   | Asset | Produced by |
   |---|---|
   | `computer_use_agent-X.Y.Z-py3-none-any.whl`, `computer_use_agent-X.Y.Z.tar.gz` | `uv build` |
   | `computer-use-agent-recorder-X.Y.Z.zip` | `npm --prefix extension run package` (`tools/package_extension.py`; the `extension/app` runtime files plus `LICENSE` and `NOTICE`) |
   | `computer-use-agent-X.Y.Z.cdx.json` | `tools/ci/supply_chain.py --write` (CycloneDX 1.6 SBOM) |
   | `SHA256SUMS` | `sha256sum` over the files above |

   Every file listed in `SHA256SUMS` has a GitHub build-provenance attestation.
   Check the notes, then publish the draft.

7. **Verify a downloaded asset** (anyone can do this):

   ```
   sha256sum --check SHA256SUMS
   gh attestation verify computer-use-agent-recorder-X.Y.Z.zip --repo appleweiping/ComputerUseAgent
   ```

## PyPI (disabled by default)

The `pypi` job runs only when the repository variable `PUBLISH_TO_PYPI` is set
to `true`. To enable it:

1. On PyPI, add a trusted publisher for project `computer-use-agent`:
   repository `appleweiping/ComputerUseAgent`, workflow `release.yml`,
   environment `pypi`.
2. In the GitHub repository settings, create the environment `pypi` with
   required reviewers, and set the variable `PUBLISH_TO_PYPI=true`.

The job then publishes the wheel and sdist from the same `release-dist`
artifact after the draft release has been created. No API token is stored.

## Chrome Web Store

Not automated. Publishing to the Chrome Web Store needs a hosted privacy
policy and Google's review; until then users load `extension/app` unpacked or
install the release ZIP in developer mode.

## If something goes wrong

- A failed workflow before the release is published: fix forward on `main`,
  delete the draft release and the tag (`git push --delete origin vX.Y.Z`),
  and tag again.
- A published release with a defect: never re-use a version. Release a new
  patch version and mark the defective release in its notes.
