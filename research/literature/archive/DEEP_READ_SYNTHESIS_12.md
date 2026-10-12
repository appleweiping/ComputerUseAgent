# Twelve-source deep read: what it changes in this project

> **HISTORICAL SNAPSHOT — SUPERSEDED 2026-08-11.** This twelve-source synthesis
> is retained as early design provenance. Current corpus truth is the 68-item
> `literature_truth_68.json` package with the explicit 60/4/4 review boundary.

This is the mentor-facing subset of the 43-row survey. I chose these twelve
sources because they answer three different questions that were getting mixed
together in the earlier slides:

1. **Can interaction traces distinguish actors?** FP-Agent and Known By Their
   Actions provide the closest evidence.
2. **What makes a task result believable?** WebArena, An Illusion of Progress,
   OSWorld 2.0, ScienceBoard, and LabOSBench explain how to reset tasks and
   validate state independently of an agent's claim.
3. **Why can two agents leave different traces even with the same model?**
   UI-TARS, Agent S2, Browser-Use's benchmark, Skyvern, and Stagehand expose
   observation, action, and executor differences that can otherwise become
   shortcuts for a detector.

The original detailed 12-row matrix is the subject of this synthesis. The
canonical `Deep Read` worksheet in `Literature_Survey_Week1.xlsx` and the
legacy-compatible `Literature_Deep_Read_12.csv` path now also include the five
addendum rows, for 17 rows total.

Five other `Taxonomy Confidence = High` records have since received the same
primary-source treatment: Cognitive Trap Framework, WebCloak, KILLBENCH,
Mind2Web 2, and BrowseComp. Their synthesis and exact write payload are in
`HIGH_PRIORITY_DEEP_READ_ADDENDUM_5.md` and
`literature_high_priority_deep_read_addendum.json`. They were integrated into
the canonical local workbook/CSV/upsert artifacts on 2026-08-10 after
closed-schema, deduplication, round-trip, formula, and all-sheet visual QA.
This local integration does not claim a newer Google Sheet revision.

## The eight axes I now use

| Axis | Question asked for every source | Why it matters here |
|---|---|---|
| 1. Research role | Detection, attribution, environment, evaluator, or framework? | A capability benchmark is not evidence that a detector works. |
| 2. Interface/environment | Browser, native desktop, VM, or scientific simulator? | The Chrome extension cannot observe native VM applications. |
| 3. Observer position | Page, browser, harness, guest OS, or environment state? | It determines which events can exist and what privacy boundary applies. |
| 4. Observable signal | Browser metadata, key/mouse/scroll events, screenshots, app state, or action logs? | Supported signals and actually observed signals must be reported separately. |
| 5. Action/architecture | DOM, coordinates, Playwright/CDP, OS input, or mixed programmatic tools? | The executor may be easier to identify than the agent or model. |
| 6. Unit/horizon | Event, burst, session, subtask, or long episode? | Feature windows and timeout rules must match the task horizon. |
| 7. Evaluation protocol | Random split, held-out task/site/agent, or state validator? | Random within-corpus accuracy is not deployment generalization. |
| 8. Failure/claim boundary | What does the source not establish? | It prevents a smoke test or scripted control from being presented as agent evidence. |

These axes lead to a concrete study design: collect low-level behavior and
task-state evidence separately, preserve executor metadata for audit, exclude
that metadata from detector features, and report held-out-task and held-out-
agent-family results instead of relying only on a random split.

## Source-by-source findings

### 1. FP-Agent

- **What the paper actually measures.** An instrumented honey website records
  418 browser-fingerprint and 50 behavioral features while seven agents and 56
  humans perform flight, shopping, and forum tasks. The classifier is XGBoost
  with an 80/20 split. See Sections 4.2-4.7 and Appendix C / Table 7.
- **Result that matters.** With agents and humans together, macro-F1 is 0.8223
  for browser-only features, 0.9994 for behavioral features, and 1.0000 when
  combined (Table 2). Section 6.1 and Table 4 are equally important: on held-out
  tasks, behavior-only F1 is 0.6303-0.7997 and combined F1 is 0.9316-0.9708.
- **My interpretation.** The paper supports collecting typing, pointer, scroll,
  paste/change behavior and explicit missing-modality indicators. It does not
  justify a deployment claim on unseen agents. Our released-model run therefore
  remains a released-split reproduction, and the next test must hold out agent
  families and tasks.
- Primary source: [FP-Agent](https://arxiv.org/html/2605.01247)

### 2. Known By Their Actions

- **What the paper actually measures.** The observer is a passive, co-located
  website operator. The threat model already assumes that a session is
  automated, so this is mainly agent/model attribution rather than the first
  human-versus-agent decision (Sections 3.1-3.2).
- **Result that matters.** The live-task corpus contains 1,050 sessions over
  2Wiki, FRAMES, WebShop, and DeepShop (Table 1). Closed-set per-agent F1 is
  mostly above 0.70 and reaches 0.96 (Figure 2), but cross-site macro-F1 falls
  to 29.70/25.98 (Appendix Table 14). Sections 6.1 and Figures 4-5 show that
  timing dominates, although action features retain signal after delay changes.
- **My interpretation.** We need separate detector and attribution questions,
  timing-perturbation tests, and site/task holdouts. The 41-feature list in
  Appendix Table 8 also supports collecting click, keydown, scroll, navigation,
  unload, and focus timing without collecting typed content.
- Primary source: [Known By Their Actions](https://arxiv.org/html/2605.14786)

### 3. ScienceBoard

- **What the benchmark provides.** ScienceBoard places professional scientific
  software in an Ubuntu VM and exposes application state through a lightweight
  HTTP server (Section 3.2, Figure 2; Appendix B.1). Its 169 tasks include 38
  GUI, 33 CLI, and 98 mixed tasks, averaging 9 steps and 124 seconds
  (Section 4.3, Table 2, Figure 4).
- **Result that matters.** Exact, range, and tolerance evaluators check task
  state (Table 1). Screenshot+accessibility GPT-5 reaches 24.20% overall,
  compared with 60.27% for humans (Section 5.2, Table 3).
- **My interpretation.** This is the right primary route for Weeks 3-4 because
  it gives us native applications and independent validators. It also proves a
  boundary: a Chrome content script cannot record VM-native applications. A
  guest-side X11 recorder must produce the same session envelope, and partial
  process evidence should be kept because binary success is incomplete.
- Primary sources: [paper](https://arxiv.org/html/2505.19897),
  [official repository](https://github.com/OS-Copilot/ScienceBoard)

### 4. OSWorld 2.0

- **What the benchmark changes.** The 108 tasks span 31 sites/applications,
  average 27.25 functional checkpoints, and often exceed 250 agent steps; the
  median human time is about 1.6 hours (Sections 2.1-2.2, Figures 3-4).
- **Result that matters.** At a 500-step budget the best reported binary success
  is 20.6% (Table 3). Section 4.4 and Tables 19-20 show premature stopping,
  final-state mistakes, and direct code/API/file strategies that differ from a
  human GUI path.
- **My interpretation.** OSWorld 2.0 is a strong evaluator-design reference but
  too long for the first paired collection. It reinforces checkpoint logging,
  explicit termination/recovery features, and executor auditing.
- Primary source: [OSWorld 2.0](https://arxiv.org/html/2606.29537)

### 5. WebArena

- **What the benchmark provides.** A deterministic, self-hosted web environment
  with screenshot, DOM, and accessibility observations and keyboard/mouse-like
  actions (Sections 2.1-2.4, Figures 1 and 3-5). The suite has 812 tasks.
- **Result that matters.** Table 1 and Section 3 define exact, must-include,
  fuzzy-answer, and programmatic state checks. The best GPT-4 agent reaches
  14.41% end-to-end success, versus 78.24% for humans (Section 5).
- **My interpretation.** The important lesson is not the score. It is that the
  evaluator checks task state and permits more than one valid action path. Our
  recorder must therefore be paired with a validator that is independent of
  the framework's final message.
- Primary source: [WebArena](https://arxiv.org/html/2307.13854)

### 6. An Illusion of Progress?

- **What the study checks.** WebJudge evaluates screenshots and actions rather
  than requiring the agent's final response (Section 3, Table 1). Six agents
  are also reviewed with human-annotated live-site trajectories
  (Sections 4.1-4.3).
- **Result that matters.** Human-evaluated success is 30.0% for Browser Use and
  61.3% for Operator (Table 2). Table 3 shows that final-response hallucination
  can inflate evaluation, while a final screenshot can omit important earlier
  actions. Performance also declines with task difficulty (Figure 4).
- **My interpretation.** A provider status of `completed` is not a validator.
  Keep the action path and key screenshots, then verify the versioned task
  state separately. This is exactly what the Skyvern pilot failure exposed.
- Primary source: [An Illusion of Progress?](https://arxiv.org/html/2504.01382)

### 7. UI-TARS

- **What the system does.** UI-TARS is an end-to-end screenshot-to-thought-to-
  action model with a unified coordinate action space (Sections 4.1 and 4.3;
  Figure 4; Table 1).
- **Result that matters.** It reports 24.6% on screenshot-only OSWorld at 50
  steps, averaged over three runs, compared with 22.0% for Claude (Section 5.4,
  Table 9). The paper also discusses the gap between offline grounding and
  online execution.
- **My interpretation.** Coordinate agents may never trigger the DOM path that
  a web-only recorder expects. OS-level capture is required for native tasks,
  and the action backend should be stored only as audit metadata.
- Primary source: [UI-TARS](https://arxiv.org/html/2501.12326)

### 8. Agent S2

- **What the system does.** Agent S2 composes a Manager, Worker, and a mixture
  of visual, textual, and structural grounding experts, with proactive
  replanning (Section 3, Figures 2-3).
- **Result that matters.** It reports 27.0% at 15 steps and 34.5% at 50 steps on
  screenshot-only OSWorld (Section 4.1, Table 1). The ablations are in Figures
  5-6.
- **My interpretation.** Its structural expert can make programmatic edits, such
  as changing spreadsheet cells. That is a concrete example of why framework
  and executor can dominate the observable trace. We should compare matched
  visible action channels before interpreting actor differences.
- Primary source: [Agent S2](https://arxiv.org/html/2504.00906)

### 9. LabOSBench

- **What the benchmark provides.** Ninety-six subtasks across eight browser-
  based scientific-instrument simulators (Section 3, Figures 1-3). Playwright
  executes actions while in-page hooks export state, subtask completion, steps,
  and PSNR where appropriate (Sections 3.3-3.5).
- **Result that matters.** GTA1 with GPT-5.5 has the best non-human average at
  0.814, versus 0.924 for humans (Table 1). Table 2 and Figure 6 show persistent
  difficulty with feedback-driven scientific state and long workflows.
- **My interpretation.** This is a useful lower-cost browser-science route and a
  good source for partial checkpoints. It does not answer whether our recorder
  works in native scientific software, so ScienceBoard remains the primary
  route.
- Primary source: [LabOSBench](https://arxiv.org/html/2606.16802)

### 10. browser-use benchmark

- **What the repository defines.** BU Bench V1 has 100 tasks: 20 each from its
  custom set, WebBench, Mind2Web2, GAIA, and BrowseComp. Re-verification uses
  task ground truth, outputs, and screenshots and requires a Google judge key.
- **Claim boundary.** The repository warns against publishing decrypted tasks
  and ground truth from `run_data`. Our three public visible-link microtasks are
  not BU Bench and must not be presented as an official Browser-Use score.
- **Project decision.** For an open matched framework comparison, use public,
  versioned BrowserGym/MiniWoB tasks with independent task outcomes.
- Primary sources: [browser-use benchmark](https://github.com/browser-use/benchmark),
  [BrowserGym](https://github.com/ServiceNow/BrowserGym)

### 11. Skyvern

- **What the repository defines.** Skyvern combines Playwright with visual
  element mapping and exposes `act`, `extract`, `validate`, and workflow
  interfaces. See the README sections “How it works,” “Run Locally (UI +
  Server),” and “AI-Powered Page Commands.”
- **What our reproduction taught me.** The first self-hosted request reached the
  wrong compatible endpoint and returned 401. Skyvern 1.0 then clicked but
  declared completion before destination evidence existed. After correcting
  the base configuration, switching to Skyvern 2.0, and strengthening the
  independent validator, the narrow pilot completed 9/9. These are integration
  results, not a Skyvern capability benchmark.
- Evidence: `results/skyvern/aggregate.json` and
  `results/skyvern/diagnostics.json`.
- Primary source: [Skyvern](https://github.com/Skyvern-AI/skyvern)

### 12. Stagehand

- **What the repository defines.** Stagehand deliberately mixes ordinary code
  and natural-language browser operations through `act`, `agent.execute`, and
  `extract`, with caching/self-healing and CDP access. See “What is Stagehand?”,
  “Why Stagehand?”, and the getting-started example.
- **Claim boundary.** Our 3/3 result is one pass over three narrow tasks. It
  verifies that a third framework can traverse the integration path; it is not
  a repeated benchmark and is not directly comparable with the Browser-Use and
  Skyvern 3 x 3 pilots.
- Evidence: `results/stagehand/aggregate.json`.
- Primary source: [Stagehand](https://github.com/browserbase/stagehand)

## Design decisions carried forward

1. Store a common, privacy-reduced session envelope across browser and X11
   adapters, but keep raw platform events adapter-specific.
2. Report **supported**, **naturally observed**, and **synthetically exercised**
   event types separately.
3. Pair every trace with a versioned task and independent state validator.
4. Preserve failed, interrupted, and timed-out runs; do not substitute a script
   for a failed reasoning agent.
5. Keep framework, executor, task, URL, version, and validator outcome for audit,
   but exclude them from detector features.
6. Evaluate within-task differences first, then leave one task out and leave one
   agent family out. A random split alone is insufficient.
7. Treat any ScienceBoard classifier as a single-participant pipeline probe
   until a broader, approved human study exists.

## Open source gaps

- I did not confirm an official code repository for **Known By Their Actions**.
- I did not confirm an official code repository for **LabOSBench**.
- Some rows in the earlier 43-source expansion lack an exact cited-versus-
  citing provenance trail. Those rows remain flagged; I did not infer the
  missing relationship.
