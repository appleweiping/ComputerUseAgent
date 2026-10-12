# Frozen External Citation Graph

Retrieval window: `2026-08-11T13:15:37.136433Z` to `2026-08-11T13:41:01.249194Z` UTC. Overall status: **partial**.

## Coverage

- Nodes: 12932 = 68 frozen seeds + 12864 exact provider-linked external works.
- Directed citing→cited edges: 22117 total assertions; seed-to-seed: 189; external-involved: 21928; multi-provider: 3571.
- OpenAlex: 63/64 seeds resolved; 63/63 resolved inbound cursors exhausted; 63/63 complete `referenced_works` arrays.
- Semantic Scholar sensitivity: 62/64 seeds resolved; citations 61/62, references 53/62 fully paged.
- Direction status (complete / partial / blocked, denominator 64 each): OpenAlex incoming 63 / 0 / 1; OpenAlex outgoing 63 / 0 / 1; Semantic Scholar citations 61 / 1 / 2; Semantic Scholar references 53 / 0 / 11.
- Crossref: 14/14 DOI seeds in its intentionally limited scope.
- XPAC-only nodes: 9. XPAC is an OpenAlex collection marker, never a relevance tier.

## OpenAlex unresolved seed ledger

- `IEEE-S&P-2026:WebCloak` — `unresolved`

## Audit boundaries

- Explicit partial seed-directions: 1; blocked seed-directions: 15; unhandled HTTP 429 responses: 0.
- OpenAlex dangling referenced-work metadata IDs: 54; their exact W identifiers remain nodes with unresolved membership.
- Semantic Scholar tracked outputs expose minimal IDs/support only; exact payloads remain ignored under `private_data/`.
- Crossref exact DOI references are deposited-reference candidates/support, not a claim that Crossref bibliographies are exhaustive.
- Node merging uses exact provider IDs, DOI, or arXiv only. Titles, topics, authors, and XPAC membership never merge nodes or create edges.

## Rebuild

With the ignored private raw window available, `python literature/scripts/build_citation_artifacts.py` rebuilds without network access and `--check` verifies byte identity. In a clean clone without licensed/private raw, `--check` instead verifies the tracked graph/CSV/QA cross-contract and reports that clone-safe mode explicitly.
