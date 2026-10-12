# %% [markdown]
# # Auxiliary data calibration
#
# ## tl;dr
#
# This executable notebook source renders only aggregate calibration evidence.
# The three sources remain separate from FP-Agent and MolmoWeb remains outside
# the 23-feature timing model.

# %%
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt

# The execution driver starts the clean kernel at the repository root and
# passes the input and figure directories explicitly. Using cwd keeps the
# percent-format source executable both as a script and as a notebook, where
# ``__file__`` is intentionally unavailable. By default the archived aggregate
# metrics are read and the figures go to a temporary build directory, so
# executing this source never rewrites research/evidence.
REPOSITORY_ROOT = Path.cwd().resolve()
EVIDENCE_ROOT = REPOSITORY_ROOT / "research" / "evidence"
RESULTS = Path(os.environ.get("CUA_AUX_RESULT_DIR") or EVIDENCE_ROOT / "auxiliary_data").resolve()
FIGURE_DIR = Path(
    os.environ.get("CUA_AUX_FIGURE_DIR")
    or Path(tempfile.gettempdir()) / "cua-auxiliary-calibration-figures"
).resolve()
if FIGURE_DIR.is_relative_to(EVIDENCE_ROOT.resolve()):
    raise RuntimeError("Figures are build outputs and must not be written into research/evidence")
FIGURE_DIR.mkdir(parents=True, exist_ok=True)
CAPTCHA_PATH = RESULTS / "captchasolve30k_metrics.json"
AALTO_PATH = RESULTS / "aalto_136m_metrics.json"
MOLMOWEB_PATH = RESULTS / "molmoweb_metrics.json"
SUMMARY_PATH = RESULTS / "summary.json"
FIGURE_PATH = FIGURE_DIR / "auxiliary_calibration.svg"
FIGURE_PNG_PATH = FIGURE_DIR / "auxiliary_calibration.png"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


captcha = load_json(CAPTCHA_PATH)
aalto = load_json(AALTO_PATH)
molmoweb = load_json(MOLMOWEB_PATH)
summary = load_json(SUMMARY_PATH)

# %% [markdown]
# ## Context & Methods
#
# The CaptchaSolve30k and Aalto tracks compare deterministic fixed samples with
# their eligible source populations. MolmoWeb compares ordered action-name
# n-grams with a domain-only baseline in five hostname-disjoint folds.
#
# ### Key Assumptions
#
# - A fixed SHA-256 priority sample is a reproducible simple random sample
#   within each declared stratum.
# - Aggregate source metadata is suitable for distribution calibration, not
#   person identification or deployment claims.
# - MolmoWeb timestamps, when present, are a schema audit finding only; no
#   timestamp value or derived timing feature is retained.

# %%
assert captcha["status"] == "complete"
assert captcha["sample_count"] == captcha["required_sample_count"] == 5_000
assert captcha["sample_touchscreen_row_count"] == 0
assert aalto["status"] == "complete"
assert aalto["sample_count"] == aalto["required_sample_count"] == 5_000
assert molmoweb["status"].startswith("complete")
assert molmoweb["human_sample_count"] == 10_000
assert molmoweb["synthetic_sample_count"] == 10_000
assert molmoweb["separation_guards"]["combined_with_fp_agent"] is False
assert molmoweb["separation_guards"]["fed_to_23_feature_timing_model"] is False
assert summary["hard_boundaries"]["headline_cross_dataset_classifier"] is False

# %% [markdown]
# ## Data
#
# Only aggregate metrics below are loaded. No source row, participant ID,
# screenshot, coordinate stream, keystroke event, instruction, or timestamp
# value is present in this notebook.

# %%
data_overview = {
    "CaptchaSolve30k": {
        "eligible": captcha["eligible_mouse_row_count"],
        "sample": captcha["sample_count"],
    },
    "Aalto 136M": {
        "eligible": aalto["eligible_physical_keyboard_participant_count"],
        "sample": aalto["sample_count"],
    },
    "MolmoWeb": {
        "human_sample": molmoweb["human_sample_count"],
        "synthetic_sample": molmoweb["synthetic_sample_count"],
        "unique_domains": molmoweb["domain_held_out_analysis"]["unique_domains"],
    },
}
print(json.dumps(data_overview, indent=2, sort_keys=True))

# %% [markdown]
# ## Results

# The first panel shows sample/population median ratios; values near 1 indicate
# that the fixed sample preserves the eligible population's center. The second
# panel shows MolmoWeb held-out-domain balanced accuracy for ordered actions and
# the domain-only baseline.


# %%
def median_ratio(distribution: dict, metric: str) -> float:
    comparison = distribution[metric]
    population = comparison["population"]["quantiles"]["p50"]
    sample = comparison["sample"]["quantiles"]["p50"]
    if population in (None, 0):
        raise ValueError(f"cannot form median ratio for {metric}")
    return sample / population


calibration_labels = [
    "Captcha duration",
    "Captcha samples",
    "Captcha ticks",
    "Aalto WPM",
    "Aalto IKI",
    "Aalto error rate",
]
calibration_ratios = [
    median_ratio(captcha["overall_distribution_calibration"], "duration"),
    median_ratio(captcha["overall_distribution_calibration"], "inputSampleCount"),
    median_ratio(captcha["overall_distribution_calibration"], "physicsTickCount"),
    median_ratio(aalto["distribution_calibration"], "AVG_WPM_15"),
    median_ratio(aalto["distribution_calibration"], "AVG_IKI"),
    median_ratio(aalto["distribution_calibration"], "ERROR_RATE"),
]

held_out = molmoweb["domain_held_out_analysis"]
model_labels = ["Action sequence", "Domain only"]
model_scores = [
    held_out["action_sequence_model"]["metrics"]["balanced_accuracy"],
    held_out["domain_only_baseline"]["metrics"]["balanced_accuracy"],
]

# Historical salt and metadata keep a rerun byte-identical to the archived figures.
mpl.rcParams["svg.hashsalt"] = "cu-detector-auxiliary-data-v1"
figure, axes = plt.subplots(1, 2, figsize=(11.5, 4.8), constrained_layout=True)
axes[0].barh(calibration_labels, calibration_ratios, color="#4C78A8")
axes[0].axvline(1.0, color="#333333", linewidth=1, linestyle="--")
axes[0].set_xlabel("Sample median / eligible-population median")
axes[0].set_title("Fixed samples preserve distribution centers")
axes[0].grid(axis="x", alpha=0.2)

bars = axes[1].bar(model_labels, model_scores, color=["#F58518", "#9D9DA0"])
axes[1].axhline(0.5, color="#333333", linewidth=1, linestyle="--")
axes[1].set_ylim(0, 1)
axes[1].set_ylabel("Balanced accuracy")
axes[1].set_title("MolmoWeb: domain-held-out source-label probe")
for bar, value in zip(bars, model_scores, strict=True):
    axes[1].text(bar.get_x() + bar.get_width() / 2, value + 0.02, f"{value:.3f}", ha="center")

figure.suptitle(
    "Independent auxiliary calibration tracks (not an FP-Agent cross-dataset model)",
    fontsize=12,
)
figure.savefig(
    FIGURE_PATH,
    format="svg",
    metadata={"Date": summary["retrieved_at_utc"], "Creator": "auxiliary_calibration.py"},
)
figure.savefig(
    FIGURE_PNG_PATH,
    format="png",
    dpi=180,
    metadata={"Creation Time": summary["retrieved_at_utc"], "Software": "matplotlib"},
)
plt.close(figure)

# %% [markdown]
# ## Takeaways
#
# Sample sizes and modality exclusions pass exactly. The MolmoWeb timestamp
# absence assumption is reported from the executed audit rather than inferred
# from the task specification. Any observed timestamp field remains excluded
# from both the model matrix and tracked outputs.

# %%
takeaways = {
    "captcha_exact_mouse_sample": captcha["sample_count"] == 5_000
    and captcha["sample_touchscreen_row_count"] == 0,
    "aalto_exact_participant_sample": aalto["sample_count"] == 5_000,
    "molmoweb_exact_samples": molmoweb["human_sample_count"] == 10_000
    and molmoweb["synthetic_sample_count"] == 10_000,
    "molmoweb_both_sources_timestamp_free": molmoweb["timestamp_audit"][
        "both_sources_verified_timestamp_free"
    ],
    "molmoweb_timing_model_used": molmoweb["separation_guards"]["fed_to_23_feature_timing_model"],
    "figure": FIGURE_PATH.name,
    "figure_png": FIGURE_PNG_PATH.name,
}
print(json.dumps(takeaways, indent=2, sort_keys=True))
