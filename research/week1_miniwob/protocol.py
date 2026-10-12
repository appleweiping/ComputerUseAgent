"""Validation and aggregation for the shared Week 1 attempt protocol.

This module deliberately contains no framework automation. Framework adapters
are separate processes so that Browser-Use, Skyvern, and Stagehand cannot
silently diverge in task IDs, seeds, limits, result fields, or validator use.

The benchmark manifest is frozen protocol data: its recorded paths
(``recorder``, ``experiments/week1/...``) use the source repository's layout
and are never rewritten, because the manifest hash identifies the protocol.
``research.week1_miniwob.checkout`` maps them onto this checkout.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .provider_probe import ProviderProbeError, validate_public_summary


MANIFEST_SCHEMA = "cui-week1-browsergym-manifest-v1"
ADAPTER_RESULT_SCHEMA = "cui-week1-adapter-result-v1"
ATTEMPT_RECORD_SCHEMA = "cui-week1-attempt-record-v1"
SUMMARY_SCHEMA = "cui-week1-summary-v1"
ALLOWED_STATUSES = {"success", "failure", "timeout", "error"}
PUBLIC_ERROR_CODE_PATTERN = re.compile(r"[a-z0-9_:-]{1,80}")
MAX_SECONDARY_ERROR_CODES = 16
FRAMEWORK_DEPENDENCY_ENVIRONMENTS = {
    "browser-use": {"browser-use"},
    "skyvern": {"orchestrator", "skyvern-local"},
    "stagehand": {"stagehand"},
}
FORMAL_EVIDENCE_SCOPE = "formal-full-matrix"
# Recorded (legacy) adapter path; part of the frozen protocol, see the module docstring.
FORMAL_ADAPTER_DISPATCH = "experiments/week1/adapters/dispatch.py"


class ProtocolError(ValueError):
    """Raised when a manifest or adapter result violates the protocol."""


def _provider_entitlement(
    orchestrator: Any,
    *,
    required: bool,
) -> dict[str, Any] | None:
    """Return a validated public provider certificate bound to run start."""

    if not isinstance(orchestrator, dict):
        if required:
            raise ProtocolError("attempt record is missing orchestrator provenance")
        return None
    value = orchestrator.get("provider_entitlement")
    if value is None and not required:
        return None
    started_at = orchestrator.get("started_at") if required else None
    if required and (not isinstance(started_at, str) or not started_at):
        raise ProtocolError("formal attempt is missing its provider-bound start time")
    try:
        return validate_public_summary(value, at=started_at)
    except ProviderProbeError as error:
        raise ProtocolError(f"attempt provider entitlement is invalid: {error}") from error


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def formal_adapter_argv(framework_id: str) -> list[str]:
    """Return the stable, reviewable adapter command contract for a framework.

    ``{python}`` is deliberately retained as a token in recorded provenance:
    the concrete interpreter path is host-specific and should not be confused
    with a different adapter implementation.
    """

    if framework_id not in FRAMEWORK_DEPENDENCY_ENVIRONMENTS:
        raise ProtocolError(f"unsupported formal framework {framework_id!r}")
    return ["{python}", FORMAL_ADAPTER_DISPATCH, framework_id]


def _require_token(value: Any, field: str) -> str:
    text = str(value or "")
    if not text or not re.fullmatch(r"[A-Za-z0-9_.:/-]+", text):
        raise ProtocolError(f"{field} must be a non-empty stable token")
    return text


def _valid_private_descriptor(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and isinstance(value.get("artifact_id"), str)
        and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", value["artifact_id"])
        and value.get("retained_private") is True
        and isinstance(value.get("bytes"), int)
        and value["bytes"] >= 0
        and isinstance(value.get("sha256"), str)
        and re.fullmatch(r"[a-f0-9]{64}", value["sha256"])
    )


def _require_boolean_claim(result: dict[str, Any], field: str) -> bool:
    value = result.get(field)
    if not isinstance(value, bool):
        raise ProtocolError(f"{field} must be an explicit boolean")
    return value


def _validate_public_error(value: Any, status: str) -> None:
    """Validate the message-free public failure classification contract."""

    if value is None:
        if status in {"error", "timeout"}:
            raise ProtocolError(f"{status} results require a public-safe error code")
        return
    if status not in {"error", "timeout"}:
        raise ProtocolError("only error/timeout results may contain an error classification")
    if not isinstance(value, dict) or set(value).difference({"code", "secondary_codes"}):
        raise ProtocolError("error must contain only code and optional secondary_codes")
    code = value.get("code")
    if not isinstance(code, str) or PUBLIC_ERROR_CODE_PATTERN.fullmatch(code) is None:
        raise ProtocolError("error.code must be a public-safe stable token")
    secondary = value.get("secondary_codes", [])
    if not isinstance(secondary, list) or len(secondary) > MAX_SECONDARY_ERROR_CODES:
        raise ProtocolError("error.secondary_codes must be a bounded list")
    if any(
        not isinstance(item, str) or PUBLIC_ERROR_CODE_PATTERN.fullmatch(item) is None
        for item in secondary
    ):
        raise ProtocolError("error.secondary_codes must contain public-safe stable tokens")
    if len(set(secondary)) != len(secondary) or code in secondary:
        raise ProtocolError("error.secondary_codes must be unique and exclude the primary code")


def _valid_trace_validation(value: Any, event_count: int) -> bool:
    required_true = (
        "schema_valid",
        "event_count_consistent",
        "event_ids_unique",
        "timestamps_monotonic",
        "privacy_safe",
        "page_scope_valid",
    )
    if not isinstance(value, dict) or any(value.get(key) is not True for key in required_true):
        return False
    natural_count = value.get("natural_non_recorder_event_count")
    event_type_count = value.get("event_type_count")
    return bool(
        isinstance(natural_count, int)
        and not isinstance(natural_count, bool)
        and 0 <= natural_count <= event_count
        and isinstance(event_type_count, int)
        and not isinstance(event_type_count, bool)
        and 1 <= event_type_count <= event_count
    )


def _valid_screenshot_validation(value: Any, viewport: dict[str, Any]) -> bool:
    required_true = (
        "png_header_valid",
        "png_complete",
        "chunk_crc_valid",
        "image_data_decoded",
    )
    return bool(
        isinstance(value, dict)
        and all(value.get(key) is True for key in required_true)
        and isinstance(value.get("chunk_count"), int)
        and not isinstance(value.get("chunk_count"), bool)
        and value["chunk_count"] >= 3
        and isinstance(value.get("width"), int)
        and not isinstance(value.get("width"), bool)
        and isinstance(value.get("height"), int)
        and not isinstance(value.get("height"), bool)
        and value["width"] == viewport.get("width")
        and value["height"] == viewport.get("height")
    )


def _valid_environment_guard(value: Any, browser: dict[str, Any]) -> bool:
    expected_locale = browser.get("locale")
    expected_timezone = browser.get("timezone")
    return bool(
        isinstance(value, dict)
        and value.get("verified") is True
        and value.get("requested_locale") == expected_locale
        and value.get("observed_locale") == expected_locale
        and value.get("requested_timezone") == expected_timezone
        and value.get("observed_timezone") == expected_timezone
        and value.get("capture_stability_verified") is True
        and value.get("capture_stability_switch_count") == 3
        and not isinstance(value.get("capture_stability_switch_count"), bool)
        and value.get("native_window_occlusion_disabled") is True
    )


def _valid_episode_guard(value: Any) -> bool:
    required_true = (
        "stable",
        "sticky_lifecycle_monitoring",
        "pre_validator_attested",
        "post_validator_attested",
        "clean_disarm_attested",
        "target_id_unchanged",
        "loader_id_unchanged",
        "document_token_unchanged",
        "time_origin_unchanged",
        "episode_start_unchanged",
    )
    return bool(
        isinstance(value, dict)
        and value.get("violation_count") == 0
        and not isinstance(value.get("violation_count"), bool)
        and isinstance(value.get("attestation_count"), int)
        and not isinstance(value.get("attestation_count"), bool)
        and value["attestation_count"] >= 2
        and all(value.get(key) is True for key in required_true)
    )


def _validate_dependency_snapshots(value: Any, framework: str, *, complete: bool) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("snapshots"), list):
        raise ProtocolError("dependencies.snapshots must be an explicit list")
    snapshots = value["snapshots"]
    expected_environments = FRAMEWORK_DEPENDENCY_ENVIRONMENTS[framework]
    observed_environments: list[str] = []
    for row in snapshots:
        if not isinstance(row, dict):
            raise ProtocolError("each dependency snapshot must be an object")
        environment = row.get("environment")
        if (
            not isinstance(environment, str)
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", environment)
            or environment not in expected_environments
        ):
            raise ProtocolError("dependency snapshot environment is not valid for this framework")
        artifact = row.get("artifact")
        if (
            not _valid_private_descriptor(artifact)
            or not re.fullmatch(
                r"private-dependency-freeze-[a-f0-9]{16}",
                artifact.get("artifact_id", ""),
            )
            or artifact.get("bytes", 0) < 1
        ):
            raise ProtocolError("dependency snapshot requires a private-safe artifact descriptor")
        observed_environments.append(environment)
    if len(observed_environments) != len(set(observed_environments)):
        raise ProtocolError("dependency snapshot environments must be unique")
    if complete and set(observed_environments) != expected_environments:
        raise ProtocolError(
            "complete artifact bundles require the exact framework dependency snapshot set"
        )


def _valid_source_provenance(value: Any, manifest_sha256: str, *, require_clean: bool) -> bool:
    return bool(
        isinstance(value, dict)
        and isinstance(value.get("commit"), str)
        and re.fullmatch(r"[a-f0-9]{40}", value["commit"])
        and isinstance(value.get("clean"), bool)
        and (not require_clean or value["clean"] is True)
        and value.get("manifest_sha256") == manifest_sha256
        and re.fullmatch(r"[a-f0-9]{64}", str(value.get("manifest_sha256", "")))
    )


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProtocolError(f"could not read manifest {path}: {error}") from error
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: dict[str, Any]) -> None:
    if not isinstance(manifest, dict):
        raise ProtocolError("manifest must be a JSON object")
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise ProtocolError(f"manifest schema_version must be {MANIFEST_SCHEMA}")
    _require_token(manifest.get("protocol_version"), "protocol_version")
    if manifest.get("model") != "gpt-5.5":
        raise ProtocolError("formal Week 1 manifest must use the single pinned model gpt-5.5")
    model_configuration = manifest.get("model_configuration")
    if not isinstance(model_configuration, dict):
        raise ProtocolError("formal framework model configuration must be explicit")
    browser_use_model = model_configuration.get("browser-use")
    skyvern_model = model_configuration.get("skyvern")
    stagehand_model = model_configuration.get("stagehand")
    if (
        not isinstance(model_configuration.get("task_seed_policy"), str)
        or not isinstance(model_configuration.get("comparison_boundary"), str)
        or browser_use_model != {"temperature": 0, "model_seed": "attempt_seed"}
        or not isinstance(skyvern_model, dict)
        or skyvern_model.get("temperature") != 1
        or skyvern_model.get("model_seed") is not None
        or skyvern_model.get("max_completion_tokens") != 8192
        or not isinstance(skyvern_model.get("exception"), str)
        or not isinstance(stagehand_model, dict)
        or stagehand_model.get("requested_temperature") != 0
        or stagehand_model.get("temperature") is not None
        or stagehand_model.get("model_seed") is not None
        or not isinstance(stagehand_model.get("exception"), str)
    ):
        raise ProtocolError("formal framework parameter support and exceptions must be explicit")
    benchmark = manifest.get("benchmark")
    if (
        not isinstance(benchmark, dict)
        or benchmark.get("miniwob_commit") != "7fd85d71a4b60325c6585396ec4f48377d049838"
        or benchmark.get("miniwob_html_tree_sha256")
        != "b6541e36c70d6621294edfe0ca3bae7794fb9cfb9def2829dfcda4fd284b3925"
        or benchmark.get("miniwob_static_tree_sha256")
        != "9883c9117804c0865920b3313021a0bd0cccc094f2f707678f6c3a72b3af4f3a"
    ):
        raise ProtocolError(
            "formal benchmark must pin the approved MiniWoB commit, task tree, and static asset tree"
        )
    repetitions = manifest.get("repetitions")
    seeds = manifest.get("seeds")
    if repetitions != 3 or not isinstance(seeds, list) or len(seeds) != repetitions:
        raise ProtocolError("manifest must define three repetitions and three seeds")
    if len(set(seeds)) != len(seeds) or not all(isinstance(seed, int) for seed in seeds):
        raise ProtocolError("seeds must be unique integers")
    limits = manifest.get("limits")
    if not isinstance(limits, dict) or limits.get("max_actions") != 30 or limits.get("timeout_seconds") != 480:
        raise ProtocolError("formal limits must be 30 actions and 480 seconds")
    tasks = manifest.get("tasks")
    frameworks = manifest.get("frameworks")
    if not isinstance(tasks, list) or len(tasks) != 3:
        raise ProtocolError("manifest must define exactly three tasks")
    if not isinstance(frameworks, list) or len(frameworks) != 3:
        raise ProtocolError("manifest must define exactly three frameworks")
    expected_tasks = {
        "browsergym/miniwob.click-checkboxes",
        "browsergym/miniwob.enter-text",
        "browsergym/miniwob.choose-list",
    }
    task_ids = {_require_token(row.get("environment_id"), "task.environment_id") for row in tasks}
    if task_ids != expected_tasks:
        raise ProtocolError(f"task environment IDs must be {sorted(expected_tasks)}")
    expected_frameworks = {"browser-use", "skyvern", "stagehand"}
    framework_ids = {_require_token(row.get("id"), "framework.id") for row in frameworks}
    if framework_ids != expected_frameworks:
        raise ProtocolError(f"framework IDs must be {sorted(expected_frameworks)}")
    for framework in frameworks:
        _require_token(framework.get("version"), f"{framework.get('id')}.version")
        security = framework.get("formal_security")
        if not isinstance(security, dict) or not isinstance(security.get("allowed"), bool):
            raise ProtocolError(f"{framework.get('id')}.formal_security must declare allowed")
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", str(security.get("reviewed_at", ""))):
            raise ProtocolError(f"{framework.get('id')}.formal_security reviewed_at is invalid")
        advisories = security.get("blocking_advisories")
        if not isinstance(advisories, list) or not all(
            isinstance(item, str) and re.fullmatch(r"GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}", item)
            for item in advisories
        ):
            raise ProtocolError(f"{framework.get('id')}.formal_security advisories are invalid")
        if security["allowed"] and advisories:
            raise ProtocolError(f"{framework.get('id')} cannot be security-allowed with blockers")
        if not security["allowed"] and (not advisories or not isinstance(security.get("reason"), str)):
            raise ProtocolError(f"{framework.get('id')} blocked security gate requires advisories and reason")
        env_name = _require_token(framework.get("adapter_command_env"), "adapter_command_env")
        if not env_name.startswith("CUI_WEEK1_") or not env_name.endswith("_ADAPTER"):
            raise ProtocolError("adapter command variables must use CUI_WEEK1_*_ADAPTER")
        default_command = framework.get("default_adapter_command")
        if (
            not isinstance(default_command, list)
            or not default_command
            or not all(isinstance(item, str) and item for item in default_command)
        ):
            raise ProtocolError(f"{framework.get('id')}.default_adapter_command must be an argv array")
        expected_command = formal_adapter_argv(framework["id"])
        if default_command != expected_command:
            raise ProtocolError(
                f"{framework.get('id')}.default_adapter_command must be exactly {expected_command!r}"
            )
    recorder = manifest.get("recorder")
    if not isinstance(recorder, dict) or recorder.get("required") is not True:
        raise ProtocolError("the real recorder extension must be required")
    browser = manifest.get("browser")
    if (
        not isinstance(browser, dict)
        or browser.get("engine") != "Google Chrome"
        or not isinstance(browser.get("product_version"), str)
        or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", browser["product_version"])
    ):
        raise ProtocolError("formal browser must pin an exact four-part Google Chrome product version")


def build_matrix(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    validate_manifest(manifest)
    rows: list[dict[str, Any]] = []
    manifest_hash = sha256_json(manifest)
    for framework in manifest["frameworks"]:
        for task in manifest["tasks"]:
            for repetition, seed in enumerate(manifest["seeds"], start=1):
                attempt_id = f"{framework['id']}__{task['id']}__r{repetition:02d}__s{seed}"
                rows.append(
                    {
                        "attempt_id": attempt_id,
                        "manifest_sha256": manifest_hash,
                        "protocol_version": manifest["protocol_version"],
                        "framework": {
                            "id": framework["id"],
                            "version": framework["version"],
                            "adapter_command_env": framework["adapter_command_env"],
                            "default_adapter_command": list(framework["default_adapter_command"]),
                        },
                        "task": {
                            "id": task["id"],
                            "environment_id": task["environment_id"],
                        },
                        "repetition": repetition,
                        "seed": seed,
                        "model": manifest["model"],
                        "model_configuration": dict(manifest["model_configuration"]),
                        "limits": dict(manifest["limits"]),
                        "browser": dict(manifest["browser"]),
                        "recorder": dict(manifest["recorder"]),
                        "benchmark": dict(manifest["benchmark"]),
                    }
                )
    if len(rows) != 27 or len({row["attempt_id"] for row in rows}) != 27:
        raise ProtocolError("formal matrix must contain 27 unique attempts")
    return rows


def validate_adapter_result(
    result: dict[str, Any],
    attempt: dict[str, Any],
    *,
    evidence_scope: str = FORMAL_EVIDENCE_SCOPE,
) -> None:
    if not isinstance(result, dict):
        raise ProtocolError("adapter stdout must be one JSON object")
    if result.get("schema_version") != ADAPTER_RESULT_SCHEMA:
        raise ProtocolError(f"adapter schema_version must be {ADAPTER_RESULT_SCHEMA}")
    if result.get("attempt_id") != attempt["attempt_id"]:
        raise ProtocolError("adapter attempt_id does not match the requested attempt")
    status = result.get("status")
    if status not in ALLOWED_STATUSES:
        raise ProtocolError(f"adapter status must be one of {sorted(ALLOWED_STATUSES)}")
    _validate_public_error(result.get("error"), status)
    agent = result.get("agent")
    if not isinstance(agent, dict):
        raise ProtocolError("adapter result is missing agent provenance")
    expected_framework = attempt["framework"]
    expected = {
        "framework": expected_framework["id"],
        "framework_version": expected_framework["version"],
        "model": attempt["model"],
    }
    for key, value in expected.items():
        if agent.get(key) != value:
            raise ProtocolError(f"agent.{key} must equal {value!r}")
    action_count = agent.get("action_count")
    if not isinstance(action_count, int) or action_count < 0 or action_count > attempt["limits"]["max_actions"]:
        raise ProtocolError("agent.action_count is outside the formal action limit")
    validator = result.get("validator")
    if not isinstance(validator, dict) or validator.get("source") != "browsergym_official_reward":
        raise ProtocolError("result must use the BrowserGym official reward")
    reward = validator.get("reward")
    if reward is not None and (not isinstance(reward, (int, float)) or isinstance(reward, bool) or not 0 <= reward <= 1):
        raise ProtocolError("validator.reward must be null or a number in [0, 1]")
    if not isinstance(validator.get("terminated"), bool) or not isinstance(validator.get("truncated"), bool):
        raise ProtocolError("validator terminal flags must be booleans")
    task_setup_descriptor = validator.get("task_setup_artifact")
    task_info_descriptor = validator.get("task_info_artifact")
    if task_setup_descriptor is not None and not _valid_private_descriptor(task_setup_descriptor):
        raise ProtocolError("validator.task_setup_artifact is not a private-safe descriptor")
    if task_info_descriptor is not None and not _valid_private_descriptor(task_info_descriptor):
        raise ProtocolError("validator.task_info_artifact is not a private-safe descriptor")
    setup_retained_private_claim = validator.get("task_setup_retained_private")
    retained_private_claim = validator.get("task_info_retained_private")
    if setup_retained_private_claim is not None and not isinstance(setup_retained_private_claim, bool):
        raise ProtocolError("validator.task_setup_retained_private must be a boolean when present")
    if retained_private_claim is not None and not isinstance(retained_private_claim, bool):
        raise ProtocolError("validator.task_info_retained_private must be a boolean when present")
    if setup_retained_private_claim is True and not _valid_private_descriptor(task_setup_descriptor):
        raise ProtocolError("task-setup retention requires a private-safe artifact descriptor")
    if retained_private_claim is True and not _valid_private_descriptor(task_info_descriptor):
        raise ProtocolError("task-info retention requires a private-safe artifact descriptor")
    if status == "success" and reward != 1:
        raise ProtocolError("success requires official reward 1")
    if status == "success" and not (validator["terminated"] or validator["truncated"]):
        raise ProtocolError("success requires a terminal BrowserGym episode")
    recorder = result.get("recorder")
    if not isinstance(recorder, dict):
        raise ProtocolError("adapter result is missing recorder evidence")
    if recorder.get("capture_adapter") != "browser_mv3":
        raise ProtocolError("formal browser attempts must use capture_adapter=browser_mv3")
    if recorder.get("source_class") != "cui_agent":
        raise ProtocolError("formal browser attempts must use source_class=cui_agent")
    event_count = recorder.get("event_count", 0)
    if not isinstance(event_count, int) or isinstance(event_count, bool) or event_count < 0:
        raise ProtocolError("recorder.event_count must be a non-negative integer")
    if status == "success" and recorder.get("status") != "complete":
        raise ProtocolError("successful task attempts require a complete recorder session")
    if status == "success" and not _require_token(recorder.get("session_id"), "recorder.session_id"):
        raise ProtocolError("successful task attempts require a recorder session ID")
    if "trace_path" in recorder:
        raise ProtocolError("public adapter results must not contain recorder.trace_path")
    provenance_complete = _require_boolean_claim(result, "provenance_complete")
    artifact_bundle_complete = _require_boolean_claim(result, "artifact_bundle_complete")
    formal_matrix_eligible = _require_boolean_claim(result, "formal_matrix_eligible")
    analysis_eligible = _require_boolean_claim(result, "analysis_eligible")
    _validate_dependency_snapshots(
        result.get("dependencies"),
        expected_framework["id"],
        complete=artifact_bundle_complete,
    )
    if artifact_bundle_complete and not provenance_complete:
        raise ProtocolError("artifact_bundle_complete requires complete provenance")
    if artifact_bundle_complete and not (
        _valid_private_descriptor(task_setup_descriptor)
        and _valid_private_descriptor(task_info_descriptor)
    ):
        raise ProtocolError("artifact_bundle_complete requires private task-setup and validator-info artifacts")
    if formal_matrix_eligible and not artifact_bundle_complete:
        raise ProtocolError("formal_matrix_eligible requires a complete artifact bundle")
    if analysis_eligible and not formal_matrix_eligible:
        raise ProtocolError("analysis_eligible requires formal matrix eligibility")
    if evidence_scope != FORMAL_EVIDENCE_SCOPE and (formal_matrix_eligible or analysis_eligible):
        raise ProtocolError("non-formal evidence must not claim formal or analysis eligibility")
    # Formal-matrix eligibility means that the planned attempt has a complete,
    # immutable, protocol-valid bundle.  It does *not* mean that the task or
    # framework succeeded.  Complete failures, provider errors, and timeouts
    # must remain in the 27-attempt denominator; analysis eligibility is the
    # narrower behavioral-data gate.
    if status == "success" and not provenance_complete:
        raise ProtocolError("success requires complete recorder and artifact provenance")
    if status == "success" and not artifact_bundle_complete:
        raise ProtocolError("success requires a complete artifact bundle")
    if status == "success" and evidence_scope == FORMAL_EVIDENCE_SCOPE and not formal_matrix_eligible:
        raise ProtocolError("success requires complete formal-matrix eligibility")
    if provenance_complete and not _valid_private_descriptor(recorder.get("trace_artifact")):
        raise ProtocolError("complete provenance requires a private-safe trace artifact descriptor")
    if provenance_complete and (
        event_count < 1
        or not _valid_trace_validation(recorder.get("artifact_validation"), event_count)
    ):
        raise ProtocolError("complete provenance requires validated, private-safe recorder evidence")
    artifacts = result.get("artifacts")
    if provenance_complete and (
        not isinstance(artifacts, dict)
        or not _valid_private_descriptor(artifacts.get("final_screenshot"))
    ):
        raise ProtocolError("complete provenance requires a private-safe screenshot artifact descriptor")
    browser_result = result.get("browser")
    expected_product = f"Chrome/{attempt['browser']['product_version']}"
    if provenance_complete and (
        not isinstance(browser_result, dict)
        or browser_result.get("product_version") != expected_product
        or browser_result.get("viewport") != attempt["browser"]["viewport"]
        or not isinstance(browser_result.get("device_pixel_ratio"), (int, float))
        or isinstance(browser_result.get("device_pixel_ratio"), bool)
        or browser_result.get("device_pixel_ratio") != 1
        or browser_result.get("locale") != attempt["browser"].get("locale")
        or browser_result.get("timezone") != attempt["browser"].get("timezone")
        or not _valid_environment_guard(browser_result.get("environment_guard"), attempt["browser"])
        or not _valid_episode_guard(browser_result.get("episode_guard"))
        or browser_result.get("fresh_profile") is not True
    ):
        raise ProtocolError(
            "complete provenance requires the pinned Chrome/environment and a stable guarded episode"
        )
    if provenance_complete and not _valid_screenshot_validation(
        artifacts.get("final_screenshot_validation"), browser_result["viewport"]
    ):
        raise ProtocolError("complete provenance requires a dimension-validated PNG screenshot")
    if analysis_eligible and recorder["artifact_validation"].get("natural_non_recorder_event_count", 0) < 1:
        raise ProtocolError("analysis eligibility requires at least one trusted natural agent event")


def validate_attempt_record(
    record: Any,
    expected_attempt: dict[str, Any],
    *,
    evidence_scope: str,
) -> dict[str, Any]:
    """Validate one immutable public attempt record against its exact cell.

    This is the resume gate.  It deliberately validates the full embedded
    attempt payload, source provenance, canonical adapter dispatch, and result
    contract before a caller may skip an existing attempt.  Artifact-bundle
    completeness is a separate caller decision because a contract-valid
    interrupted attempt remains evidence but is not resumable as "complete".
    """

    if not isinstance(record, dict) or record.get("schema_version") != ATTEMPT_RECORD_SCHEMA:
        raise ProtocolError("attempt record schema is invalid")
    if record.get("evidence_scope") != evidence_scope:
        raise ProtocolError("attempt record evidence scope does not match the ledger")
    if record.get("attempt") != expected_attempt:
        raise ProtocolError("attempt record payload does not match the requested attempt")
    orchestrator = record.get("orchestrator")
    if not isinstance(orchestrator, dict):
        raise ProtocolError("attempt record is missing orchestrator provenance")
    is_formal = evidence_scope == FORMAL_EVIDENCE_SCOPE
    if not _valid_source_provenance(
        orchestrator.get("source_provenance"),
        expected_attempt["manifest_sha256"],
        require_clean=is_formal,
    ):
        raise ProtocolError("attempt record source provenance is invalid")
    if is_formal:
        if orchestrator.get("adapter_command_source") != "repository-default":
            raise ProtocolError("formal attempt record did not use the repository adapter")
        expected_argv = formal_adapter_argv(expected_attempt["framework"]["id"])
        if orchestrator.get("adapter_argv") != expected_argv:
            raise ProtocolError("formal attempt record adapter argv is not canonical")
    _provider_entitlement(orchestrator, required=is_formal)
    if not _valid_private_descriptor(orchestrator.get("stderr_artifact")):
        raise ProtocolError("attempt record private stderr descriptor is invalid")
    validate_adapter_result(
        record.get("result"), expected_attempt, evidence_scope=evidence_scope
    )
    return record


def aggregate_records(records: Iterable[dict[str, Any]], planned: Iterable[dict[str, Any]]) -> dict[str, Any]:
    planned_rows = list(planned)
    planned_by_id = {row["attempt_id"]: row for row in planned_rows}
    expected = set(planned_by_id)
    rows = list(records)

    def record_attempt_id(row: Any) -> str | None:
        if not isinstance(row, dict) or not isinstance(row.get("attempt"), dict):
            return None
        value = row["attempt"].get("attempt_id")
        return value if isinstance(value, str) and value else None

    ids = [record_attempt_id(row) for row in rows]
    id_counts = Counter(value for value in ids if value is not None)
    duplicate_ids = sorted(value for value, count in id_counts.items() if count > 1)
    unknown_ids = sorted(value for value in id_counts if value not in expected)
    present = {value for value in id_counts if value in expected}
    missing = sorted(expected - present)
    expected_scope = "formal-full-matrix" if len(planned_rows) == 27 else "partial-debug-evidence"
    malformed_records: list[str] = []
    payload_mismatches: list[str] = []
    manifest_hash_mismatches: list[str] = []
    scope_mismatches: list[str] = []
    protocol_invalid: list[str] = []
    source_provenance_invalid: list[str] = []
    adapter_source_invalid: list[str] = []
    adapter_argv_invalid: list[str] = []
    provider_entitlement_invalid: list[str] = []
    provider_entitlements: list[dict[str, Any]] = []
    valid_unique_rows: list[dict[str, Any]] = []
    result_statuses: Counter[str] = Counter()

    for index, row in enumerate(rows, start=1):
        attempt_id = record_attempt_id(row)
        label = attempt_id or f"record-index-{index:03d}"
        record_valid = True
        if attempt_id is None:
            malformed_records.append(label)
            record_valid = False
        if not isinstance(row, dict) or row.get("schema_version") != ATTEMPT_RECORD_SCHEMA:
            malformed_records.append(label)
            record_valid = False
        if not isinstance(row, dict) or row.get("evidence_scope") != expected_scope:
            scope_mismatches.append(label)
            record_valid = False
        expected_attempt = planned_by_id.get(attempt_id or "")
        attempt_payload = row.get("attempt") if isinstance(row, dict) else None
        if expected_attempt is not None and attempt_payload != expected_attempt:
            payload_mismatches.append(label)
            record_valid = False
        if expected_attempt is not None and isinstance(attempt_payload, dict) and (
            attempt_payload.get("manifest_sha256") != expected_attempt.get("manifest_sha256")
        ):
            manifest_hash_mismatches.append(label)
            record_valid = False
        orchestrator = row.get("orchestrator") if isinstance(row, dict) else None
        source_provenance = (
            orchestrator.get("source_provenance") if isinstance(orchestrator, dict) else None
        )
        expected_manifest_hash = (
            expected_attempt.get("manifest_sha256") if isinstance(expected_attempt, dict) else ""
        )
        row_scope = row.get("evidence_scope") if isinstance(row, dict) else None
        if not _valid_source_provenance(
            source_provenance,
            expected_manifest_hash,
            require_clean=expected_scope == FORMAL_EVIDENCE_SCOPE,
        ):
            source_provenance_invalid.append(label)
            record_valid = False
        command_source = orchestrator.get("adapter_command_source") if isinstance(orchestrator, dict) else None
        if expected_scope == FORMAL_EVIDENCE_SCOPE and command_source != "repository-default":
            adapter_source_invalid.append(label)
            record_valid = False
        expected_argv = (
            formal_adapter_argv(expected_attempt["framework"]["id"])
            if isinstance(expected_attempt, dict)
            else None
        )
        adapter_argv = orchestrator.get("adapter_argv") if isinstance(orchestrator, dict) else None
        if expected_scope == FORMAL_EVIDENCE_SCOPE and adapter_argv != expected_argv:
            adapter_argv_invalid.append(label)
            record_valid = False
        try:
            provider = _provider_entitlement(
                orchestrator,
                required=expected_scope == FORMAL_EVIDENCE_SCOPE,
            )
        except ProtocolError:
            provider_entitlement_invalid.append(label)
            record_valid = False
        else:
            if provider is not None:
                provider_entitlements.append(provider)
        result = row.get("result") if isinstance(row, dict) else None
        if expected_attempt is not None:
            try:
                validate_adapter_result(result, expected_attempt, evidence_scope=str(row_scope))
            except ProtocolError:
                protocol_invalid.append(label)
                record_valid = False
        else:
            record_valid = False
        if attempt_id in expected:
            status_value = result.get("status") if isinstance(result, dict) else None
            result_statuses[status_value if status_value in ALLOWED_STATUSES else "invalid"] += 1
        if record_valid and attempt_id is not None and id_counts[attempt_id] == 1:
            valid_unique_rows.append(row)

    complete = not missing and not duplicate_ids and not unknown_ids and len(rows) == len(planned_rows)
    release_contract_valid = not any(
        (
            malformed_records,
            payload_mismatches,
            manifest_hash_mismatches,
            scope_mismatches,
            protocol_invalid,
            source_provenance_invalid,
            adapter_source_invalid,
            adapter_argv_invalid,
            provider_entitlement_invalid,
            duplicate_ids,
            unknown_ids,
        )
    )
    artifact_bundle_count = sum(
        row["result"].get("artifact_bundle_complete") is True for row in valid_unique_rows
    )
    def has_formal_execution_contract(row: dict[str, Any]) -> bool:
        """A claimed eligibility only counts when its record is formal evidence.

        This deliberately repeats the scope, clean-source, repository-default
        and canonical-argv checks rather than relying on a caller selecting a
        27-row matrix.  A partial/debug record may be useful evidence, but it
        can never inflate a formal-matrix aggregate.
        """

        attempt = row.get("attempt")
        orchestrator = row.get("orchestrator")
        if not isinstance(attempt, dict) or not isinstance(orchestrator, dict):
            return False
        framework = attempt.get("framework")
        if not isinstance(framework, dict):
            return False
        framework_id = framework.get("id")
        if not isinstance(framework_id, str):
            return False
        try:
            provider_valid = _provider_entitlement(orchestrator, required=True) is not None
        except ProtocolError:
            provider_valid = False
        return bool(
            row.get("evidence_scope") == FORMAL_EVIDENCE_SCOPE
            and _valid_source_provenance(
                orchestrator.get("source_provenance"),
                attempt.get("manifest_sha256", ""),
                require_clean=True,
            )
            and orchestrator.get("adapter_command_source") == "repository-default"
            and orchestrator.get("adapter_argv") == formal_adapter_argv(framework_id)
            and provider_valid
        )

    formal_contract_rows = [row for row in valid_unique_rows if has_formal_execution_contract(row)]
    formal_eligible_count = sum(
        row["result"].get("formal_matrix_eligible") is True for row in formal_contract_rows
    )
    analysis_eligible_count = sum(
        row["result"].get("analysis_eligible") is True for row in formal_contract_rows
    )
    official_successes = sum(
        row["result"].get("status") == "success"
        and row["result"].get("validator", {}).get("reward") == 1
        for row in valid_unique_rows
    )
    exact_full_matrix = len(planned_rows) == 27
    source_rows = [
        row.get("orchestrator", {}).get("source_provenance")
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("orchestrator"), dict)
        and isinstance(row["orchestrator"].get("source_provenance"), dict)
    ]
    source_commits = sorted(
        {row.get("commit") for row in source_rows if isinstance(row.get("commit"), str)}
    )
    source_manifest_hashes = sorted(
        {
            row.get("manifest_sha256")
            for row in source_rows
            if isinstance(row.get("manifest_sha256"), str)
        }
    )
    source_clean_values = {row.get("clean") for row in source_rows if isinstance(row.get("clean"), bool)}
    source_provenance_consistent = bool(
        source_rows
        and not source_provenance_invalid
        and len(source_rows) == len(rows)
        and len(source_commits) == 1
        and len(source_manifest_hashes) == 1
        and len(source_clean_values) == 1
    )
    source_provenance_summary = {
        "commit": source_commits[0] if len(source_commits) == 1 else None,
        "clean": next(iter(source_clean_values)) if len(source_clean_values) == 1 else None,
        "manifest_sha256": source_manifest_hashes[0] if len(source_manifest_hashes) == 1 else None,
    }
    provider_response_models = sorted(
        {str(item["response_model"]) for item in provider_entitlements}
    )
    provider_certificate_hashes = sorted(
        {str(item["payload_sha256"]) for item in provider_entitlements}
    )
    provider_identities = {
        sha256_json(
            {
                "provider": item["provider"],
                "endpoint": item["endpoint"],
                "requested_model": item["requested_model"],
                "official_pricing": item["official_pricing"],
            }
        )
        for item in provider_entitlements
    }
    provider_required = expected_scope == FORMAL_EVIDENCE_SCOPE
    provider_entitlement_consistent: bool | None = (
        bool(
            provider_entitlements
            and len(provider_entitlements) == len(rows)
            and not provider_entitlement_invalid
            and len(provider_response_models) == 1
            and len(provider_identities) == 1
        )
        if provider_required
        else None
    )
    provider_entitlement_summary = (
        {
            "provider": provider_entitlements[0]["provider"],
            "endpoint": provider_entitlements[0]["endpoint"],
            "requested_model": provider_entitlements[0]["requested_model"],
            "response_model": (
                provider_response_models[0] if len(provider_response_models) == 1 else None
            ),
            "official_pricing": provider_entitlements[0]["official_pricing"],
            "certificate_sha256s": provider_certificate_hashes,
        }
        if provider_entitlements
        else None
    )
    release_contract_valid = bool(
        release_contract_valid
        and source_provenance_consistent
        and (provider_entitlement_consistent is True if provider_required else True)
    )
    selected_artifact_bundles_complete = bool(
        complete and release_contract_valid and artifact_bundle_count == len(planned_rows)
    )
    selected_formal_matrix_eligible = bool(
        selected_artifact_bundles_complete
        and len(formal_contract_rows) == len(planned_rows)
        and formal_eligible_count == len(planned_rows)
    )
    selected_analysis_eligible = bool(
        selected_formal_matrix_eligible and analysis_eligible_count == len(planned_rows)
    )
    operationally_complete = complete and release_contract_valid and not any(
        result_statuses.get(status, 0) for status in ("error", "timeout", "invalid")
    )
    return {
        "schema_version": SUMMARY_SCHEMA,
        "planned_attempts": len(planned_rows),
        "recorded_attempt_artifacts": len(rows),
        "matrix_artifacts_complete": complete,
        "record_contracts_valid": release_contract_valid,
        "source_provenance": source_provenance_summary,
        "source_provenance_consistent": source_provenance_consistent,
        "provider_entitlement": provider_entitlement_summary,
        "provider_entitlement_consistent": provider_entitlement_consistent,
        "artifact_bundle_complete_attempts": artifact_bundle_count,
        "formal_matrix_eligible_attempts": formal_eligible_count,
        "analysis_eligible_attempts": analysis_eligible_count,
        "selected_artifact_bundles_complete": selected_artifact_bundles_complete,
        "selected_formal_matrix_eligible": selected_formal_matrix_eligible,
        "selected_analysis_eligible": selected_analysis_eligible,
        "all_27_artifact_bundles_complete": exact_full_matrix and selected_artifact_bundles_complete,
        "all_27_formal_matrix_eligible": exact_full_matrix and selected_formal_matrix_eligible,
        "all_27_analysis_eligible": exact_full_matrix and selected_analysis_eligible,
        "operationally_complete": operationally_complete,
        "official_reward_successes": official_successes,
        "status_counts": dict(sorted(result_statuses.items())),
        "missing_attempt_ids": missing,
        "duplicate_attempt_ids": duplicate_ids,
        "unknown_attempt_ids": unknown_ids,
        "malformed_record_ids": sorted(set(malformed_records)),
        "attempt_payload_mismatch_ids": sorted(set(payload_mismatches)),
        "manifest_hash_mismatch_ids": sorted(set(manifest_hash_mismatches)),
        "evidence_scope_mismatch_ids": sorted(set(scope_mismatches)),
        "protocol_invalid_attempt_ids": sorted(set(protocol_invalid)),
        "source_provenance_invalid_attempt_ids": sorted(set(source_provenance_invalid)),
        "adapter_source_invalid_attempt_ids": sorted(set(adapter_source_invalid)),
        "adapter_argv_invalid_attempt_ids": sorted(set(adapter_argv_invalid)),
        "provider_entitlement_invalid_attempt_ids": sorted(
            set(provider_entitlement_invalid)
        ),
        "interpretation": (
            "matrix_artifacts_complete preserves the legacy record-file presence check. "
            "The artifact-bundle, formal-eligibility, and analysis-eligibility fields are stricter. "
            "Task failures and qualified task-level timeouts remain part of the formal result."
            " Provider/model/official-pricing drift invalidates the release contract."
        ),
    }
