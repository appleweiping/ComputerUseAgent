## Summary

<!-- What does this change do, and why? Link the issue it closes. -->

## Checklist

- [ ] Tests added or updated; `nox` (lint, typecheck, tests, node) passes locally
- [ ] `CHANGELOG.md` updated under `[Unreleased]` (user-visible changes only)
- [ ] **Privacy impact** considered: no new captured field, permission or export path
      without a matching update to `docs/extension/privacy.md`; no raw human traces,
      typed text, credentials or machine-specific absolute paths in this PR
- [ ] **Schema / wire format**: no change to a `cui-*` schema, chrome.storage key or
      export format, **or** the change is versioned and documented
- [ ] **Version**: no version bump, **or** pyproject, `extension/app/manifest.json`,
      `extension/package.json` and `CITATION.cff` were bumped together
      (`python tools/ci/check_versions.py`)
- [ ] Archived evidence under `research/evidence/` is untouched (or the change is a
      documented, re-hashed correction)

## Notes for reviewers

<!-- Anything that needs special attention: risky areas, follow-ups, screenshots of UI changes (synthetic data only). -->
