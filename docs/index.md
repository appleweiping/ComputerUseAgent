# Documentation

A map of every document in this repository. Start with the
[README](../README.md) ([简体中文](../README.zh-CN.md)) for installation and the
quick start.

## Overview

| Document | What it covers |
|---|---|
| [Architecture](architecture.md) | Components, data flow, wire formats and the historical `cui-` namespace, package layout, import rules |
| [Research summary](research/summary.md) | What the research phase did and found, its method and limitations, and corrections to the original reports |
| [Related work](research/related-work.md) | Practices reused from related projects, the evidence hierarchy and the stop conditions for formal runs |

## Recorder extension

| Document | What it covers |
|---|---|
| [Extension overview](extension/index.md) | Capture, limits, permissions, message authority, Chrome version, install, recording, playground, development |
| [Privacy and consent](extension/privacy.md) | What is and is not recorded, private fields and the 0.4.0 leak, raw-key mode, the disclosure template, data handling |
| [Trace schema](extension/data-schema.md) | The `cui-trace-v2` envelope and event fields, and the legacy `cui-trace-v1` projection |
| [Local data store](extension/data-store.md) | `chrome.storage.local` layout, chunks, limits and deletion |
| [Event coverage](extension/event-coverage.md) | Supported versus observed event types and the retained 0.4.0 run |
| [End-to-end tests](extension/e2e.md) | The Puppeteer E2E harness, its cases, environment variables and result bundle |

## Python package and CLI

| Document | What it covers |
|---|---|
| [Detector pipeline](detector.md) | Feature contract and leakage boundary, `extract`, `analyze` profiles, the result bundle, provenance validators and human-data policy |
| [Viewer](viewer.md) | The loopback-only, privacy-projected trace and result viewer |
| [Examples](../examples/README.md) | The synthetic example traces and detector dataset |

## Development, CI and releases

| Document | What it covers |
|---|---|
| [Contributing](../CONTRIBUTING.md) | Setup, checks, data rules and pull requests |
| [Continuous integration and supply chain](ci.md) | Every workflow and job, pinned Actions, locks, SBOM, coverage gates |
| [Releasing](releasing.md) | Versioning, the release checklist, PyPI (disabled by default) |
| [Security policy](../SECURITY.md) | Supported versions, private vulnerability reporting, scope |
| [Code of Conduct](../CODE_OF_CONDUCT.md) | Community standards |
| [Changelog](../CHANGELOG.md) | Release history, including the 0.5.0 security fixes |

## Research archive

| Document | What it covers |
|---|---|
| [research/README.md](../research/README.md) | Layout of the archive, running the research tests, reproducing stages |
| [Provenance](../research/PROVENANCE.md) | The source snapshot, the old-to-new path map and every path that was not imported |
| [Evidence](../research/evidence/README.md) | The archived results, `MANIFEST.sha256`, `INDEX.json` and the restructure's redactions |
| [Study configurations](../research/configs/README.md) | The formal-study, retrospective and Codex case configurations |
| [Archive](../research/archive/README.md) | The frozen, unsealable 2026-08 release capsule |
| [Literature](../research/literature/README.md) | The literature corpus, the citation graph and the large-file download |
| [FP-Agent analyses](../research/fp_agent/README.md) | The released-model reproduction, leave-one-family-out probe and 23-feature retrospective code |
| [Auxiliary data](../research/auxiliary_data/README.md) | MolmoWeb, CaptchaSolve30k and Aalto sampling-fidelity and leakage checks |
| [ScienceBoard harness](../research/scienceboard/README.md) | The VM harness and X11 guest deployment (never run on a VM) |
| [MiniWoB harness](../research/week1_miniwob/README.md) | The frozen formal MiniWoB harness (pinned to recorder 0.4.0) |
| [Pilots](../research/pilots/browser_use/README.md) | Browser-Use, [Skyvern](../research/pilots/skyvern/README.md) and [Stagehand](../research/pilots/stagehand/README.md) pilots |
| [Codex case](../research/codex_case/README.md) | The blocked Codex Desktop case study |
| [Scientific simulators](../research/scientific_simulators/README.md) | The scripted NetLogo/PhET control harness |

## Legal and citation

[LICENSE](../LICENSE) (MIT), [NOTICE](../NOTICE),
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) and
[CITATION.cff](../CITATION.cff).
