# Research summary

This page digests the two internal progress reports of the research project
that preceded ComputerUseAgent: a cumulative report on the first four weeks
and a week-four analysis plan. Personal, institutional and scheduling details
were removed; the scientific narrative, method and limitations were kept.
Where the reports disagree with the archived evidence, this page follows
[research/evidence/INDEX.json](../../research/evidence/INDEX.json) and lists
the correction in [Corrections to the source reports](#corrections-to-the-source-reports).
Every number in the results passages below is bound to an INDEX claim and
checked by `tools/ci/check_readme.py`.

For the evidence files themselves see
[research/evidence/README.md](../../research/evidence/README.md); for how the
studies relate to other projects see [related-work.md](related-work.md).

## 1. Question

Can privacy-reduced keystroke, pointer, scroll, focus and timing patterns help
distinguish a human user from a computer-use agent when both perform the same
versioned task?

## 2. Answer so far

The instrumentation path is promising, but a detector claim would be
premature.

<!-- results:begin -->

- No validated human-versus-agent detector exists. The formal MiniWoB matrix
  completed 0/27<!-- claim:formal.miniwob.completed_attempts --> attempts, the
  formal ScienceBoard matrix collected
  0/27<!-- claim:formal.scienceboard.collected_sessions --> sessions, and the
  project collected 0<!-- claim:formal.human_traces --> human traces.
- The only real-data detection evidence is a retrospective on the public
  FP-Agent corpus. It shows ranking signal within tasks but a cross-task
  threshold and calibration transfer failure, with strong task and source
  confounds; see the FP-Agent section below.
- The recorder works end to end in a real browser, but its retained E2E run is
  engineering evidence about the recorder, not actor-behavior evidence; see
  the recorder section below.

<!-- results:end -->

The most important methodological point is that **task success, recorder
compatibility and actor detection are three separate claims**. The evidence
keeps them separate, and so does this page.

## 3. How the literature shaped the design

<!-- results:begin -->

The literature corpus holds 68<!-- claim:literature.items --> items:
60<!-- claim:literature.fulltext_reviewed --> full-text-reviewed papers,
4<!-- claim:literature.access_blocked --> access-blocked papers whose claims
were not inferred from full text, and
4<!-- claim:literature.repository_reviews --> repository reviews. A citation
graph of 12,932<!-- claim:literature.citation_nodes --> nodes and
22,117<!-- claim:literature.citation_assertions --> assertions was retrieved
from public indexes; the retrieval is partial
(15<!-- claim:literature.citation_blocked_directions --> seed directions were
blocked). See [research/literature/README.md](../../research/literature/README.md).

<!-- results:end -->

The syntheses that changed the design:

- [FP-Agent](https://arxiv.org/html/2605.01247) shows that behavioral features
  can be much more discriminative than browser metadata, but its held-out-task
  results are lower than its random split.
- [Known By Their Actions](https://arxiv.org/html/2605.14786) shows that UI
  traces can attribute known agents, and also shows timing, site and task
  dependence.
- [WebArena](https://arxiv.org/html/2307.13854) and
  [An Illusion of Progress?](https://arxiv.org/html/2504.01382) show why the
  final task state and trajectory must be checked independently of an agent's
  completion message.
- UI-TARS and Agent S2 show that coordinate-based and programmatic action
  backends change the observable trace.
- [ScienceBoard](https://arxiv.org/html/2505.19897) supplies native scientific
  applications with state validators; [LabOSBench](https://arxiv.org/html/2606.16802)
  is a lighter browser-based science alternative.

This led to five design decisions:

1. Collect behavior evidence and task-state evidence separately.
2. Keep framework, executor and task metadata for audit, but never use it as a
   model input.
3. Report supported, naturally observed and synthetically exercised event types
   separately.
4. Preserve failures, interruptions and timeouts.
5. Evaluate on held-out tasks and held-out agent families, not only on a random
   train/test split.

## 4. Agent pilots and the formal MiniWoB attempt

### Pilots

The pilots checked the simplest end-to-end path: install a current framework,
run an agent from a fresh browser profile, keep its action history and validate
the destination independently. The Browser-Use and Skyvern pilots used three
public, read-only micro-tasks: following the information link on example.com,
following the Root Zone Management link on the IANA homepage, and opening the
newest-stories page on Hacker News. The Stagehand smoke run used its own
variants: example.com to IANA's example-domains page, IANA's example-domains
page to `/domains`, and Hacker News `/news` to `/newest`.

<!-- results:begin -->

- **Browser-Use 0.11.5<!-- claim:pilots.browser_use.version -->** passed
  9/9<!-- claim:pilots.browser_use.successful_runs --> runs (three tasks, three
  repetitions) at an application-reported cost of
  US$0.436519<!-- claim:pilots.browser_use.cost_usd -->.
- **Skyvern 1.0.47<!-- claim:pilots.skyvern.version -->**, a self-hosted
  service stack, passed 9/9<!-- claim:pilots.skyvern.successful_runs --> runs at
  US$0.432419<!-- claim:pilots.skyvern.cost_usd -->.
- **Stagehand 3.7.1<!-- claim:pilots.stagehand.version -->** passed a
  single-pass smoke run, 3/3<!-- claim:pilots.stagehand.successful_runs -->, with
  no reported cost. It was later retired because its dependency graph carried
  13<!-- claim:pilots.stagehand.blocking_advisories --> blocking security
  advisories when the harness was frozen.

<!-- results:end -->

These are integration evidence, not benchmark scores. For Skyvern the failure
analysis mattered more than the pass rate. The first container reached the
wrong provider endpoint until the existing settings were mapped to the
variables Skyvern reads. The original engine reported completion before
destination evidence and structured output existed, so the pilot switched to
Skyvern's newer engine and made the external validator mandatory. A timed-out
live verification was re-audited against its timestamped DOM instead of being
rerun, and an extra attempt started by a stale process was cancelled and kept
outside the pilot. The lessons: a provider's "completed" status cannot replace
task evidence, and setup and cancelled attempts must stay visible.

### Formal MiniWoB matrix

The experiment was then rebuilt around three official BrowserGym/MiniWoB
tasks (`click-checkboxes`, `enter-text`, `choose-list`) with Browser-Use,
Skyvern and Stagehand, three repetitions each. The protocol fixes the model,
task version, seed, viewport, action and time caps, the real recorder
extension and an independent task validator.

<!-- results:begin -->

The first 2<!-- claim:formal.miniwob.published_failure_records --> Browser-Use
`click-checkboxes` launches ended with
`model_provider_error`<!-- claim:formal.miniwob.attempt1_cause --> before the
agent acted: 0<!-- claim:formal.miniwob.attempt1_actions --><!-- claim:formal.miniwob.attempt2_actions -->
actions, and traces that held only the recorder's navigation record. A later
minimal preflight for the same model returned
HTTP 502<!-- claim:formal.miniwob.preflight_http_status -->. The matrix was
paused rather than switch the planned model or provider, and the result is
0/27<!-- claim:formal.miniwob.completed_attempts --> completed: neither a
count of task failures nor a pass rate. The two operational failures are kept
in [research/evidence/week1_miniwob_prefix_failures/](../../research/evidence/week1_miniwob_prefix_failures/README.md)
and excluded from every success-rate and behavior analysis.

<!-- results:end -->

Two further pre-run gates stayed closed: the source reports record a later
private readiness check that failed on an invalid credential before any paid or
formal call, and the Stagehand dependency graph failed the security gate.
Neither gate was waived to manufacture a complete matrix. The first launch also
hit a Browser-Use screenshot timeout. The adapter weakness behind it was later
reproduced and fixed and the fix checked with a no-model probe, which is a
compatibility check rather than an agent result; the launch's exact window
state was not recorded.

## 5. FP-Agent reproduction and retrospective

FP-Agent is the closest prior work to the detector question. The project first
checked whether the released models reproduce, then ran a separate,
raw-derived retrospective designed to expose task shift, calibration failure
and source confounding. The data are the public FP-Agent corpus
(<https://osf.io/j6b5p/>); only aggregate results are archived, in
[research/evidence/fp_agent/](../../research/evidence/fp_agent/README.md).

### Released-model reproduction

The three released all-class XGBoost models were run, without retraining, on
the authors' released test split after verifying the repository, data, split
and model hashes.

<!-- results:begin -->

| Features | Accuracy | Macro-F1 |
|---|---:|---:|
| Browser fingerprint | 0.8212<!-- claim:fp_agent.released.browser_accuracy --> | 0.8223<!-- claim:fp_agent.released.browser_macro_f1 --> |
| Behavioral | 0.9994<!-- claim:fp_agent.released.behavioral_accuracy --> | 0.9994<!-- claim:fp_agent.released.behavioral_macro_f1 --> |
| Combined | 1.0000<!-- claim:fp_agent.released.combined_accuracy --> | 1.0000<!-- claim:fp_agent.released.combined_macro_f1 --> |

The split has 1,544<!-- claim:fp_agent.released.test_examples --> test
examples in 8<!-- claim:fp_agent.released.classes --> classes (upstream commit
e614e2f<!-- claim:fp_agent.released.upstream_commit -->, run under Python
3.13.14<!-- claim:fp_agent.released.python --> outside the hash-locked
environment). These scores re-execute a within-corpus random split; they do not
measure unseen agents or deployment distributions. All of the browser-only
model's errors fall within three agent families (Browser Use, Claude and Atlas
Agent) that it confuses with one another, while humans and the other families
are classified without error; browser and system fingerprints shared across
families are a weak basis for family attribution.

<!-- results:end -->

### Leave-one-agent-family-out probe

A binary agent-versus-human model was trained from scratch per fold on the
released feature vectors, holding out one agent family at a time for each of
three feature sets. This probe is kept as provenance; the retrospective below
is the main analysis.

<!-- results:begin -->

Over 21<!-- claim:fp_agent.loafo.folds --> folds, browser features reached a
mean balanced accuracy (BA) of 0.9286<!-- claim:fp_agent.loafo.browser_mean_ba -->
with a worst fold of 0.5000<!-- claim:fp_agent.loafo.browser_worst_ba -->:
with Manus<!-- claim:fp_agent.loafo.browser_manus_fold_family --> held out,
recall was 0.0<!-- claim:fp_agent.loafo.browser_manus_recall --> although AUROC
was 0.9174<!-- claim:fp_agent.loafo.browser_manus_auroc -->, so the ranking
transferred but the threshold did not. Behavioral and combined features kept
BA ≥0.9968<!-- claim:fp_agent.loafo.behavioral_min_ba --><!-- claim:fp_agent.loafo.combined_min_ba -->
in every fold. The same 109<!-- claim:fp_agent.loafo.human_test_rows --> human
test rows are reused in every fold, so the folds are not independent (run under
Python 3.14.5<!-- claim:fp_agent.loafo.python --> outside the hash-locked
environment).

<!-- results:end -->

### 23-feature retrospective: method

- The raw FP-Agent events are recomputed into the same fixed 23-feature
  allowlist that the detector library uses (timing, pointer, keyboard, focus
  and event mix, and missingness indicators).
- Primary model: three-fold leave-one-task-out. Each fold removes from training
  every participant or trial group observed in the held-out task (the purge);
  no outer or inner group overlap was detected.
- A standard scaler and a class-weighted L2 logistic regression
  (C = 1.0, lbfgs, `max_iter` = 2,000) are fitted on the surviving training
  rows. Three group-disjoint inner folds produce out-of-fold scores for Platt
  calibration (C = 1,000,000). The operating threshold is fixed at 0.5, and
  calibration error uses 10 equal-width bins.
- Controls: a seven-fold agent-family holdout, task and source confound
  classifiers, a within-task group-label permutation test, an XGBoost model as
  a secondary estimator, and group and single-feature ablations.
- The analysis ran in an isolated, hash-locked Python environment with a fixed
  seed.

### 23-feature retrospective: results

<!-- results:begin -->

The corpus has 7,728<!-- claim:fp_agent.retro.sessions --> sessions
(546<!-- claim:fp_agent.retro.human_sessions --> human,
7,182<!-- claim:fp_agent.retro.agent_sessions --> agent) over
3<!-- claim:fp_agent.retro.tasks --> tasks, analyzed with seed
20260811<!-- claim:fp_agent.retro.seed -->.

| Held-out task | Training rows after the purge | BA at the fixed threshold | AUROC |
|---|---:|---:|---:|
| Flight-booking | 79<!-- claim:fp_agent.retro.purge_train_rows_flight --> | 0.500<!-- claim:fp_agent.retro.flight_ba --> | 0.998<!-- claim:fp_agent.retro.flight_auroc --> |
| Forums | 212<!-- claim:fp_agent.retro.purge_train_rows_forums --> | 0.497<!-- claim:fp_agent.retro.forums_ba --> | 0.829<!-- claim:fp_agent.retro.forums_auroc --> |
| Shopping | 37<!-- claim:fp_agent.retro.purge_train_rows_shopping --> | 0.853<!-- claim:fp_agent.retro.shopping_ba --> | 0.967<!-- claim:fp_agent.retro.shopping_auroc --> |

Flight-booking and Forums do not have chance-level ranking: their AUROC is
high, but in both folds every human session is classified as an agent, so their
BA at the fixed threshold sits near chance. The primary failure is cross-task
calibration and threshold transfer. Pooled over the three folds, the logistic
model reaches BA 0.6001<!-- claim:fp_agent.retro.pooled_ba -->, AUROC
0.6358<!-- claim:fp_agent.retro.pooled_auroc --> and macro-F1
0.5746<!-- claim:fp_agent.retro.pooled_macro_f1 -->, but the fold behavior is
the more important result.

- With only n=3 tasks, the reported BA interval
  0.497<!-- claim:fp_agent.retro.ba_interval_lower --> to
  0.853<!-- claim:fp_agent.retro.ba_interval_upper --> is the min/max over the
  3 folds, a descriptive range rather than a population confidence interval.
- The pooled AUROC mixes three separately calibrated fold models, and AUPRC
  0.958<!-- claim:fp_agent.retro.pooled_auprc --> is inflated by the 92.9% agent
  prevalence.
- The permutation p-value 0.000999<!-- claim:fp_agent.retro.permutation_p --> is
  the minimum possible with 1,000<!-- claim:fp_agent.retro.permutation_draws -->
  draws. It rejects that restricted shuffled-label null, but it does not repair
  threshold transfer.
- The same features predict the task (BA
  0.866<!-- claim:fp_agent.retro.task_confound_ba -->) and the agent family (BA
  0.974<!-- claim:fp_agent.retro.source_confound_ba -->): strong task and source
  confounds.
- Dropping the keyboard features raises BA by
  +0.181<!-- claim:fp_agent.retro.keyboard_ablation_delta -->.
- FP-Agent records no focus events, so `missing_focus=1`<!-- claim:fp_agent.retro.missing_focus -->
  for every session.
- The agent-family holdout evaluates the same human sessions in every fold, so
  its pooled rows are repeated measures, not independent observations, and it
  cannot replace an external-domain test. The secondary XGBoost model has a
  higher pooled BA than the logistic model but a lower macro-F1 and much worse
  calibration.

<!-- results:end -->

**What it means.** Observable behavior carries ranking signal in this corpus,
but a fixed decision threshold does not transfer uniformly across tasks. The
analysis is group-disjoint under the specified split and feature contract. It
does not show that source, collection, missingness or measurement confounding
is absent, and it is not a prospective deployment estimate.

## 6. The recorder and its E2E evidence

The recorder is a local Manifest V3 extension. Its simple path is actor, task,
consent, start and stop; protocol, framework and executor fields sit under an
advanced section. It supports 59 browser event types plus a recorder-generated
navigation record, stores events in 500-event chunks, and offers a dashboard
with JSON, JSONL and CSV export and deletion.

During development the session-global `cui-trace-v2` timeline replaced the
earlier `cui-trace-v1` format. The first strict run of the new format exposed a
same-origin reload race: the stored chunks held more events than the stale
session metadata reported. It was fixed by draining queued page-hide writes,
reloading the metadata, persisting the new document epoch before re-injecting
the content script and rejecting late batches from the old epoch. Later
hardening, including the private-field fix and the message-authority rules of
0.5.0, is described in the [CHANGELOG](../../CHANGELOG.md).

<!-- results:begin -->

The retained run of recorder 0.4.0<!-- claim:recorder_e2e.extension_version -->
(2026-08-09<!-- claim:recorder_e2e.run_started_at -->) passed
14/14<!-- claim:recorder_e2e.cases_passed --> cases with
0<!-- claim:recorder_e2e.browser_errors --> browser errors. Its closed-schema
trace holds 527<!-- claim:recorder_e2e.records_total --> records:
516<!-- claim:recorder_e2e.records_natural --> naturally observed events,
10<!-- claim:recorder_e2e.records_synthetic --> synthetic QA events and
1<!-- claim:recorder_e2e.records_navigation --> recorder-generated navigation
record, covering 42 of 59<!-- claim:recorder_e2e.types_observed --> supported
event types; 17<!-- claim:recorder_e2e.types_never_observed --> types never
occur in it (listed in [event coverage](../extension/event-coverage.md)). The
run used Chrome 151.0.7922.108<!-- claim:recorder_e2e.chrome_version -->,
operated through Puppeteer and the Chrome DevTools Protocol, and is labelled
`source_class=synthetic_coverage`.

<!-- results:end -->

The record count is an observation of that run, not an invariant of reruns.
The run proves the extension lifecycle and automation compatibility; it is not
a reasoning-agent trace, a human trace, a matched pair or a detector result,
and `is_trusted` stays diagnostic metadata that is never used to infer the
actor. The current suite has 15 cases and runs in CI; see
[the E2E documentation](../extension/e2e.md).

## 7. ScienceBoard study design (never executed)

ScienceBoard was chosen as the primary scientific route because it runs native
professional applications inside a VM and includes state validators. The
provisional tasks, selected by the researcher and never independently
confirmed, are three official A-01 tasks:

| Task | Required final state |
|---|---|
| KAlgebra A-01 | compute the conjugate of `1+i` and store it in `p` |
| Celestia A-01 | set the Julian date to `2400000` |
| GrassGIS A-01 | use the natural-earth dataset and load `countries@PERMANENT` |

The intended actors are one self-recorded human participant, ScienceBoard's
AllInOne agent (AIOAgent) and SeeAct: 3 actors x 3 tasks x 3 repetitions = 27
session bundles, interleaved with a fixed randomization seed
(`research/configs/formal-study.json`) and reset from the same VM snapshot
before each run. Failures and timeouts stay in the matrix: "27/27 complete"
would mean that 27 artifact bundles exist, not that 27 tasks succeeded. Every
attempted session must keep its trace, task JSON and hashes, run manifest and
environment provenance, final screenshot, official validator result and, where
applicable, a failure or timeout log.

**Capture.** A browser extension cannot see input inside a VM, so the study
uses the guest-side X11 recorder (`capture_adapter=x11_vm`), deployed as the
single deterministic `computer-use-agent-x11-recorder.pyz` zipapp. It records
monotonic and wall-clock timestamps, key down and up metadata without typed
text, pointer movement, buttons and wheel events, and focus and window
transitions without window titles. It never collects clipboard data, file
contents, application text or raw key characters. Its guest dependencies must
be baked into the VM snapshot, because each run reverts to that snapshot.
Private outputs go under `CUA_PRIVATE_ROOT`.

**Calibration gates.** Before the first formal session, calibration must pass
XRecord/XTest input, direct human input, start and stop, a post-stop negative
control, timestamp monotonicity and the privacy scan. A failed calibration
blocks collection instead of producing a partially trusted dataset.

<!-- results:begin -->

**Status.** 0/27<!-- claim:formal.scienceboard.collected_sessions --> sessions
were collected and the harness never ran on a VM. The nine browser-based
NetLogo and PhET sessions that passed their validators
(9/9<!-- claim:formal.simulators.passed_runs -->,
908<!-- claim:formal.simulators.events --> events) were produced by a
deterministic Puppeteer/CDP script with recorder 0.2.1. They are labelled
`scripted_automation_control`<!-- claim:formal.simulators.source_class --> and
serve only as a calibration control, excluded from any model training. The
Codex Desktop case study ended with
9/9<!-- claim:formal.codex.blocked_attempts --> attempts blocked because the
interaction surfaces were unavailable.

<!-- results:end -->

## 8. Analysis contract fixed before data collection

The week-four plan fixed the analysis before any formal data existed, so that
features and evaluation rules could not change after seeing results. The
detector library implements it.

**Features.** The deterministic extractor produces 23 features: event count,
duration and rate; inter-event median, 90th percentile, coefficient of
variation and burstiness; time to first input; pointer path length, speed,
straightness and turning entropy; click dwell and pre-click movement;
inter-key and key-hold latency and a correction ratio; focus-transition count;
event-type entropy; and four missing-modality indicators. It must handle
zero-duration sessions, missing modalities, duplicate or non-monotonic events
and unmatched key or button pairs without inventing values.

**Inputs the detector may not use.** Actor, framework or executor labels; task
IDs; URLs and selectors; session UUIDs; absolute time; OS or browser versions;
validator outcomes; and `is_trusted`. They remain available for audit and
stratification, but as model inputs they would let a classifier memorize the
collection setup instead of learning behavior.

**Three layers of evaluation.**

1. Within-task description: distributions, trajectory plots and effect sizes
   per task. With a single human participant, intervals describe run
   variation, not population variation.
2. Exploratory ScienceBoard probes: human-versus-agent and three-way actor
   classification under strict leave-one-task-out evaluation, with task
   identification as a negative control; strong task prediction would warn
   that task leakage dominates the actor signal.
3. FP-Agent generalization: the leave-one-agent-family-out probe, kept separate
   from the small planned ScienceBoard dataset.

Any ScienceBoard classifier would be labelled a **single-participant pipeline
probe**; it could not support a population-level or deployment-ready detector
claim. The implemented pipeline (strict leave-one-task-out, nested
training-only calibration, a task-identification control, within-task
permutation, task-cluster bootstrap and ablations) has run only on synthetic
fixtures and always reports `formal_claim_permitted=false`; see
[detector.md](../detector.md).

## 9. What the evidence supports

**Supported, within the stated scope:**

- The historical Browser-Use and Skyvern pilots and the FP-Agent
  reproduction ran successfully within their stated scope.
- The 23-feature FP-Agent retrospective is complete as a same-corpus,
  group-disjoint analysis: it shows ranking signal plus a documented
  cross-task calibration and threshold failure.
- The formal MiniWoB protocol fails closed and keeps its provider-gate
  failures without counting them as task attempts.
- The recorder works end to end for a real-extension automation fixture, and
  it can record richer browser-simulator interactions.
- The matched-trace detector and viewer code paths exist; their synthetic
  tests are not a detector finding.
- The literature and the local failures justify independent validators,
  source labels, privacy reduction and held-out evaluation.

**Not supported:**

- a completed formal MiniWoB attempt, or any reasoning-agent trace through the
  current recorder;
- any human trace, paired or not;
- any VM import, ScienceBoard calibration or session;
- a leave-one-task-out ScienceBoard classifier;
- a prospective FP-Agent test on a newly collected agent family or
  environment;
- any universal, population-level or operational detector claim.

## 10. Open decisions

1. Confirm or replace the three provisional ScienceBoard tasks.
2. Confirm whether AIOAgent and SeeAct are the right agent actors.
3. Record a human-research determination through the operator's own review
   process before any human session is used for formal analysis, detector
   training or research claims. Until then, a self-recorded session is private
   engineering QA only, and nobody else may be recorded.
4. Decide the evaluation emphasis: pipeline validation first, or a broader
   participant study later.

## Corrections to the source reports

| The source reports said | The archived evidence shows |
|---|---|
| The model provider returned an HTTP 403 during the two MiniWoB launches. | The attempt records show `model_provider_error` with zero actions, and the later preflight recorded HTTP 502; no evidence file records an HTTP 403. |
| The retained E2E suite covers browser-start recovery. | No E2E case covers browser startup; the suite covers a service-worker restart, and startup recovery is covered by a unit test. |
| The Stagehand dependency graph failed on a handful of reviewed production advisories. | The frozen harness manifest lists 13 blocking advisories. |
| The ScienceBoard VM archive was verified in private storage. | Nothing in the public evidence can verify this, so no such claim is made. |
| The retained E2E run is identified by a source commit. | That commit exists only in the private source repository; the source-tree SHA-256 recorded in the run manifest identifies the tested code. |
| A Windows screenshot-capture fix was confirmed with exact capture counts. | No machine-readable record of that probe is archived; only the fix itself is described. |
