"""Read-only source, dependency, and DesktopEnv compatibility doctor."""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
import re
import subprocess
import sys
import zipfile
from collections.abc import Mapping
from importlib import metadata as importlib_metadata
from pathlib import Path

try:
    from .worker_contract import (
        EVALUATOR_SOURCES,
        GUEST_BUNDLE_NAME,
        HOST_DOCTOR_SCHEMA,
        PINNED_COMMIT,
        WorkerProtocolError,
        sha256,
        worker_source_manifest,
        worker_source_sha256,
    )
except ImportError:  # Direct ``host_worker.py`` execution.
    from worker_contract import (  # type: ignore[no-redef, import-not-found]
        EVALUATOR_SOURCES,
        GUEST_BUNDLE_NAME,
        HOST_DOCTOR_SCHEMA,
        PINNED_COMMIT,
        WorkerProtocolError,
        sha256,
        worker_source_manifest,
        worker_source_sha256,
    )


HOST_DEPENDENCY_VERSIONS = {
    "Pillow": "12.3.0",
    "requests": "2.34.2",
    "tiktoken": "0.13.0",
}
DESKTOP_ENV_INIT_ARGUMENTS = (
    "self", "provider_name", "region", "path_to_vm", "snapshot_name",
    "action_space", "cache_dir", "screen_size", "headless",
    "require_a11y_tree", "require_terminal", "os_type",
)
RUNTIME_COMPAT_HASHES = {
    "NOTICE.md": "5222d5cbb3b0d32b073e6ebce20f66d86d540a39599d617bb391ea8542afbc94",
    "desktop_env/__init__.py": "2fc939f79850817e64269269d58f53d162577af2708f44f0acb4a0cff2657732",
    "desktop_env/controller.py": "6855d1d9d2343511b5df9fa820800aac16e1092d0c3dcf8a485aae234d6328e1",
    "desktop_env/desktop_env.py": "08ad50edf2a9141b750e6c8145aa87863f5020c09adaa0304d4d8522adb3884a",
    "desktop_env/vmware.py": "e6f4f12bf9964de98f9594e61364300b72d66ff80f36d6609501c1a2317c4b89",
    "provenance.json": "83ef34f31c4fa6ff511380d5db088436592a8a2db002df6a363b4d952bdbce3a",
}
RUNTIME_COMPAT_SIGNATURES: dict[str, dict[str, tuple[str, ...]]] = {
    "desktop_env/desktop_env.py": {
        "DesktopEnv.__init__": DESKTOP_ENV_INIT_ARGUMENTS,
        "DesktopEnv._start_emulator": ("self",),
        "DesktopEnv._revert_to_snapshot": ("self",),
        "DesktopEnv.close": ("self",),
    },
    "desktop_env/controller.py": {
        "PythonController.__init__": ("self", "vm_ip", "server_port"),
        "PythonController.get_screenshot": ("self",),
        "PythonController.get_accessibility_tree": ("self",),
        "PythonController.get_terminal_output": ("self",),
        "PythonController.execute_python_command": ("self", "command"),
        "PythonController.start_recording": ("self",),
        "PythonController.end_recording": ("self", "destination"),
        "PythonController.close": ("self",),
    },
    "desktop_env/vmware.py": {
        "VMwareProvider.__init__": ("self", "region"),
        "VMwareProvider.start_emulator": (
            "self", "path_to_vm", "headless", "os_type",
        ),
        "VMwareProvider.get_ip_address": ("self", "path_to_vm"),
        "VMwareProvider.revert_to_snapshot": (
            "self", "path_to_vm", "snapshot_name",
        ),
        "VMwareProvider.stop_emulator": ("self", "path_to_vm"),
    },
}

SOURCE_HASHES = {
    "sci/base/task.py": "1fea84f4c5d9d4b888ea333bccfeff5a640c8a63dc83fab0d9a2e8c0c6b1b456",
    "sci/vm/vtask.py": "8fc5b82a8359e163c163239e952454e8626b4ab325f0e6bc8e27019cfdaf6acc",
    "sci/vm/vmanager.py": "bace9ae0b45a0b74532b5996d7b7a5a942cd1ffc34e827948cca1a1d520966f9",
    "sci/base/community.py": "cfb0f2eb331d226e9e6450fceb6a788c6d6cb418b818dd43e17ed7e074a2ae95",
    "sci/base/log.py": "51faf5c0c24f3641ce57c0b7846d1b30286fee34575111043e7ff0d42a999abf",
    "sci/Tester.py": "bd94579e469a5428e83c6ad92c4dc6ae663dfbf26462a26cc0621a2c616d6108",
    "sci/base/model.py": "dd7be783da6d81c0ee33bd05cd1ef44ddb4f2c175026c0b594fc1c2a2991fd29",
    "sci/base/agent.py": "ddc7b0d86509fda9e3578afc2e75c9f5fb8e9c4b6b7d6bf47099fa00e27d8863",
}

# Every member the guest zipapp may contain, and the members it must contain.
GUEST_BUNDLE_MEMBER_PATTERN = re.compile(
    r"^(__main__\.py|computer_use_agent/(__init__|trace/[a-z_]+|capture/__init__"
    r"|capture/x11/[a-z_0-9]+)\.py)$"
)
REQUIRED_GUEST_BUNDLE_MEMBERS = (
    "__main__.py",
    "computer_use_agent/__init__.py",
    "computer_use_agent/capture/__init__.py",
    "computer_use_agent/capture/x11/__init__.py",
    "computer_use_agent/capture/x11/__main__.py",
    "computer_use_agent/capture/x11/guest_controller.py",
    "computer_use_agent/capture/x11/recorder.py",
    "computer_use_agent/trace/__init__.py",
    "computer_use_agent/trace/io.py",
    "computer_use_agent/trace/privacy.py",
    "computer_use_agent/trace/validation.py",
)

# Line numbers are measured from the pinned AST and included in doctor output.
SOURCE_SIGNATURES: dict[str, dict[str, tuple[str, ...]]] = {
    "sci/base/task.py": {
        "Task.__init__": (
            "self", "config_path", "manager", "community", "obs_types",
            "primitives", "debug", "relative",
        ),
        "Task.init": ("self",),
        "Task.predict": ("self",),
        "Task.eval": ("self", "stop_type", "stop_args"),
    },
    "sci/vm/vtask.py": {
        "VTask.__init__": ("self", "config_path", "manager", "*args", "**kwargs"),
        "VTask._init": ("self",),
    },
    "sci/vm/vmanager.py": {
        "VManager.__init__": (
            "self", "version", "vm_path", "headless", "a11y_tree_limit",
            "**kwargs",
        ),
        "VManager.revert": ("self", "snapshot_name"),
        "VManager.__enter__": ("self",),
        "VManager.__exit__": ("self", "exc_type", "exc_value", "traceback"),
    },
    "sci/base/community.py": {
        "AllInOne.__call__": (
            "self", "steps", "inst", "obs", "code_info", "type_sort",
            "timeout", "manager",
        ),
        "SeeAct.__call__": (
            "self", "steps", "inst", "obs", "code_info", "type_sort",
            "timeout", "manager",
        ),
    },
    "sci/Tester.py": {
        "Tester.__init__": (
            "self", "tasks_path", "logs_path", "community", "obs_types",
            "vm_path", "headless", "ignore", "debug", "optimize",
            "relative", "primitives", "handle_managers",
        ),
    },
    "sci/base/log.py": {
        "Log.save": (
            "self", "step_index", "obs", "codes", "community", "is_textual",
        ),
        "Log.result_handler": ("method",),
    },
}


def _function_arguments(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[str, ...]:
    positional = [*node.args.posonlyargs, *node.args.args]
    names = [argument.arg for argument in positional]
    if node.args.vararg is not None:
        names.append("*" + node.args.vararg.arg)
    names.extend(argument.arg for argument in node.args.kwonlyargs)
    if node.args.kwarg is not None:
        names.append("**" + node.args.kwarg.arg)
    return tuple(names)


def inspect_source_signatures(
    path: Path,
    expected: Mapping[str, tuple[str, ...]],
) -> dict[str, object]:
    """Return exact AST signature/line evidence without importing ScienceBoard."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    observations: dict[str, object] = {}
    all_match = True
    for qualified, arguments in expected.items():
        class_name, method_name = qualified.split(".", 1)
        class_node = classes.get(class_name)
        method = next(
            (
                node
                for node in getattr(class_node, "body", [])
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == method_name
            ),
            None,
        )
        observed = None if method is None else _function_arguments(method)
        matches = observed == arguments
        all_match = all_match and matches
        observations[qualified] = {
            "expected_arguments": list(arguments),
            "observed_arguments": None if observed is None else list(observed),
            "line": None if method is None else method.lineno,
            "matches": matches,
        }
    return {"passed": all_match, "symbols": observations}


def _git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *arguments],
        text=True,
        encoding="utf-8",
        stderr=subprocess.STDOUT,
        timeout=20,
    ).strip()


def runtime_compat_doctor() -> dict[str, object]:
    """Verify the narrow, repo-owned DesktopEnv compatibility surface by hash."""

    root = (Path(__file__).resolve().parent / "runtime_compat").resolve()
    files: dict[str, dict[str, object]] = {}
    passed = True
    for relative, expected_hash in RUNTIME_COMPAT_HASHES.items():
        path = root / relative
        observed_hash = sha256(path) if path.is_file() else None
        signatures = (
            inspect_source_signatures(path, RUNTIME_COMPAT_SIGNATURES[relative])
            if path.is_file() and relative in RUNTIME_COMPAT_SIGNATURES
            else None
        )
        matches = observed_hash == expected_hash and (
            signatures is None or signatures.get("passed") is True
        )
        passed = passed and matches
        files[relative] = {
            "sha256": observed_hash,
            "expected_sha256": expected_hash,
            "signatures": signatures,
            "passed": matches,
        }

    provenance_path = root / "provenance.json"
    provenance_contract = False
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        upstream = provenance["upstream"]
        boundary = provenance["compatibility_boundary"]
        provenance_contract = (
            provenance["schema_version"]
            == "scienceboard-runtime-compat-provenance-v1"
            and upstream["distribution"] == "desktop-env"
            and upstream["version"] == "0.1.22"
            and upstream["wheel_sha256"]
            == "ca7d3c5bc5f219e073af404709528968301ad84de32cb7fca7a2144ae1ee92cd"
            and boundary["consumer"] == f"ScienceBoard commit {PINNED_COMMIT}"
            and boundary["provider"] == "vmware"
            and boundary["guest_os"] == "Ubuntu"
            and boundary["action_space"] == "pyautogui"
        )
    except (KeyError, OSError, TypeError, json.JSONDecodeError):
        provenance = None
    return {
        "passed": passed and provenance_contract,
        "implementation": "research/scienceboard/runtime_compat/desktop_env",
        "files": files,
        "provenance_contract": provenance_contract,
        "upstream": None if provenance is None else provenance.get("upstream"),
        "boundary": (
            None if provenance is None else provenance.get("compatibility_boundary")
        ),
    }



def activate_runtime_compat() -> Path:
    """Load only the verified repo-owned ``desktop_env`` package."""

    report = runtime_compat_doctor()
    if report["passed"] is not True:
        raise WorkerProtocolError("ScienceBoard runtime compatibility hashes drifted")
    root = (Path(__file__).resolve().parent / "runtime_compat").resolve()
    expected_module = (root / "desktop_env" / "desktop_env.py").resolve()
    existing = sys.modules.get("desktop_env.desktop_env")
    if existing is not None:
        observed = Path(str(getattr(existing, "__file__", ""))).resolve()
        if observed != expected_module:
            raise WorkerProtocolError("An unverified desktop_env module is already loaded")
        return root

    sys.path.insert(0, str(root))
    try:
        module = importlib.import_module("desktop_env.desktop_env")
    except Exception:
        sys.path.remove(str(root))
        raise
    observed = Path(str(getattr(module, "__file__", ""))).resolve()
    if observed != expected_module:
        sys.path.remove(str(root))
        raise WorkerProtocolError(
            "desktop_env resolved outside the verified compatibility root"
        )
    return root


def guest_bundle_doctor(guest_bundle_root: Path) -> dict[str, object]:
    """Check the single guest zipapp without executing it.

    The bundle must be a readable ZIP archive containing the recorder entry
    points and the shared trace core, and nothing outside that closed set.
    """

    path = Path(guest_bundle_root) / GUEST_BUNDLE_NAME
    present = path.is_file()
    members: list[str] = []
    readable = False
    if present:
        try:
            with zipfile.ZipFile(path) as archive:
                members = sorted(archive.namelist())
                readable = archive.testzip() is None
        except (OSError, zipfile.BadZipFile):
            readable = False
    missing = sorted(set(REQUIRED_GUEST_BUNDLE_MEMBERS) - set(members))
    unexpected = [name for name in members if not GUEST_BUNDLE_MEMBER_PATTERN.match(name)]
    return {
        "name": GUEST_BUNDLE_NAME,
        "present": present,
        "sha256": sha256(path) if present else None,
        "readable_zip": readable,
        "member_count": len(members),
        "missing_members": missing,
        "unexpected_members": unexpected,
        "passed": present and readable and not missing and not unexpected,
    }


def source_doctor(
    source_root: Path,
    *,
    expected_commit: str,
    task_bindings: list[Mapping[str, object]],
    guest_bundle_root: Path,
) -> dict[str, object]:
    """Audit pinned host source and guest recorder without executing either."""

    source_root = Path(source_root).resolve()
    checks: list[dict[str, object]] = []

    def add(name: str, passed: bool, detail: object) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    dependency_versions: dict[str, dict[str, object]] = {}
    for distribution_name, expected_version in HOST_DEPENDENCY_VERSIONS.items():
        try:
            observed_version = importlib_metadata.version(distribution_name)
        except importlib_metadata.PackageNotFoundError:
            observed_version = None
        dependency_versions[distribution_name] = {
            "expected": expected_version,
            "observed": observed_version,
            "passed": observed_version == expected_version,
        }
    compatibility = runtime_compat_doctor()
    dependencies_pass = all(
        bool(item["passed"]) for item in dependency_versions.values()
    ) and compatibility["passed"] is True
    add(
        "host_dependency_contract",
        dependencies_pass,
        {"versions": dependency_versions, "runtime_compat": compatibility},
    )

    commit = dirty = None
    try:
        commit = _git(source_root, "rev-parse", "HEAD")
        dirty = _git(source_root, "status", "--porcelain")
    except Exception as error:
        add("pinned_checkout", False, f"git inspection failed: {type(error).__name__}")
    else:
        add(
            "pinned_checkout",
            commit == expected_commit == PINNED_COMMIT and not dirty,
            {
                "observed_commit": commit,
                "expected_commit": expected_commit,
                "clean": not bool(dirty),
            },
        )

    source_evidence: dict[str, dict[str, object]] = {}
    for relative, expected_hash in SOURCE_HASHES.items():
        path = source_root / relative
        observed_hash = sha256(path) if path.is_file() else None
        signature = (
            inspect_source_signatures(path, SOURCE_SIGNATURES[relative])
            if path.is_file() and relative in SOURCE_SIGNATURES
            else None
        )
        matches = observed_hash == expected_hash and (
            signature is None or signature["passed"] is True
        )
        source_evidence[relative] = {
            "sha256": observed_hash,
            "expected_sha256": expected_hash,
            "signatures": signature,
            "passed": matches,
        }
    add(
        "official_source_contract",
        all(bool(item["passed"]) for item in source_evidence.values()),
        source_evidence,
    )

    evaluator_evidence: dict[str, dict[str, object]] = {}
    for application, (relative, expected_hash) in EVALUATOR_SOURCES.items():
        path = source_root / relative
        observed = sha256(path) if path.is_file() else None
        evaluator_evidence[application] = {
            "module": relative,
            "sha256": observed,
            "expected_sha256": expected_hash,
            "passed": observed == expected_hash,
        }
    add(
        "official_evaluators",
        all(bool(item["passed"]) for item in evaluator_evidence.values()),
        evaluator_evidence,
    )

    task_evidence: dict[str, dict[str, object]] = {}
    for binding in task_bindings:
        relative = str(binding.get("upstream_path", ""))
        path = (source_root / relative).resolve()
        try:
            path.relative_to(source_root)
            payload = json.loads(path.read_text(encoding="utf-8"))
            observed = hashlib.sha256(
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
        except Exception:
            observed = None
        expected = binding.get("canonical_payload_sha256")
        task_evidence[str(binding.get("id"))] = {
            "upstream_path": relative,
            "canonical_sha256": observed,
            "expected_canonical_sha256": expected,
            "passed": observed == expected,
        }
    add(
        "official_task_payloads",
        bool(task_evidence)
        and all(bool(item["passed"]) for item in task_evidence.values()),
        task_evidence,
    )

    bundle_evidence = guest_bundle_doctor(Path(guest_bundle_root))
    add("guest_recorder_bundle", bundle_evidence["passed"] is True, bundle_evidence)

    ready = bool(checks) and all(bool(check["passed"]) for check in checks)
    return {
        "schema_version": HOST_DOCTOR_SCHEMA,
        "ready": ready,
        "binding": {
            "scienceboard_commit": commit,
            "worker_source_sha256": worker_source_sha256(),
            "worker_source_files": worker_source_manifest(),
        },
        "checks": checks,
        "claim_boundary": (
            "Read-only host source/signature probe; no VM, model, or task was run."
        ),
    }
