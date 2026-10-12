# Literature corpus and citation graph

This directory holds the literature review behind the project: a frozen 68-item corpus
of prior work on telling computer-use agents apart from people, its deep-read reviews
and claim ledger, and a directed citation graph around the corpus. It is research
provenance, not part of the `computer-use-agent` package. Run every command below from
the repository root.

## What the corpus contains

- **68 items**: 64 papers or preprints and 4 code repositories. `Persistent ID` (for
  example a normalized DOI, arXiv or GitHub identifier) is the stable key.
- The corpus merges three hand-curated inputs (43 + 24 + 1 rows) and applies one
  reviewed addendum. Persistent IDs are normalized before deduplication, so case-only
  differences cannot create duplicates.
- **Review status**: 60 papers have a full-text review, 4 DOI items are
  access-blocked and kept as metadata-and-abstract inventory only (no empirical number
  from them is promoted), and all 4 repositories have commit and license snapshots.
- Every review uses one field contract: research question, threat model, observer
  position, interface, data, observable features, evaluation protocol, baselines,
  metrics, ablations, uncertainty, failure modes, limitations, reproducibility assets,
  consequence for this project, evidence locators, and the license or reuse boundary.
  Unknown values stay explicit; nothing is inferred from an abstract.
- The **claim ledger** has 98 claims, each with a source URL and a page, section,
  figure, table or appendix locator. Numeric claims never come from abstracts alone.

### Citation graph (status: partial)

- 12,932 nodes: the 68 seeds plus 12,864 external works linked by exact provider
  identifiers. Titles never merge nodes or create edges.
- 22,117 directed `citing -> cited` assertions: 189 seed-to-seed and 21,928 involving an
  external work.
- OpenAlex (CC0) is the primary source. Semantic Scholar contributes only minimal
  provider IDs, support flags and attribution, as a sensitivity layer. Crossref exact-DOI
  deposited references are bounded candidates. XPAC is only an OpenAlex membership
  marker (`openalex-core`, `openalex-xpac` or `unresolved`), never a relevance tier.
- Direction coverage over the 64 paper seeds (complete / partial / blocked): OpenAlex
  incoming 63/0/1, OpenAlex outgoing 63/0/1, Semantic Scholar citations 61/1/2,
  Semantic Scholar references 53/0/11. These are counts for one retrieval window, not a
  claim that any index is complete.

## Layout

| Path | Contents |
|---|---|
| `sources/` | The 8 hand-curated inputs: corpus rows, the foundational expansion, the newest comparator, the reviewed addendum, manual claims, repository snapshots, full-text access overrides and browser-review records |
| `snapshots/` | The two retrieval manifests written by the network collectors: `fulltext_evidence_manifest.json` (access times, hashes, page or heading inventories) and `citation_retrieval_manifest.json` (retrieval window, pagination state, HTTP and rate-limit headers, parser versions, raw-response hashes) |
| `generated/` | Deterministic builder outputs: the 68-item truth (JSON, CSV, Markdown), reviews, claim ledger, QA summaries, citation nodes and edges, citation QA and narrative, and `LARGE_ARTIFACTS.json` |
| `generated/large/` | Git-ignored. The two large builder outputs, fetched on demand (below) |
| `archive/` | Superseded early survey and deep-read files, kept unchanged as history (see `archive/README.md`) |
| `scripts/` | Builders (`build_literature_artifacts.py`, `build_citation_artifacts.py`), the network collectors (`collect_fulltext_evidence.py`, `fetch_citation_graph.py`) and shared helpers (`corpus_common.py`) |
| `fetch_large.py` | Fetches and verifies the large artifacts |
| `tests/` | Contract tests for all of the above |

Files under `generated/` are byte-exact (`.gitattributes` marks them `-text`) and
hash-bound. They record their inputs with the source repository's flat layout, for
example `literature/manual_claims.json`, and two of them contain rebuild hints of the
form `python literature/scripts/...`. Those strings are part of the recorded bytes, so
they are kept; the builders reproduce them, and `corpus_common.resolve_recorded_path`
maps them to the current layout. The current commands are the ones in this README.

## Large artifacts

`citation_graph.json` (72,766,360 bytes) and `citation_assertions.csv` (46,375,030
bytes) are generated outputs too large to commit. `generated/LARGE_ARTIFACTS.json`
records each file's byte count, SHA-256 and an immutable download URL: the source
repository at a pinned commit (see `../PROVENANCE.md`). Fetch them into
`generated/large/` with:

```
python -m research.literature.fetch_large                  # download about 119 MB over HTTPS
python -m research.literature.fetch_large --from-dir DIR   # or copy from a local directory
python -m research.literature.fetch_large --verify-only    # check files already in place
```

The command verifies the size and SHA-256 of every file before moving it into place,
and is idempotent: verified files are left untouched and a wrong file is replaced.
`--dest DIR` writes somewhere else and `--name FILE` limits the run to one artifact.

## Checking the artifacts

```
python -B -m research.literature.scripts.build_literature_artifacts --check
python -B -m research.literature.scripts.build_citation_artifacts --check
python -B -m research.literature.scripts.build_citation_artifacts --check --require-large
python -B -m pytest research/literature
python -B -m pytest research/literature -m "not requires_large_artifacts"
```

What each check proves:

- **`build_literature_artifacts --check`** rebuilds the seven corpus outputs in
  `generated/` from `sources/` and `snapshots/` and compares them byte for byte. This
  works in any clone. It proves that the generated files follow from the tracked
  snapshots. It does not re-collect the snapshots: the full-text manifest was produced
  from raw PDF and HTML bytes that are not redistributed.
- **`build_citation_artifacts --check`** runs in one of three modes and prints which:
  1. `byte-identical-rebuild-from-private-raw`: when the private raw provider responses
     of the retrieval window are available (only on the machine that ran the
     retrieval), every response is re-hashed and all six outputs are rebuilt without
     network access and compared byte for byte (a large output that has not been
     fetched is compared with its recorded size and SHA-256). A partial raw cache is an
     error, never a silent fallback.
  2. `tracked-bundle-with-private-raw-absent`: the normal mode for a clone that has run
     `fetch_large`. The large files are checked against `LARGE_ARTIFACTS.json`, then the
     graph, CSVs and QA are cross-checked as a closed bundle: node and edge counts,
     required provider-evidence fields, raw-hash format, QA bound to the retrieval
     window, and the graph's recorded hashes of the retrieval manifest and corpus
     inputs. Nothing is rebuilt.
  3. `tracked-small-files-only-large-artifacts-absent`: without the large files, only
     the committed QA, node and edge CSVs and narrative are checked, and the output says
     so. `--require-large` makes this mode fail instead.

  A present large file with the wrong size or SHA-256 always fails the check.

**A byte-for-byte rebuild of the citation graph is not possible from a public clone.**
It needs the raw provider responses, and the full-text evidence needs the raw full
texts; both stay private for licensing reasons. A public clone gets the consistency
checks above.

The tests in `tests/test_literature_artifacts.py` that read the citation graph are
marked `requires_large_artifacts` and skip with a reason when the files are absent.
`tests/test_large_artifacts.py` covers `fetch_large`, the large-artifact record, the
layout helpers and the `--check` modes without the network or the large files.

## Private raw data and the collectors

The manifests refer to raw payloads by recorded paths under `private_data/`:
`private_data/literature/fulltext_raw/...` and
`private_data/literature/citation_raw/run-<timestamp>/...`. The tools resolve that prefix
against `CUA_PRIVATE_ROOT` when it is set (it must point outside the repository), and
otherwise against the git-ignored `private_data/` directory at the repository root.

`scripts/collect_fulltext_evidence.py` and `scripts/fetch_citation_graph.py` are the
network collectors that produced `snapshots/`. They are kept for provenance and are not
run in CI. Running them starts a new retrieval window and changes the snapshots and
therefore every generated output. The citation collector reads the optional
`OPENALEX_API_KEY`, `SEMANTIC_SCHOLAR_API_KEY`, `CROSSREF_MAILTO` and
`CUA_S2_REQUEST_INTERVAL` environment variables. Public URLs in the manifests have API
keys and contact addresses removed, and only the presence of a credential is recorded.
The full-text collector needs `pypdf` for PDF sources.

## Reuse boundary

Prefer project-made plots. Reusing a figure or table from a source requires its
access-copy license to allow it, or a separately documented quotation decision. Sources
recorded as `NOASSERTION` or publisher copyright are not default screenshot sources.
