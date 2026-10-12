"""Run one immutable Codex Desktop MiniWoB case with the real MV3 recorder.

The runner owns the benchmark, validator, Chrome profile, recorder, and
artifacts.  Codex computer control owns only the visible task interactions.
The terminal acknowledgement records a bounded action count; it does not
inject, replay, or rescue browser actions.

The nine archived cases live in ``research/evidence/codex_case_blocked`` and
are read-only: ``--results`` is required and must be a new directory inside
the repository (public artifacts are recorded by repository-relative path)
but outside ``research/evidence``.  Run from the repository root:
``python -m research.codex_case.runner``.

The runner is frozen against recorder 0.4.0, the ``recorder.version`` pinned in
``research/configs/codex-case.json``.  Every case binds the archived 0.4.0
source manifest (``research/evidence/codex_case_blocked/source_manifest.json``)
and never writes a new one, so the runner refuses to start (exit 2,
``recorder_version_mismatch``) unless ``extension/app/manifest.json`` declares
that pinned version.  The live extension is 0.5.0, so in this repository the
supported entry points are ``python -m research.codex_case.summarize --check``
and the tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import queue
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from computer_use_agent.contracts.validation import validate_contract
from research.week1_miniwob.adapters.artifact_validation import (
    validate_formal_trace,
    validate_png_screenshot,
)
from research.week1_miniwob.adapters.browser_lifecycle import BrowserHost, OfficialMiniwobEpisode
from research.week1_miniwob.adapters.miniwob_source import PinnedMiniwobServer


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "research" / "configs" / "codex-case.json"
EVIDENCE_ROOT = REPO_ROOT / "research" / "evidence"
DEFAULT_RESULTS = EVIDENCE_ROOT / "codex_case_blocked"
SOURCE_MANIFEST_PATH = DEFAULT_RESULTS / "source_manifest.json"
RECORDER_MANIFEST = REPO_ROOT / "extension" / "app" / "manifest.json"
UTC = timezone.utc


class CaseError(RuntimeError):
    """A bounded case preparation or completion error."""


class RecorderVersionError(CaseError):
    """The checkout's recorder is not the version this frozen harness pins."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CaseError(f"cannot read Codex case config: {error}") from error
    if value.get("schema_version") != "cui-codex-case-study-v1":
        raise CaseError("Codex case config schema changed")
    if value.get("limits") != {"max_actions": 30, "timeout_seconds": 480}:
        raise CaseError("Codex case limits must remain 30 actions / 480 seconds")
    controls = value.get("controls")
    if not isinstance(controls, dict) or any(
        controls.get(name) is not expected
        for name, expected in {
            "manual_rescue_allowed": False,
            "scripted_control_allowed": False,
            "success_required": False,
            "retain_failures": True,
            "formal_claim_permitted": False,
            "human_participant_data_included": False,
        }.items()
    ):
        raise CaseError("Codex case controls changed")
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 3:
        raise CaseError("Codex case config must contain three tasks")
    cells = [
        (task.get("task_id"), item.get("repetition"), item.get("seed"))
        for task in tasks
        for item in task.get("repetitions", [])
        if isinstance(item, dict)
    ]
    if len(cells) != 9 or len(cells) != len(set(cells)):
        raise CaseError("Codex case matrix must contain nine unique cells")
    return value


def select_cell(config: dict[str, Any], task_id: str, repetition: int) -> tuple[dict[str, Any], int]:
    for task in config["tasks"]:
        if task.get("task_id") != task_id:
            continue
        for item in task["repetitions"]:
            if item.get("repetition") == repetition:
                return task, int(item["seed"])
    raise CaseError(f"unknown Codex case cell: {task_id} repetition {repetition}")


def require_results_outside_evidence(results: Path) -> Path:
    """Refuse to write case bundles into the archived evidence tree."""

    resolved = results.resolve()
    evidence = EVIDENCE_ROOT.resolve()
    if resolved == evidence or evidence in resolved.parents:
        raise CaseError(
            "--results must not be inside research/evidence; the archived case bundles are read-only"
        )
    return resolved


def require_frozen_recorder(config: dict[str, Any], manifest_path: Path | None = None) -> str:
    """Refuse a checkout whose recorder differs from the version the config pins.

    Every case binds the archived 0.4.0 source manifest, so a case run with any
    other recorder tree would describe a recorder it did not use.
    """

    path = RECORDER_MANIFEST if manifest_path is None else manifest_path
    pinned = config["recorder"]["version"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RecorderVersionError(
            f"cannot read the recorder manifest ({type(error).__name__}); this frozen Codex "
            f"harness requires recorder {pinned}"
        ) from error
    live = payload.get("version") if isinstance(payload, dict) else None
    if live != pinned:
        raise RecorderVersionError(
            f"extension/app is recorder {live}; this frozen Codex harness and its archived "
            f"source manifest require recorder {pinned}"
        )
    return str(live)


def case_id(task_id: str, repetition: int) -> str:
    return f"codex-miniwob-{task_id}-r{repetition}-20260811"


def build_attempt(config: dict[str, Any], task: dict[str, Any], repetition: int, seed: int) -> dict[str, Any]:
    return {
        "attempt_id": case_id(task["task_id"], repetition),
        "protocol_version": config["protocol_version"],
        "seed": seed,
        "model": "codex-desktop-current-session",
        "framework": {"id": "codex-desktop", "version": "observed-2026-08-11"},
        "task": {
            "id": task["task_id"],
            "environment_id": task["environment_id"],
        },
        "limits": dict(config["limits"]),
        "browser": {
            "product_version": config["browser"]["product_version"],
            "viewport": dict(config["browser"]["viewport"]),
            "locale": config["browser"]["locale"],
            "timezone": config["browser"]["timezone"],
        },
    }


def archived_source_manifest() -> tuple[dict[str, Any], str]:
    """Return the archived 0.4.0 source manifest and its SHA-256.

    The frozen runner never writes a source manifest: one written now would hash
    the live recorder tree under the archived 0.4.0 identity.  A missing or
    invalid archived manifest is an error.
    """

    if not SOURCE_MANIFEST_PATH.is_file():
        raise CaseError(
            "the archived Codex source manifest is missing; this frozen runner reuses it "
            "and never writes a new one"
        )
    try:
        existing = json.loads(SOURCE_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CaseError("existing Codex source manifest is unreadable") from error
    validate_contract(existing, schema_version="cui-source-manifest-v1")
    return existing, sha256_file(SOURCE_MANIFEST_PATH)


def _source_manifest_ref(manifest: dict[str, Any], sha256: str) -> dict[str, Any]:
    """Reference the archived source manifest by its current repository path."""

    return {
        "schema_version": "cui-source-manifest-v1",
        "manifest_id": manifest["manifest_id"],
        "repository_path": SOURCE_MANIFEST_PATH.relative_to(REPO_ROOT).as_posix(),
        "sha256": sha256,
    }


def _await_completion(timeout_seconds: int) -> tuple[str, dict[str, Any]]:
    messages: queue.Queue[str] = queue.Queue(maxsize=1)

    def read_one() -> None:
        messages.put(sys.stdin.readline())

    threading.Thread(target=read_one, daemon=True).start()
    try:
        line = messages.get(timeout=timeout_seconds)
    except queue.Empty:
        return "timeout", {"action_count": 0, "actions": []}
    if not line:
        return "error", {"action_count": 0, "actions": []}
    try:
        value = json.loads(line)
    except json.JSONDecodeError as error:
        raise CaseError("completion acknowledgement must be one JSON object") from error
    if not isinstance(value, dict) or set(value) != {"action_count", "actions"}:
        raise CaseError("completion acknowledgement has an invalid shape")
    action_count = value.get("action_count")
    actions = value.get("actions")
    if (
        not isinstance(action_count, int)
        or isinstance(action_count, bool)
        or not 0 <= action_count <= 30
        or not isinstance(actions, list)
        or len(actions) != action_count
        or any(
            not isinstance(item, str)
            or len(item) > 80
            or any(token in item.lower() for token in ("script", "playwright", "cdp", "rescue"))
            for item in actions
        )
    ):
        raise CaseError("completion acknowledgement exceeds the bounded action contract")
    return "completed", {"action_count": action_count, "actions": actions}


def _copy_public_image(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return {
        "path": destination.relative_to(REPO_ROOT).as_posix(),
        "sha256": sha256_file(destination),
        "byte_count": destination.stat().st_size,
    }


def record_blocked_case(args: argparse.Namespace) -> dict[str, Any]:
    """Record one planned cell after both permitted Codex surfaces fail closed.

    The benchmark, Chrome profile, MV3 recorder startup, and screenshot are real.
    No task input is injected, the zero-action recorder output remains private and
    ineligible, and the public case explicitly marks both recorder evidence and
    the official reward as unavailable.
    """

    config_path = args.config.resolve()
    config = load_config(config_path)
    require_frozen_recorder(config)
    task, seed = select_cell(config, args.task, args.repetition)
    attempt = build_attempt(config, task, args.repetition, seed)
    public_root = args.results.resolve() / "attempts" / attempt["attempt_id"]
    if public_root.exists():
        raise CaseError("public case bundle already exists; immutable attempts are never overwritten")
    blocker_code = args.blocker_code
    if blocker_code != "codex_interaction_surfaces_unavailable":
        raise CaseError("blocked cases require the frozen Codex interaction-surface failure code")
    blocker_evidence = args.blocker_evidence.resolve()
    try:
        blocker_repo_path = blocker_evidence.relative_to(REPO_ROOT).as_posix()
    except ValueError as error:
        raise CaseError("blocker evidence must be a repository-relative public artifact") from error
    if not blocker_evidence.is_file():
        raise CaseError("blocker evidence is missing")

    source_root = args.miniwob_root.resolve()
    private_root = args.private_root.resolve()
    started_at = utc_now()
    source_manifest, source_manifest_sha = archived_source_manifest()
    initial_screenshot: Path | None = None
    screenshot_validation: dict[str, Any] | None = None
    recorder_started = False
    recorder_stopped = False
    private_trace_retained = False
    browser_version = ""
    instruction = ""
    with PinnedMiniwobServer(source_root) as server:
        with BrowserHost(attempt, private_root) as host:
            browser_version = host.browser_version
            with OfficialMiniwobEpisode(attempt, host, server.url) as episode:
                instruction = episode.instruction
                setup_path = host.attempt_root / "official_task_setup.json"
                setup_path.write_text(
                    json.dumps(
                        {
                            "schema_version": "cui-codex-private-task-setup-v1",
                            "case_id": attempt["attempt_id"],
                            "instruction": instruction,
                            "task_id": task["task_id"],
                            "seed": seed,
                            "blocked_before_task_input": True,
                        },
                        indent=2,
                        ensure_ascii=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                recorder_start = host.start_recorder(instruction)
                recorder_started = bool(recorder_start.get("session_id"))
                if not recorder_started:
                    raise CaseError("recorder did not return a session ID during blocked-cell setup")
                host.arm_episode_guard(episode.official_target)
                initial_screenshot = host.attempt_root / "initial.png"
                episode.save_final_screenshot(initial_screenshot)
                host.assert_episode_guard()
                host.disarm_episode_guard(require_stable=True)
                recorder_stop = host.stop_recorder()
                recorder_stopped = recorder_stop.get("status") == "complete"
                private_trace_retained = (host.attempt_root / "recorder_trace.json").is_file()

    assert initial_screenshot is not None
    screenshot_validation = validate_png_screenshot(initial_screenshot, config["browser"]["viewport"])
    initial_public = _copy_public_image(initial_screenshot, public_root / "initial.png")
    verification = {
        "schema_version": "cui-codex-case-verification-v1",
        "case_id": attempt["attempt_id"],
        "status": "blocked",
        "failure_code": blocker_code,
        "surface_preflight": {
            "repository_path": blocker_repo_path,
            "sha256": sha256_file(blocker_evidence),
        },
        "browser_version": browser_version,
        "official_task_initialized": True,
        "instruction_observed": bool(instruction),
        "recorder_session_started": recorder_started,
        "recorder_session_stopped": recorder_stopped,
        "private_zero_action_trace_retained": private_trace_retained,
        "private_raw_redistributed": False,
        "natural_codex_actions": 0,
        "scripted_control_used": False,
        "manual_rescue_used": False,
        "official_reward_available": False,
        "screenshot": screenshot_validation,
    }
    # _copy_public_image creates the leaf only after the immutable existence
    # check above has passed.
    verification_path = public_root / "verification.json"
    verification_path.write_text(
        json.dumps(verification, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    verification_sha = sha256_file(verification_path)
    completed_at = utc_now()
    case = {
        "schema_version": "cui-codex-case-v1",
        "case_id": attempt["attempt_id"],
        "objective": (
            f"Attempt official MiniWoB task {task['task_id']} with Codex Desktop through only "
            "the permitted in-app-browser or Computer Use surfaces."
        ),
        "execution_mode": "exploratory_interaction",
        "benchmark": "miniwob",
        "task_id": task["task_id"],
        "environment_id": task["environment_id"],
        "repetition": args.repetition,
        "seed": seed,
        "limits": dict(config["limits"]),
        "actor": {
            "identity": "Codex Desktop",
            "surface": "app",
            "app_version": "unavailable",
            "harness": config["actor"]["harness"],
            "observed_at_utc": started_at,
            "backend_snapshot": config["actor"]["backend_snapshot"],
        },
        "recorder": {
            "implementation": "browser_mv3",
            "version": config["recorder"]["version"],
            "status": "unavailable",
            "session_id": None,
            "record_sha256": None,
            "event_count": None,
        },
        "validator": {
            "implementation": "browsergym_official_reward",
            "reward": None,
            "terminated": False,
            "truncated": False,
        },
        "outcome": {"status": "blocked", "failure_code": blocker_code},
        "fresh_profile": True,
        "screenshots": [
            {
                "repository_path": initial_public["path"],
                "sha256": initial_public["sha256"],
                "caption": (
                    "Official MiniWoB task after real fresh-profile initialization and before "
                    "any task input; the permitted Codex interaction surfaces were unavailable."
                ),
                "cropped": False,
            }
        ],
        "action_count": 0,
        "no_manual_rescue": True,
        "no_scripted_control": True,
        "started_at_utc": started_at,
        "completed_at_utc": completed_at,
        "source_manifest": _source_manifest_ref(source_manifest, source_manifest_sha),
        "artifacts": [
            {
                "artifact_id": "initial-screenshot",
                "kind": "image",
                "repository_path": initial_public["path"],
                "sha256": initial_public["sha256"],
            },
            {
                "artifact_id": "verification",
                "kind": "data",
                "repository_path": verification_path.relative_to(REPO_ROOT).as_posix(),
                "sha256": verification_sha,
            },
            {
                "artifact_id": "surface-preflight",
                "kind": "log",
                "repository_path": blocker_repo_path,
                "sha256": sha256_file(blocker_evidence),
            },
        ],
        "verification": [
            {
                "check_id": "recorder-trace",
                "status": "blocked",
                "evidence_sha256": verification_sha,
            },
            {
                "check_id": "official-validator",
                "status": "blocked",
                "evidence_sha256": verification_sha,
            },
            {
                "check_id": "screenshot-integrity",
                "status": "pass",
                "evidence_sha256": verification_sha,
            },
            {
                "check_id": "interaction-surface",
                "status": "blocked",
                "evidence_sha256": sha256_file(blocker_evidence),
            },
        ],
        "formal_claim_permitted": False,
        "human_participant_data_included": False,
        "secrets_included": False,
        "claim_boundary": config["claim_boundary"],
    }
    validate_contract(case, schema_version="cui-codex-case-v1", for_write=True)
    case_path = public_root / "case.json"
    case_path.write_text(
        json.dumps(case, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return case


def run_case(args: argparse.Namespace) -> dict[str, Any]:
    config_path = args.config.resolve()
    config = load_config(config_path)
    require_frozen_recorder(config)
    task, seed = select_cell(config, args.task, args.repetition)
    attempt = build_attempt(config, task, args.repetition, seed)
    public_root = args.results.resolve() / "attempts" / attempt["attempt_id"]
    if public_root.exists():
        raise CaseError("public case bundle already exists; immutable attempts are never overwritten")
    source_root = args.miniwob_root.resolve()
    private_root = args.private_root.resolve()
    started_at = utc_now()
    source_manifest, source_manifest_sha = archived_source_manifest()
    outcome = "error"
    failure_code: str | None = None
    reward: float | None = None
    terminated = False
    truncated = False
    acknowledgement = {"action_count": 0, "actions": []}
    recorder_start: dict[str, Any] | None = None
    recorder_stop: dict[str, Any] | None = None
    trace_validation: dict[str, Any] | None = None
    screenshot_validation: dict[str, Any] | None = None
    initial_screenshot: Path | None = None
    final_screenshot: Path | None = None
    host: BrowserHost | None = None
    official_url = ""
    try:
        with PinnedMiniwobServer(source_root) as server:
            with BrowserHost(attempt, private_root) as host:
                with OfficialMiniwobEpisode(attempt, host, server.url) as episode:
                    official_url = episode.official_target["url"]
                    setup_path = host.attempt_root / "official_task_setup.json"
                    setup_path.write_text(
                        json.dumps(
                            {
                                "schema_version": "cui-codex-private-task-setup-v1",
                                "case_id": attempt["attempt_id"],
                                "instruction": episode.instruction,
                                "task_id": task["task_id"],
                                "seed": seed,
                            },
                            indent=2,
                            ensure_ascii=True,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    recorder_start = host.start_recorder(episode.instruction)
                    if not recorder_start.get("session_id"):
                        raise CaseError("recorder did not return a session ID")
                    host.arm_episode_guard(episode.official_target)
                    initial_screenshot = host.attempt_root / "initial.png"
                    episode.save_final_screenshot(initial_screenshot)
                    print(
                        json.dumps(
                            {
                                "type": "codex_case_ready",
                                "case_id": attempt["attempt_id"],
                                "task_id": task["task_id"],
                                "repetition": args.repetition,
                                "seed": seed,
                                "instruction": episode.instruction,
                                "limits": config["limits"],
                                "expected_window_title": task["task_id"],
                                "acknowledgement_shape": {
                                    "action_count": "integer 0..30",
                                    "actions": "array of concise visible CUA action labels",
                                },
                            },
                            ensure_ascii=True,
                        ),
                        flush=True,
                    )
                    completion_state, acknowledgement = _await_completion(
                        config["limits"]["timeout_seconds"]
                    )
                    truncated = completion_state == "timeout"
                    if completion_state != "completed":
                        failure_code = f"codex_{completion_state}"
                    host.assert_episode_guard()
                    reward, terminated, _ = episode.validate()
                    host.assert_episode_guard()
                    host.disarm_episode_guard(require_stable=True)
                    final_screenshot = host.attempt_root / "final.png"
                    episode.save_final_screenshot(final_screenshot)
                    recorder_stop = host.stop_recorder()
                    recorder_status = recorder_stop.get("status")
                    if recorder_status != "complete":
                        failure_code = failure_code or "recorder_incomplete"
                    if completion_state == "completed" and failure_code is None:
                        outcome = "success" if reward == 1 and terminated else "failure"
                        if outcome == "failure":
                            failure_code = "task_not_completed"
                    elif completion_state == "timeout":
                        outcome = "timeout"
                    else:
                        outcome = "error"
    except Exception as error:
        failure_code = failure_code or type(error).__name__.lower()
        if host is not None:
            initial_screenshot = initial_screenshot if initial_screenshot and initial_screenshot.is_file() else None
            final_screenshot = final_screenshot if final_screenshot and final_screenshot.is_file() else None
        raise
    finally:
        # Successful and task-level failure attempts are written below. Setup
        # exceptions deliberately fail before claiming a complete bundle.
        pass

    assert host is not None and recorder_start is not None and recorder_stop is not None
    assert initial_screenshot is not None and final_screenshot is not None
    trace_path = host.attempt_root / "recorder_trace.json"
    session_id = str(recorder_stop.get("session_id") or recorder_start["session_id"])
    trace_validation = validate_formal_trace(
        trace_path,
        expected_session_id=session_id,
        expected_event_count=int(recorder_stop.get("event_count") or 0),
        expected_url=official_url,
        expected_viewport=config["browser"]["viewport"],
        require_natural_event=True,
    )
    screenshot_validation = validate_png_screenshot(
        final_screenshot, config["browser"]["viewport"]
    )
    initial_public = _copy_public_image(initial_screenshot, public_root / "initial.png")
    final_public = _copy_public_image(final_screenshot, public_root / "final.png")
    verification = {
        "schema_version": "cui-codex-case-verification-v1",
        "case_id": attempt["attempt_id"],
        "trace_sha256": sha256_file(trace_path),
        "trace_byte_count": trace_path.stat().st_size,
        "recorder": trace_validation,
        "screenshot": screenshot_validation,
        "validator": {
            "source": "browsergym_official_reward",
            "reward": reward,
            "terminated": terminated,
            "truncated": truncated,
        },
        "action_count": acknowledgement["action_count"],
        "actions": acknowledgement["actions"],
        "private_raw_redistributed": False,
    }
    verification_path = public_root / "verification.json"
    verification_path.write_text(
        json.dumps(verification, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    verification_sha = sha256_file(verification_path)
    completed_at = utc_now()
    case = {
        "schema_version": "cui-codex-case-v1",
        "case_id": attempt["attempt_id"],
        "objective": (
            f"Complete official MiniWoB task {task['task_id']} with Codex Desktop computer "
            "control while the real MV3 recorder and official validator observe the attempt."
        ),
        "execution_mode": "exploratory_interaction",
        "started_at_utc": started_at,
        "completed_at_utc": completed_at,
        "source_manifest": _source_manifest_ref(source_manifest, source_manifest_sha),
        "benchmark": "miniwob",
        "task_id": task["task_id"],
        "environment_id": task["environment_id"],
        "repetition": args.repetition,
        "seed": seed,
        "actor": {
            "identity": "Codex Desktop",
            "surface": "app",
            "app_version": "unavailable",
            "harness": config["actor"]["harness"],
            "observed_at_utc": started_at,
            "backend_snapshot": config["actor"]["backend_snapshot"],
        },
        "limits": dict(config["limits"]),
        "fresh_profile": True,
        "action_count": acknowledgement["action_count"],
        "recorder": {
            "implementation": "browser_mv3",
            "version": config["recorder"]["version"],
            "status": "captured",
            "session_id": session_id,
            "record_sha256": verification["trace_sha256"],
            "event_count": int(recorder_stop.get("event_count") or 0),
        },
        "validator": {
            "implementation": "browsergym_official_reward",
            "reward": reward,
            "terminated": terminated,
            "truncated": truncated,
        },
        "outcome": {"status": outcome, "failure_code": failure_code},
        "screenshots": [
            {
                "repository_path": initial_public["path"],
                "sha256": initial_public["sha256"],
                "caption": "Official MiniWoB task immediately before Codex interaction.",
                "cropped": False,
            },
            {
                "repository_path": final_public["path"],
                "sha256": final_public["sha256"],
                "caption": "Official MiniWoB task at the validator boundary after Codex interaction.",
                "cropped": False,
            },
        ],
        "artifacts": [
            {
                "artifact_id": "initial-screenshot",
                "kind": "image",
                "repository_path": initial_public["path"],
                "sha256": initial_public["sha256"],
            },
            {
                "artifact_id": "final-screenshot",
                "kind": "image",
                "repository_path": final_public["path"],
                "sha256": final_public["sha256"],
            },
            {
                "artifact_id": "verification",
                "kind": "data",
                "repository_path": verification_path.relative_to(REPO_ROOT).as_posix(),
                "sha256": verification_sha,
            },
        ],
        "verification": [
            {
                "check_id": "recorder-trace",
                "status": "pass",
                "evidence_sha256": verification_sha,
            },
            {
                "check_id": "official-validator",
                "status": "pass",
                "evidence_sha256": verification_sha,
            },
            {
                "check_id": "screenshot-integrity",
                "status": "pass",
                "evidence_sha256": verification_sha,
            },
        ],
        "no_manual_rescue": True,
        "no_scripted_control": True,
        "formal_claim_permitted": False,
        "human_participant_data_included": False,
        "secrets_included": False,
        "claim_boundary": config["claim_boundary"],
    }
    validate_contract(case, schema_version="cui-codex-case-v1", for_write=True)
    case_path = public_root / "case.json"
    case_path.write_text(
        json.dumps(case, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return case


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, choices=("click-checkboxes", "enter-text", "choose-list"))
    parser.add_argument("--repetition", required=True, type=int, choices=(1, 2, 3))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--results",
        type=Path,
        required=True,
        help="New public results directory inside the repository, outside research/evidence",
    )
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--miniwob-root", type=Path, required=True)
    parser.add_argument(
        "--record-blocked",
        action="store_true",
        help="Initialize the real task but record a zero-action blocked bundle after surface preflight failure",
    )
    parser.add_argument(
        "--blocker-code",
        default="codex_interaction_surfaces_unavailable",
        help="Frozen failure code for --record-blocked",
    )
    parser.add_argument(
        "--blocker-evidence",
        type=Path,
        default=DEFAULT_RESULTS / "surface_preflight.json",
        help="Repository-relative public surface-preflight evidence",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.monotonic()
    try:
        require_results_outside_evidence(args.results)
    except CaseError as error:
        print(
            json.dumps(
                {
                    "type": "codex_case_error",
                    "error_code": "results_in_archived_evidence",
                    "detail": str(error),
                }
            ),
            file=sys.stderr,
        )
        return 2
    try:
        require_frozen_recorder(load_config(args.config.resolve()))
    except CaseError as error:
        print(
            json.dumps(
                {
                    "type": "codex_case_error",
                    "error_code": (
                        "recorder_version_mismatch"
                        if isinstance(error, RecorderVersionError)
                        else "invalid_config"
                    ),
                    "detail": str(error),
                }
            ),
            file=sys.stderr,
        )
        return 2
    try:
        case = record_blocked_case(args) if args.record_blocked else run_case(args)
    except Exception as error:
        print(
            json.dumps(
                {
                    "type": "codex_case_error",
                    "error_code": type(error).__name__.lower(),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "type": "codex_case_complete",
                "case_id": case["case_id"],
                "status": case["outcome"]["status"],
                "reward": case["validator"]["reward"],
                "event_count": case["recorder"]["event_count"],
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
