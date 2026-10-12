# %% [markdown]
# # FP-Agent retrospective: what survives task and agent-family shift?
#
# ## tl;dr
#
# The primary, prespecified task-held-out logistic model has a highly unstable
# estimate: two of three task folds are effectively chance, while one is much
# higher. Purging every participant/trial group shared with the held-out task
# leaves only 79, 212, and 37 training rows. The defensible conclusion is
# therefore **same-corpus retrospective signal with severe data-support and
# task/source-confounding limits**, not a validated general computer-user-agent
# detector. This notebook fails closed unless the frozen, raw-derived
# 23-feature result and its checksum status are present.

# %%
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from IPython.display import Image, display

# The execution driver (research/fp_agent/execute_notebook.py) starts the clean
# kernel at the repository root and passes both locations explicitly. By
# default the archived result is read and figures go to a temporary build
# directory: executing this source never rewrites research/evidence.
ROOT = Path.cwd().resolve()
EVIDENCE_ROOT = ROOT / "research" / "evidence"
RESULT_DIR = Path(os.environ.get("CUA_FP_RESULT_DIR") or EVIDENCE_ROOT / "fp_agent").resolve()
FIGURE_DIR = Path(
    os.environ.get("CUA_FP_FIGURE_DIR")
    or Path(tempfile.gettempdir()) / "cua-fp-retrospective-figures"
).resolve()
if not RESULT_DIR.is_dir():
    raise RuntimeError("Result directory is absent; run from the repository root")
if FIGURE_DIR.is_relative_to(EVIDENCE_ROOT.resolve()):
    raise RuntimeError("Figures are build outputs and must not be written into research/evidence")

RESULT = RESULT_DIR / "fp_agent_retrospective.json"
STATUS = RESULT_DIR / "fp_agent_retrospective_status.json"
FIGURE_DIR.mkdir(parents=True, exist_ok=True)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


status = (
    json.loads(STATUS.read_text(encoding="utf-8"))
    if STATUS.exists()
    else {
        "status": "blocked",
        "error": "evaluation status has not been generated",
    }
)
if status.get("status") != "complete" or not RESULT.exists():
    raise RuntimeError(f"Fail-closed retrospective status: {status}")
if status.get("output_sha256") != sha256(RESULT):
    raise RuntimeError("Result SHA-256 differs from the completion status")
result = json.loads(RESULT.read_text(encoding="utf-8"))
if result.get("status") != "complete":
    raise RuntimeError("Retrospective result is not complete")

COLORS = {
    "navy": "#17324D",
    "teal": "#0B7A7F",
    "orange": "#D97706",
    "red": "#B42318",
    "blue": "#2F6B9A",
    "gray": "#667085",
}
sns.set_theme(style="whitegrid", context="notebook")
figure_manifest: list[dict[str, object]] = []


def save_figure(fig: plt.Figure, filename: str, caption: str) -> None:
    path = FIGURE_DIR / filename
    # The Creator string is historical: keeping it lets a clean-kernel rerun
    # reproduce the archived PNG files byte for byte.
    fig.savefig(
        path,
        dpi=220,
        bbox_inches="tight",
        facecolor="white",
        metadata={"Creator": "CU Detector locked retrospective notebook"},
    )
    figure_manifest.append(
        {
            "path": path.name,
            "sha256": sha256(path),
            "bytes": path.stat().st_size,
            "caption": caption,
            "crop": "none; Matplotlib bbox_inches=tight",
        }
    )
    display(Image(filename=str(path)))
    plt.close(fig)


def aggregate(section: str) -> dict[str, object]:
    return result[section]["aggregate"]


primary = aggregate("primary_3_fold_loto_logistic")
xgb = aggregate("secondary_3_fold_loto_xgboost")
family = aggregate("seven_fold_agent_family_logistic")
bootstrap = result["primary_group_bootstrap"]["intervals_95"]

print(
    json.dumps(
        {
            "status": result["status"],
            "headline_scope": result["headline_scope"],
            "result_sha256": status["output_sha256"],
            "primary_macro_f1": primary["macro_f1"],
            "primary_balanced_accuracy": primary["balanced_accuracy"],
            "balanced_accuracy_95_ci": bootstrap["balanced_accuracy"],
        },
        indent=2,
    )
)

# %% [markdown]
# ## Context & Methods
#
# The source is the restricted FP-Agent raw event corpus. Events are streamed
# into the project's exact 23-feature allowlist; actor, task, framework, URL,
# absolute time, software version, validator outcome, and text never enter the
# model matrix. `inter_key_median_ms` is recomputed as keydown-to-keydown. The
# missing focus channel is represented structurally (`missing_focus=1`) rather
# than interpreted as observed zero focus behavior.
#
# The primary analysis is deterministic three-fold leave-one-task-out (LOTO)
# regularized logistic regression. Every group present in the held-out task is
# purged from outer training; train-fold scaling and group-disjoint out-of-fold
# Platt calibration remain inside that reduced training set. XGBoost is
# secondary exploratory evidence. Seven leave-one-agent-family-out folds,
# task/source confound controls, 1,000 group-label permutations stratified by
# observed task coverage, grouped/single-feature ablations, and 2,000 whole-task
# bootstrap draws probe transfer and leakage. Failed folds remain in the
# denominator.

# %%
print(json.dumps(result["protocol"], indent=2))
print(json.dumps(result["execution_environment"], indent=2))
print(json.dumps(result["split_audit"], indent=2))

assert result["protocol"]["permutation_repetitions"] == 1000
assert result["protocol"]["bootstrap_repetitions"] == 2000
assert result["permutation"]["successful"] == 1000
assert result["permutation"]["failed"] == 0
assert result["primary_3_fold_loto_logistic"]["failed_fold_count"] == 0
assert result["secondary_3_fold_loto_xgboost"]["failed_fold_count"] == 0
assert result["seven_fold_agent_family_logistic"]["failed_fold_count"] == 0

# %% [markdown]
# ## Data
#
# Raw rows remain ignored/private. The public sampling ledger contains only
# the derived-dataset hash, fixed seed, aggregate task/family counts, and row
# count. Human participant groups are never exposed in this notebook.

# %%
ledger = result["sampling_ledger"]
print(json.dumps(ledger, indent=2))

task_df = pd.DataFrame(
    [{"Task": key, "Sessions": value} for key, value in ledger["counts_by_task"].items()]
)
family_df = pd.DataFrame(
    [{"Actor family": key, "Sessions": value} for key, value in ledger["counts_by_family"].items()]
).sort_values("Sessions", ascending=True)

fig, axes = plt.subplots(
    1,
    2,
    figsize=(12.8, 4.8),
    gridspec_kw={"width_ratios": [1, 1.65]},
)
sns.barplot(data=task_df, x="Task", y="Sessions", color=COLORS["teal"], ax=axes[0])
axes[0].set_title("Three task domains")
axes[0].tick_params(axis="x", rotation=20)
sns.barplot(data=family_df, x="Sessions", y="Actor family", color=COLORS["blue"], ax=axes[1])
axes[1].set_title("Class/source imbalance is substantial")
axes[1].set_ylabel("")
fig.suptitle("FP-Agent raw-derived analysis set", fontweight="bold")
fig.subplots_adjust(wspace=0.38, top=0.82)
save_figure(
    fig,
    "data_composition.png",
    "Raw-derived session counts by task and actor family; aggregate counts only.",
)

# %% [markdown]
# ## Results
#
# Metrics must be read together. The agent class dominates the corpus, so
# AUPRC is inflated by prevalence and cannot replace balanced accuracy or the
# confusion matrix. The primary confidence interval resamples only the three
# outer tasks; its coarse support is itself an uncertainty warning.

# %%
metric_names = ["macro_f1", "balanced_accuracy", "auroc", "auprc", "brier", "ece"]
metric_table = pd.DataFrame(
    {
        "Metric": metric_names,
        "Primary logistic": [primary[name] for name in metric_names],
        "Exploratory XGBoost": [xgb[name] for name in metric_names],
        "Family holdout logistic": [family[name] for name in metric_names],
    }
)
print(metric_table.to_string(index=False, float_format=lambda value: f"{value:.4f}"))

fig, ax = plt.subplots(figsize=(10.8, 4.8))
plot_metrics = metric_table[metric_table["Metric"].isin(["macro_f1", "balanced_accuracy", "auroc"])]
plot_metrics = plot_metrics.assign(
    Metric=plot_metrics["Metric"].map(
        {"macro_f1": "Macro-F1", "balanced_accuracy": "Balanced accuracy", "auroc": "AUROC"}
    )
)
long_metrics = plot_metrics.melt(id_vars="Metric", var_name="Analysis", value_name="Score")
sns.barplot(
    data=long_metrics,
    x="Metric",
    y="Score",
    hue="Analysis",
    palette=[COLORS["navy"], COLORS["orange"], COLORS["red"]],
    ax=ax,
)
ax.axhline(0.5, color=COLORS["gray"], linestyle="--", linewidth=1.2, label="Chance reference")
ax.set_ylim(0, 1)
ax.set_title("Aggregate metrics do not establish transfer")
ax.set_xlabel("")
ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1), frameon=True)
save_figure(
    fig,
    "model_metric_comparison.png",
    "Primary logistic, exploratory XGBoost, and agent-family-holdout aggregate metrics.",
)

# %%
task_rows = []
for label, section in (
    ("Primary logistic", "primary_3_fold_loto_logistic"),
    ("Exploratory XGBoost", "secondary_3_fold_loto_xgboost"),
):
    for fold in result[section]["folds"]:
        task_rows.append(
            {
                "Held-out task": f"{fold['fold']}\ntrain n={fold['train']}",
                "Model": label,
                "Balanced accuracy": fold["metrics"]["balanced_accuracy"],
            }
        )
task_fold_df = pd.DataFrame(task_rows)
agent_fold_df = pd.DataFrame(
    [
        {
            "Held-out family": fold["fold"],
            "Balanced accuracy": fold["metrics"]["balanced_accuracy"],
        }
        for fold in result["seven_fold_agent_family_logistic"]["folds"]
    ]
)

fig, axes = plt.subplots(1, 2, figsize=(13.5, 4.8), gridspec_kw={"width_ratios": [1, 1.5]})
sns.barplot(
    data=task_fold_df,
    x="Held-out task",
    y="Balanced accuracy",
    hue="Model",
    palette=[COLORS["navy"], COLORS["orange"]],
    ax=axes[0],
)
legend_handles, legend_labels = axes[0].get_legend_handles_labels()
axes[0].get_legend().remove()
axes[0].axhline(0.5, color=COLORS["gray"], linestyle="--")
axes[0].set_ylim(0.45, 1.01)
axes[0].tick_params(axis="x", rotation=20)
sns.barplot(
    data=agent_fold_df,
    x="Balanced accuracy",
    y="Held-out family",
    color=COLORS["red"],
    ax=axes[1],
)
axes[1].axvline(0.5, color=COLORS["gray"], linestyle="--")
axes[1].set_xlim(0.45, 1.0)
axes[1].set_title("Family holdout remains corpus-bound")
axes[1].set_ylabel("")
axes[1].legend(
    legend_handles,
    legend_labels,
    loc="upper left",
    bbox_to_anchor=(1.01, 1),
    title="Task-holdout model",
)
axes[0].set_title("Task sensitivity")
fig.suptitle("Outer-fold transfer checks", fontweight="bold")
fig.subplots_adjust(wspace=0.42, top=0.82)
save_figure(
    fig,
    "held_out_fold_performance.png",
    "Balanced accuracy for every task-held-out and agent-family-held-out fold; no folds omitted.",
)

# %%
fig, axes = plt.subplots(1, 2, figsize=(9.5, 4.2))
for ax, title, summary in (
    (axes[0], "Task-held-out logistic", primary),
    (axes[1], "Agent-family-held-out logistic", family),
):
    values = np.asarray(summary["confusion"]["values"])
    sns.heatmap(values, annot=True, fmt="d", cmap="Blues", cbar=False, ax=ax)
    ax.set_xticklabels(["Pred. human", "Pred. agent"])
    ax.set_yticklabels(["Human", "Agent"], rotation=0)
    ax.set_title(title)
    ax.set_xlabel("")
    ax.set_ylabel("")
fig.suptitle("Transfer error profiles depend on the evaluation axis", fontweight="bold")
save_figure(
    fig,
    "confusion_matrices.png",
    "Aggregate confusion matrices for primary task-held-out and seven-family-held-out logistic models.",
)

# %%
fig, ax = plt.subplots(figsize=(8.5, 5.2))
for label, summary, color in (
    ("Primary logistic", primary, COLORS["navy"]),
    ("Exploratory XGBoost", xgb, COLORS["orange"]),
):
    curve = pd.DataFrame(summary["calibration_curve"])
    ax.plot(
        curve["mean_probability"],
        curve["observed_fraction"],
        marker="o",
        linewidth=2,
        label=f"{label} (ECE={summary['ece']:.3f})",
        color=color,
    )
ax.plot([0, 1], [0, 1], linestyle="--", color=COLORS["gray"], label="Ideal")
ax.set(
    xlim=(0, 1), ylim=(0, 1), xlabel="Mean predicted probability", ylabel="Observed agent fraction"
)
ax.set_title("Nested calibration remains imperfect")
ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1))
save_figure(
    fig,
    "calibration_curves.png",
    "Aggregate task-held-out calibration curves and expected calibration error.",
)

# %%
control_df = pd.DataFrame(
    [
        {"Analysis": "Primary logistic", "Balanced accuracy": primary["balanced_accuracy"]},
        {
            "Analysis": "Task encoding probe",
            "Balanced accuracy": result["task_confound"]["balanced_accuracy"],
        },
        {
            "Analysis": "Agent-family encoding probe",
            "Balanced accuracy": result["source_confound"]["balanced_accuracy"],
        },
        {
            "Analysis": "Permutation null mean",
            "Balanced accuracy": result["permutation"]["null_mean"],
        },
    ]
)
fig, ax = plt.subplots(figsize=(9.2, 4.6))
sns.barplot(
    data=control_df,
    x="Balanced accuracy",
    y="Analysis",
    palette=[COLORS["navy"], COLORS["orange"], COLORS["red"], COLORS["gray"]],
    hue="Analysis",
    legend=False,
    ax=ax,
)
ax.axvline(0.5, color=COLORS["gray"], linestyle="--")
ax.set_xlim(0.45, 1.0)
ax.set_title(
    f"Nuisance-variable probes dominate the primary signal (permutation p={result['permutation']['p_value']:.4f})"
)
save_figure(
    fig,
    "confound_and_permutation_controls.png",
    "Primary balanced accuracy compared with task/source encoding controls and the permutation null mean.",
)

# %%
group_ablation = pd.DataFrame(result["ablations"]["group_drop"]).sort_values("delta")
feature_ablation = pd.DataFrame(result["ablations"]["single_feature_drop"]).sort_values("delta")
group_label = {
    "timing": "Timing",
    "pointer": "Pointer",
    "keyboard": "Keyboard",
    "focus_and_mix": "Focus & mix",
    "missingness": "Missingness",
}
group_ablation["display_name"] = group_ablation["dropped"].map(group_label)
feature_label = {
    "event_count": "Event count",
    "duration_ms": "Duration",
    "event_rate_hz": "Event rate",
    "inter_event_median_ms": "Inter-event median",
    "inter_event_p90_ms": "Inter-event p90",
    "inter_event_cv": "Inter-event CV",
    "inter_event_burstiness": "Event burstiness",
    "time_to_first_input_ms": "Time to first input",
    "pointer_path_px": "Pointer path",
    "pointer_speed_px_s": "Pointer speed",
    "pointer_straightness": "Pointer straightness",
    "pointer_turning_entropy": "Pointer turn entropy",
    "click_dwell_median_ms": "Click dwell median",
    "pre_click_movement_median_px": "Pre-click movement",
    "inter_key_median_ms": "Inter-key median",
    "key_hold_median_ms": "Key-hold median",
    "correction_ratio": "Correction ratio",
    "focus_transition_count": "Focus transitions",
    "event_type_entropy": "Event-type entropy",
    "missing_pointer": "Missing pointer",
    "missing_keyboard": "Missing keyboard",
    "missing_click_dwell": "Missing click dwell",
    "missing_focus": "Missing focus",
}
feature_ablation["display_name"] = feature_ablation["dropped"].map(feature_label)
fig, axes = plt.subplots(1, 2, figsize=(15.5, 8), gridspec_kw={"width_ratios": [0.8, 2.1]})
sns.barplot(data=group_ablation, x="delta", y="display_name", color=COLORS["teal"], ax=axes[0])
sns.barplot(data=feature_ablation, x="delta", y="display_name", color=COLORS["blue"], ax=axes[1])
for ax in axes:
    ax.axvline(0, color=COLORS["gray"], linewidth=1)
    ax.set_xlabel("Δ balanced accuracy after dropping feature(s)")
    ax.set_ylabel("")
axes[1].tick_params(axis="y", labelsize=8.5, pad=2)
axes[0].set_title("Grouped ablation")
axes[1].set_title("Single-feature ablation")
fig.suptitle("Ablations are unstable around a weak primary baseline", fontweight="bold")
fig.subplots_adjust(wspace=0.55, top=0.90)
save_figure(
    fig,
    "ablation_deltas.png",
    "Change in task-held-out logistic balanced accuracy after grouped and single-feature removal.",
)

# %% [markdown]
# ## Takeaways
#
# 1. The primary aggregate is dominated by heterogeneous task folds: two are at
#    chance and one is much higher. The whole-task interval is correspondingly
#    wide, and only three outer task units exist.
# 2. Group purging removes participant/trial leakage but also removes roughly
#    five thousand rows per fold, leaving as few as 37 training rows. This is a
#    dataset-support limitation, not evidence of broad deployment validity.
# 3. Family holdout is stronger than task holdout inside this corpus, but task
#    and family/source identity remain highly predictable from the same feature
#    space; neither the family result nor XGBoost establishes future transfer.
# 4. The significant group-level permutation result rejects its restricted
#    exchangeability null; it does **not** remove task/source confounding or
#    overcome the three-task uncertainty.
# 5. No auxiliary dataset was combined with FP-Agent, and no claim is made
#    about detecting reasoning, all CUAs, or future unseen interaction systems.

# %%
manifest_path = FIGURE_DIR / "figure_manifest.json"
# v2: figure paths are relative to the directory holding this manifest.
manifest = {
    "schema_version": "cui-retrospective-figure-manifest-v2",
    "source_result": RESULT.name,
    "source_result_sha256": sha256(RESULT),
    "notebook_source": "research/fp_agent/notebooks/retrospective.py",
    "figures": figure_manifest,
}
manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(json.dumps(manifest, indent=2))
