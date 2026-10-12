"""Plan or execute the matched 27-session ScienceBoard protocol.

Dry-run is fully functional without VMware, provider credentials, the private
storage root or the archived release capsule, and never writes data: it loads
the hash-bound task manifest, checks it against the formal-study config matrix,
prints the deterministic 27-cell schedule and the read-only preflight report.

A formal run is gated by a sealed formal-study binding, the read-only
preflight, an explicit confirmation flag, and a private shell-free driver
configuration. Raw sessions are written only under ``CUA_PRIVATE_ROOT``.
Failures and missing artifacts are retained as session outcomes instead of
being silently retried or replaced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import secrets
import shutil
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from computer_use_agent.capture.x11.bundle import build as build_guest_bundle
from computer_use_agent.study.ledger import (
    LedgerError,
    atomic_write_json,
    claim,
    initialize_or_resume,
    ledger_payload,
)
from computer_use_agent.study.policy import (
    FormalStudyError,
    load_binding,
    require_human_research_use,
)
from research.scienceboard import PROTOCOL_VERSION
from research.scienceboard.artifacts import (
    EvidenceValidationError,
    validate_session_evidence,
    verify_stored_artifact_hashes,
)
from research.scienceboard.preflight import DEFAULT_MANIFEST, run_preflight
from research.scienceboard.private_storage import (
    PrivateStorageError,
    default_private_path,
    require_private_path,
)
from research.scienceboard.worker_contract import (
    DEFAULT_GUEST_CONTROL_ROOT,
    DEFAULT_GUEST_DATA_ROOT,
    DEPRECATED_GUEST_VARIABLES,
    GUEST_BUNDLE_NAME,
    OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL,
    worker_source_sha256,
)

ROOT = Path(__file__).resolve().parents[2]
# Research callers always pass these explicit paths to the study policy.
FORMAL_STUDY_CONFIG = ROOT / "research" / "configs" / "formal-study.json"
RELEASE_CAPSULE = ROOT / "research" / "archive" / "release-capsule-2026-08.json"
# Raw sessions default to $CUA_PRIVATE_ROOT/scienceboard/formal/<UTC timestamp>.
PRIVATE_OUTPUT_PARTS = ("scienceboard", "formal")
# The hash-bound formal config still records pre-restructure paths.
LEGACY_PATH_PREFIXES = (("experiments/scienceboard/", "research/scienceboard/"),)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json_sha256(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("ScienceBoard task manifest must be a JSON object")
    if payload.get("schema_version") != "scienceboard-task-matrix-v2":
        raise ValueError("Unexpected ScienceBoard task manifest schema")
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("Task manifest and package protocol versions differ")
    if len(payload.get("tasks", [])) != 3 or len(payload.get("actors", [])) != 3:
        raise ValueError("Formal matrix must contain exactly three tasks and three actors")
    for task in payload["tasks"]:
        task_path = path.parent / task["local_file"]
        if not task_path.is_file() or canonical_json_sha256(task_path) != task.get(
            "canonical_payload_sha256"
        ):
            raise ValueError(f"Pinned task payload integrity failure: {task.get('local_file')}")
    return dict(payload)


def build_schedule(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a deterministic, greedily interleaved matrix order."""

    cells = [
        {
            "actor": actor["id"],
            "actor_type": actor["actor_type"],
            "source_class": actor["source_class"],
            "scienceboard_community": actor["scienceboard_community"],
            "task_id": task["id"],
            "application": task["application"],
            "task_file": task["local_file"],
            "repetition": repetition,
        }
        for repetition in range(1, int(manifest["repetitions_per_cell"]) + 1)
        for task in manifest["tasks"]
        for actor in manifest["actors"]
    ]
    rng = random.Random(int(manifest["randomization_seed"]))
    rng.shuffle(cells)
    remaining = list(enumerate(cells))
    schedule: list[dict[str, Any]] = []
    while remaining:
        previous = schedule[-1] if schedule else None
        ranked = sorted(
            remaining,
            key=lambda item: (
                int(previous is not None and item[1]["actor"] == previous["actor"])
                + int(previous is not None and item[1]["task_id"] == previous["task_id"]),
                item[0],
            ),
        )
        chosen = ranked[0]
        remaining.remove(chosen)
        cell = dict(chosen[1])
        cell["order"] = len(schedule) + 1
        cell["session_id"] = (
            f"sb-{cell['application'].lower()}-a01-{cell['actor']}-r{cell['repetition']:02d}"
        )
        schedule.append(cell)
    return schedule


def filtered_schedule(
    schedule: list[dict[str, Any]],
    *,
    actor: str | None,
    task_id: str | None,
) -> list[dict[str, Any]]:
    return [
        cell
        for cell in schedule
        if (actor is None or cell["actor"] == actor)
        and (task_id is None or cell["task_id"] == task_id)
    ]


def select_session_id(
    schedule: list[dict[str, Any]], session_id: str | None
) -> list[dict[str, Any]]:
    """Select one exact session ID; prefixes and fuzzy matching are forbidden."""

    if session_id is None:
        return schedule
    matches = [cell for cell in schedule if cell["session_id"] == session_id]
    if len(matches) != 1:
        raise ValueError(f"Unknown formal ScienceBoard session_id: {session_id}")
    return matches


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvidenceValidationError(f"Cannot read {path}: {error}") from error
    if not isinstance(payload, dict):
        raise EvidenceValidationError(f"Expected one JSON object in {path}")
    return payload


def validate_resumable_session(
    session_dir: Path,
    *,
    expected: dict[str, Any],
    manifest: dict[str, Any],
    expected_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a stored session bundle before it may be skipped on resume."""

    session_dir = Path(session_dir).resolve()
    run_path = session_dir / "run_manifest.json"
    run = _read_object(run_path)
    exact_fields = {
        "schema_version": "scienceboard-session-run-v2",
        "protocol_version": manifest["protocol_version"],
        "session_id": expected["session_id"],
        "task_id": expected["task_id"],
        "actor": expected["actor"],
        "actor_type": expected["actor_type"],
        "source_class": expected["source_class"],
        "scienceboard_community": expected["scienceboard_community"],
        "repetition": expected["repetition"],
        "schedule_order": expected["order"],
    }
    mismatches = [key for key, value in exact_fields.items() if run.get(key) != value]
    if mismatches:
        raise EvidenceValidationError(
            "stored session identity/protocol mismatch: " + ", ".join(mismatches)
        )
    if run.get("evidence_valid") is not True:
        raise EvidenceValidationError(
            "stored session is retained incomplete evidence and cannot be skipped as complete"
        )
    if run.get("status") not in {"task_passed", "task_failed"}:
        raise EvidenceValidationError("stored session status is not a completed task outcome")
    if type(run.get("validator_passed")) is not bool:
        raise EvidenceValidationError("stored validator outcome must be boolean")
    if (run["status"] == "task_passed") != run["validator_passed"]:
        raise EvidenceValidationError("stored status and validator outcome disagree")
    provenance = run.get("source_provenance")
    if not isinstance(provenance, dict):
        raise EvidenceValidationError("stored session source provenance is missing")
    if expected_provenance is not None and provenance != expected_provenance:
        raise EvidenceValidationError(
            "stored session provenance differs from the active formal run"
        )
    verified = validate_session_evidence(session_dir, expected=expected, manifest=manifest)
    verify_stored_artifact_hashes(session_dir, run)
    if run.get("artifacts") != verified.get("artifacts"):
        raise EvidenceValidationError("stored artifact descriptors do not match revalidation")
    if (
        run.get("validator_passed") != verified.get("validator_passed")
        or run.get("trace_event_count") != verified.get("trace_event_count")
        or run.get("trace_duration_ms") != verified.get("trace_duration_ms")
        or run.get("trace_quality") != verified.get("trace_quality")
    ):
        raise EvidenceValidationError("stored evidence summary differs from revalidation")
    return run


def collect_ledger_sessions(
    output_dir: Path,
    *,
    schedule: list[dict[str, Any]],
    manifest: dict[str, Any],
    expected_provenance: dict[str, Any] | None = None,
    allowed_invalid_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Revalidate all known sessions and report missing/unknown directories."""

    by_id = {cell["session_id"]: cell for cell in schedule}
    results: list[dict[str, Any]] = []
    missing: list[str] = []
    for session_id, cell in by_id.items():
        session_dir = output_dir / session_id
        if not session_dir.is_dir():
            missing.append(session_id)
            continue
        try:
            results.append(
                validate_resumable_session(
                    session_dir,
                    expected=cell,
                    manifest=manifest,
                    expected_provenance=expected_provenance,
                )
            )
        except EvidenceValidationError as error:
            if allowed_invalid_ids is None or session_id not in allowed_invalid_ids:
                raise
            results.append(
                {
                    "schema_version": "scienceboard-session-run-v2",
                    "session_id": session_id,
                    "task_id": cell["task_id"],
                    "actor": cell["actor"],
                    "status": "incomplete",
                    "evidence_valid": False,
                    "evidence_validation_error": str(error),
                    "validator_passed": None,
                }
            )
    known_non_session = {".claims"}
    unknown = sorted(
        path.name
        for path in output_dir.iterdir()
        if path.is_dir() and path.name not in by_id and path.name not in known_non_session
    ) if output_dir.is_dir() else []
    return results, sorted(missing), unknown


def build_batch_summary(
    *,
    plan: dict[str, Any],
    output_dir: Path,
    schedule: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    results: list[dict[str, Any]],
    missing: list[str],
    unknown: list[str],
    executed_ids: list[str],
    resumed_ids: list[str],
    provenance: dict[str, Any] | None,
) -> dict[str, Any]:
    valid_count = sum(row.get("evidence_valid") is True for row in results)
    task_successes = sum(row.get("validator_passed") is True for row in results)
    task_failures = sum(row.get("validator_passed") is False for row in results)
    complete = (
        len(schedule) == 27
        and valid_count == 27
        and not missing
        and not unknown
    )
    return {
        **plan,
        "execution_status": (
            "formal_matrix_evidence_complete" if complete else "formal_ledger_incomplete"
        ),
        "writes_performed": bool(executed_ids),
        "formal_matrix_evidence_complete": complete,
        "artifact_bundle_completeness": {
            "complete": complete,
            "valid_bundles": valid_count,
            "planned_bundles": len(schedule),
            "missing_session_ids": missing,
            "unknown_session_directories": unknown,
        },
        "task_outcomes": {
            "validator_passed": task_successes,
            "validator_failed": task_failures,
            "not_yet_complete": len(schedule) - valid_count,
        },
        "selected_session_ids": [cell["session_id"] for cell in selected],
        "executed_session_ids": executed_ids,
        "resumed_session_ids": sorted(set(resumed_ids)),
        "output_dir": str(output_dir),
        "source_provenance": provenance,
        "results": results,
        "completed_at": utc_now(),
    }


def _private_output(path: Path) -> Path:
    """Resolve a raw-session location, failing closed outside ``CUA_PRIVATE_ROOT``."""

    return require_private_path(path, label="Raw ScienceBoard session output")


def default_output_root() -> Path:
    """Return ``$CUA_PRIVATE_ROOT/scienceboard/formal`` (fails closed when unset)."""

    return default_private_path(*PRIVATE_OUTPUT_PARTS)


def translate_legacy_path(relative: str) -> str:
    """Map a pre-restructure repository path recorded in hash-bound data."""

    for legacy, current in LEGACY_PATH_PREFIXES:
        if relative.startswith(legacy):
            return current + relative[len(legacy) :]
    return relative


def read_formal_config(path: Path) -> dict[str, Any]:
    """Read the formal-study config as plain JSON (no capsule, no sealing)."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FormalStudyError(f"could not read formal-study config {path}: {error}") from error
    if not isinstance(payload, dict):
        raise FormalStudyError("formal-study config must contain one JSON object")
    return payload


def config_manifest_path(config: Mapping[str, Any], config_path: Path) -> Path:
    """Return the task manifest the config binds.

    The config records the manifest relative to the config file's directory;
    older snapshots record a pre-restructure repository path, which is
    translated through :data:`LEGACY_PATH_PREFIXES`. Either way it must be the
    canonical ``research/scienceboard/tasks/manifest.json``.
    """

    section = config.get("scienceboard")
    relative = section.get("manifest") if isinstance(section, Mapping) else None
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise FormalStudyError("formal-study config does not bind a ScienceBoard manifest")
    canonical = DEFAULT_MANIFEST.resolve()
    candidates = (
        (Path(config_path).resolve().parent / relative).resolve(),
        (ROOT / translate_legacy_path(relative)).resolve(),
    )
    if canonical not in candidates:
        raise FormalStudyError(
            "formal-study config manifest does not resolve to "
            "research/scienceboard/tasks/manifest.json"
        )
    return canonical


def check_config_matrix(config: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    """Require the config's 3x3 matrix to name exactly the manifest's actors and tasks."""

    section = config.get("scienceboard")
    matrix = section.get("matrix") if isinstance(section, Mapping) else None
    if not isinstance(matrix, Mapping):
        raise FormalStudyError("formal-study config has no ScienceBoard matrix")
    actors = matrix.get("actors")
    tasks = matrix.get("tasks")
    if (
        not isinstance(actors, list)
        or not isinstance(tasks, list)
        or set(actors) != {row["id"] for row in manifest["actors"]}
        or set(tasks) != {f"{row['application']}-A-01" for row in manifest["tasks"]}
    ):
        raise FormalStudyError("formal config matrix differs from ScienceBoard manifest")


def _render_argv(template: list[str], context: dict[str, object]) -> list[str]:
    rendered = []
    for item in template:
        try:
            rendered.append(item.format_map(context))
        except KeyError as error:
            raise ValueError(f"Unknown driver placeholder {error.args[0]!r}") from error
    return rendered


def _run_command(
    name: str,
    specification: dict[str, Any],
    context: dict[str, object],
    log_path: Path,
) -> dict[str, Any]:
    argv = _render_argv(specification["argv"], context)
    started = utc_now()
    started_clock = datetime.now(timezone.utc)
    try:
        completed = subprocess.run(
            argv,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=float(specification.get("timeout_seconds", 120)),
            check=False,
            shell=False,
        )
        return_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
        error = None
    except subprocess.TimeoutExpired as timeout:
        return_code = 124
        timeout_stdout, timeout_stderr = timeout.stdout, timeout.stderr
        stdout = (
            timeout_stdout.decode("utf-8", errors="replace")
            if isinstance(timeout_stdout, bytes)
            else timeout_stdout or ""
        )
        stderr = (
            timeout_stderr.decode("utf-8", errors="replace")
            if isinstance(timeout_stderr, bytes)
            else timeout_stderr or ""
        )
        error = "timeout"
    except Exception as failure:
        return_code = 125
        stdout = ""
        stderr = type(failure).__name__
        error = "launch_failure"
    finished = utc_now()
    duration_seconds = (datetime.now(timezone.utc) - started_clock).total_seconds()
    # This log is private because upstream tools may print prompts or paths.
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"\n[{started}] phase={name}\n")
        handle.write(f"argv={json.dumps(argv, ensure_ascii=False)}\n")
        handle.write(f"return_code={return_code} duration_seconds={duration_seconds:.3f}\n")
        if stdout:
            handle.write("stdout:\n" + str(stdout) + "\n")
        if stderr:
            handle.write("stderr:\n" + str(stderr) + "\n")
    return {
        "phase": name,
        "started_at": started,
        "finished_at": finished,
        "duration_seconds": round(duration_seconds, 3),
        "return_code": return_code,
        "error": error,
    }


def formal_provenance(
    manifest_path: Path,
    manifest: dict[str, Any],
    *,
    driver_path: Path,
    preflight: dict[str, Any],
) -> dict[str, Any]:
    repository_commit = subprocess.check_output(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        stderr=subprocess.STDOUT,
        timeout=20,
    ).strip().lower()
    repository_dirty = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        stderr=subprocess.STDOUT,
        timeout=20,
    ).strip()
    if repository_dirty:
        raise EvidenceValidationError(
            "formal ScienceBoard execution requires a clean repository source commit"
        )
    scienceboard_root = Path(os.environ["SCIENCEBOARD_ROOT"]).resolve()
    commit = subprocess.check_output(
        ["git", "-C", str(scienceboard_root), "rev-parse", "HEAD"],
        text=True,
        encoding="utf-8",
        stderr=subprocess.STDOUT,
        timeout=20,
    ).strip()
    vm_certificate = json.loads(
        Path(os.environ["SCIENCEBOARD_VM_PROVENANCE"]).read_text(encoding="utf-8")
    )
    return {
        "orchestrator_repository_commit": repository_commit,
        "orchestrator_repository_clean_at_start": True,
        "scienceboard_repository": manifest["upstream"]["repository"],
        "scienceboard_commit": commit,
        "driver_config_sha256": sha256(driver_path),
        # Legacy public field name retained for publication-schema compatibility;
        # v3 content is the host API/evaluator/recorder-bundle capability report.
        "guest_capability_report_sha256": hashlib.sha256(
            json.dumps(
                preflight["guest_capability_report"],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest(),
        "guest_capability_report": preflight["guest_capability_report"],
        "task_matrix_sha256": sha256(manifest_path),
        "task_matrix_canonical_sha256": canonical_json_sha256(manifest_path),
        "vm_zip_sha256": vm_certificate["vm_zip_sha256"],
        "snapshot": manifest["snapshot"],
        "agent_model": manifest["agent_model"],
        "agent_action_limit": manifest["agent_action_limit"],
        "agent_timeout_seconds": manifest["agent_timeout_seconds"],
        "human_timeout_seconds": manifest["human_timeout_seconds"],
        "guest_binding": manifest["guest_binding"],
        "selection_status": manifest["selection_status"],
    }


def run_session(
    cell: dict[str, Any],
    *,
    manifest: dict[str, Any],
    driver: dict[str, Any],
    output_root: Path,
    vm_path: Path,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    session_dir = output_root / cell["session_id"]
    if session_dir.exists():
        raise FileExistsError(f"Refusing to overwrite an existing formal session: {session_dir}")
    session_dir.mkdir(parents=True)
    task_source = Path(__file__).resolve().parent / "tasks" / cell["task_file"]
    task_copy = session_dir / "task.json"
    shutil.copy2(task_source, task_copy)
    log_path = session_dir / "run.log"
    log_path.touch(exist_ok=False)
    task_spec = next(task for task in manifest["tasks"] if task["id"] == cell["task_id"])
    guest_data_root = os.environ.get("SCIENCEBOARD_GUEST_DATA_ROOT", DEFAULT_GUEST_DATA_ROOT)
    guest_control_root = os.environ.get(
        "SCIENCEBOARD_GUEST_CONTROL_DIR", DEFAULT_GUEST_CONTROL_ROOT
    )
    if not guest_data_root.startswith("/home/user/") or not guest_control_root.startswith(
        "/home/user/"
    ):
        raise EvidenceValidationError("ScienceBoard guest recorder paths must remain under /home/user")
    configured_deprecated = [name for name in DEPRECATED_GUEST_VARIABLES if os.environ.get(name)]
    if configured_deprecated:
        raise EvidenceValidationError(
            "Guest-side agent/validator configuration is forbidden in driver v3: "
            + ", ".join(configured_deprecated)
        )

    worker_path = Path(__file__).resolve().with_name("host_worker.py")
    # One deterministic zipapp is the whole guest deployment; its hash is bound
    # into the immutable worker spec before the worker starts.
    guest_bundle = build_guest_bundle(session_dir / "guest_bundle" / GUEST_BUNDLE_NAME)
    worker_spec_path = session_dir / "worker_spec.json"
    worker_state_path = session_dir / "worker_state.json"
    worker_nonce_path = session_dir / ".worker-nonce"
    manual_stop_path = session_dir / ".manual-stop.json"
    with worker_nonce_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(secrets.token_urlsafe(48) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    if os.name != "nt":
        os.chmod(worker_nonce_path, 0o600)
    timeout_seconds = (
        manifest["human_timeout_seconds"]
        if cell["actor"] == "human"
        else manifest["agent_timeout_seconds"]
    )
    provider_endpoint = os.environ.get(
        "SCIENCEBOARD_OPENAI_CHAT_COMPLETIONS_URL",
        OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL,
    )
    if provider_endpoint != OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL:
        raise ValueError("Formal ScienceBoard execution refuses a custom provider endpoint")
    worker_spec = {
        "schema_version": "scienceboard-host-worker-spec-v3",
        "session_id": cell["session_id"],
        "task_id": cell["task_id"],
        "task_path": str(task_copy.resolve()),
        "task_canonical_sha256": task_spec["canonical_payload_sha256"],
        "task_binding": {
            key: task_spec[key]
            for key in ("id", "application", "upstream_path", "canonical_payload_sha256")
        },
        "session_dir": str(session_dir.resolve()),
        "scienceboard_root": str(Path(os.environ["SCIENCEBOARD_ROOT"]).resolve()),
        "scienceboard_commit": manifest["upstream"]["commit"],
        "worker_source_sha256": worker_source_sha256(),
        "vm_path": str(vm_path.resolve()),
        "snapshot": manifest["snapshot"],
        "actor": cell["actor"],
        "community": cell["scienceboard_community"],
        "model": manifest["agent_model"],
        "reasoning_effort": os.environ.get("SCIENCEBOARD_REASONING_EFFORT", "medium"),
        "openai_chat_completions_url": provider_endpoint,
        "action_limit": manifest["agent_action_limit"],
        "timeout_seconds": timeout_seconds,
        "protocol_version": manifest["protocol_version"],
        "executor_version": (
            "self-human-protocol-v3"
            if cell["actor"] == "human"
            else f"scienceboard@{manifest['upstream']['commit']}"
        ),
        "guest_id": manifest["guest_binding"]["guest_id"],
        "guest_os_id": manifest["guest_binding"]["guest_os_id"],
        "guest_os_version": manifest["guest_binding"]["guest_os_version"],
        "screen_width": manifest["guest_binding"]["screen_resolution"]["width"],
        "screen_height": manifest["guest_binding"]["screen_resolution"]["height"],
        "guest_data_root": guest_data_root,
        "guest_control_root": guest_control_root,
        "guest_bundle_root": str(guest_bundle.path.parent),
        "guest_bundle_hashes": {GUEST_BUNDLE_NAME: guest_bundle.sha256},
        "manual_stop_file": str(manual_stop_path),
        "self_consent_confirmed": (
            cell["actor"] == "human"
            and os.environ.get("SCIENCEBOARD_SELF_CONSENT_SESSION_ID") == cell["session_id"]
        ),
    }
    atomic_write_json(worker_spec_path, worker_spec, replace=False)
    context: dict[str, object] = {
        "actor": cell["actor"],
        "model": manifest["agent_model"],
        "executor_version": (
            "self-human-protocol-v3"
            if cell["actor"] == "human"
            else f"scienceboard@{manifest['upstream']['commit']}"
        ),
        "action_limit": manifest["agent_action_limit"],
        "protocol_version": manifest["protocol_version"],
        "scienceboard_commit": manifest["upstream"]["commit"],
        "guest_id": manifest["guest_binding"]["guest_id"],
        "guest_os_id": manifest["guest_binding"]["guest_os_id"],
        "guest_os_version": manifest["guest_binding"]["guest_os_version"],
        "screen_width": manifest["guest_binding"]["screen_resolution"]["width"],
        "screen_height": manifest["guest_binding"]["screen_resolution"]["height"],
        "task_canonical_sha256": task_spec["canonical_payload_sha256"],
        "host_worker": str(worker_path),
        "worker_spec_file": str(worker_spec_path),
        "worker_state_file": str(worker_state_path),
        "worker_nonce_file": str(worker_nonce_path),
        "repetition": cell["repetition"],
        "session_dir": str(session_dir),
        "session_id": cell["session_id"],
        "snapshot": manifest["snapshot"],
        "task_id": cell["task_id"],
        "task_path": str(task_copy),
        "timeout_seconds": timeout_seconds,
        "vm_path": str(vm_path),
    }
    phases: list[dict[str, Any]] = []
    commands = driver["commands"]
    prepare = _run_command("prepare_session", commands["prepare_session"], context, log_path)
    phases.append(prepare)
    recorder_started = False
    if prepare["return_code"] == 0:
        initialize = _run_command("initialize", commands["initialize"], context, log_path)
        phases.append(initialize)
        if initialize["return_code"] == 0:
            start = _run_command("start_recorder", commands["start_recorder"], context, log_path)
            phases.append(start)
            recorder_started = start["return_code"] == 0
        if recorder_started:
            phases.append(
                _run_command(f"run_{cell['actor']}", commands[f"run_{cell['actor']}"], context, log_path)
            )
            phases.append(_run_command("screenshot", commands["screenshot"], context, log_path))
    if recorder_started:
        phases.append(_run_command("stop_recorder", commands["stop_recorder"], context, log_path))
        phases.append(_run_command("validate", commands["validate"], context, log_path))
    phases.append(_run_command("fetch_artifacts", commands["fetch_artifacts"], context, log_path))

    try:
        phase_codes = {phase["phase"]: phase["return_code"] for phase in phases}
        required_infrastructure = {
            "prepare_session", "initialize", "start_recorder", "screenshot",
            "stop_recorder", "validate", "fetch_artifacts",
        }
        failed_infrastructure = sorted(
            name for name in required_infrastructure if phase_codes.get(name) != 0
        )
        if failed_infrastructure:
            raise EvidenceValidationError(
                f"Infrastructure phases did not complete cleanly: {failed_infrastructure}"
            )
        evidence = validate_session_evidence(session_dir, expected=cell, manifest=manifest)
        evidence_valid = True
        evidence_error = None
        validator_passed = bool(evidence["validator_passed"])
        status = "task_passed" if validator_passed else "task_failed"
    except Exception as error:
        evidence = {"artifacts": {}}
        evidence_valid = False
        evidence_error = f"{type(error).__name__}: {error}"
        validator_passed = None
        status = "incomplete"
    result = {
        "schema_version": "scienceboard-session-run-v2",
        "protocol_version": manifest["protocol_version"],
        "session_id": cell["session_id"],
        "task_id": cell["task_id"],
        "actor": cell["actor"],
        "actor_type": cell["actor_type"],
        "source_class": cell["source_class"],
        "scienceboard_community": cell["scienceboard_community"],
        "repetition": cell["repetition"],
        "schedule_order": cell["order"],
        "task_sha256": sha256(task_copy),
        "task_canonical_sha256": canonical_json_sha256(task_copy),
        "source_provenance": provenance,
        "limits": {
            "action_limit": (
                None if cell["actor"] == "human" else manifest["agent_action_limit"]
            ),
            "action_limit_applies": cell["actor"] != "human",
            "time_limit_seconds": context["timeout_seconds"],
        },
        "status": status,
        "evidence_valid": evidence_valid,
        "evidence_validation_error": evidence_error,
        "validator_passed": validator_passed,
        "trace_event_count": evidence.get("trace_event_count"),
        "trace_duration_ms": evidence.get("trace_duration_ms"),
        "trace_quality": evidence.get("trace_quality"),
        "trace_empty": evidence.get("trace_empty"),
        "trace_missing_modalities": evidence.get("trace_missing_modalities"),
        "phases": phases,
        "artifacts": evidence["artifacts"],
        "completed_at": utc_now(),
    }
    atomic_write_json(session_dir / "run_manifest.json", result, replace=False)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=FORMAL_STUDY_CONFIG)
    parser.add_argument(
        "--capsule",
        type=Path,
        default=RELEASE_CAPSULE,
        help="release capsule for a formal run (not read by --dry-run)",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--actor", choices=("human", "aio", "seeact"))
    parser.add_argument("--task-id")
    parser.add_argument("--session-id", help="Exact canonical ScienceBoard session ID")
    parser.add_argument("--attempt-id", dest="attempt_id", help="Alias for --session-id")
    parser.add_argument(
        "--output-root",
        type=Path,
        help="private batch parent (default: CUA_PRIVATE_ROOT/scienceboard/formal)",
    )
    parser.add_argument("--output-dir", type=Path, help="Exact immutable ledger directory")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-formal-run", action="store_true")
    args = parser.parse_args(argv)
    if args.session_id and args.attempt_id and args.session_id != args.attempt_id:
        parser.error("--session-id and --attempt-id disagree")
    exact_id = args.session_id or args.attempt_id
    if exact_id and (args.actor or args.task_id):
        parser.error("exact session selection cannot be combined with actor/task filters")
    if args.resume and args.output_dir is None:
        parser.error("--resume requires an exact --output-dir")
    if args.output_dir is not None and args.output_root is not None:
        parser.error("--output-dir cannot be combined with --output-root")
    canonical_manifest = DEFAULT_MANIFEST.resolve()
    try:
        if args.manifest.resolve() != canonical_manifest:
            raise FormalStudyError(
                "formal ScienceBoard mode rejects external or copied task manifests"
            )
        config = read_formal_config(args.config)
        config_manifest_path(config, args.config)
        manifest = load_manifest(canonical_manifest)
        full_schedule = build_schedule(manifest)
        check_config_matrix(config, manifest)
        schedule = (
            select_session_id(full_schedule, exact_id)
            if exact_id
            else filtered_schedule(full_schedule, actor=args.actor, task_id=args.task_id)
        )
        if not schedule:
            raise ValueError("No sessions match the selected actor/task")
        preflight = run_preflight(canonical_manifest)
    except (FormalStudyError, ValueError, OSError) as error:
        print(json.dumps({"ready": False, "error": str(error)}, indent=2), file=sys.stderr)
        return 2
    plan: dict[str, Any] = {
        "schema_version": "scienceboard-run-plan-v1",
        "execution_status": "dry_run" if args.dry_run else "pending",
        "formal_run_ready": preflight["ready_for_formal_run"],
        "selection_status": manifest["selection_status"],
        "randomization_seed": manifest["randomization_seed"],
        "session_count": len(schedule),
        "formal_matrix_planned_sessions": len(full_schedule),
        "writes_performed": False if args.dry_run else None,
        "schedule": schedule,
        "preflight": preflight,
        "formal_study_binding": None,
    }
    if args.dry_run:
        plan["formal_study_binding"] = {
            "status": "not_evaluated_in_dry_run",
            "detail": (
                "A formal run additionally requires a sealed formal-study binding; the "
                "archived 2026-08 release capsule cannot be sealed, so formal execution "
                "stays blocked until a new capsule is produced."
            ),
        }
        print(json.dumps(plan, indent=2))
        return 0
    try:
        binding = load_binding(
            args.config,
            args.capsule,
            require_sealed=True,
            inputs_root=ROOT,
            reference_root=ROOT,
        )
    except (FormalStudyError, ValueError, OSError) as error:
        print(json.dumps({"ready": False, "error": str(error)}, indent=2), file=sys.stderr)
        return 2
    plan["formal_study_binding"] = binding.ledger_binding()
    try:
        if args.output_dir is not None:
            batch_dir = _private_output(args.output_dir)
        else:
            output_root = _private_output(
                args.output_root if args.output_root is not None else default_output_root()
            )
            batch_dir = output_root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        expected_ledger = ledger_payload(
            stage="scienceboard",
            binding=binding.ledger_binding(),
            protocol_sha256=sha256(canonical_manifest),
            planned_ids=[cell["session_id"] for cell in full_schedule],
        )
        ledger_exists = (batch_dir / "study_ledger.json").is_file()
        if args.resume and not ledger_exists and batch_dir.is_dir() and any(batch_dir.iterdir()):
            raise LedgerError(
                "resume output contains evidence but no immutable study_ledger.json"
            )
        if ledger_exists:
            initialize_or_resume(batch_dir, expected_ledger, resume=args.resume)

        resumed_ids: list[str] = []
        existing: dict[str, dict[str, Any]] = {}
        for cell in schedule:
            session_dir = batch_dir / cell["session_id"]
            if session_dir.exists():
                if not args.resume:
                    raise LedgerError(
                        "session evidence already exists; use --resume or a new --output-dir"
                    )
                existing[cell["session_id"]] = validate_resumable_session(
                    session_dir, expected=cell, manifest=manifest
                )
                resumed_ids.append(cell["session_id"])
        pending = [cell for cell in schedule if cell["session_id"] not in existing]

        if any(cell["actor"] == "human" for cell in pending):
            require_human_research_use(
                binding.config,
                use="scienceboard_formal",
                participant_scope="single_self_participant",
            )

        # A complete resume is an evidence audit; it does not need VMware or
        # provider credentials to be available again.
        if pending and not args.confirm_formal_run:
            print("Formal execution requires --confirm-formal-run", file=sys.stderr)
            return 2
        if pending and not preflight["ready_for_formal_run"]:
            print(json.dumps(preflight, indent=2), file=sys.stderr)
            return 2

        provenance: dict[str, Any] | None = None
        driver: dict[str, Any] | None = None
        vm_path: Path | None = None
        executed_ids: list[str] = []
        if pending:
            driver_path = Path(os.environ["SCIENCEBOARD_DRIVER_CONFIG"]).resolve()
            vm_path = Path(os.environ["VM_PATH"]).resolve()
            driver = json.loads(driver_path.read_text(encoding="utf-8"))
            provenance = formal_provenance(
                canonical_manifest, manifest, driver_path=driver_path, preflight=preflight
            )
            if not ledger_exists:
                initialize_or_resume(batch_dir, expected_ledger, resume=args.resume)
            for stored in existing.values():
                if stored.get("source_provenance") != provenance:
                    raise EvidenceValidationError(
                        "resume source provenance differs from existing immutable sessions"
                    )
            assert driver is not None and vm_path is not None
            for cell in pending:
                with claim(batch_dir, cell["session_id"]):
                    session_dir = batch_dir / cell["session_id"]
                    if session_dir.exists():
                        if not args.resume:
                            raise LedgerError("concurrent session evidence appeared")
                        validate_resumable_session(
                            session_dir,
                            expected=cell,
                            manifest=manifest,
                            expected_provenance=provenance,
                        )
                        resumed_ids.append(cell["session_id"])
                        continue
                    result = run_session(
                        cell,
                        manifest=manifest,
                        driver=driver,
                        output_root=batch_dir,
                        vm_path=vm_path,
                        provenance=provenance,
                    )
                    executed_ids.append(cell["session_id"])
                    # A retained incomplete bundle is never promoted to a
                    # completed ledger cell, even though run_session records it.
                    if result.get("evidence_valid") is not True:
                        break

        results, missing, unknown = collect_ledger_sessions(
            batch_dir,
            schedule=full_schedule,
            manifest=manifest,
            expected_provenance=provenance if pending else None,
            allowed_invalid_ids=set(executed_ids),
        )
        batch = build_batch_summary(
            plan=plan,
            output_dir=batch_dir,
            schedule=full_schedule,
            selected=schedule,
            results=results,
            missing=missing,
            unknown=unknown,
            executed_ids=executed_ids,
            resumed_ids=resumed_ids,
            provenance=provenance,
        )
        atomic_write_json(batch_dir / "batch_manifest.json", batch, replace=True)
        print(json.dumps(batch, indent=2))
        selected_ids = {cell["session_id"] for cell in schedule}
        selected_valid = {
            row["session_id"] for row in results if row.get("evidence_valid") is True
        }
        return 0 if selected_ids <= selected_valid else 1
    except (
        EvidenceValidationError,
        FormalStudyError,
        LedgerError,
        OSError,
        PrivateStorageError,
        ValueError,
    ) as error:
        print(json.dumps({"ready": False, "error": str(error)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
