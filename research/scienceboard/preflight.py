"""Fail-closed host preflight for a formal ScienceBoard recording batch.

The preflight is read-only: it inspects the host, the private storage root, the
pinned ScienceBoard checkout and the private driver configuration, and runs the
host capability doctor against a freshly built guest bundle in a temporary
directory. ``--allow-unready`` reports every blocking check but exits 0.

    python -m research.scienceboard.preflight --json --allow-unready
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import string
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from computer_use_agent.capture.x11.bundle import build as build_guest_bundle
from computer_use_agent.capture.x11.calibration import evaluate_calibration
from research.scienceboard.private_storage import (
    PRIVATE_ROOT_ENV,
    PrivateStorageError,
    is_private_path,
    private_root,
)
from research.scienceboard.worker_contract import (
    DEFAULT_GUEST_CONTROL_ROOT,
    DEFAULT_GUEST_DATA_ROOT,
    DEPRECATED_GUEST_VARIABLES,
    EXPECTED_VM_ZIP_SHA256,
    OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL,
    PHASE_RESPONSE_MARGIN_SECONDS,
)
from research.scienceboard.worker_contract import (
    PINNED_COMMIT as PINNED_SCIENCEBOARD_COMMIT,
)

TASKS_ROOT = Path(__file__).resolve().parent / "tasks"
DEFAULT_MANIFEST = TASKS_ROOT / "manifest.json"
VMWARE_PROVIDER_SOURCE = (
    Path(__file__).resolve().parent / "runtime_compat" / "desktop_env" / "vmware.py"
)
MINIMUM_FREE_GIB = 100
# The task choice passes only after an independent reviewer confirmed it and
# the manifest records exactly this status.
TASK_SELECTION_CONFIRMED_STATUS = "researcher-selected, independently confirmed"


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    blocking: bool
    detail: str


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        encoding="utf-8",
        stderr=subprocess.STDOUT,
        timeout=20,
    ).strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_vmware_provider() -> ModuleType:
    """Load the runtime's VMware provider module by path (standard library only).

    Importing the ``desktop_env`` package would pull in ``requests`` through its
    controller; the provider module itself needs only the standard library.
    """

    spec = importlib.util.spec_from_file_location(
        "_scienceboard_preflight_vmware", VMWARE_PROVIDER_SOURCE
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {VMWARE_PROVIDER_SOURCE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _discover_vmrun() -> Path | None:
    """Find ``vmrun`` with exactly the rule the runtime provider uses."""

    discovered = _load_vmware_provider().discover_vmrun()
    return discovered if isinstance(discovered, Path) else None


def _placeholders(argv: list[str]) -> set[str]:
    fields: set[str] = set()
    formatter = string.Formatter()
    for item in argv:
        for _, field, _, _ in formatter.parse(item):
            if field:
                fields.add(field)
    return fields


COMMAND_PLACEHOLDERS = {
    "capability_doctor": {
        "host_worker",
        "scienceboard_root",
        "scienceboard_commit",
        "task_bindings_json",
        "guest_bundle_root",
    },
    "prepare_session": {
        "host_worker",
        "worker_spec_file",
        "worker_state_file",
        "worker_nonce_file",
    },
    "initialize": {
        "host_worker",
        "worker_state_file",
        "worker_nonce_file",
        "session_id",
    },
    "start_recorder": {
        "host_worker",
        "worker_state_file",
        "worker_nonce_file",
        "session_id",
    },
    "run_human": {
        "host_worker",
        "worker_state_file",
        "worker_nonce_file",
        "session_id",
        "timeout_seconds",
    },
    "run_aio": {
        "host_worker",
        "worker_state_file",
        "worker_nonce_file",
        "session_id",
        "timeout_seconds",
    },
    "run_seeact": {
        "host_worker",
        "worker_state_file",
        "worker_nonce_file",
        "session_id",
        "timeout_seconds",
    },
    "screenshot": {
        "host_worker",
        "worker_state_file",
        "worker_nonce_file",
        "session_id",
    },
    "stop_recorder": {
        "host_worker",
        "worker_state_file",
        "worker_nonce_file",
        "session_id",
    },
    "validate": {
        "host_worker",
        "worker_state_file",
        "worker_nonce_file",
        "session_id",
    },
    "fetch_artifacts": {
        "host_worker",
        "worker_state_file",
        "worker_nonce_file",
        "session_id",
    },
}


def _driver_is_valid(
    path: Path, manifest: Mapping[str, Any] | None = None
) -> tuple[bool, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        return False, f"driver config cannot be parsed: {type(error).__name__}"
    if payload.get("schema_version") != "scienceboard-driver-v3":
        return False, "driver schema must be scienceboard-driver-v3"
    commands = payload.get("commands")
    if not isinstance(commands, dict):
        return False, "driver commands must be an object"
    missing = sorted(set(COMMAND_PLACEHOLDERS) - set(commands))
    if missing:
        return False, f"driver config missing commands: {missing}"
    for name, required_fields in COMMAND_PLACEHOLDERS.items():
        command = commands[name]
        argv = command.get("argv") if isinstance(command, dict) else None
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) for item in argv)
        ):
            return False, f"driver command {name} requires a non-empty string argv list"
        if any(
            marker in item.upper() for item in argv for marker in ("REPLACE_WITH", "UNCONFIGURED")
        ):
            return False, f"driver command {name} still contains an unresolved executable"
        if command.get("shell") not in (None, False):
            return False, f"driver command {name} may not enable a shell"
        missing_fields = sorted(required_fields - _placeholders(argv))
        if missing_fields:
            return False, f"driver command {name} omits protocol placeholders: {missing_fields}"
    forbidden = {
        "guest_scienceboard_root",
        "aio_launcher",
        "seeact_launcher",
        "validator_launcher",
        "guest_task_path",
    }
    serialized = json.dumps(payload, sort_keys=True)
    leaked = sorted(field for field in forbidden if "{" + field + "}" in serialized)
    if leaked:
        return False, f"driver retains forbidden guest-side agent/validator fields: {leaked}"
    margin_errors = _timeout_margin_errors(commands, manifest)
    if margin_errors:
        return False, "driver phase timeout margins are too small: " + "; ".join(margin_errors)
    return True, (
        "driver is shell-free and binds one authenticated host worker; "
        "the guest surface is recorder-only"
    )


def _render_argv(template: list[str], context: dict[str, object]) -> list[str]:
    try:
        return [item.format_map(context) for item in template]
    except KeyError as error:
        raise ValueError(f"Unknown capability-doctor placeholder: {error.args[0]}") from error


def _is_private_path(path: Path | None) -> bool:
    """True only for a path under ``CUA_PRIVATE_ROOT`` and outside the repository."""

    return is_private_path(path)


def _task_selection_gate(manifest: Mapping[str, Any]) -> tuple[bool, str]:
    """Require one exact, reviewable task-selection confirmation from the manifest."""

    observed = manifest.get("selection_status")
    passed = observed == TASK_SELECTION_CONFIRMED_STATUS
    if passed:
        return True, f"canonical task manifest records {TASK_SELECTION_CONFIRMED_STATUS!r}"
    return False, (
        "canonical task manifest has no confirmed task selection; "
        f"required={TASK_SELECTION_CONFIRMED_STATUS!r}; observed={observed!r}"
    )


def _phase_timeout(argv: list[str]) -> str | None:
    """Return the ``--timeout`` operand of a host-worker phase argv, if any."""

    for index, item in enumerate(argv[:-1]):
        if item == "--timeout":
            return argv[index + 1]
    return None


def _timeout_margin_errors(
    commands: Mapping[str, Any], manifest: Mapping[str, Any] | None
) -> list[str]:
    """Each driver command must outlive the worker phase it waits for.

    A phase client waits for its ``--timeout`` budget plus the response margin;
    the driver's subprocess ``timeout_seconds`` must exceed the budget by at
    least twice that margin, or the driver kills a client that is still
    legitimately waiting for the worker's answer.
    """

    actor_budgets = {
        "run_human": (manifest or {}).get("human_timeout_seconds"),
        "run_aio": (manifest or {}).get("agent_timeout_seconds"),
        "run_seeact": (manifest or {}).get("agent_timeout_seconds"),
    }
    errors: list[str] = []
    for name, command in commands.items():
        if not isinstance(command, Mapping) or not isinstance(command.get("argv"), list):
            continue
        operand = _phase_timeout([str(item) for item in command["argv"]])
        if operand is None:
            continue
        if operand == "{timeout_seconds}":
            budget = actor_budgets.get(name)
            if not isinstance(budget, (int, float)) or isinstance(budget, bool):
                errors.append(f"{name}: no protocol timeout is known for {{timeout_seconds}}")
                continue
            phase_budget = float(budget)
        else:
            try:
                phase_budget = float(operand)
            except ValueError:
                errors.append(f"{name}: --timeout must be a number or {{timeout_seconds}}")
                continue
        outer = command.get("timeout_seconds")
        if (
            not isinstance(outer, (int, float))
            or isinstance(outer, bool)
            or float(outer) < phase_budget + 2 * PHASE_RESPONSE_MARGIN_SECONDS
        ):
            errors.append(
                f"{name}: timeout_seconds={outer!r} must be >= phase budget "
                f"{phase_budget:g} + {2 * PHASE_RESPONSE_MARGIN_SECONDS:g}"
            )
    return errors


def _capability_report(
    driver_path: Path, manifest: dict[str, Any], context: dict[str, object]
) -> tuple[bool, str, dict[str, Any] | None]:
    driver = json.loads(driver_path.read_text(encoding="utf-8"))
    command = driver["commands"]["capability_doctor"]
    argv = _render_argv(command["argv"], context)
    try:
        completed = subprocess.run(
            argv,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=float(command.get("timeout_seconds", 60)),
            check=False,
            shell=False,
        )
        report = json.loads(completed.stdout)
    except Exception as error:
        return False, f"host capability doctor failed: {type(error).__name__}", None
    binding = report.get("binding", {})
    passed = (
        completed.returncode == 0
        and report.get("schema_version") == "scienceboard-host-capabilities-v3"
        and report.get("ready") is True
        and binding.get("scienceboard_commit") == manifest["upstream"]["commit"]
        and isinstance(binding.get("worker_source_sha256"), str)
        and len(binding["worker_source_sha256"]) == 64
        and all(check.get("passed") is True for check in report.get("checks", []))
        and bool(report.get("checks"))
    )
    return (
        passed,
        (
            "host ScienceBoard APIs, evaluator sources, task payloads, and recorder bundle are pinned"
            if passed
            else f"host capability report rejected; return_code={completed.returncode}"
        ),
        report,
    )


def _recompute_calibration(
    evidence_path: Path,
    certificate_path: Path,
    expected_binding: dict[str, Any],
) -> tuple[bool, str]:
    try:
        recomputed = evaluate_calibration(evidence_path)
        certificate = json.loads(certificate_path.read_text(encoding="utf-8"))
        def phase_signature(payload: Mapping[str, Any]) -> list[str]:
            return sorted(
                json.dumps(
                    [
                        phase.get("name"),
                        phase.get("trace_sha256"),
                        phase.get("session_id"),
                        phase.get("binding"),
                    ],
                    sort_keys=True,
                )
                for phase in payload.get("phases", [])
            )

        passed = (
            recomputed.get("passed") is True
            and certificate.get("schema_version") == "scienceboard-x11-calibration-certificate-v1"
            and certificate.get("passed") is True
            and certificate.get("evidence_sha256") == _sha256(evidence_path)
            and certificate.get("evidence_sha256") == recomputed.get("evidence_sha256")
            and certificate.get("binding") == recomputed.get("binding")
            and recomputed.get("binding") == expected_binding
            and certificate.get("cross_phase_checks") == recomputed.get("cross_phase_checks")
            and phase_signature(certificate) == phase_signature(recomputed)
        )
        return (
            passed,
            "calibration evidence recomputed and certificate matched"
            if passed
            else "certificate differs from recomputed evidence",
        )
    except Exception as error:
        return False, f"calibration recomputation failed: {type(error).__name__}"


def run_preflight(manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    checks: list[Check] = []
    capability: dict[str, Any] | None = None

    def add(name: str, passed: bool, detail: str, *, blocking: bool = True) -> None:
        checks.append(Check(name, bool(passed), blocking, detail))

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        binding = manifest["guest_binding"]
        valid_manifest = (
            manifest.get("schema_version") == "scienceboard-task-matrix-v2"
            and manifest.get("upstream", {}).get("commit") == PINNED_SCIENCEBOARD_COMMIT
            and manifest.get("upstream", {}).get("source_ref") == PINNED_SCIENCEBOARD_COMMIT
            and len(manifest.get("tasks", [])) == 3
            and len(manifest.get("actors", [])) == 3
            and binding
            == {
                "guest_id": "scienceboard-vm-sci-bench-v1",
                "guest_os_id": "ubuntu",
                "guest_os_version": "22.04",
                "screen_resolution": {"width": 1920, "height": 1080},
                "verification_policy": binding.get("verification_policy"),
            }
            and isinstance(binding.get("verification_policy"), str)
            and bool(binding.get("verification_policy"))
            and manifest.get("agent_model") == "gpt-5.5"
            and manifest.get("agent_action_limit") == 5
            and manifest.get("agent_timeout_seconds") == 480
            and manifest.get("human_timeout_seconds") == 300
        )
        add(
            "task_manifest",
            valid_manifest,
            "pinned 27-cell v2 protocol parsed"
            if valid_manifest
            else "task matrix/provenance pins are invalid",
        )
    except Exception as error:
        add("task_manifest", False, f"manifest cannot be parsed: {type(error).__name__}")
        manifest, binding = {}, {}

    add(
        "python_version",
        sys.version_info >= (3, 12),
        f"Python {platform.python_version()} (requires >=3.12)",
    )
    private: Path | None = None
    try:
        private = private_root()
        private_ok = private.is_dir()
        private_detail = (
            f"{PRIVATE_ROOT_ENV} resolves outside the repository"
            if private_ok
            else f"{PRIVATE_ROOT_ENV} does not name an existing directory"
        )
    except PrivateStorageError as error:
        private_ok, private_detail = False, str(error)
    add("private_root", private_ok, private_detail)
    if private is not None and private.is_dir():
        try:
            free_gb = shutil.disk_usage(private).free / (1024**3)
            add(
                "free_disk",
                free_gb >= MINIMUM_FREE_GIB,
                f"{free_gb:.1f} GiB free on the {PRIVATE_ROOT_ENV} volume; "
                f"requires >={MINIMUM_FREE_GIB} GiB",
            )
        except OSError as error:
            add("free_disk", False, f"disk query failed: {type(error).__name__}")
    else:
        add("free_disk", False, f"not measured because {PRIVATE_ROOT_ENV} is unavailable")

    try:
        vmrun = _discover_vmrun()
    except Exception as error:
        vmrun = None
        vmrun_detail = f"vmrun discovery failed: {type(error).__name__}"
    else:
        vmrun_detail = str(vmrun) if vmrun else "vmrun was not found via VMRUN_PATH or PATH"
    add("vmrun", vmrun is not None, vmrun_detail)
    vm_path = Path(os.environ["VM_PATH"]).resolve() if os.environ.get("VM_PATH") else None
    add(
        "vm_path",
        bool(vm_path and vm_path.is_file() and vm_path.suffix.lower() == ".vmx"),
        str(vm_path) if vm_path else "VM_PATH is not set (an existing .vmx is required)",
    )

    provenance_path = (
        Path(os.environ["SCIENCEBOARD_VM_PROVENANCE"]).resolve()
        if os.environ.get("SCIENCEBOARD_VM_PROVENANCE")
        else None
    )
    provenance_valid, provenance_detail = False, "SCIENCEBOARD_VM_PROVENANCE is not set"
    if provenance_path and provenance_path.is_file():
        try:
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            provenance_valid = (
                provenance.get("schema_version") == "scienceboard-vm-provenance-v1"
                and provenance.get("vm_zip_sha256") == EXPECTED_VM_ZIP_SHA256
                and provenance.get("expected_sha256") == EXPECTED_VM_ZIP_SHA256
                and provenance.get("passed") is True
                and _is_private_path(provenance_path)
            )
            provenance_detail = (
                f"official VM hash certificate matches under {PRIVATE_ROOT_ENV}"
                if provenance_valid
                else "VM provenance/hash/private-path gate failed"
            )
        except Exception as error:
            provenance_detail = f"VM provenance certificate invalid: {type(error).__name__}"
    add("vm_provenance", provenance_valid, provenance_detail)

    scienceboard_root = (
        Path(os.environ["SCIENCEBOARD_ROOT"]).resolve()
        if os.environ.get("SCIENCEBOARD_ROOT")
        else None
    )
    source_ok, source_detail = False, "SCIENCEBOARD_ROOT is not set"
    if scienceboard_root and (scienceboard_root / ".git").is_dir():
        try:
            commit, dirty = (
                _git(scienceboard_root, "rev-parse", "HEAD"),
                _git(scienceboard_root, "status", "--porcelain"),
            )
            source_ok = commit == manifest.get("upstream", {}).get("commit") and not dirty
            source_detail = f"commit={commit}; pinned={source_ok}; clean={not bool(dirty)}"
        except Exception as error:
            source_detail = f"ScienceBoard git inspection failed: {type(error).__name__}"
    add("scienceboard_source", source_ok, source_detail)

    task_match, details = bool(scienceboard_root and source_ok and manifest.get("tasks")), []
    if task_match:
        for task in manifest["tasks"]:
            try:
                matches = json.loads(
                    (TASKS_ROOT / task["local_file"]).read_text(encoding="utf-8")
                ) == json.loads(
                    (scienceboard_root / task["upstream_path"]).read_text(encoding="utf-8")
                )
            except Exception:
                matches = False
            task_match = task_match and matches
            details.append(f"{task['application']}={matches}")
    add(
        "official_task_payloads",
        task_match,
        ", ".join(details) or "official pinned checkout unavailable",
    )

    add(
        "openai_api_key",
        bool(os.environ.get("OPENAI_API_KEY")),
        "OPENAI_API_KEY is present"
        if os.environ.get("OPENAI_API_KEY")
        else "OPENAI_API_KEY is absent",
    )
    configured_model = os.environ.get("SCIENCEBOARD_MODEL")
    add(
        "agent_model",
        configured_model == manifest.get("agent_model"),
        f"configured={configured_model!r}; protocol={manifest.get('agent_model')!r}",
    )
    configured_endpoint = os.environ.get(
        "SCIENCEBOARD_OPENAI_CHAT_COMPLETIONS_URL",
        OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL,
    )
    add(
        "provider_endpoint",
        configured_endpoint == OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL,
        "official OpenAI Chat Completions endpoint"
        if configured_endpoint == OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL
        else "custom/fallback model endpoints are prohibited",
    )

    evidence_path = (
        Path(os.environ["SCIENCEBOARD_CALIBRATION_EVIDENCE"]).resolve()
        if os.environ.get("SCIENCEBOARD_CALIBRATION_EVIDENCE")
        else None
    )
    certificate_path = (
        Path(os.environ["SCIENCEBOARD_CALIBRATION_CERT"]).resolve()
        if os.environ.get("SCIENCEBOARD_CALIBRATION_CERT")
        else None
    )
    calibration_ok, calibration_detail = (
        False,
        "both calibration evidence and certificate are required",
    )
    if (
        evidence_path
        and evidence_path.is_file()
        and certificate_path
        and certificate_path.is_file()
    ):
        if _is_private_path(evidence_path) and _is_private_path(certificate_path):
            expected_calibration_binding = {
                "guest_id": binding["guest_id"],
                "guest_os_id": binding["guest_os_id"],
                "guest_os_version": binding["guest_os_version"],
                "screen_resolution": binding["screen_resolution"],
                "scienceboard_commit": manifest["upstream"]["commit"],
                "capture_adapter": "x11_vm",
                "recorder_version": "x11-v2",
            }
            calibration_ok, calibration_detail = _recompute_calibration(
                evidence_path, certificate_path, expected_calibration_binding
            )
        else:
            calibration_detail = (
                f"calibration evidence/certificate must remain under {PRIVATE_ROOT_ENV}"
            )
    add("x11_calibration", calibration_ok, calibration_detail)

    driver_path = (
        Path(os.environ["SCIENCEBOARD_DRIVER_CONFIG"]).resolve()
        if os.environ.get("SCIENCEBOARD_DRIVER_CONFIG")
        else None
    )
    driver_ok, driver_detail = (False, "SCIENCEBOARD_DRIVER_CONFIG is not set")
    if driver_path and driver_path.is_file():
        driver_ok, driver_detail = _driver_is_valid(driver_path, manifest)
        driver_ok = driver_ok and _is_private_path(driver_path)
        if not _is_private_path(driver_path):
            driver_detail = f"driver config must remain under {PRIVATE_ROOT_ENV}"
    add("driver_config", driver_ok, driver_detail)

    guest_data_root = os.environ.get("SCIENCEBOARD_GUEST_DATA_ROOT", DEFAULT_GUEST_DATA_ROOT)
    guest_control_root = os.environ.get(
        "SCIENCEBOARD_GUEST_CONTROL_DIR", DEFAULT_GUEST_CONTROL_ROOT
    )
    deprecated_guest_execution = {
        name: os.environ.get(name) for name in DEPRECATED_GUEST_VARIABLES
    }
    guest_execution_ok = (
        guest_data_root.startswith("/home/user/")
        and guest_control_root.startswith("/home/user/")
        and not any(deprecated_guest_execution.values())
    )
    add(
        "guest_recorder_config",
        guest_execution_ok,
        "guest paths are under /home/user and no guest-side agent/validator launcher is configured"
        if guest_execution_ok
        else (
            "guest paths must be under /home/user and deprecated guest agent/validator variables "
            "must be unset"
        ),
    )

    if driver_ok and driver_path is not None and scienceboard_root and guest_execution_ok:
        task_bindings = json.dumps(
            [
                {key: task[key] for key in ("id", "upstream_path", "canonical_payload_sha256")}
                for task in manifest["tasks"]
            ],
            separators=(",", ":"),
        )
        # The doctor inspects the exact bundle a formal session would deploy; it
        # is built into a temporary directory so the preflight writes nothing.
        with tempfile.TemporaryDirectory(prefix="cua-guest-bundle-") as bundle_dir:
            build_guest_bundle(Path(bundle_dir))
            capability_context = {
                "host_worker": str(Path(__file__).resolve().with_name("host_worker.py")),
                "scienceboard_root": str(scienceboard_root),
                "scienceboard_commit": manifest["upstream"]["commit"],
                "task_bindings_json": task_bindings,
                "guest_bundle_root": bundle_dir,
            }
            capability_ok, capability_detail, capability = _capability_report(
                driver_path, manifest, capability_context
            )
    else:
        capability_ok, capability_detail = (
            False,
            "driver/source unavailable; host APIs not measured",
        )
    add("host_capabilities", capability_ok, capability_detail)

    snapshot_ok, snapshot_detail = (
        False,
        "snapshot not queried because vmrun/VM_PATH is unavailable",
    )
    if vmrun and vm_path and vm_path.is_file() and vm_path.suffix.lower() == ".vmx":
        try:
            output = subprocess.check_output(
                [str(vmrun), "listSnapshots", str(vm_path)],
                text=True,
                encoding="utf-8",
                stderr=subprocess.STDOUT,
                timeout=30,
            )
            snapshot_ok = manifest.get("snapshot") in {line.strip() for line in output.splitlines()}
            snapshot_detail = (
                f"required snapshot={manifest.get('snapshot')!r}; present={snapshot_ok}"
            )
        except Exception as error:
            snapshot_detail = f"snapshot query failed: {type(error).__name__}"
    add("snapshot", snapshot_ok, snapshot_detail)
    selection_ok, selection_detail = _task_selection_gate(manifest)
    add("task_selection_confirmed", selection_ok, selection_detail)
    ready = all(check.passed for check in checks if check.blocking)
    return {
        "schema_version": "scienceboard-preflight-v3",
        "ready_for_formal_run": ready,
        "checks": [asdict(check) for check in checks],
        "host_capability_report": capability,
        "guest_capability_report": capability,
        "secret_values_logged": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--allow-unready", action="store_true")
    args = parser.parse_args(argv)
    report = run_preflight(args.manifest)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for check in report["checks"]:
            marker = "PASS" if check["passed"] else "BLOCK" if check["blocking"] else "NOTE"
            print(f"[{marker}] {check['name']}: {check['detail']}")
        print(f"ready_for_formal_run={report['ready_for_formal_run']}")
    return 0 if report["ready_for_formal_run"] or args.allow_unready else 2


if __name__ == "__main__":
    raise SystemExit(main())
