# Literature survey audit — 2026-08-10 local integration

> **HISTORICAL SNAPSHOT — SUPERSEDED 2026-08-11.** The 43/17/12 counts below
> document the earlier integration checkpoint only. Current corpus truth is
> `literature_truth_68.json`: 68 items, comprising 60 full-text paper reviews,
> 4 access-blocked papers, and 4 repository reviews.

## Result

- Canonical records: **43 x 41 columns**
- Unique persistent IDs: **43**; duplicates: **0**
- Blank canonical cells: **0**; ERROR flags: **0**
- Workbook-integrated sources with locator-backed deep reads: **17**
- High-confidence addendum rows integrated by stable `Persistent ID`: **5 / 5**
- High-confidence source coverage in the integrated matrix: **17 / 17**
- High-confidence taxonomy rows: **17**
- Rows still explicitly awaiting full-text review: **25**
- Read status: **Abstract reviewed: 25**; **Abstract/project reviewed: 0**; **Key sections reviewed: 14**; **Repository/project reviewed: 4**
- XLSX round-trip and 17-row Deep Read round-trip: **passed**
- Workbook formula-error scan: **0 matches**

## What changed

- Added the **Deep Read** worksheet and **Literature_Deep_Read_12.csv** for FP-Agent, Known By Their Actions, ScienceBoard, OSWorld 2.0, WebArena, An Illusion of Progress, UI-TARS, Agent S2, LabOSBench, browser-use benchmark, Skyvern, and Stagehand.
- Used an eight-axis analytic taxonomy: research role; interface/environment; observer position; observable signal; action/architecture; unit/horizon; evaluation protocol; and failure/claim boundary.
- Added exact section/figure/table or README-section locators where the primary source supports them.
- Updated the matching 12 canonical rows without deleting or rewriting any of the other 31 records.
- Completed key-section reviews for the five remaining high-confidence rows:
  Cognitive Trap Framework, WebCloak, KILLBENCH, Mind2Web 2, and BrowseComp.
  The findings and exact locators are in
  `HIGH_PRIORITY_DEEP_READ_ADDENDUM_5.md`; the deterministic keyed write payload
  is in `literature_high_priority_deep_read_addendum.json`.
- Applied exactly those five closed-schema canonical updates and appended the
  five 14-field rows to the Deep Read worksheet and legacy-compatible
  `Literature_Deep_Read_12.csv` path.
- Preserved every existing formula and verified all five rendered sheets after
  extending Deep Read formatting through row 18.

## Current integration boundary

The five-source review is now part of the local XLSX, canonical CSV, upsert
files, and QA truth. The workbook was imported and exported with the
loader-verified bundled artifact runtime; no alternate XLSX writer was used.
The 43 x 41 canonical table, 17 x 14 Deep Read matrix, closed schemas, stable-ID
deduplication, formulas, CSV/upsert round trips, formula-error scan, and all
five rendered sheets passed. Shared release checks/status and the native Google
Sheet remain a separate transition owned outside this literature-only stage.

## Claim boundaries kept in the literature table

- Browser-Use and Skyvern results are narrow integration pilots, not official benchmark scores.
- Stagehand is a 3/3 single-pass smoke check, not a repeated benchmark.
- FP-Agent is released-model/released-split inference; it is not retraining or unseen-family validation.
- The Week 3 nine-run browser-simulator set is a scripted automation control, not a reasoning CUI-agent evaluation.
- ScienceBoard, the self-human sessions, AIOAgent/SeeAct runs, and Week 4 classifier results are not yet completed evidence.

## Source gaps left explicit

- An official code repository URL was not confirmed for **Known By Their Actions**.
- An official code repository URL was not confirmed for **LabOSBench**.
- An official code or data repository URL was not confirmed for **KILLBENCH**;
  the addendum relies on the arXiv v4 paper and does not infer an artifact.
- Some earlier expansion rows still lack an exact cited-versus-citing provenance trail. Their QA flags remain; no relationship was inferred.

## Cloud status

The current five-tab native Google Sheet was written and read back through the
UMN account on 2026-08-09. The readback covered the 43 x 41 literature table
and 12-row Deep Read companion; `Saved to Drive` was observed. This proves the
`adb57a0` baseline snapshot. The local workbook now has 17 Deep Read rows, but
no later authenticated Google Sheet write/readback occurred. The exact boundary
is tracked in
[`status/project_status.json`](../status/project_status.json).

## SHA-256

- XLSX: **b0c91ddef6512336aa42178f8ea9b9f5281d7d3fda767d6e84f423409a30f670**
- Canonical CSV: **4e3296dd08f4e51b2ef096c51bd45dd60fd1a7229d05c35770e1bd4990d8f6de**
- Upsert CSV: **4e3296dd08f4e51b2ef096c51bd45dd60fd1a7229d05c35770e1bd4990d8f6de**
- Deep-read CSV: **0cab972cef9ba2299e242c23a28db37e2d0e5f107b03c43d06367217e516cf14**
- Upsert JSON: **15e39f7fde8ea81ce98ed22f2faeaea5fe001754f125752610aea40eb65945bc**
- Survey QA JSON: **f80d75cf09d8a9aabe1e23d199f90f9aeb64975745ed8398b804b8b49bc8afe5**
- Five-source addendum Markdown:
  **76a6fbdea52c6780da08e721922693487e141690c9a4572c4e50736789662120**
- Five-source keyed payload JSON:
  **687990d8372e7585b80d4cb0379be062fa1bb6714de61f2421fa64f6c37f8635**
- Five-source integrated QA JSON:
  **c3489b7e897da16158a5cd6332ba9d8c059c8275457e011b08fe84be9c12a174**
