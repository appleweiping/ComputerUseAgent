# High-confidence literature addendum: five remaining deep reads

> **HISTORICAL SNAPSHOT — SUPERSEDED 2026-08-11.** This five-item addendum is
> retained as provenance for the earlier 43/17/12 integration checkpoint. It is
> not the current corpus or completeness claim; use `literature_truth_68.json`.

## Why this addendum exists

The canonical survey has no `Priority` column. The reproducible selection rule
for this pass is therefore: `Taxonomy Confidence = High` and `Persistent ID`
absent from the existing 12-row `Deep Read` worksheet. That query returns
exactly five records:

1. Cognitive Trap Framework (`DOI:10.1093/jcr/ucag006`)
2. WebCloak (`IEEE-S&P-2026:WebCloak`)
3. KILLBENCH (`arXiv:2511.13725`)
4. Mind2Web 2 (`arXiv:2506.21506`)
5. BrowseComp (`arXiv:2504.12516`)

The exact keyed updates and five 14-field Deep Read rows in
`literature_high_priority_deep_read_addendum.json` were integrated into the
canonical local artifacts on 2026-08-10 with the loader-verified bundled
`@oai/artifact-tool` runtime. The 43 x 41 survey shape was preserved, the five
rows were updated by `Persistent ID`, and the Deep Read matrix grew from 12 to
17 unique rows. XLSX/CSV/upsert round trips, formula preservation, the formula-
error scan, closed-schema checks, and visual inspection of all five sheets
passed. The UMN Google Sheet baseline still contains 12 Deep Read rows; no
cloud write/readback was performed for this local revision.

## What the five sources add to the project

The earlier 12-source synthesis centered on passive actor traces, independent
task validation, and executor confounding. These five sources close a different
gap: they show how active challenges, adversarial pages, termination signals,
long-horizon partial credit, and outcome-only browsing benchmarks should—and
should not—be connected to the detector.

| Source | What it actually evaluates | Direct boundary for this project |
|---|---|---|
| Cognitive Trap Framework | An active visual challenge embedded in a survey | Complementary detector channel, not evidence that passive traces generalize |
| WebCloak | Scraper capability and an active in-page defense | DOM/input mode is an executor confound; disruption is not actor identification |
| KILLBENCH | Whether an environmental signal halts an in-progress harmful agent | Stop, refusal, timeout, and task success must be separate states |
| Mind2Web 2 | Long-horizon answer quality, attribution, and partial completion | Final-answer evaluation needs a separate low-level recorder and pinned web evidence |
| BrowseComp | Hard-to-find short-answer browsing capability | Outcome-only accuracy cannot validate a recorder or identify the actor |

## 1. Cognitive Trap Framework

### What was studied

Affonso proposes a four-step method for converting documented
vision-language-model constraints into visual survey questions: identify a
constraint, implement a survey-compatible stimulus, validate it against humans
and models, and deploy it with a classification rule. Five constraints produced
seven candidate traps; six survived validation. The retained battery includes
modified visual illusions, a motion-extrapolation task, spatial simulation, and
cross-object binding/counting tasks.

The evidence is layered rather than a single train/test split:

- Initial retention used 202 Prolific participants and six frontier models,
  with ten independent model trials per trap. The seventh candidate was removed
  because human accuracy and the human-model gap were too low.
- Expanded validation tested the six retained traps on 34 models across 2,040
  trials. Figure 2 reports heterogeneous, non-monotonic changes over model
  generations rather than uniform improvement.
- The main deployment included 1,007 presumed-human Prolific participants and
  526 author-deployed browser agents across Google Project Mariner, Perplexity
  Comet, ChatGPT Agent, and Claude via Cursor Agent.
- The main study was preregistered, but the three-failure classification
  threshold was selected during method validation. That threshold was then
  preregistered for the MTurk and CloudResearch Connect replications.

The relevant observation surface is the survey itself: trap answers, per-item
pass/fail, choice-page decision time, attention checks, and reCAPTCHA. It is not
a raw keyboard or pointer trace. Agents reached the author's own Qualtrics
survey, captured screenshots, answered, and submitted; the paper explicitly
states that they were not deployed on live recruitment platforms.

### Result and failure boundary

Failing at least three of six traps detected 511 of 526 deployed agents (97.1%)
while flagging 41 of 1,007 presumed humans (4.1%). Traditional attention checks
detected 2.3% of agents. Tables 3 and 4 show why the battery matters: individual
trap failure rates vary substantially across platforms and models. A two-failure
threshold improves sensitivity to 99.4% but raises the presumed-human false
positive rate to 13.3%; a four-failure threshold lowers false positives to 1.9%
but drops sensitivity to 89.2%.

The result does not establish a universal passive detector. The paper lists
uncertain human ground truth, visual-accessibility differences, platform
heterogeneity, model drift, and absence of a direct behavioral-biometric
comparison as limitations. Vision impairment was associated with higher trap
failure, and no one trap worked uniformly across agent implementations.

### Decision for this project

Active cognitive traps can become a separate, optional evidence channel after
population-specific validation and an appropriate human-research determination.
They should not be converted into ordinary trace features or used to claim that
our passive detector is validated. The more immediate contribution is the
paper's response-time finding: identical mean agent decision time across easy
and conflict conditions, versus longer human time under conflict, motivates
task-difficulty-conditioned timing analysis rather than a single global speed
threshold.

Primary sources:

- Publisher corrected proof: https://academic.oup.com/jcr/advance-article/doi/10.1093/jcr/ucag006/8540432
- Author-linked data, code, deployment scripts, and preregistration: https://osf.io/f2jhx/
- Public trap repository: https://FelipeMAffonso.github.io/cognitive-trap-repository

Exact locators: Cognitive Trap Framework steps 1-4; Human Validation; Initial
and Expanded Model Validation; Figure 2; Prolific Method and Results; Tables
2-4; Practical Recommendations; Limitations.

## 2. WebCloak

### What was studied

WebCloak studies an agent-as-attacker threat: LLM-assisted systems extracting
visual assets from websites. LLMCrawlBench contains 237 reproducible offline
page snapshots, 10,895 manually annotated image URLs, and 50 sites across five
categories. The measurement study covers 32 variants in three operational
families:

- LLM-to-Script (L2S), where an LLM generates scraping code;
- LLM-native crawlers (LNC), which simplify and interpret page structure; and
- LLM-based web agents (LWA), including Browser-Use and visual agents.

The paper's important mechanism is “parse then interpret.” Raw webpages are too
large and noisy for direct model consumption, so frameworks first reduce HTML,
DOM, accessibility, or rendered content. WebCloak attacks that dependency with
dynamic structural obfuscation and an optimized semantic labyrinth while
restoring the human-visible page in JavaScript.

The observer is a website owner or offline benchmark harness that knows which
assets should be extracted. Signals are asset recall/precision, execution time,
token and resource overhead, and protected/unprotected condition—not
keystrokes or pointer motion. Evaluation is benchmark-wide rather than a
detector train/test split, with additional adaptive-attacker, browser/OS,
multimodal-asset, overhead, visual-fidelity, and N=35 user-study checks.

### Result and failure boundary

On the full LLMCrawlBench characterization, Browser-Use obtains 60.4% recall
and 88.8% precision, Crawl4AI obtains 71.0% recall and 81.7% precision, and the
Gemini L2S route obtains 65.0% recall and 78.5% precision (Table 4). The mismatch
between frameworks is itself useful: UI-only agents often became lost after a
few steps, while systems with structural access were much more effective. The
full defense reduces average recall across the evaluated leading agents from
88.7% to 0%, and the adaptive tests in Table 10 remain near zero.

This is an active defense result, not a detector. The benchmark is primarily
image-centric; text and audio are tested on a smaller supplement. Client-side
restoration requires JavaScript, the expensive semantic component takes roughly
three minutes per page to generate, and the threat model focuses on scalable
novice-to-intermediate attackers rather than a bespoke expert willing to
reverse-engineer each session.

### Decision for this project

WebCloak is strong evidence that executor and page representation can dominate
observable behavior. A DOM-aware framework, a screenshot-only agent, and a
script generator can solve the same nominal task through different channels.
We therefore retain input mode, capture adapter, and page condition as audit
provenance but exclude them from the detector feature allowlist. A future
robustness test should verify event capture across DOM mutation and visually
equivalent page transformations; missing DOM events alone must never be read as
proof of an agent.

Primary sources:

- Official project-linked paper: https://letterligo.netlify.app/paper/SP26_WebAgent.pdf
- Official code: https://github.com/LetterLiGO/Agent-WebCloak
- Official artifact record: https://zenodo.org/records/17251212

Exact locators: Sections 2.1-2.3, 3-5, 7-8; Figures 1-2 and 8; Tables 2-4,
6-7, and 10-15.

## 3. KILLBENCH

### What was studied

KILLBENCH asks whether a signal embedded in an agent's external environment can
halt harmful behavior already in progress. It extends WebArena/SafeArena tasks
using BrowserGym and AgentLab infrastructure. The benchmark contains 550
bypass-expanded prompts across eight scenarios and seven sites. It crosses four
agent configurations—text, adversarial system prompt, multimodal, and
uncensored—with four external defenses and several model backbones.

The evaluator scores the entire trajectory. Immediate intrinsic refusal (ISR),
immediate kill-switch stop (KSR), later post-signal stop (PSR), and run-through
(ASR) are mutually exclusive. `KSRcond = KSR / (1 - ISR)` removes trials where
the external signal never had an opportunity to act. The signal is inserted on
the second page, which makes time of encounter and all post-encounter actions
methodologically important.

### Result and failure boundary

AUTOGUARD has the highest average conditional stop rate at 43.5%, narrowly
ahead of handcrafted indirect prompt injection at 41.9%; warning text averages
20.7% and the gradient-optimized suffix 2.8%. The headline hides large
configuration variation: AUTOGUARD exceeds 88% on aligned GPT-5.2 text and
multimodal settings, while adversarial system prompts and the uncensored
configuration remain much harder. Figures 2-5 and Tables 6-8 expose variation
by defense, configuration, backbone, scenario, and attack pattern.

The authors deliberately do not score whether real-world harm actually
succeeds. A payload might perturb planning without causing a genuine stop, and
the paper says that resolving this would require paired counterfactuals or an
instrumental-variable design. It also flags one unrecoverable data inconsistency
in a Table 7 cell. Those boundaries prevent interpreting ASR as a direct harm
metric.

### Decision for this project

We should never collapse `refused`, `stopped`, `timed_out`, `crashed`, and
`validator_success` into one boolean. Trace v2's terminal event and independent
task result should remain separate, with a post-stop negative control confirming
that no further low-level events arrive. The benchmark-only chain-of-thought and
harmful prompt content must not enter the feature table, and this project should
not reproduce the harmful scenarios. KILLBENCH is a design reference for state
semantics, not a candidate data-collection route.

Primary source:

- arXiv v4 paper: https://arxiv.org/pdf/2511.13725

Exact locators: Sections 4.1-4.3, 5.1-5.3, 6-7, and Limitations; Figures
1-5; Tables 1-4 and 6-8; Appendix A.7-A.10.

## 4. Mind2Web 2

### What was studied

Mind2Web 2 evaluates long-horizon agentic search rather than low-level browser
control. Its 130 tasks span six domains and 24 subdomains, and each task has a
tree-structured evaluation rubric. The average rubric contains 50 nodes and the
largest contains 603. In the paper's split, 10 tasks form the public development
set and 120 form the test set. The official repository later released all
evaluation scripts on October 23, 2025, which makes evaluator version a required
piece of provenance.

The judge consumes an agent's final Markdown answer, cited URLs, and cached
webpage evidence. It reports Partial Completion, full Success Rate, Pass@3,
time, answer length, and attribution errors. The official workflow explicitly
pre-caches cited pages; its documentation also warns that CAPTCHA, anti-bot, and
login walls can prevent automatic caching. These are evaluator artifacts, not
keyboard or pointer traces.

The experiment runs each system three times per task. Humans are evaluated on a
random 30-task subset, with three completions per task. On that subset, an
average human task takes 18 minutes and touches eight websites and 110 pages;
maxima reach 44 minutes, 31 sites, and 375 pages.

### Result and failure boundary

OpenAI Deep Research reaches 0.54 Partial Completion, 0.28 full Success Rate,
and 0.40 Pass@3. Humans reach 0.79, 0.54, and 0.83 on Subset-30. The gap between
partial and full success is a more useful lesson than the leaderboard: systems
often find part of an answer but omit required facts, violate a criterion, or
provide missing/invalid citations. Figure 5 separates information-not-found,
partial-missing, criterion, attribution, synthesis, and retrieval failures.

This is an answer-and-attribution benchmark. It does not standardize executor
or expose low-level traces, most strong systems in the study are closed, and
live webpages create temporal drift. A high score therefore cannot demonstrate
that our recorder captured the agent, and two systems with similar answers may
have entirely different action paths.

### Decision for this project

If we later use long-horizon search tasks, each run needs a pinned task version,
cached or hashed evidence, an independent event trace, and partial-progress
state. Task holdout must respect the 10/120 split and evaluator version. The
duration and page count make Mind2Web 2 inappropriate for the first paired
human/agent collection, but its graded rubric is a strong model for preserving
partial evidence instead of reporting only success/failure.

Primary sources:

- Paper: https://arxiv.org/pdf/2506.21506
- Official repository: https://github.com/OSU-NLP-Group/Mind2Web-2
- Official dataset/evaluator release: https://huggingface.co/datasets/osunlp/Mind2Web-2

Exact locators: Sections 3.2-3.5 and 4.1-4.4; Tables 2-3; Figures 3-5;
Appendix F.1; repository sections Submission Guideline, Precache Webpage, and
Run Evaluation Locally Yourself.

## 5. BrowseComp

### What was studied

BrowseComp contains 1,266 difficult questions whose answers require persistent,
creative web search but remain short enough to grade. Human trainers start from
a known fact and invert it into a hard-to-find question, then check that existing
models and simple search strategies do not readily solve it. The official
reference implementation downloads an encrypted test-set CSV, decrypts each
question and answer for evaluation, optionally samples a deterministic subset
with seed 0, and asks a model grader whether the predicted exact answer is
semantically equivalent to the reference.

The observer sees the question, response, exact answer, confidence, and grader
decision. It does not receive a standardized browser trajectory, action space,
or executor. The paper also analyzes 64 samples per question for compute scaling
and task pass-rate distributions.

### Result and failure boundary

The original Deep Research system achieves 51.5% accuracy; GPT-4o with browsing
achieves 1.9%, while non-browsing o1 reaches 9.9%, showing that browsing access
alone does not isolate the capability being measured. In the human verification
campaign, trainers solved 367 of 1,255 attempted questions (29.2%); their answer
matched the original reference on 317 of those 367 (86.4%). Deep Research's
reported calibration error is 91%, so correctness and expressed confidence are
not interchangeable.

BrowseComp intentionally trades away ordinary user-query realism, long-form
answer quality, and ambiguity resolution. The paper acknowledges that inverted
questions may have another valid answer. After inspecting 118 zero-pass tasks,
the authors removed 21 with an incorrect, ambiguous, or format-mismatched
reference. The project page and paper ask researchers not to expose decrypted
examples, and the canary exists to reduce benchmark leakage.

### Decision for this project

BrowseComp is outcome-only capability evidence. It cannot validate a Chrome
extension, cannot identify who produced an answer, and should not become the
matched mouse/keyboard protocol. If it is ever used as a stress test, the
encrypted corpus and canary must be preserved, no decrypted task should enter a
report or trace, and all interaction evidence must come from our own recorder.

Primary sources:

- Official paper: https://cdn.openai.com/pdf/5e10f4ab-d6f7-442e-9508-59515c65e35d/browsecomp.pdf
- Official release page: https://openai.com/index/browsecomp/
- Official evaluator: https://github.com/openai/simple-evals/blob/main/browsecomp_eval.py

Exact locators: Sections 2.1-2.4, 3, and 4.1-4.5; Tables 2-3; Figures 1 and
3-5; Appendix B; `browsecomp_eval.py` test-set loading, seed-0 sampling,
decryption, prompt, and grading logic.

## Cross-source decisions

These five papers reinforce six project rules:

1. Separate passive detection, active challenge, active defense, and external
   intervention. Their scores are not interchangeable.
2. Store environment, executor, input mode, and intervention condition for
   audit, but never let them enter the detector feature allowlist.
3. Distinguish intrinsic refusal, explicit stop, delayed stop, timeout, crash,
   and independent validator success.
4. Keep partial progress and versioned evidence for long tasks; a final answer
   or framework `done` message is insufficient.
5. Never claim recorder compatibility from a benchmark that only evaluates a
   final text answer.
6. Preserve human-research, benchmark-leakage, and harmful-task boundaries:
   no unapproved participant expansion, no decrypted BrowseComp examples, and
   no KILLBENCH harmful-scenario reproduction.
