# Third-party notices

Original first-party code and text are licensed under the MIT License in
[`LICENSE`](LICENSE). The licenses below apply only to their respective upstream
projects and dependencies; the first-party MIT License does not relicense those
materials. Package metadata is evidence about an upstream declaration, not
independent legal advice.

## What this repository does not redistribute

The MIT License covers only first-party code and text. This repository does
not contain, and contributions must not add:

- FP-Agent upstream source, released data or model weights (the pinned research
  checkout did not expose a redistribution license);
- ScienceBoard VM images, snapshots, guest filesystems or source checkouts;
- MiniWoB++ or other benchmark assets copied from a local checkout;
- downloaded article or supplementary-material PDFs;
- raw human traces, typed text, credentials, browser profiles, private logs or
  machine-specific absolute paths;
- upstream model weights, unless an exact-file review records explicit
  redistribution authority.

The research reports and talk PDFs of the predecessor project are not part of
this repository; `research/PROVENANCE.md` links to them in the archived source
repository. If a file's redistribution status is unclear, it is left out: a
citation is never treated as a substitute for permission.

## Components

| Component | How it is used | Upstream | License (as declared upstream) |
|---|---|---|---|
| jsonschema | runtime dependency of the Python package | <https://github.com/python-jsonschema/jsonschema> | MIT |
| python-xlib, PyAutoGUI | optional `x11` extra for guest-side capture | <https://github.com/python-xlib/python-xlib>, <https://github.com/asweigart/pyautogui> | LGPL-2.1-or-later, BSD-3-Clause |
| NumPy, scikit-learn, XGBoost, ijson, PyArrow, Requests | optional `research` extra for the analyses under `research/` | upstream project pages on PyPI | BSD-3-Clause, BSD-3-Clause, Apache-2.0, BSD-3-Clause, Apache-2.0, Apache-2.0 |
| Puppeteer / Puppeteer Core | 25.4.0, extension E2E harness (development only); 25.8.0 in the archived Week 1 browser host | <https://github.com/puppeteer/puppeteer> | Apache-2.0 |
| Chrome for Testing | downloaded by Puppeteer at test time; never redistributed | <https://github.com/GoogleChromeLabs/chrome-for-testing> | Chromium licenses |
| ESLint, @eslint/js, globals | extension linting (development only) | <https://github.com/eslint/eslint>, <https://github.com/sindresorhus/globals> | MIT |
| Browser-Use | 0.11.5, archived research adapter/pilot | <https://github.com/browser-use/browser-use> | MIT |
| BrowserGym / browsergym-miniwob | 0.14.1, archived benchmark adapter | <https://github.com/ServiceNow/BrowserGym> | Apache-2.0 |
| Skyvern | 1.0.47, archived self-hosted adapter/pilot; source not vendored | <https://github.com/Skyvern-AI/skyvern> | AGPL-3.0 |
| Stagehand | 3.7.1, historical only; not installed or executed by any current entry point | <https://github.com/browserbase/stagehand> | MIT |
| MiniWoB++ | commit `7fd85d71…`, task source referenced by the archived harness | <https://github.com/Farama-Foundation/miniwob-plusplus> | MIT |
| ScienceBoard | commit `c8d5010b…`, VM/source protocol referenced by the archived harness | <https://github.com/OS-Copilot/ScienceBoard> | MIT |
| desktop-env | 0.1.22, interface reference for the narrow compatibility layer in `research/scienceboard/runtime_compat/` (see its `NOTICE.md`); upstream package code is not bundled | <https://github.com/xlang-ai/OSWorld> | Apache-2.0 |
| FP-Agent | commit `e614e2f0…`, released-model and data reproduction | <https://github.com/ethanbwang/fp-agent> | no license file in the pinned checkout; do not redistribute upstream code, data or weights without permission |

## Version inventories

The lockfiles are the authoritative version inventories:

- `uv.lock`: the Python package, its extras and the test, lint and dev tools;
- `extension/package-lock.json`: the extension's development graph;
- `research/**/requirements*.lock`, `research/**/requirements*.txt` and
  `research/**/package-lock.json` (the Week 1 Node adapter and the scientific
  simulator harness, which pins Puppeteer 25.4.0): frozen research
  environments, kept for provenance. Every npm lock entry must resolve from
  `https://registry.npmjs.org/` (`tools/ci/supply_chain.py` enforces it).

Every GitHub Release attaches a CycloneDX SBOM generated from these files by
`tools/ci/supply_chain.py`, with each component's source manifests recorded.
