# Canonical Literature Truth - 68 Items

Built from the persisted evidence snapshot `2026-08-11T12:11:52.660499Z`. This file is derived; run `python literature/scripts/build_literature_artifacts.py --check` to verify byte-for-byte reproducibility.

## Corpus contract

| Check | Result |
|---|---:|
| Total items | 68 |
| Papers/preprints | 64 |
| Repositories | 4 |
| Full text reviewed | 60 |
| Access blocked | 4 |
| Repository reviews | 4 |
| Claim-ledger entries | 98 |
| Located numeric claims | 8 |

The four access-blocked DOI items are retained as metadata/abstract inventory only. No empirical number from those items is promoted. Monrose and Rubin is not blocked: a university-hosted full paper was byte-verified, while its access-copy license/reuse boundary is recorded separately.

## Exact access-blocked set

- `10.1109/tdsc.2007.70207`
- `10.1109/tifs.2012.2223677`
- `10.1145/1085126.1085129`
- `10.1145/581271.581272`

## Latest direct comparator

`arXiv:2607.26935` is included as item 68. Its full-text review explicitly separates dataset composition, DOM-event schema, task mismatch, Playwright executor signature, OS-level threat-model exclusion, minimal-feature in-corpus results, and unreleased-artifact status. The report must not restate its executor-specific result as universal reasoning detection.

## Reuse and screenshot boundary

Prefer project-created plots. Reusing a source figure/table requires the access-copy license to allow it or a separately documented quotation/fair-use decision. Every reused visual must retain the paper title, figure/table locator, and citation; `NOASSERTION` and publisher-copyright sources are not default screenshot sources.

## Machine-readable outputs

- `literature_truth_68.json` / `literature_sources_68.csv`: workbook-builder source truth.
- `deep_read_reviews_68.json`: unified review fields and source-access/license evidence.
- `claim_ledger_68.json` / `literature_claims_68.csv`: claim-level locators and boundaries.
- `literature_qa_68.json`: closed-schema, count, license, locator, and numeric-claim checks.
