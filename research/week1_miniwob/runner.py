"""Run or inspect the formal 27-attempt BrowserGym/MiniWoB matrix.

Adapters are non-shell commands supplied as JSON arrays in the environment.
Each command receives one attempt specification on stdin and must return one
contract-valid JSON object on stdout. Logs belong on stderr.

Run from the repository root as ``python -m research.week1_miniwob.runner``.
The harness is frozen against recorder 0.4.0 and the retired Stagehand cell,
so only ``--dry-run`` and ``--preflight`` are meaningful in this repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import evidence_projection as _evidence_projection
from . import runner_process as _runner_process
from .checkout import (
    recorder_extension_check,
    require_output_outside_evidence,
    translate_legacy_path,
)
from .protocol import (
    ADAPTER_RESULT_SCHEMA,
    ATTEMPT_RECORD_SCHEMA,
    FORMAL_EVIDENCE_SCOPE,
    ProtocolError,
    aggregate_records,
    build_matrix,
    formal_adapter_argv,
    load_manifest,
    validate_adapter_result,
    validate_attempt_record,
)
from .provider_probe import (
    ProviderProbeError,
    load_private_certificate,
    validate_public_summary,
)

_public_artifact_descriptor = _evidence_projection._public_artifact_descriptor
_public_dependencies = _evidence_projection._public_dependencies
_public_trace_validation = _evidence_projection._public_trace_validation
_public_viewport = _evidence_projection._public_viewport
_public_screenshot_validation = _evidence_projection._public_screenshot_validation
_stable_environment_token = _evidence_projection._stable_environment_token
_public_environment_guard = _evidence_projection._public_environment_guard
_public_episode_guard = _evidence_projection._public_episode_guard
_public_framework_evidence = _evidence_projection._public_framework_evidence
public_adapter_result = _evidence_projection.public_adapter_result
_text_output = _runner_process._text_output
_retain_private_stderr_impl = _runner_process.retain_private_stderr
_terminate_process_tree_impl = _runner_process.terminate_process_tree
_run_adapter_process_impl = _runner_process.run_adapter_process

from computer_use_agent.study.policy import (
    FormalStudyError,
    load_binding,
    stage_manifest,
)
from computer_use_agent.study.ledger import (
    LedgerError,
    atomic_write_json,
    claim,
    initialize_or_resume,
    ledger_payload,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = Path(__file__).with_name("benchmark_manifest.json")
FORMAL_STUDY_CONFIG = REPO_ROOT / "research" / "configs" / "formal-study.json"
RELEASE_CAPSULE = REPO_ROOT / "research" / "archive" / "release-capsule-2026-08.json"
ADAPTER_PROCESS_OVERHEAD_SECONDS = 180
ADAPTER_SELF_TEST_TIMEOUT_SECONDS = 150
PROCESS_TREE_SHUTDOWN_SECONDS = 15
CANONICAL_MANIFEST_RELATIVE_PATH = Path("research/week1_miniwob/benchmark_manifest.json")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def check(check_id: str, status: str, detail: str) -> dict[str, str]:
    return {"id": check_id, "status": status, "detail": detail}


def parse_command_env(name: str) -> list[str] | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ProtocolError(f"{name} must be a JSON array of command arguments: {error}") from error
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ProtocolError(f"{name} must be a non-empty JSON array of non-empty strings")
    return value


def expand_default_command(command: list[str]) -> list[str]:
    """Expand interpreter tokens and map recorded legacy script paths onto this checkout."""

    replacements = {"{python}": sys.executable, "{repo_root}": str(REPO_ROOT)}
    return [
        replacements[item]
        if item in replacements
        else translate_legacy_path(item) if item.startswith("experiments/") else item
        for item in command
    ]


def is_canonical_tracked_manifest(path: Path, manifest: dict[str, Any]) -> bool:
    """Return whether ``path`` is the tracked, canonical formal manifest.

    An externally supplied 27-row JSON file can be useful for a debug run but
    must never acquire the formal scope merely because its shape resembles the
    official matrix.  The semantic hash check also makes the bound explicit in
    the runner before it labels an execution formal.
    """

    try:
        if path.resolve() != DEFAULT_MANIFEST.resolve():
            return False
        canonical_manifest = load_manifest(DEFAULT_MANIFEST)
    except (OSError, ProtocolError):
        return False
    if hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest() != hashlib.sha256(
        json.dumps(canonical_manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest():
        return False
    if not shutil.which("git"):
        return False
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", str(CANONICAL_MANIFEST_RELATIVE_PATH).replace("\\", "/")],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return tracked.returncode == 0 and tracked.stdout.strip() == str(CANONICAL_MANIFEST_RELATIVE_PATH).replace("\\", "/")


def resolve_adapter_command(
    framework: dict[str, Any],
    *,
    require_repository_default: bool = False,
) -> tuple[list[str] | None, str]:
    env_name = framework["adapter_command_env"]
    override = parse_command_env(env_name)
    if override:
        if require_repository_default:
            raise ProtocolError(
                f"formal full-matrix execution forbids the {env_name} adapter override"
            )
        return override, f"environment:{env_name}"
    default = framework.get("default_adapter_command")
    if not isinstance(default, list):
        return None, "missing"
    if require_repository_default and default != formal_adapter_argv(framework["id"]):
        raise ProtocolError(
            f"formal full-matrix execution requires exact adapter argv "
            f"{formal_adapter_argv(framework['id'])!r}"
        )
    return expand_default_command(default), "repository-default"


def _validate_source_provenance(
    value: Any,
    *,
    manifest_sha256: str,
    require_clean: bool,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError("source provenance is required for every executed attempt")
    commit = value.get("commit")
    clean = value.get("clean")
    observed_manifest = value.get("manifest_sha256")
    if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ProtocolError("source provenance commit must be an exact 40-hex Git commit")
    if not isinstance(clean, bool):
        raise ProtocolError("source provenance clean must be an explicit boolean")
    if observed_manifest != manifest_sha256 or not re.fullmatch(r"[a-f0-9]{64}", str(observed_manifest)):
        raise ProtocolError("source provenance manifest hash does not match the attempt")
    if require_clean and clean is not True:
        raise ProtocolError("formal full-matrix execution requires a clean source commit")
    return {
        "commit": commit,
        "clean": clean,
        "manifest_sha256": observed_manifest,
    }


def capture_source_provenance(manifest_sha256: str, *, require_clean: bool) -> dict[str, Any]:
    if not shutil.which("git"):
        raise ProtocolError("Git is required to establish experiment source provenance")
    commit_process = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    status_process = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if commit_process.returncode != 0 or status_process.returncode != 0:
        raise ProtocolError("Git could not establish source commit and cleanliness")
    value = {
        "commit": commit_process.stdout.strip().lower(),
        "clean": status_process.stdout.strip() == "",
        "manifest_sha256": manifest_sha256,
    }
    return _validate_source_provenance(
        value,
        manifest_sha256=manifest_sha256,
        require_clean=require_clean,
    )


def capture_resume_source_provenance(
    existing_records: list[dict[str, Any]],
    *,
    manifest_sha256: str,
    output_dir: Path,
) -> dict[str, Any]:
    """Reuse the original clean-source attestation after an interrupted batch.

    Formal evidence written inside the repository naturally makes the working
    tree non-empty.  Resume is safe only when HEAD is unchanged and every
    current dirty path is contained by the exact evidence output directory.
    Source/config drift therefore remains fail-closed.
    """

    provenances = {
        json.dumps(
            row.get("orchestrator", {}).get("source_provenance"),
            sort_keys=True,
            separators=(",", ":"),
        )
        for row in existing_records
    }
    if len(provenances) != 1:
        raise ProtocolError("existing ledger does not have one consistent source provenance")
    provenance = json.loads(next(iter(provenances)))
    safe = _validate_source_provenance(
        provenance, manifest_sha256=manifest_sha256, require_clean=True
    )
    commit = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if commit.returncode != 0 or status.returncode != 0 or commit.stdout.strip().lower() != safe["commit"]:
        raise ProtocolError("repository HEAD differs from the existing formal ledger")
    try:
        output_relative = output_dir.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        output_relative = None
    dirty_outside: list[str] = []
    for line in status.stdout.splitlines():
        value = line[3:].strip().strip('"').replace("\\", "/") if len(line) >= 4 else ""
        candidates = [item.strip() for item in value.split(" -> ")]
        allowed = output_relative is not None and all(
            candidate == output_relative.as_posix()
            or candidate.startswith(output_relative.as_posix().rstrip("/") + "/")
            for candidate in candidates
        )
        if not allowed:
            dirty_outside.append(value or line)
    if dirty_outside:
        raise ProtocolError(
            "source/config drift exists outside the immutable evidence output: "
            + ", ".join(dirty_outside[:5])
        )
    return safe


def run_preflight(
    manifest: dict[str, Any],
    *,
    require_clean: bool = False,
    require_repository_defaults: bool = False,
    require_provider_entitlement: bool = False,
    framework_ids: set[str] | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    provider_entitlement: dict[str, Any] | None = None
    checks.append(check("python", "pass", sys.version.split()[0]))
    checks.append(check("node", "pass" if shutil.which("node") else "fail", shutil.which("node") or "node not found"))
    # BrowserGym and each framework intentionally live in separate pinned
    # environments.  The dispatcher self-tests below are the authority for
    # those packages; the orchestrator interpreter need not import them.
    checks.append(recorder_extension_check(manifest))
    checks.append(check("openai-key", "pass" if os.environ.get("OPENAI_API_KEY") else "fail", "present" if os.environ.get("OPENAI_API_KEY") else "OPENAI_API_KEY is not set"))
    if require_provider_entitlement:
        try:
            provider_entitlement = load_private_certificate()
            checks.append(
                check(
                    "gpt-5.5-entitlement",
                    "pass",
                    (
                        f"official OpenAI Responses certificate; "
                        f"resolved {provider_entitlement['response_model']}; "
                        f"expires {provider_entitlement['expires_at']}"
                    ),
                )
            )
        except ProviderProbeError as error:
            checks.append(check("gpt-5.5-entitlement", "fail", str(error)))
    else:
        checks.append(
            check(
                "gpt-5.5-entitlement",
                "warn",
                "not required for this non-executing or non-formal inspection",
            )
        )
    for framework in manifest["frameworks"]:
        if framework_ids is not None and framework["id"] not in framework_ids:
            continue
        security = framework["formal_security"]
        security_allowed = security["allowed"] is True
        security_detail = (
            f"reviewed {security['reviewed_at']}; no blocking advisories"
            if security_allowed
            else f"reviewed {security['reviewed_at']}; blocked by "
            + ", ".join(security["blocking_advisories"])
        )
        checks.append(
            check(
                f"security-{framework['id']}",
                "pass" if security_allowed else "fail",
                security_detail,
            )
        )
        env_name = framework["adapter_command_env"]
        try:
            command, source = resolve_adapter_command(
                framework,
                require_repository_default=require_repository_defaults,
            )
            status = "pass" if command else "fail"
            detail = f"{source}: {command[0]}" if command else f"no default or {env_name} override"
            if command:
                completed = subprocess.run(
                    [*command, "--self-test"],
                    cwd=REPO_ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    # Skyvern.local performs a real embedded constructor/
                    # close lifecycle guard during the one-time matrix
                    # preflight and takes ~45s on the pinned Windows host.
                    # This budget is outside every attempt's strict 480s
                    # agent deadline and is not repeated per attempt.
                    timeout=ADAPTER_SELF_TEST_TIMEOUT_SECONDS,
                    check=False,
                )
                try:
                    diagnostic = json.loads(completed.stdout)
                except json.JSONDecodeError as error:
                    raise ProtocolError(f"{framework['id']} self-test returned invalid JSON: {error}") from error
                failures = [
                    f"{item.get('id')}: {item.get('detail')}"
                    for item in diagnostic.get("checks", [])
                    if item.get("status") == "fail"
                ]
                check_statuses = {
                    item.get("id"): item.get("status") for item in diagnostic.get("checks", [])
                    if isinstance(item, dict)
                }
                for required_id in ("miniwob-pinned-source", "miniwob-loopback"):
                    if check_statuses.get(required_id) != "pass":
                        failures.append(f"{required_id}: required formal self-test did not pass")
                if completed.returncode != 0 or diagnostic.get("ready") is not True:
                    status = "fail"
                    detail = "; ".join(failures) or f"adapter self-test exited {completed.returncode}"
                elif failures:
                    status = "fail"
                    detail = "; ".join(failures)
        except ProtocolError as error:
            status, detail = "fail", str(error)
        except (OSError, subprocess.TimeoutExpired) as error:
            status, detail = "fail", f"adapter self-test could not run: {error}"
        checks.append(check(f"adapter-{framework['id']}", status, detail))
    git_status = "unavailable"
    if shutil.which("git"):
        proc = subprocess.run(
            ["git", "status", "--short"], cwd=REPO_ROOT, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        git_status = proc.stdout.strip()
    if git_status == "":
        checks.append(check("clean-source", "pass", "working tree is clean"))
    elif require_clean:
        checks.append(check("clean-source", "fail", "working tree has changes; commit source before a formal run"))
    else:
        checks.append(check("clean-source", "warn", "working tree has changes; use --require-clean for a formal run"))
    return {
        "schema_version": "cui-week1-preflight-v1",
        "framework_scope": sorted(framework_ids) if framework_ids is not None else sorted(
            framework["id"] for framework in manifest["frameworks"]
        ),
        "ready": all(row["status"] != "fail" for row in checks),
        "checks": checks,
        "provider_entitlement": provider_entitlement,
        "secrets_printed": False,
    }


def filter_matrix(
    matrix: list[dict[str, Any]], framework: str | None, task: str | None, repetition: int | None
) -> list[dict[str, Any]]:
    rows = [
        row for row in matrix
        if (framework is None or row["framework"]["id"] == framework)
        and (task is None or row["task"]["id"] == task or row["task"]["environment_id"] == task)
        and (repetition is None or row["repetition"] == repetition)
    ]
    if not rows:
        raise ProtocolError("filters selected no planned attempts")
    return rows


def select_attempt_id(
    matrix: list[dict[str, Any]], attempt_id: str | None
) -> list[dict[str, Any]]:
    """Select one exact canonical cell without broad/prefix matching."""

    if attempt_id is None:
        return matrix
    matches = [row for row in matrix if row["attempt_id"] == attempt_id]
    if len(matches) != 1:
        raise ProtocolError(f"unknown formal attempt_id: {attempt_id}")
    return matches


def read_attempt_record(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProtocolError(f"could not read existing attempt artifact {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ProtocolError(f"existing attempt artifact is not an object: {path}")
    return payload


def _verify_private_file(path: Path, descriptor: Any, *, label: str) -> None:
    if not isinstance(descriptor, dict) or not path.is_file():
        raise ProtocolError(f"resume validation cannot find private {label} evidence")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    if digest.hexdigest() != descriptor.get("sha256") or size != descriptor.get("bytes"):
        raise ProtocolError(f"private {label} evidence changed after the attempt was recorded")


def validate_private_attempt_bundle(
    record: dict[str, Any], expected_attempt: dict[str, Any], *, public_path: Path
) -> None:
    """Re-hash every retained private file needed by a complete Week 1 cell."""

    private_value = os.environ.get("CUI_WEEK1_PRIVATE_ROOT")
    if not private_value:
        raise ProtocolError(
            "CUI_WEEK1_PRIVATE_ROOT is required to validate a complete bundle before resume"
        )
    private_root = Path(private_value).expanduser().resolve()
    attempt_root = private_root / "week1" / expected_attempt["attempt_id"]
    result = record["result"]
    _verify_private_file(
        attempt_root / "recorder_trace.json",
        result.get("recorder", {}).get("trace_artifact"),
        label="recorder trace",
    )
    _verify_private_file(
        attempt_root / "final.png",
        result.get("artifacts", {}).get("final_screenshot"),
        label="final screenshot",
    )
    _verify_private_file(
        attempt_root / "official_task_setup.json",
        result.get("validator", {}).get("task_setup_artifact"),
        label="official task setup",
    )
    _verify_private_file(
        attempt_root / "official_validator_task_info.json",
        result.get("validator", {}).get("task_info_artifact"),
        label="official validator task info",
    )
    environment_value = os.environ.get("CUI_WEEK1_ADAPTER_ENV_ROOT")
    environment_root = (
        Path(environment_value).expanduser().resolve()
        if environment_value
        else private_root / "environments"
    )
    for snapshot in result.get("dependencies", {}).get("snapshots", []):
        name = snapshot.get("environment") if isinstance(snapshot, dict) else None
        if not isinstance(name, str):
            raise ProtocolError("dependency snapshot has no stable environment name")
        _verify_private_file(
            environment_root / name / "cui-week1-dependency-freeze.txt",
            snapshot.get("artifact"),
            label=f"{name} dependency freeze",
        )
    output_root = public_path.resolve().parents[1]
    namespace = hashlib.sha256(str(output_root).encode("utf-8")).hexdigest()[:12]
    stderr_path = (
        private_root
        / "week1"
        / "orchestrator_logs"
        / f"{namespace}__{expected_attempt['attempt_id']}.stderr.log"
    )
    _verify_private_file(
        stderr_path,
        record.get("orchestrator", {}).get("stderr_artifact"),
        label="orchestrator stderr",
    )


def validate_resumable_attempt(
    path: Path,
    expected_attempt: dict[str, Any],
    *,
    evidence_scope: str,
    source_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return an existing complete bundle or fail without modifying it."""

    record = read_attempt_record(path)
    validate_attempt_record(record, expected_attempt, evidence_scope=evidence_scope)
    result = record["result"]
    if result.get("artifact_bundle_complete") is not True:
        raise ProtocolError(
            f"existing attempt is contract-valid but its artifact bundle is incomplete; "
            f"immutable evidence was preserved and a new --output-dir is required: "
            f"{expected_attempt['attempt_id']}"
        )
    validate_private_attempt_bundle(record, expected_attempt, public_path=path)
    if source_provenance is not None and (
        record.get("orchestrator", {}).get("source_provenance") != source_provenance
    ):
        raise ProtocolError(
            "existing attempt came from different source provenance; use the original clean "
            "commit or start a new output ledger"
        )
    return record


def read_ledger_records(
    output_dir: Path,
    planned: list[dict[str, Any]],
    *,
    evidence_scope: str,
) -> list[dict[str, Any]]:
    """Read all attempt artifacts, including unknown files for aggregate audit."""

    attempts_dir = output_dir / "attempts"
    if not attempts_dir.is_dir():
        return []
    planned_by_id = {row["attempt_id"]: row for row in planned}
    records: list[dict[str, Any]] = []
    for path in sorted(attempts_dir.glob("*.json")):
        record = read_attempt_record(path)
        embedded_id = record.get("attempt", {}).get("attempt_id")
        expected = planned_by_id.get(embedded_id)
        if expected is not None:
            # Validate here so a corrupt record fails the invocation rather
            # than being summarized as a soft warning.
            validate_attempt_record(record, expected, evidence_scope=evidence_scope)
        records.append(record)
    return records


def label_partial_attempts(attempts: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,48}", label):
        raise ProtocolError("partial run label must be a 1-48 character stable token")
    labeled: list[dict[str, Any]] = []
    for attempt in attempts:
        row = json.loads(json.dumps(attempt))
        formal_attempt_id = row["attempt_id"]
        row["formal_attempt_id"] = formal_attempt_id
        row["attempt_id"] = f"partial__{label}__{formal_attempt_id}"
        labeled.append(row)
    return labeled


def retain_private_stderr(
    attempt_id: str, stderr_text: str, *, output_dir: Path | None = None
) -> dict[str, Any]:
    """Retain private stderr while preserving the historical runner boundary."""

    return _retain_private_stderr_impl(
        attempt_id,
        stderr_text,
        repo_root=REPO_ROOT,
        output_dir=output_dir,
    )


def _terminate_process_tree(
    process: subprocess.Popen[str],
    *,
    platform_name: str | None = None,
) -> str:
    """Terminate one owned adapter tree through the isolated process module."""

    return _terminate_process_tree_impl(
        process,
        platform_name=platform_name,
        shutdown_seconds=PROCESS_TREE_SHUTDOWN_SECONDS,
    )


def _run_adapter_process(
    command: list[str],
    *,
    input_text: str,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    """Run one adapter while keeping runner-level monkeypatch compatibility."""

    return _run_adapter_process_impl(
        command,
        input_text=input_text,
        timeout_seconds=timeout_seconds,
        terminate_process_tree_fn=_terminate_process_tree,
        shutdown_seconds=PROCESS_TREE_SHUTDOWN_SECONDS,
        cwd=REPO_ROOT,
    )


def execute_attempt(
    attempt: dict[str, Any],
    output_dir: Path,
    *,
    evidence_scope: str = "formal-full-matrix",
    source_provenance: dict[str, Any] | None = None,
    provider_entitlement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    env_name = attempt["framework"]["adapter_command_env"]
    is_formal = evidence_scope == "formal-full-matrix"
    safe_source_provenance = _validate_source_provenance(
        source_provenance,
        manifest_sha256=attempt["manifest_sha256"],
        require_clean=is_formal,
    )
    command, command_source = resolve_adapter_command(
        attempt["framework"],
        require_repository_default=is_formal,
    )
    if not command:
        raise ProtocolError(f"neither the repository adapter nor {env_name} is configured")
    started = utc_now()
    safe_provider_entitlement: dict[str, Any] | None = None
    if is_formal:
        try:
            safe_provider_entitlement = validate_public_summary(
                provider_entitlement,
                at=started,
            )
        except ProviderProbeError as error:
            raise ProtocolError(
                f"formal attempt has no current official gpt-5.5 entitlement: {error}"
            ) from error
        # Detach the immutable public descriptor from any caller-owned nested
        # dictionaries before it is retained in an attempt artifact.
        safe_provider_entitlement = json.loads(
            json.dumps(safe_provider_entitlement, sort_keys=True, ensure_ascii=True)
        )
    elif provider_entitlement is not None:
        try:
            safe_provider_entitlement = validate_public_summary(provider_entitlement)
        except ProviderProbeError as error:
            raise ProtocolError(f"provider entitlement descriptor is invalid: {error}") from error
    attempt_dir = output_dir / "attempts"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    destination = attempt_dir / f"{attempt['attempt_id']}.json"
    if destination.exists():
        raise ProtocolError("public attempt artifact already exists; refusing to overwrite it")
    monotonic_start = time.monotonic()
    stderr_text = ""

    def orchestration_failure(
        status: str,
        code: str,
        *,
        truncated: bool = False,
        recorder_status: str = "unknown",
    ) -> dict[str, Any]:
        return {
            "schema_version": ADAPTER_RESULT_SCHEMA,
            "attempt_id": attempt["attempt_id"],
            "status": status,
            "agent": {
                "framework": attempt["framework"]["id"],
                "framework_version": attempt["framework"]["version"],
                "model": attempt["model"],
                "action_count": 0,
                "action_count_observed": False,
            },
            "validator": {
                "source": "browsergym_official_reward",
                "reward": None,
                "terminated": False,
                "truncated": truncated,
            },
            "recorder": {
                "capture_adapter": "browser_mv3",
                "source_class": "cui_agent",
                "status": recorder_status,
                "session_id": None,
                "event_count": 0,
            },
            "dependencies": {"snapshots": []},
            "error": {"code": code},
            "provenance_complete": False,
            "artifact_bundle_complete": False,
            "formal_matrix_eligible": False,
            "analysis_eligible": False,
        }

    task_timeout_seconds = int(attempt["limits"]["timeout_seconds"])
    outer_watchdog_seconds = task_timeout_seconds + ADAPTER_PROCESS_OVERHEAD_SECONDS
    try:
        completed = _run_adapter_process(
            command,
            input_text=json.dumps(attempt, ensure_ascii=True),
            timeout_seconds=outer_watchdog_seconds,
        )
        stderr_text = completed.stderr
        if completed.returncode != 0:
            stderr_text += f"\n[orchestrator] adapter exited {completed.returncode}\n"
            result = orchestration_failure(
                "error", "adapter_process_failed", recorder_status="interrupted"
            )
        else:
            try:
                result = json.loads(completed.stdout.strip())
                validate_adapter_result(result, attempt)
                result = public_adapter_result(result, evidence_scope=evidence_scope)
                validate_adapter_result(result, attempt, evidence_scope=evidence_scope)
            except (json.JSONDecodeError, ProtocolError) as error:
                stderr_text += f"\n[orchestrator] invalid adapter result: {type(error).__name__}: {error}\n"
                result = orchestration_failure(
                    "error", "adapter_protocol_error", recorder_status="interrupted"
                )
    except subprocess.TimeoutExpired as error:
        stderr_text = (error.stderr or "") if isinstance(error.stderr, str) else ""
        stderr_text += (
            "\n[orchestrator] adapter process exceeded the outer watchdog "
            f"({outer_watchdog_seconds}s = {task_timeout_seconds}s task deadline + "
            f"{ADAPTER_PROCESS_OVERHEAD_SECONDS}s startup/cleanup allowance)\n"
        )
        result = orchestration_failure(
            "timeout", "adapter_outer_timeout", truncated=True, recorder_status="interrupted"
        )
    except OSError as error:
        stderr_text = f"[orchestrator] adapter process could not start: {type(error).__name__}: {error}\n"
        result = orchestration_failure("error", "adapter_start_failed")
    stderr_artifact = retain_private_stderr(
        attempt["attempt_id"], stderr_text, output_dir=output_dir
    )
    record = {
        "schema_version": ATTEMPT_RECORD_SCHEMA,
        "evidence_scope": evidence_scope,
        "attempt": attempt,
        "orchestrator": {
            "started_at": started,
            "finished_at": utc_now(),
            "elapsed_seconds": round(time.monotonic() - monotonic_start, 3),
            "adapter_command_env": env_name,
            "adapter_command_source": command_source,
            # Do not retain environment override argv in a public record.
            # A repository default is represented by the canonical portable
            # contract rather than its host-specific Python path.
            "adapter_argv": (
                formal_adapter_argv(attempt["framework"]["id"])
                if command_source == "repository-default"
                and command == expand_default_command(formal_adapter_argv(attempt["framework"]["id"]))
                else None
            ),
            "adapter_executable": Path(command[0]).name,
            "source_provenance": safe_source_provenance,
            "provider_entitlement": safe_provider_entitlement,
            "task_timeout_seconds": task_timeout_seconds,
            "process_overhead_seconds": ADAPTER_PROCESS_OVERHEAD_SECONDS,
            "outer_watchdog_seconds": outer_watchdog_seconds,
            "stderr_artifact": stderr_artifact,
        },
        "result": result,
    }
    try:
        atomic_write_json(destination, record, replace=False)
    except LedgerError as error:
        raise ProtocolError(str(error)) from error
    return record


def write_summary(output_dir: Path, summary: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_dir / "summary.json", summary, replace=True)
    title = (
        "# Week 1 BrowserGym formal matrix"
        if summary["evidence_scope"] == "formal-full-matrix"
        else "# Week 1 BrowserGym partial debug evidence (not the formal matrix)"
    )
    lines = [
        title,
        "",
        f"- Evidence scope: {summary['evidence_scope']}",
        f"- Planned attempts: {summary['planned_attempts']}",
        f"- Attempt artifacts: {summary['recorded_attempt_artifacts']}",
        f"- Matrix artifacts complete: {summary['matrix_artifacts_complete']}",
        f"- Record contracts valid: {summary['record_contracts_valid']}",
        f"- Source commit: {summary['source_provenance']['commit']}",
        f"- Source clean: {summary['source_provenance']['clean']}",
        f"- Manifest SHA-256: {summary['source_provenance']['manifest_sha256']}",
        f"- Source provenance consistent: {summary['source_provenance_consistent']}",
        (
            "- Provider entitlement consistent: "
            f"{summary['provider_entitlement_consistent']} "
            f"({summary.get('provider_entitlement', {}).get('response_model') if summary.get('provider_entitlement') else None})"
        ),
        (
            "- Complete private artifact bundles: "
            f"{summary['artifact_bundle_complete_attempts']}/{summary['planned_attempts']}"
        ),
        (
            "- Formal-matrix eligible attempts: "
            f"{summary['formal_matrix_eligible_attempts']}/{summary['planned_attempts']}"
        ),
        (
            "- Analysis-eligible attempts: "
            f"{summary['analysis_eligible_attempts']}/{summary['planned_attempts']}"
        ),
        f"- All 27 formal-matrix eligible: {summary['all_27_formal_matrix_eligible']}",
        f"- Operationally complete: {summary['operationally_complete']}",
        f"- Official-reward successes: {summary['official_reward_successes']}",
        f"- Statuses: {json.dumps(summary['status_counts'], sort_keys=True)}",
        "",
        summary["interpretation"],
    ]
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def summary_exit_code(summary: dict[str, Any]) -> int:
    """Task reward failures are evidence; operational error/timeout is nonzero."""
    return 0 if summary.get("operationally_complete") is True else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=FORMAL_STUDY_CONFIG)
    parser.add_argument("--capsule", type=Path, default=RELEASE_CAPSULE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Attempt output root for live or resumed runs (never inside research/evidence)",
    )
    parser.add_argument("--attempt-id", help="Exact canonical attempt ID to run or resume")
    parser.add_argument("--resume", action="store_true", help="Validate and skip complete immutable bundles")
    parser.add_argument("--framework", choices=["browser-use", "skyvern"])
    parser.add_argument("--task")
    parser.add_argument("--repetition", type=int, choices=[1, 2, 3])
    parser.add_argument("--run-label", help="Stable label for filtered partial/debug evidence")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan and preflight without writing files")
    parser.add_argument("--preflight", action="store_true", help="Run readiness checks only")
    parser.add_argument("--allow-unready", action="store_true", help="Return zero from preflight even when dependencies are missing")
    parser.add_argument("--require-clean", action="store_true", help="Require a clean Git worktree before execution")
    args = parser.parse_args()
    if args.attempt_id and any((args.framework, args.task, args.repetition, args.run_label)):
        parser.error("--attempt-id cannot be combined with legacy partial-run filters")
    return args


def main() -> int:
    args = parse_args()
    config_path = Path(getattr(args, "config", FORMAL_STUDY_CONFIG))
    capsule_path = Path(getattr(args, "capsule", RELEASE_CAPSULE))
    attempt_id = getattr(args, "attempt_id", None)
    resume = bool(getattr(args, "resume", False))
    framework_filter = getattr(args, "framework", None)
    task_filter = getattr(args, "task", None)
    repetition_filter = getattr(args, "repetition", None)
    run_label_arg = getattr(args, "run_label", None)
    binding = None
    binding_error: str | None = None
    try:
        manifest = load_manifest(args.manifest.resolve())
        matrix = build_matrix(manifest)
        if attempt_id is not None and any(
            (framework_filter, task_filter, repetition_filter, run_label_arg)
        ):
            raise ProtocolError(
                "--attempt-id cannot be combined with legacy partial-run filters"
            )
        selected = (
            select_attempt_id(matrix, attempt_id)
            if attempt_id is not None
            else filter_matrix(matrix, framework_filter, task_filter, repetition_filter)
        )
        is_unfiltered = (
            attempt_id is None
            and framework_filter is None
            and task_filter is None
            and repetition_filter is None
        )
        canonical_manifest = is_canonical_tracked_manifest(args.manifest, manifest)
        formal_ledger_mode = canonical_manifest and (is_unfiltered or attempt_id is not None)
        if formal_ledger_mode:
            evidence_scope = FORMAL_EVIDENCE_SCOPE
        else:
            evidence_scope = "partial-debug-evidence"
        if resume and not formal_ledger_mode:
            raise ProtocolError("--resume is supported only by the canonical formal study ledger")
        if not formal_ledger_mode:
            run_label = run_label_arg or datetime.now(timezone.utc).strftime("debug-%Y%m%dT%H%M%SZ")
            selected = label_partial_attempts(selected, run_label)
        selected_frameworks = {attempt["framework"]["id"] for attempt in selected}
        formal_execution = formal_ledger_mode and not args.dry_run and not args.preflight
        if formal_execution:
            raise ProtocolError(
                "the historical 27-cell Week 1 matrix cannot execute: Stagehand 3.7.1 "
                "is retired from active install and dispatch; retained formal completion "
                "remains 0/27"
            )
        preflight = run_preflight(
            manifest,
            require_clean=args.require_clean or (formal_execution and not resume),
            require_repository_defaults=formal_execution,
            require_provider_entitlement=formal_ledger_mode and not args.dry_run,
            framework_ids=None if is_unfiltered else selected_frameworks,
        )
        # The runner validates the formal binding whenever it can produce a
        # plan, execute, or inspect a resume.  A dry run only prints a plan, so
        # an unverifiable (archived, unsealable) capsule is reported, not fatal.
        if formal_ledger_mode and (
            preflight["ready"] or args.dry_run or args.preflight or resume
        ):
            try:
                binding = load_binding(
                    config_path,
                    capsule_path,
                    require_sealed=formal_execution,
                )
                if stage_manifest(binding, "week1").resolve() != args.manifest.resolve():
                    raise ProtocolError("formal config is not bound to the selected Week 1 manifest")
                config_matrix = binding.config["week1_miniwob"]["matrix"]
                if set(config_matrix["frameworks"]) != {
                    row["id"] for row in manifest["frameworks"]
                } or set(config_matrix["tasks"]) != {row["id"] for row in manifest["tasks"]}:
                    raise ProtocolError("formal config matrix differs from the benchmark manifest")
            except (FormalStudyError, ProtocolError) as error:
                if not args.dry_run:
                    raise
                binding, binding_error = None, str(error)
    except (FormalStudyError, LedgerError, ProtocolError) as error:
        print(json.dumps({"ready": False, "error": str(error)}, indent=2), file=sys.stderr)
        return 2
    if args.dry_run or args.preflight:
        payload = {
            "schema_version": "cui-week1-dry-run-v1" if args.dry_run else "cui-week1-preflight-output-v1",
            "preflight": preflight,
            "evidence_scope": evidence_scope,
            "planned_attempt_count": len(selected),
            "attempts": selected if args.dry_run else [],
            "writes_performed": False,
            "resume_requested": resume,
            "formal_study_binding": binding.ledger_binding() if binding else None,
        }
        if binding_error is not None:
            payload["formal_study_binding_error"] = binding_error
        print(json.dumps(payload, indent=2))
        return 0 if preflight["ready"] or args.allow_unready or args.dry_run else 2
    try:
        if args.output_dir is None:
            raise ProtocolError("--output-dir is required unless --dry-run or --preflight is used")
        output_dir = require_output_outside_evidence(args.output_dir)
    except ProtocolError as error:
        print(json.dumps({"ready": False, "error": str(error)}, indent=2), file=sys.stderr)
        return 2
    resume_selection_complete = False
    if formal_ledger_mode and resume:
        try:
            assert binding is not None
            early_output = output_dir
            early_ledger = ledger_payload(
                stage="week1",
                binding=binding.ledger_binding(),
                protocol_sha256=matrix[0]["manifest_sha256"],
                planned_ids=[row["attempt_id"] for row in matrix],
            )
            if (early_output / "study_ledger.json").is_file():
                initialize_or_resume(early_output, early_ledger, resume=True)
                resume_selection_complete = all(
                    validate_resumable_attempt(
                        early_output / "attempts" / f"{attempt['attempt_id']}.json",
                        attempt,
                        evidence_scope=evidence_scope,
                    )
                    for attempt in selected
                    if (early_output / "attempts" / f"{attempt['attempt_id']}.json").is_file()
                ) and all(
                    (early_output / "attempts" / f"{attempt['attempt_id']}.json").is_file()
                    for attempt in selected
                )
        except (LedgerError, ProtocolError) as error:
            print(json.dumps({"ready": False, "error": str(error)}, indent=2), file=sys.stderr)
            return 2
    if not preflight["ready"] and not resume_selection_complete:
        print(json.dumps(preflight, indent=2), file=sys.stderr)
        print("Formal run blocked by preflight; no attempt artifacts were written.", file=sys.stderr)
        return 2
    resumed_ids: list[str] = []
    executed_ids: list[str] = []
    try:
        if formal_ledger_mode:
            assert binding is not None
            expected_ledger = ledger_payload(
                stage="week1",
                binding=binding.ledger_binding(),
                protocol_sha256=selected[0]["manifest_sha256"],
                planned_ids=[row["attempt_id"] for row in matrix],
            )
            # Validate an existing ledger before any environment-dependent
            # work.  A complete resume therefore does not need an API key.
            ledger_exists = (output_dir / "study_ledger.json").is_file()
            if resume and not ledger_exists and output_dir.is_dir() and any(output_dir.iterdir()):
                raise LedgerError(
                    "resume output contains evidence but no immutable study_ledger.json"
                )
            if ledger_exists:
                initialize_or_resume(output_dir, expected_ledger, resume=resume)
            existing: dict[str, dict[str, Any]] = {}
            for attempt in selected:
                path = output_dir / "attempts" / f"{attempt['attempt_id']}.json"
                if path.exists():
                    if not resume:
                        raise ProtocolError(
                            "attempt evidence already exists; use --resume or a new --output-dir"
                        )
                    existing[attempt["attempt_id"]] = validate_resumable_attempt(
                        path, attempt, evidence_scope=evidence_scope
                    )
                    resumed_ids.append(attempt["attempt_id"])
            pending = [row for row in selected if row["attempt_id"] not in existing]
            if pending:
                # Preflight was computed above before any ledger write.  Only
                # now establish source provenance and initialize a new root.
                all_existing_records = (
                    read_ledger_records(output_dir, matrix, evidence_scope=evidence_scope)
                    if ledger_exists
                    else []
                )
                source_provenance = (
                    capture_resume_source_provenance(
                        all_existing_records,
                        manifest_sha256=pending[0]["manifest_sha256"],
                        output_dir=output_dir,
                    )
                    if all_existing_records
                    else capture_source_provenance(
                        pending[0]["manifest_sha256"], require_clean=True
                    )
                )
                if not ledger_exists:
                    initialize_or_resume(output_dir, expected_ledger, resume=resume)
                for record in existing.values():
                    if record["orchestrator"]["source_provenance"] != source_provenance:
                        raise ProtocolError(
                            "resume source provenance differs from existing immutable attempts"
                        )
                for attempt in pending:
                    with claim(output_dir, attempt["attempt_id"]):
                        destination = output_dir / "attempts" / f"{attempt['attempt_id']}.json"
                        if destination.exists():
                            # A second process may have completed immediately
                            # before this claim.  Validate; never overwrite.
                            if not resume:
                                raise ProtocolError("concurrent attempt evidence appeared")
                            validate_resumable_attempt(
                                destination,
                                attempt,
                                evidence_scope=evidence_scope,
                                source_provenance=source_provenance,
                            )
                            resumed_ids.append(attempt["attempt_id"])
                            continue
                        execute_attempt(
                            attempt,
                            output_dir,
                            evidence_scope=evidence_scope,
                            source_provenance=source_provenance,
                            provider_entitlement=preflight.get("provider_entitlement"),
                        )
                        executed_ids.append(attempt["attempt_id"])
            records = read_ledger_records(
                output_dir, matrix, evidence_scope=evidence_scope
            )
            summary_planned = matrix
        else:
            source_provenance = capture_source_provenance(
                selected[0]["manifest_sha256"],
                require_clean=args.require_clean,
            )
            records = [
                execute_attempt(
                    attempt,
                    output_dir,
                    evidence_scope=evidence_scope,
                    source_provenance=source_provenance,
                    provider_entitlement=preflight.get("provider_entitlement"),
                )
                for attempt in selected
            ]
            executed_ids = [row["attempt_id"] for row in selected]
            summary_planned = selected
    except (LedgerError, ProtocolError) as error:
        print(json.dumps({"ready": False, "error": str(error)}, indent=2), file=sys.stderr)
        return 2
    summary = aggregate_records(records, summary_planned)
    summary["evidence_scope"] = evidence_scope
    summary["is_full_27_attempt_matrix"] = formal_ledger_mode
    summary["selected_attempt_ids"] = [row["attempt_id"] for row in selected]
    summary["executed_attempt_ids"] = executed_ids
    summary["resumed_attempt_ids"] = sorted(set(resumed_ids))
    summary["task_success_count"] = summary["official_reward_successes"]
    summary["formal_bundle_completeness"] = {
        "complete": summary.get("selected_artifact_bundles_complete") is True,
        "complete_bundles": summary.get("artifact_bundle_complete_attempts", 0),
        "planned_bundles": summary.get("planned_attempts", 0),
    }
    write_summary(output_dir, summary)
    print(json.dumps(summary, indent=2))
    if formal_ledger_mode and len(selected) < len(matrix):
        selected_ids = {row["attempt_id"] for row in selected}
        selected_records = [
            row for row in records if row.get("attempt", {}).get("attempt_id") in selected_ids
        ]
        return 0 if len(selected_records) == len(selected) and all(
            row.get("result", {}).get("artifact_bundle_complete") is True
            for row in selected_records
        ) else 1
    return summary_exit_code(summary)


if __name__ == "__main__":
    raise SystemExit(main())
