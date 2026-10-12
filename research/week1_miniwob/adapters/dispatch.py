"""Select an isolated pinned framework environment, then exec its adapter.

Browser-Use 0.11.5 and Skyvern 1.0.47 have incompatible ``rich`` dependency
ranges. Skyvern's local extra also requires Playwright newer than BrowserGym's
1.44 pin, so its harness runs in an orchestrator environment and invokes a
second ``skyvern-local`` process for the framework itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


ADAPTERS = {
    "browser-use": "research.week1_miniwob.adapters.browser_use_adapter",
    "skyvern": "research.week1_miniwob.adapters.skyvern_adapter",
}
RUNTIME_ENVIRONMENTS = {
    "browser-use": "browser-use",
    "skyvern": "orchestrator",
}
REPO_ROOT = Path(__file__).resolve().parents[3]


def _inside_repository(path: Path) -> bool:
    try:
        path.relative_to(REPO_ROOT)
        return True
    except ValueError:
        return False


def environment_root() -> Path | None:
    explicit = os.environ.get("CUI_WEEK1_ADAPTER_ENV_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    private = os.environ.get("CUI_WEEK1_PRIVATE_ROOT")
    if private:
        return (Path(private).expanduser().resolve() / "environments")
    return None


def environment_python(root: Path, framework: str) -> Path:
    if os.name == "nt":
        return root / framework / "Scripts" / "python.exe"
    return root / framework / "bin" / "python"


def missing_self_test(framework: str, detail: str) -> int:
    payload = {
        "schema_version": "cui-week1-adapter-self-test-v1",
        "framework": framework,
        "ready": False,
        "checks": [{"id": "isolated-environment", "status": "fail", "detail": detail}],
        "secrets_printed": False,
        "writes_performed": False,
    }
    print(json.dumps(payload, indent=2))
    return 2


def missing_result(raw: str, framework: str, detail: str) -> int:
    try:
        attempt: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as error:
        print(json.dumps({"ready": False, "error": f"invalid request JSON: {error}"}), file=sys.stderr)
        return 2
    framework_data = attempt.get("framework") if isinstance(attempt.get("framework"), dict) else {}
    print(json.dumps({"code": "isolated_environment_missing", "detail": detail}), file=sys.stderr)
    result = {
        "schema_version": "cui-week1-adapter-result-v1",
        "attempt_id": attempt.get("attempt_id"),
        "status": "error",
        "agent": {
            "framework": framework,
            "framework_version": framework_data.get("version", "unknown"),
            "model": attempt.get("model", "gpt-5.5"),
            "action_count": 0,
            "action_count_observed": False,
            "action_history": [],
        },
        "validator": {
            "source": "browsergym_official_reward",
            "reward": None,
            "terminated": False,
            "truncated": False,
        },
        "recorder": {
            "capture_adapter": "browser_mv3",
            "source_class": "cui_agent",
            "status": "unknown",
            "session_id": None,
        },
        "error": {
            "code": "isolated_environment_missing",
        },
        "provenance_complete": False,
        "artifact_bundle_complete": False,
        "formal_matrix_eligible": False,
        "analysis_eligible": False,
        "dependencies": {"snapshots": []},
    }
    print(json.dumps(result, ensure_ascii=True))
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ADAPTERS:
        print(f"usage: {Path(sys.argv[0]).name} {'|'.join(ADAPTERS)} [--self-test]", file=sys.stderr)
        return 2
    framework = sys.argv[1]
    self_test = "--self-test" in sys.argv[2:]
    raw = "" if self_test else sys.stdin.read()
    root = environment_root()
    detail = "CUI_WEEK1_PRIVATE_ROOT or CUI_WEEK1_ADAPTER_ENV_ROOT is not set"
    if root is None:
        return missing_self_test(framework, detail) if self_test else missing_result(raw, framework, detail)
    if _inside_repository(root):
        detail = "adapter environment root must be outside the Git repository"
        return missing_self_test(framework, detail) if self_test else missing_result(raw, framework, detail)
    runtime_environment = RUNTIME_ENVIRONMENTS[framework]
    python = environment_python(root, runtime_environment)
    if not python.is_file():
        detail = f"isolated {runtime_environment} interpreter for {framework} not found at {python}"
        return missing_self_test(framework, detail) if self_test else missing_result(raw, framework, detail)

    # ``-m`` with cwd=REPO_ROOT puts the repository root on sys.path, so the
    # adapter imports its package without any sys.path manipulation.
    command = [str(python), "-m", ADAPTERS[framework]]
    if self_test:
        command.append("--self-test")
    completed = subprocess.run(
        command,
        input=None if self_test else raw,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        cwd=REPO_ROOT,
    )
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    sys.stdout.write(completed.stdout)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
