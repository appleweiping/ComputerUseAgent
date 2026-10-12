"""Stable shared contract for the formal Week 1 framework adapters.

This module contains only immutable protocol constants and the two public
types shared by the adapter entry points, browser lifecycle, artifact
validators, and orchestration facade.  ``adapters.common`` re-exports these
names so existing imports remain source compatible.

The harness is frozen against recorder 0.4.0 (``EXPECTED_RECORDER_VERSION``).
The live extension in ``extension/app`` is newer, so every readiness check
fails closed and the harness cannot be launched from this repository; see
``research/week1_miniwob/README.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ADAPTER_RESULT_SCHEMA = "cui-week1-adapter-result-v1"
REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_ROOT = Path(__file__).with_name("node")
NODE_HOST = NODE_ROOT / "browser_host.mjs"
EXTENSION_ROOT = REPO_ROOT / "extension" / "app"
SCHEMA_ROOT = REPO_ROOT / "src" / "computer_use_agent" / "schemas"
TRACE_SCHEMA_PATH = SCHEMA_ROOT / "cui-trace-v2.schema.json"
LEGACY_TRACE_SCHEMA_PATH = SCHEMA_ROOT / "cui-trace-v1.schema.json"
# Frozen pin: the harness was reviewed against recorder 0.4.0 only.
EXPECTED_RECORDER_VERSION = "0.4.0"
DEPENDENCY_FREEZE_FILENAME = "cui-week1-dependency-freeze.txt"
DEPENDENCY_FREEZE_DIGEST_FILENAME = "cui-week1-dependency-freeze.sha256"
FRAMEWORK_ENVIRONMENT_NAMES = {
    "browser-use": ("browser-use",),
    "skyvern": ("orchestrator", "skyvern-local"),
    "stagehand": ("stagehand",),
}

EXPECTED_DISTRIBUTIONS = {
    "browsergym-core": "0.14.1",
    "browsergym-miniwob": "0.14.1",
    "playwright": "1.44.0",
    "browser-use": "0.11.5",
    "pydantic-settings": "2.14.2",
    "skyvern": "1.0.47",
}
EXPECTED_NODE_PACKAGES = {
    "puppeteer-core": "25.8.0",
}
TASK_CLASS_NAMES = {
    "browsergym/miniwob.click-checkboxes": "ClickCheckboxesTask",
    "browsergym/miniwob.enter-text": "EnterTextTask",
    "browsergym/miniwob.choose-list": "ChooseListTask",
}
SAFE_ACTION_TYPES = {
    "act",
    "agent",
    "aria_tree",
    "check",
    "choose",
    "click",
    "click_and_hold",
    "done",
    "drag",
    "drag_and_drop",
    "extract",
    "fill",
    "fill_form",
    "focus",
    "goto",
    "input_text",
    "key",
    "keys",
    "mouse",
    "navback",
    "scroll",
    "select",
    "skyvern_step",
    "think",
    "type",
    "wait",
}

# Frameworks the harness can still dispatch.  Stagehand stays in the frozen
# protocol (and FRAMEWORK_ENVIRONMENT_NAMES) only as a historical cell; it has
# no adapter, so every readiness check rejects it.
ACTIVE_FRAMEWORKS = ("browser-use", "skyvern")

STEP_ALIGNMENT_HOOKS = {
    "browser-use": "browser_use_on_step_start",
}
STEP_ALIGNMENT_PATH = "/v1/step"
STEP_ALIGNMENT_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")
STEP_ID_PATTERN = re.compile(r"step-([0-9]{3})")


class AdapterError(RuntimeError):
    """A diagnostic failure that must not be converted into experimental success."""

    def __init__(self, code: str, detail: str, hint: str | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.hint = hint

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "detail": self.detail}
        if self.hint:
            value["hint"] = self.hint
        return value


@dataclass
class FrameworkRun:
    """Framework-side evidence returned before official task validation."""

    action_history: list[dict[str, Any]]
    action_count: int
    completed: bool
    timed_out: bool = False
    error: str | None = None
    artifacts: dict[str, Any] = field(default_factory=dict)


def error_result(attempt: dict[str, Any], framework: str, error: AdapterError) -> dict[str, Any]:
    """Return the stable, secret-free adapter error envelope."""

    version = str(attempt.get("framework", {}).get("version") or "unknown")
    return {
        "schema_version": ADAPTER_RESULT_SCHEMA,
        "attempt_id": attempt.get("attempt_id"),
        "status": "error",
        "agent": {
            "framework": framework,
            "framework_version": version,
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
        "error": {"code": error.code},
        "provenance_complete": False,
        "artifact_bundle_complete": False,
        "formal_matrix_eligible": False,
        "analysis_eligible": False,
        "dependencies": {"snapshots": []},
    }
