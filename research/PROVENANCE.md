# Provenance of `research/`

This repository starts a fresh history. Its content was imported from a snapshot of
an earlier research repository; that repository remains the archive of record.

| Field | Value |
|---|---|
| Source repository | https://github.com/appleweiping/cu-detector-research-artifact |
| Source commit | `437070f519d4bd40e9e4e46bf19fcca52b4b7557` |
| Source tree | `cdec6a295903cf37b73bc5ef5fffc7815e2b25ee` |
| Commit date | 2026-08-18T17:25:36+08:00 |
| Import date | 2026-10-11 |
| Tracked files at the source commit | 566 |
| Imported (moved or kept) | 457 |
| Not imported | 109 (listed below) |

The import was a mechanical relayout: files were moved, a few were renamed, and the
minimum edits needed to make the moved code import and find its fixtures were applied
(module paths, repository-relative path constants). The restructure into the
maintained project then edited code and documentation outside `research/evidence/`.

Files under `research/evidence/` are byte-identical to their source except for three
minimal redactions: in `fp_agent/fp_agent_classifier_eval.json` (`/dataset/source`) and
`fp_agent/fp_agent_loafo.json` (`/source/feature_vectors_source`) an OSF view-only link
became `https://osf.io/j6b5p/`, and in `scripted_simulator_control_v0.2.1/run_manifest.json`
(`/mentor_selection_status`) a name became `mentor`. The READMEs were rewritten, and
`INDEX.json`, `LEGACY_PATHS.json`, `MANIFEST.sha256` and four READMEs were added (see
`research/evidence/README.md`). Paths recorded inside those files still use the old
layout (`results/...`, `experiments/...`, `recorder/...`);
`research/evidence/LEGACY_PATHS.json` maps the `results/...` prefixes, and the map
below covers the rest. `research/week1_miniwob/checkout.py` translates the frozen
Week 1 manifest paths `recorder` to `extension/app` and `experiments/week1/` to
`research/week1_miniwob/`.

Two research inputs were edited after the import to remove personal or institutional
wording, so their bytes no longer match hashes recorded in archived files:
`research/scienceboard/tasks/manifest.json` (its `selection_status` and privacy strings
were neutralized; task payload hashes are unchanged, but the archived release capsule's
descriptor for `experiments/scienceboard/tasks/manifest.json` no longer matches; the
capsule is archived and never validated) and the three task files in
`research/scientific_simulators/tasks/` (their as-executed hashes are listed in
`research/scientific_simulators/README.md`). `research/configs/formal-study.json` was
de-personalized too; the retrospective policy binds its historical bytes through the
archived capsule record.

Wire-format identifiers were not renamed. `schema_version` values such as
`cui-trace-v2` and the `chrome.storage` keys `cui-recorder-root`, `cui-session-*` and
`cui-events-*` keep the historical `cui-` namespace.

## Old to new path map (summary)

| Source path | Destination |
|---|---|
| `experiments/contracts/{__init__,validation}.py` | `src/computer_use_agent/contracts/` |
| `experiments/contracts/schema/*.schema.json`, `recorder/schema/*.json`, `experiments/scienceboard/x11_trace.schema.json` | `src/computer_use_agent/schemas/` (the X11 schema is renamed `x11-trace-v2.schema.json`) |
| `experiments/contracts/tests/` (except `test_retrospective.py`) | `tests/contracts/` |
| `experiments/contracts/tests/test_retrospective.py` | `research/fp_agent/tests/test_config.py` |
| `experiments/detector/*.py` | `src/computer_use_agent/detector/` |
| `experiments/detector/tests/` | `tests/detector/` |
| `experiments/detector/README.md` | `docs/detector.md` |
| `experiments/scienceboard/{trace_io,schema_validation,privacy}.py` | `src/computer_use_agent/trace/{io,validation,privacy}.py` |
| `experiments/scienceboard/{features,task_statistics}.py` | `src/computer_use_agent/features/{extract,statistics}.py` |
| `experiments/scienceboard/{x11_recorder,guest_controller,calibration,calibration_probe}.py` | `src/computer_use_agent/capture/x11/{recorder,guest_controller,calibration,calibration_probe}.py` |
| `experiments/scienceboard/tests/test_features.py` | `tests/features/test_features.py` |
| `experiments/scienceboard/tests/test_trace_and_calibration.py` | split by test class into `tests/trace/test_trace_io.py`, `tests/capture/test_calibration.py` and `research/scienceboard/tests/test_preflight.py` |
| `experiments/scienceboard/` (harness, tasks, runtime_compat, remaining tests) | `research/scienceboard/` |
| `experiments/week1/` | `research/week1_miniwob/` |
| `experiments/{browser_use,skyvern,stagehand}/` | `research/pilots/{browser_use,skyvern,stagehand}/` |
| `experiments/{codex_case,scientific_simulators,auxiliary_data}/` | `research/{codex_case,scientific_simulators,auxiliary_data}/` |
| `experiments/fp_agent/` (tests into `tests/`) | `research/fp_agent/` and `research/fp_agent/tests/` |
| `viewer/{__init__,privacy,serve}.py` | `src/computer_use_agent/viewer/` |
| `viewer/{index.html,app.js,styles.css}` | `src/computer_use_agent/viewer/static/` |
| `viewer/tests/`, `viewer/README.md` | `tests/viewer/`, `docs/viewer.md` |
| `recorder/{manifest.json,src,popup,dashboard,privacy}` | `extension/app/` |
| `recorder/tests/` | `extension/test/unit/` |
| `recorder/examples/` | `examples/traces/` |
| `recorder/event_coverage.json` | `extension/event_coverage.json` |
| `recorder/demo/{index.html,demo.js,demo.css}` | `extension/playground/` |
| `recorder/{README,DATA_SCHEMA,DATA_STORE,EVENT_COVERAGE,PRIVACY}.md` | `docs/extension/{index,data-schema,data-store,event-coverage,privacy}.md` |
| `scripts/formal_study.py`, `scripts/study_ledger.py` | `src/computer_use_agent/study/{policy,ledger}.py` |
| `scripts/{scan_secrets,check_actions_pinned,supply_chain}.py` | `tools/ci/` |
| `scripts/week2_e2e_bundle.py` | `tools/ci/validate_e2e_bundle.py` |
| `scripts/package_recorder.py` | `tools/package_extension.py` |
| `scripts/validate_retrospective.py` | `research/fp_agent/validate_config.py` |
| `scripts/reproduce.py` | `research/reproduce.py`, rewritten as a research-stage dispatcher (product stages moved to the `computer-use-agent` CLI; the week2-e2e stage became the `recorder-e2e` stage, `npm --prefix extension run e2e`; the deliverables and release-check stages were dropped) |
| `scripts/doctor.py` | dropped after the import; environment checks are `computer-use-agent doctor`, harness readiness the per-harness `--preflight` commands |
| `scripts/e2e/{chromium_launch_policy,chromium_launch_policy.test}.mjs` | `extension/test/e2e/` |
| `scripts/e2e/recorder_extension_e2e.mjs` | `extension/test/e2e/recorder.e2e.mjs` |
| `scripts/e2e/{package.json,package-lock.json}` | `extension/{package.json,package-lock.json}` |
| `scripts/e2e/scientific_simulator_extension_e2e.mjs` | `research/scientific_simulators/run_extension_e2e.mjs` |
| `scripts/e2e/README.md` | `docs/extension/e2e.md` |
| `scripts/tests/test_study_ledger.py` | `tests/study/test_study_ledger.py` |
| `scripts/tests/test_core_properties.py` | `tests/features/test_properties.py` |
| `scripts/tests/test_ci_supply_chain.py` | `tests/tools/test_ci_supply_chain.py` |
| `scripts/tests/test_entrypoints.py` | `research/tests/test_cli.py` |
| `configs/{formal-study,retrospective-study}.yaml`, `configs/codex-case.json` | `research/configs/{formal-study,retrospective-study,codex-case}.json` (the `.yaml` files already contained JSON) |
| `configs/release-capsule.yaml` | `research/archive/release-capsule-2026-08.json` (frozen, historical, not validated) |
| `configs/README.md` | `research/configs/README.md` |
| `docs/CI_SUPPLY_CHAIN.md` | `docs/ci.md` |
| `docs/RESEARCH_ENGINEERING_BENCHMARK.md` | `docs/research/related-work.md` |
| `literature/{scripts,tests}/`, `literature/README.md` | `research/literature/{scripts,tests}/`, `research/literature/README.md` |
| `literature/` hand-curated inputs (8 files) | `research/literature/sources/` |
| `literature/{fulltext_evidence,citation_retrieval}_manifest.json` | `research/literature/snapshots/` |
| `literature/` generated corpus and citation files (11 files) | `research/literature/generated/` |
| `literature/` superseded Week 1 survey and deep-read files (6 files) | `research/literature/archive/` |
| `results/fp_agent/`, `results/auxiliary_data/` | `research/evidence/fp_agent/`, `research/evidence/auxiliary_data/` |
| `results/week2_recorder/extension_e2e/` | `research/evidence/recorder_e2e_v0.4.0/` |
| `results/week2_recorder/` (demo trace, validation summary, screenshots, README) | `research/evidence/archive/playground_fixture/` (a copy of `agent_demo_trace.json` is also in `extension/test/fixtures/`) |
| `results/{browser_use,skyvern,stagehand}/`, `results/cost_summary.json` | `research/evidence/pilots/` |
| `results/week3_scientific_simulators/` | `research/evidence/scripted_simulator_control_v0.2.1/` |
| `results/week1_browsergym_pre_fix_c56a7b5/` | `research/evidence/week1_miniwob_prefix_failures/` |
| `results/codex_case/` | `research/evidence/codex_case_blocked/` |
| `results/week1_recap/` | `research/evidence/archive/week1_recap/` |
| `results/README.md` | `research/evidence/README.md` |
| Root files `LICENSE`, `NOTICE`, `README.md`, `CHANGELOG.md`, `CITATION.cff`, `CONTRIBUTING.md`, `SECURITY.md`, `THIRD_PARTY_NOTICES.md`, `.env.example`, `.github/workflows/codeql.yml` | kept at the root and rewritten for the new project (`LICENSE` unchanged) |
| `THIRD_PARTY.md` | merged into `THIRD_PARTY_NOTICES.md` and deleted |
| `pyproject.toml`, `.gitattributes`, `.gitignore` | rewritten for the new layout |

## Documents that stay in the source repository

The four PDF deliverables are not part of this repository. They remain available at
the pinned commit: https://github.com/appleweiping/cu-detector-research-artifact/tree/437070f519d4bd40e9e4e46bf19fcca52b4b7557/deliverables

| Document | File at the pinned commit | Bytes |
|---|---|---|
| Technical Report | [Weiping_Yan_CU_Detector_Technical_Report.pdf](https://github.com/appleweiping/cu-detector-research-artifact/blob/437070f519d4bd40e9e4e46bf19fcca52b4b7557/deliverables/Weiping_Yan_CU_Detector_Technical_Report.pdf) | 2,142,560 |
| Final Talk | [Weiping_Yan_CU_Detector_Final_Talk.pdf](https://github.com/appleweiping/cu-detector-research-artifact/blob/437070f519d4bd40e9e4e46bf19fcca52b4b7557/deliverables/Weiping_Yan_CU_Detector_Final_Talk.pdf) | 885,451 |
| Mentor Brief | [Weiping_Yan_CU_Detector_Mentor_Brief.pdf](https://github.com/appleweiping/cu-detector-research-artifact/blob/437070f519d4bd40e9e4e46bf19fcca52b4b7557/deliverables/Weiping_Yan_CU_Detector_Mentor_Brief.pdf) | 456,359 |
| Cumulative Progress Deck | [Weiping_Yan_CU_Detector_Cumulative_Progress_Deck.pdf](https://github.com/appleweiping/cu-detector-research-artifact/blob/437070f519d4bd40e9e4e46bf19fcca52b4b7557/deliverables/Weiping_Yan_CU_Detector_Cumulative_Progress_Deck.pdf) | 1,595,802 |

## Large literature artifacts (not committed)

These two generated files are excluded because of their size. They remain in the
source repository at the pinned commit.
`research/literature/generated/LARGE_ARTIFACTS.json` records them (bytes, sha256, git
blob id and a raw.githubusercontent.com URL pinned to the commit above), and
`python -m research.literature.fetch_large` downloads and verifies them into the
git-ignored `research/literature/generated/large/`.

| Source path | Bytes | SHA-256 |
|---|---|---|
| [`literature/citation_graph.json`](https://github.com/appleweiping/cu-detector-research-artifact/blob/437070f519d4bd40e9e4e46bf19fcca52b4b7557/literature/citation_graph.json) | 72,766,360 | `ed3f7050b257562f8602379fcbbb22d20b29b2e7d4fcf6227291955450754642` |
| [`literature/citation_assertions.csv`](https://github.com/appleweiping/cu-detector-research-artifact/blob/437070f519d4bd40e9e4e46bf19fcca52b4b7557/literature/citation_assertions.csv) | 46,375,030 | `1bb6e05747ef5051810896765b2c1013d70be8a29988afc5bdb039099d0018b2` |

## Every path that was not imported

| Source path | Reason |
|---|---|
| `reports/MENTOR_REPORT_WEEKS1_4.md` | Not committed. Digested, without personal, institutional or scheduling details, into [`docs/research/summary.md`](../docs/research/summary.md), which also lists its corrections against the archived evidence. |
| `reports/WEEK4_REPORT.md` | Not committed. Digested, without personal, institutional or scheduling details, into [`docs/research/summary.md`](../docs/research/summary.md), which also lists its corrections against the archived evidence. |
| `.github/workflows/public-artifact-quality.yml` | ran only the dropped public-mirror check (scripts/check_public_artifact.py) and was gated on the old repository name; CI was rewritten (see docs/ci.md) |
| `.gitleaksignore` | all 4 fingerprints point at commits that are not in the public history |
| `PUBLIC_MIRROR_MANIFEST.json` | private-to-public mirror process; any edit breaks it |
| `PUBLIC_MIRROR_MANIFEST.sha256` | private-to-public mirror process; any edit breaks it |
| `assets/templates/mentor-progress-deviation-log.txt` | provenance for absent private PPTX files; lists slide titles from a private deck |
| `assets/templates/mentor-progress-frame-map.json` | provenance for absent private PPTX files; lists slide titles from a private deck |
| `assets/templates/mentor-progress-template-audit.txt` | provenance for absent private PPTX files; lists slide titles from a private deck |
| `configs/public-mirror-binaries.json` | public-mirror process |
| `deliverables/README.md` | lists 6 absent DOCX/PPTX/ZIP files |
| `deliverables/Weiping_Yan_CU_Detector_Cumulative_Progress_Deck.pdf` | internship PDFs (decision D2); linked below at the pinned commit |
| `deliverables/Weiping_Yan_CU_Detector_Final_Talk.pdf` | internship PDFs (decision D2); linked below at the pinned commit |
| `deliverables/Weiping_Yan_CU_Detector_Mentor_Brief.pdf` | internship PDFs (decision D2); linked below at the pinned commit |
| `deliverables/Weiping_Yan_CU_Detector_Technical_Report.pdf` | internship PDFs (decision D2); linked below at the pinned commit |
| `docs/PUBLIC_MIRROR.md` | public-mirror process |
| `docs/RELEASE_CLAIM_SCAN.md` | claim scanner removed (brief section 5) |
| `experiments/scienceboard/requirements-x11.txt` | moved into [project.optional-dependencies] x11 in pyproject.toml |
| `literature/Literature_Survey_Week1.build.json` | binds an absent .xlsx |
| `literature/Literature_Survey_Week1.upsert.csv` | byte-identical duplicate of Literature_Survey_Week1.csv |
| `literature/citation_assertions.csv` | 46.4 MB; never committed. Recorded in research/literature/generated/LARGE_ARTIFACTS.json and fetched by research.literature.fetch_large |
| `literature/citation_graph.json` | 72.8 MB; never committed. Recorded in research/literature/generated/LARGE_ARTIFACTS.json and fetched by research.literature.fetch_large |
| `ops/sync_manifest.example.json` | institutional Drive sync template |
| `recorder/demo/trace.css` | stylesheet of the dropped trace.html |
| `recorder/demo/trace.html` | broken absolute fetch, innerHTML XSS (trace.js:22-40); superseded by the Python viewer |
| `recorder/demo/trace.js` | broken absolute fetch, innerHTML XSS (trace.js:22-40); superseded by the Python viewer |
| `reports/EVALUATION_AND_TALK_PREP.md` | internship communications (mentor names, Slack channel, Zoom times, broken links); the substance of MENTOR_REPORT_WEEKS1_4.md and WEEK4_REPORT.md is digested in docs/research/summary.md |
| `reports/MENTOR_REQUIREMENTS_BASELINE.md` | internship communications (mentor names, Slack channel, Zoom times, broken links); the substance of MENTOR_REPORT_WEEKS1_4.md and WEEK4_REPORT.md is digested in docs/research/summary.md |
| `reports/MENTOR_REQUIREMENTS_MATRIX.md` | internship communications (mentor names, Slack channel, Zoom times, broken links); the substance of MENTOR_REPORT_WEEKS1_4.md and WEEK4_REPORT.md is digested in docs/research/summary.md |
| `reports/PROJECT_BRIEF.md` | internship communications (mentor names, Slack channel, Zoom times, broken links); the substance of MENTOR_REPORT_WEEKS1_4.md and WEEK4_REPORT.md is digested in docs/research/summary.md |
| `reports/README.md` | internship communications (mentor names, Slack channel, Zoom times, broken links); the substance of MENTOR_REPORT_WEEKS1_4.md and WEEK4_REPORT.md is digested in docs/research/summary.md |
| `reports/REPRODUCED_TASKS_AND_RECORDER_BRIEF.md` | internship communications (mentor names, Slack channel, Zoom times, broken links); the substance of MENTOR_REPORT_WEEKS1_4.md and WEEK4_REPORT.md is digested in docs/research/summary.md |
| `reports/REQUIREMENTS_EVIDENCE_MATRIX.md` | internship communications (mentor names, Slack channel, Zoom times, broken links); the substance of MENTOR_REPORT_WEEKS1_4.md and WEEK4_REPORT.md is digested in docs/research/summary.md |
| `reports/SLACK_UPDATE_DRAFT.md` | internship communications (mentor names, Slack channel, Zoom times, broken links); the substance of MENTOR_REPORT_WEEKS1_4.md and WEEK4_REPORT.md is digested in docs/research/summary.md |
| `reports/SLACK_UPDATE_FINAL_DRAFT.md` | internship communications (mentor names, Slack channel, Zoom times, broken links); the substance of MENTOR_REPORT_WEEKS1_4.md and WEEK4_REPORT.md is digested in docs/research/summary.md |
| `reports/SLACK_UPDATE_WEEK2_DRAFT.md` | internship communications (mentor names, Slack channel, Zoom times, broken links); the substance of MENTOR_REPORT_WEEKS1_4.md and WEEK4_REPORT.md is digested in docs/research/summary.md |
| `reports/SLACK_UPDATE_WEEK3_DRAFT.md` | internship communications (mentor names, Slack channel, Zoom times, broken links); the substance of MENTOR_REPORT_WEEKS1_4.md and WEEK4_REPORT.md is digested in docs/research/summary.md |
| `reports/SLACK_UPDATE_WEEK4_DRAFT.md` | internship communications (mentor names, Slack channel, Zoom times, broken links); the substance of MENTOR_REPORT_WEEKS1_4.md and WEEK4_REPORT.md is digested in docs/research/summary.md |
| `reports/WEEK1_REPORT.md` | internship communications (mentor names, Slack channel, Zoom times, broken links); the substance of MENTOR_REPORT_WEEKS1_4.md and WEEK4_REPORT.md is digested in docs/research/summary.md |
| `reports/WEEK2_REPORT.md` | internship communications (mentor names, Slack channel, Zoom times, broken links); the substance of MENTOR_REPORT_WEEKS1_4.md and WEEK4_REPORT.md is digested in docs/research/summary.md |
| `reports/WEEK3_REPORT.md` | internship communications (mentor names, Slack channel, Zoom times, broken links); the substance of MENTOR_REPORT_WEEKS1_4.md and WEEK4_REPORT.md is digested in docs/research/summary.md |
| `requirements-audit.txt` | an inventory, not installable; the audit job runs uv export + pip-audit |
| `requirements-dev.lock` | replaced by uv.lock |
| `requirements-dev.txt` | replaced by [dependency-groups] in pyproject.toml |
| `sbom/cu-detector.cdx.json` | generated in the release workflow instead; its ROOT_REF names a third repository identity |
| `sbom/python-audit-groups.json` | generated in the release workflow instead; its ROOT_REF names a third repository identity |
| `scripts/__init__.py` | scripts/ is no longer a package |
| `scripts/check_current_claims.py` | claim scanner; replaced by research/evidence/INDEX.json + tools/ci/check_readme.py |
| `scripts/check_public_artifact.py` | public-mirror process (brief section 5) |
| `scripts/export_public_mirror.py` | public-mirror process (brief section 5) |
| `scripts/generate_project_status.py` | status hashing (brief section 5) |
| `scripts/generate_release_capsule.py` | capsule tooling; the capsule is archived as research/archive/release-capsule-2026-08.json |
| `scripts/package_week14.py` | milestone packager |
| `scripts/package_week3.py` | milestone packager |
| `scripts/project_status_lib.py` | status hashing (brief section 5) |
| `scripts/release_checks/__init__.py` | release gates with Office, Slack and hard-coded test-count checks |
| `scripts/release_checks/deliverables_runtime.py` | release gates with Office, Slack and hard-coded test-count checks |
| `scripts/release_checks/foundation.py` | release gates with Office, Slack and hard-coded test-count checks |
| `scripts/release_checks/recorder.py` | release gates with Office, Slack and hard-coded test-count checks |
| `scripts/release_checks/scienceboard_detector.py` | release gates with Office, Slack and hard-coded test-count checks |
| `scripts/release_checks/week1.py` | release gates with Office, Slack and hard-coded test-count checks |
| `scripts/tests/__init__.py` | scripts/tests/ is no longer a package; its surviving tests moved to tests/ and research/tests/ |
| `scripts/tests/test_current_claims.py` | tests a dropped tool |
| `scripts/tests/test_package_boundaries.py` | tests the old recorder ZIP boundary; rewritten as tests/tools/test_package_extension.py |
| `scripts/tests/test_project_status.py` | tests a dropped tool |
| `scripts/tests/test_public_mirror.py` | tests a dropped tool |
| `scripts/tests/test_validate_release_modules.py` | tests a dropped tool |
| `scripts/validate_formal_study.py` | folded into tests/study/test_policy.py (it depended on the dropped project_status_lib) |
| `scripts/validate_literature.py` | needs the excluded literature_survey_qa.json |
| `scripts/validate_project_status.py` | status hashing (brief section 5) |
| `scripts/validate_release.py` | release gates with Office, Slack and hard-coded test-count checks |
| `scripts/validate_release_capsule.py` | capsule tooling; the capsule is archived as research/archive/release-capsule-2026-08.json |
| `scripts/validate_week3.py` | recomputes the 0.4.0 recorder tree against evidence recorded with 0.2.1 |
| `slides/README.md` | stale, and publishes 2 Google Drive file IDs |
| `status/README.md` | replaced by research/evidence/INDEX.json and MANIFEST.sha256; entries point at absent files and a Drive folder URL |
| `status/project_status.json` | replaced by research/evidence/INDEX.json and MANIFEST.sha256; entries point at absent files and a Drive folder URL |
| `status/project_status.schema.json` | replaced by research/evidence/INDEX.json and MANIFEST.sha256; entries point at absent files and a Drive folder URL |
| `tools/deliverables/README.md` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/build.py` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/build_data_figures.py` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/build_literature_workbook.mjs` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/build_mentor_doc.py` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/build_research_documents.py` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/build_research_figures.py` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/build_research_presentations.mjs` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/build_week14_deck.mjs` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/content_contract.json` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/deck_authoring/README.md` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/deck_authoring/build_week14_deck.mjs` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/deck_authoring/entry.mjs` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/export_office_artifact.ps1` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/export_office_artifact.py` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/inputs.json` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/merge_append_only_deck.py` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/project_truth.mjs` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/project_truth.py` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/qa_office_artifacts.py` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/sanitize_docx_style_effects.py` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/source_citations.json` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/tests/__init__.py` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/tests/test_build_safety.py` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/tests/test_docx_style_effects.py` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/tests/test_portability.py` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/tests/test_project_truth_mutations.mjs` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/tests/test_project_truth_mutations.py` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/tests/test_research_document_claims.py` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/tests/test_runtime_split.py` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/tests/test_xlsx_ooxml_postprocess.py` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |
| `tools/deliverables/xlsx_ooxml_postprocess.py` | needs proprietary tooling, Office via win32com and LibreOffice; 11 of 70 tests fail publicly (optional figure-script salvage not done) |

Any of these files can be retrieved from the source repository at the pinned commit:
`https://github.com/appleweiping/cu-detector-research-artifact/blob/437070f519d4bd40e9e4e46bf19fcca52b4b7557/<path>`.
