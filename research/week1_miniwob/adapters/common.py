"""Shared, fail-closed BrowserGym/MiniWoB adapter orchestration facade.

Public imports remain available here for the three framework adapters.  The
resource lifecycle and deterministic artifact validators live in focused
modules; this facade keeps experiment policy, CLI behavior, and testable
orchestration seams in one place.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Awaitable, Callable, TextIO, TypeVar
from urllib.parse import urlparse

from . import artifact_validation as _artifacts
from . import evidence_sanitization as _sanitization
from .adapter_contract import (
    ACTIVE_FRAMEWORKS,
    ADAPTER_RESULT_SCHEMA,
    DEPENDENCY_FREEZE_DIGEST_FILENAME,
    DEPENDENCY_FREEZE_FILENAME,
    EXPECTED_DISTRIBUTIONS,
    EXPECTED_NODE_PACKAGES,
    EXPECTED_RECORDER_VERSION,
    EXTENSION_ROOT,
    FRAMEWORK_ENVIRONMENT_NAMES,
    LEGACY_TRACE_SCHEMA_PATH,
    NODE_HOST,
    NODE_ROOT,
    REPO_ROOT,
    SAFE_ACTION_TYPES,
    STEP_ALIGNMENT_HOOKS,
    STEP_ALIGNMENT_PATH,
    STEP_ALIGNMENT_TOKEN_PATTERN,
    STEP_ID_PATTERN,
    TASK_CLASS_NAMES,
    TRACE_SCHEMA_PATH,
    AdapterError,
    FrameworkRun,
    error_result,
)
from .browser_lifecycle import (
    BrowserHost as _BrowserHost,
    OfficialMiniwobEpisode,
    StepAlignmentSidecar,
    assert_miniwob_timer_is_milliseconds,
    post_step_alignment,
    terminate_owned_process_tree as _terminate_owned_process_tree_impl,
    validate_step_alignment_descriptor,
)
from .miniwob_source import (
    PinnedMiniwobServer,
    VerifiedMiniwobEndpoint,
    default_source_root,
    verify_source_root,
)


__all__ = [
    "ACTIVE_FRAMEWORKS",
    "ADAPTER_RESULT_SCHEMA",
    "DEPENDENCY_FREEZE_DIGEST_FILENAME",
    "DEPENDENCY_FREEZE_FILENAME",
    "EXPECTED_DISTRIBUTIONS",
    "EXPECTED_NODE_PACKAGES",
    "EXPECTED_RECORDER_VERSION",
    "EXTENSION_ROOT",
    "FRAMEWORK_ENVIRONMENT_NAMES",
    "LEGACY_TRACE_SCHEMA_PATH",
    "NODE_HOST",
    "NODE_ROOT",
    "REPO_ROOT",
    "SAFE_ACTION_TYPES",
    "STEP_ALIGNMENT_HOOKS",
    "STEP_ALIGNMENT_PATH",
    "STEP_ALIGNMENT_TOKEN_PATTERN",
    "STEP_ID_PATTERN",
    "TASK_CLASS_NAMES",
    "TRACE_SCHEMA_PATH",
    "AdapterError",
    "BrowserHost",
    "FrameworkRun",
    "OfficialMiniwobEpisode",
    "StepAlignmentSidecar",
    "adapter_cli",
    "assert_miniwob_timer_is_milliseconds",
    "common_diagnostics",
    "compact_actions",
    "configure_browser_use_private_runtime",
    "error_result",
    "hashlib",
    "optional_private_artifact_descriptor",
    "post_step_alignment",
    "private_artifact_descriptor",
    "read_attempt",
    "require_fast_attempt_ready",
    "require_ready",
    "run_coroutine_in_worker",
    "run_formal_adapter",
    "validate_attempt",
    "validate_formal_trace",
    "validate_png_screenshot",
    "validate_step_alignment_descriptor",
    "verify_dependency_freezes",
]


T = TypeVar("T")


def run_coroutine_in_worker(factory: Callable[[], Awaitable[T]]) -> T:
    """Run an async framework driver away from Playwright's sync event loop.

    BrowserGym's synchronous Playwright client remains active while the agent
    controls the shared CDP page. Creating both the coroutine and its event
    loop in a short-lived worker avoids nesting ``asyncio.run()`` on that
    thread, while ``Future.result()`` still propagates driver exceptions to the
    normal evidence-retention path.
    """

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="cui-agent-driver") as executor:
        return executor.submit(lambda: asyncio.run(factory())).result()


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _node_package_version(name: str) -> str | None:
    package_path = NODE_ROOT / "node_modules"
    for part in name.split("/"):
        package_path /= part
    package_path /= "package.json"
    try:
        value = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    version = value.get("version")
    return str(version) if version else None


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def configure_browser_use_private_runtime() -> None:
    """Disable framework telemetry/version checks and route its state to private storage."""
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
    os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")
    os.environ.setdefault("BROWSER_USE_VERSION_CHECK", "false")
    private = os.environ.get("CUI_WEEK1_PRIVATE_ROOT")
    if private:
        config_root = Path(private).expanduser().resolve() / "framework_state" / "browser-use"
        os.environ.setdefault("BROWSER_USE_CONFIG_DIR", str(config_root))


def validate_attempt(attempt: Any, framework: str) -> dict[str, Any]:
    if not isinstance(attempt, dict):
        raise AdapterError("invalid_request", "stdin must contain one JSON object")
    expected_fields = {
        "attempt_id": str,
        "framework": dict,
        "task": dict,
        "model": str,
        "limits": dict,
        "browser": dict,
        "recorder": dict,
    }
    for key, expected_type in expected_fields.items():
        if not isinstance(attempt.get(key), expected_type):
            raise AdapterError("invalid_request", f"attempt.{key} must be {expected_type.__name__}")
    if attempt["framework"].get("id") != framework:
        raise AdapterError(
            "framework_mismatch",
            f"request is for {attempt['framework'].get('id')!r}, not {framework!r}",
        )
    if attempt["model"] != "gpt-5.5":
        raise AdapterError("model_mismatch", "formal adapters accept only model gpt-5.5")
    model_configuration = attempt.get("model_configuration")
    browser_use_model = (
        model_configuration.get("browser-use") if isinstance(model_configuration, dict) else None
    )
    skyvern_model = (
        model_configuration.get("skyvern") if isinstance(model_configuration, dict) else None
    )
    stagehand_model = (
        model_configuration.get("stagehand") if isinstance(model_configuration, dict) else None
    )
    if (
        not isinstance(model_configuration, dict)
        or not isinstance(model_configuration.get("task_seed_policy"), str)
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
        raise AdapterError(
            "model_configuration_mismatch", "formal framework parameter policy changed"
        )
    benchmark = attempt.get("benchmark")
    if (
        not isinstance(benchmark, dict)
        or benchmark.get("miniwob_commit") != "7fd85d71a4b60325c6585396ec4f48377d049838"
        or benchmark.get("miniwob_html_tree_sha256")
        != "b6541e36c70d6621294edfe0ca3bae7794fb9cfb9def2829dfcda4fd284b3925"
        or benchmark.get("miniwob_static_tree_sha256")
        != "9883c9117804c0865920b3313021a0bd0cccc094f2f707678f6c3a72b3af4f3a"
    ):
        raise AdapterError(
            "benchmark_version_mismatch",
            "formal MiniWoB commit/task/static tree hash changed",
        )
    if (
        attempt["limits"].get("max_actions") != 30
        or attempt["limits"].get("timeout_seconds") != 480
    ):
        raise AdapterError("limit_mismatch", "formal adapters require 30 actions and 480 seconds")
    environment_id = attempt["task"].get("environment_id")
    if environment_id not in TASK_CLASS_NAMES:
        raise AdapterError(
            "task_mismatch", f"unsupported official MiniWoB task: {environment_id!r}"
        )
    viewport = attempt["browser"].get("viewport")
    if viewport != {"width": 332, "height": 214}:
        raise AdapterError("viewport_mismatch", "formal viewport must be exactly 332x214")
    if (
        attempt["browser"].get("engine") != "Google Chrome"
        or attempt["browser"].get("product_version") != "151.0.7922.108"
    ):
        raise AdapterError(
            "browser_version_mismatch", "formal browser must be Google Chrome 151.0.7922.108"
        )
    recorder = attempt["recorder"]
    if recorder.get("required") is not True or recorder.get("capture_adapter") != "browser_mv3":
        raise AdapterError("recorder_mismatch", "formal attempts require the browser_mv3 recorder")
    return attempt


def read_attempt(stream: TextIO, framework: str) -> dict[str, Any]:
    try:
        value = json.loads(stream.read())
    except json.JSONDecodeError as error:
        raise AdapterError("invalid_json", f"could not parse request JSON: {error}") from error
    return validate_attempt(value, framework)


def common_diagnostics(framework: str) -> dict[str, Any]:
    checks: list[dict[str, str]] = []

    def add(check_id: str, passed: bool, detail: str) -> None:
        checks.append({"id": check_id, "status": "pass" if passed else "fail", "detail": detail})

    add(
        "framework-active",
        framework in ACTIVE_FRAMEWORKS,
        f"{framework} has an active adapter"
        if framework in ACTIVE_FRAMEWORKS
        else f"{framework} has no active adapter; active: {', '.join(ACTIVE_FRAMEWORKS)}",
    )

    python_supported = (3, 11) <= sys.version_info[:2] < (3, 14)
    add(
        "python-version",
        python_supported,
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}; required >=3.11,<3.14",
    )

    for distribution in ("browsergym-core", "browsergym-miniwob", "playwright"):
        actual = _package_version(distribution)
        expected = EXPECTED_DISTRIBUTIONS[distribution]
        add(
            f"package-{distribution}",
            actual == expected,
            f"installed={actual or 'missing'} expected={expected}",
        )
    try:
        from browsergym.miniwob.base import AbstractMiniwobTask

        assert_miniwob_timer_is_milliseconds(AbstractMiniwobTask)
        add("browsergym-timer-unit", True, "episode_max_time verified as milliseconds")
    except Exception as error:
        add(
            "browsergym-timer-unit",
            False,
            f"{type(error).__name__}: timer contract could not be verified",
        )

    framework_distribution = {"browser-use": "browser-use"}.get(framework)
    if framework_distribution:
        actual = _package_version(framework_distribution)
        expected = EXPECTED_DISTRIBUTIONS[framework_distribution]
        add(
            f"package-{framework_distribution}",
            actual == expected,
            f"installed={actual or 'missing'} expected={expected}",
        )
    if framework == "browser-use":
        actual = _package_version("pydantic-settings")
        expected = EXPECTED_DISTRIBUTIONS["pydantic-settings"]
        add(
            "package-pydantic-settings",
            actual == expected,
            f"installed={actual or 'missing'} expected={expected}",
        )
        try:
            configure_browser_use_private_runtime()
            with redirect_stdout(sys.stderr):
                from browser_use import Agent, BrowserSession, ChatOpenAI
                from research.week1_miniwob.adapters.browser_use_compat import (
                    verify_browser_use_external_cdp_compatibility,
                )

            imported = all(value is not None for value in (Agent, BrowserSession, ChatOpenAI))
            guard = verify_browser_use_external_cdp_compatibility()
            add(
                "framework-api-import",
                imported,
                "Browser-Use Agent/BrowserSession/ChatOpenAI imported",
            )
            add(
                "external-cdp-compat-guard",
                True,
                (
                    f"browser-use={guard['browser_use_version']} cdp-use={guard['cdp_use_version']} "
                    f"sources={len(guard['guards'])} timeout={guard['resume_timeout_seconds']:.1f}s"
                ),
            )
        except Exception as error:
            add(
                "framework-api-import",
                False,
                f"{type(error).__name__}: framework API import failed",
            )
            add(
                "external-cdp-compat-guard",
                False,
                f"{type(error).__name__}: compatibility guard failed",
            )
    elif framework == "skyvern":
        try:
            from research.week1_miniwob.adapters.skyvern_adapter import bridge_self_test

            local_diagnostic = bridge_self_test()
            local_ready = local_diagnostic.get("ready") is True
            add(
                "framework-api-import",
                local_ready,
                "isolated Skyvern.local bridge passed exact package, API, UTF-8, and lifecycle guards"
                if local_ready
                else "isolated Skyvern.local bridge self-test failed",
            )
            for row in local_diagnostic.get("checks", []):
                if isinstance(row, dict):
                    add(
                        f"skyvern-local-{row.get('id', 'unknown')}",
                        row.get("status") == "pass",
                        str(row.get("detail") or "no detail"),
                    )
        except Exception as error:
            add(
                "framework-api-import",
                False,
                f"{type(error).__name__}: framework API import failed",
            )

    node = shutil.which("node")
    add("node", bool(node), node or "node executable not found")
    for package in ("puppeteer-core",):
        actual = _node_package_version(package)
        expected = EXPECTED_NODE_PACKAGES[package]
        add(
            f"node-package-{package}",
            actual == expected,
            f"installed={actual or 'missing'} expected={expected}",
        )

    chrome_value = os.environ.get("CUI_WEEK1_CHROME_EXECUTABLE")
    chrome = Path(chrome_value).expanduser() if chrome_value else None
    add(
        "chrome-executable",
        bool(chrome and chrome.is_file()),
        str(chrome) if chrome else "CUI_WEEK1_CHROME_EXECUTABLE is not set",
    )

    miniwob_root = default_source_root()
    try:
        if miniwob_root is None:
            raise RuntimeError("CUI_WEEK1_PRIVATE_ROOT or CUI_WEEK1_MINIWOB_ROOT is not set")
        source = verify_source_root(miniwob_root)
        add(
            "miniwob-pinned-source",
            True,
            (
                f"commit={source['commit']} kind={source['source_kind']} "
                f"task_tree={source['html_tree_sha256'][:16]} "
                f"static_tree={source['static_tree_sha256'][:16]}"
            ),
        )
        with PinnedMiniwobServer(miniwob_root) as server:
            add(
                "miniwob-loopback",
                True,
                f"official task pages and shared assets byte-verified on {urlparse(server.url).hostname} loopback",
            )
    except Exception as error:
        add("miniwob-pinned-source", False, f"{type(error).__name__}: {error}")
        add(
            "miniwob-loopback",
            False,
            "loopback server not started because pinned source verification failed",
        )
    else:
        override_url = os.environ.get("MINIWOB_URL")
        if override_url:
            try:
                with VerifiedMiniwobEndpoint(miniwob_root, override_url):
                    add(
                        "miniwob-url-override",
                        True,
                        "explicit endpoint bytes match the pinned local tasks",
                    )
            except Exception as error:
                add(
                    "miniwob-url-override",
                    False,
                    f"{type(error).__name__}: endpoint verification failed",
                )

    private_value = os.environ.get("CUI_WEEK1_PRIVATE_ROOT")
    private_root = Path(private_value).expanduser().resolve() if private_value else None
    private_ok = bool(private_root and not _is_relative_to(private_root, REPO_ROOT.resolve()))
    add(
        "private-root",
        private_ok,
        str(private_root) if private_root else "CUI_WEEK1_PRIVATE_ROOT is not set",
    )

    try:
        dependency_evidence = verify_dependency_freezes(framework, compare_live_environment=True)
        for row in dependency_evidence:
            add(
                f"dependency-freeze-{row['environment']}",
                True,
                f"private freeze matches the live {row['environment']} environment",
            )
    except Exception as error:
        add(
            "dependency-freeze",
            False,
            f"{type(error).__name__}: dependency freeze validation failed",
        )

    add(
        "openai-key",
        bool(os.environ.get("OPENAI_API_KEY")),
        "present" if os.environ.get("OPENAI_API_KEY") else "OPENAI_API_KEY is not set",
    )
    extension_manifest = EXTENSION_ROOT / "manifest.json"
    try:
        extension_version = json.loads(extension_manifest.read_text(encoding="utf-8")).get(
            "version"
        )
    except (OSError, json.JSONDecodeError):
        extension_version = None
    add(
        "recorder-extension",
        extension_version == EXPECTED_RECORDER_VERSION,
        f"installed source version={extension_version or 'missing'} expected={EXPECTED_RECORDER_VERSION}",
    )
    add("browser-host", NODE_HOST.is_file(), str(NODE_HOST))

    return {
        "schema_version": "cui-week1-adapter-self-test-v1",
        "framework": framework,
        "ready": all(row["status"] == "pass" for row in checks),
        "checks": checks,
        "secrets_printed": False,
        "repository_writes_performed": False,
        "private_state_may_change": framework == "skyvern",
        "writes_performed": framework == "skyvern",
    }


def require_ready(framework: str) -> None:
    diagnostic = common_diagnostics(framework)
    failures = [row for row in diagnostic["checks"] if row["status"] == "fail"]
    if failures:
        detail = "; ".join(f"{row['id']}: {row['detail']}" for row in failures)
        raise AdapterError(
            "preflight_failed",
            detail,
            "Run the adapter with --self-test after installing pinned Python and Node dependencies.",
        )


def require_fast_attempt_ready(framework: str) -> None:
    """Cheap per-attempt invariants; heavy lifecycle/network checks run once in runner preflight."""

    failures: list[str] = []
    for distribution in ("browsergym-core", "browsergym-miniwob", "playwright"):
        actual = _package_version(distribution)
        expected = EXPECTED_DISTRIBUTIONS[distribution]
        if actual != expected:
            failures.append(f"{distribution}={actual or 'missing'} expected={expected}")
    if framework == "browser-use":
        for distribution in ("browser-use", "pydantic-settings"):
            actual = _package_version(distribution)
            expected = EXPECTED_DISTRIBUTIONS[distribution]
            if actual != expected:
                failures.append(f"{distribution}={actual or 'missing'} expected={expected}")
    if _node_package_version("puppeteer-core") != EXPECTED_NODE_PACKAGES["puppeteer-core"]:
        failures.append("pinned puppeteer-core is missing")
    if framework not in ACTIVE_FRAMEWORKS:
        failures.append(f"{framework} has no active adapter")
    private_value = os.environ.get("CUI_WEEK1_PRIVATE_ROOT")
    if not private_value:
        failures.append("CUI_WEEK1_PRIVATE_ROOT is missing")
    else:
        private_root = Path(private_value).expanduser().resolve()
        if _is_relative_to(private_root, REPO_ROOT.resolve()):
            failures.append("private root must be outside the repository")
    if not os.environ.get("OPENAI_API_KEY"):
        failures.append("OPENAI_API_KEY is missing")
    try:
        from research.week1_miniwob.adapters.dispatch import environment_root

        adapter_environment_root = environment_root()
    except Exception:
        adapter_environment_root = None
    if adapter_environment_root is None:
        failures.append("adapter environment root is missing")
    elif _is_relative_to(adapter_environment_root, REPO_ROOT.resolve()):
        failures.append("adapter environment root must be outside the repository")
    chrome = Path(os.environ.get("CUI_WEEK1_CHROME_EXECUTABLE", "")).expanduser()
    if not chrome.is_file():
        failures.append("CUI_WEEK1_CHROME_EXECUTABLE is missing")
    if not NODE_HOST.is_file() or not EXTENSION_ROOT.joinpath("manifest.json").is_file():
        failures.append("browser host or recorder extension source is missing")
    if framework == "skyvern":
        try:
            from research.week1_miniwob.adapters.skyvern_adapter import _skyvern_python

            skyvern_python = _skyvern_python()
        except Exception:
            skyvern_python = None
        if skyvern_python is None or not skyvern_python.is_file():
            failures.append("isolated skyvern-local interpreter is missing")
    try:
        verify_dependency_freezes(framework, compare_live_environment=False)
    except Exception as error:
        failures.append(f"dependency freeze unavailable: {type(error).__name__}")
    if failures:
        raise AdapterError(
            "fast_preflight_failed",
            "; ".join(failures),
            "Run runner.py --preflight once before the selected matrix.",
        )


# Compatibility facade -----------------------------------------------------
#
# These names intentionally remain mutable globals in ``common``.  The
# orchestration function below resolves them at call time, preserving the
# established test/diagnostic monkeypatch seams while implementation ownership
# lives in the focused modules.


def _terminate_owned_process_tree(process: subprocess.Popen[Any]) -> None:
    _terminate_owned_process_tree_impl(process)


class BrowserHost(_BrowserHost):
    """Compatibility wrapper that preserves the common-module terminator seam."""

    def __init__(self, attempt: dict[str, Any], private_root: Path):
        super().__init__(
            attempt,
            private_root,
            terminate_process_tree_fn=lambda process: _terminate_owned_process_tree(process),
        )


private_artifact_descriptor = _artifacts.private_artifact_descriptor
optional_private_artifact_descriptor = _artifacts.optional_private_artifact_descriptor
_normalized_dependency_freeze = _artifacts._normalized_dependency_freeze
_dependency_environments = _artifacts.dependency_environments
validate_formal_trace = _artifacts.validate_formal_trace
validate_png_screenshot = _artifacts.validate_png_screenshot
_safe_public_artifacts = _sanitization.safe_public_artifacts
compact_actions = _sanitization.compact_actions


def verify_dependency_freezes(
    framework: str,
    *,
    compare_live_environment: bool,
) -> list[dict[str, Any]]:
    return _artifacts.verify_dependency_freezes(
        framework,
        compare_live_environment=compare_live_environment,
        dependency_environments_fn=_dependency_environments,
    )


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, default=str))


def run_formal_adapter(
    attempt: dict[str, Any],
    framework: str,
    driver: Callable[[dict[str, Any], str, str], FrameworkRun],
) -> dict[str, Any]:
    # The runner performs expensive framework lifecycle and loopback checks
    # once before the selected matrix. Repeating Skyvern.local construction on
    # every attempt consumed 45-120 seconds of the outer task budget. Formal
    # attempts retain only cheap immutable/path/credential guards here; each
    # framework bridge still verifies its exact source contract when invoked.
    require_fast_attempt_ready(framework)
    dependency_snapshots = verify_dependency_freezes(framework, compare_live_environment=True)
    private_root = Path(os.environ["CUI_WEEK1_PRIVATE_ROOT"]).expanduser().resolve()
    recorder_start: dict[str, Any] | None = None
    recorder_stop: dict[str, Any] | None = None
    framework_run: FrameworkRun | None = None
    reward: float | None = None
    terminated = False
    task_info: dict[str, Any] = {}
    failure_code: str | None = None
    secondary_failure_codes: list[str] = []
    host: BrowserHost | None = None
    official_target: dict[str, str] = {}
    episode_guard_stable = False
    episode_guard_pre_validator = False
    episode_guard_post_validator = False
    episode_guard_disarm_clean = False
    trace_validation: dict[str, Any] | None = None
    screenshot_validation: dict[str, Any] | None = None
    task_setup_artifact: dict[str, Any] | None = None
    task_info_artifact: dict[str, Any] | None = None
    step_alignment_evidence: dict[str, Any] = {
        "supported": False,
        "support_status": "not_supported",
        "hook_type": "none",
        "transport_type": "none",
        "max_steps": attempt["limits"]["max_actions"],
        "request_count": 0,
        "accepted_requests": 0,
        "rejected_requests": 0,
        "clear_acknowledged": True,
        "framework_hook_calls": 0,
        "framework_host_counts_match": True,
    }

    def retain_failure(code: str, error: Exception) -> None:
        nonlocal failure_code
        if failure_code is None:
            failure_code = code
        elif code != failure_code and code not in secondary_failure_codes:
            secondary_failure_codes.append(code)
        print(
            json.dumps(
                {"code": code, "detail": f"{type(error).__name__}: {error}"}, ensure_ascii=True
            ),
            file=sys.stderr,
        )

    def promote_failure(code: str, error: Exception) -> None:
        """Make an integrity-critical code primary while retaining prior cause."""

        nonlocal failure_code
        if failure_code and failure_code != code and failure_code not in secondary_failure_codes:
            secondary_failure_codes.append(failure_code)
        if code in secondary_failure_codes:
            secondary_failure_codes.remove(code)
        failure_code = code
        print(
            json.dumps(
                {"code": code, "detail": f"{type(error).__name__}: {error}"}, ensure_ascii=True
            ),
            file=sys.stderr,
        )

    miniwob_root = default_source_root()
    if miniwob_root is None:
        raise AdapterError("miniwob_source_missing", "no private MiniWoB source root is configured")
    explicit_miniwob_url = os.environ.get("MINIWOB_URL")
    server_context = (
        VerifiedMiniwobEndpoint(miniwob_root, explicit_miniwob_url)
        if explicit_miniwob_url
        else PinnedMiniwobServer(miniwob_root)
    )
    with server_context as miniwob_server:
        with BrowserHost(attempt, private_root) as host:
            with OfficialMiniwobEpisode(attempt, host, miniwob_server.url) as episode:
                official_target = dict(episode.official_target)
                task_setup_path = host.attempt_root / "official_task_setup.json"
                task_setup_path.write_text(
                    json.dumps(
                        {
                            "schema_version": "cui-week1-private-task-setup-v1",
                            "attempt_id": attempt["attempt_id"],
                            "environment_id": attempt["task"]["environment_id"],
                            "seed": attempt["seed"],
                            "instruction": episode.instruction,
                            "setup_info": episode.initial_info,
                            "official_target": official_target,
                        },
                        indent=2,
                        ensure_ascii=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                recorder_start = host.start_recorder(episode.instruction)
                if not recorder_start.get("session_id"):
                    raise AdapterError(
                        "recorder_start_failed", "recorder did not return a session ID"
                    )
                try:
                    host.arm_episode_guard(official_target)
                except Exception as error:
                    retain_failure("episode_guard_arm_failed", error)
                try:
                    if failure_code is None:
                        runtime_attempt = dict(attempt)
                        runtime_attempt["_runtime"] = {"official_target": official_target}
                        hook_type = STEP_ALIGNMENT_HOOKS.get(framework)
                        if hook_type is None:
                            # Skyvern 1.0.47's exact guarded public run_task
                            # signature has no reliable pre-action callback.
                            # Session capture remains formal evidence, but
                            # step-level analysis must fail closed.
                            runtime_attempt["_runtime"]["step_alignment"] = {
                                "supported": False,
                                "support_status": "not_supported",
                                "hook_type": "none",
                                "max_steps": attempt["limits"]["max_actions"],
                            }
                            framework_run = driver(
                                runtime_attempt, episode.instruction, host.cdp_url
                            )
                        else:
                            sidecar = StepAlignmentSidecar(
                                host,
                                framework,
                                attempt["limits"]["max_actions"],
                            )
                            try:
                                with sidecar:
                                    runtime_attempt["_runtime"]["step_alignment"] = (
                                        sidecar.descriptor()
                                    )
                                    framework_run = driver(
                                        runtime_attempt,
                                        episode.instruction,
                                        host.cdp_url,
                                    )
                            finally:
                                step_alignment_evidence = sidecar.public_evidence()
                        if framework_run is not None:
                            hook_calls = framework_run.artifacts.pop(
                                "framework_step_hook_calls", None
                            )
                            if (
                                not isinstance(hook_calls, int)
                                or isinstance(hook_calls, bool)
                                or hook_calls < 0
                            ):
                                hook_calls = 0
                                counts_match = step_alignment_evidence["accepted_requests"] == 0
                            else:
                                counts_match = (
                                    hook_calls == step_alignment_evidence["accepted_requests"]
                                )
                            step_alignment_evidence["framework_hook_calls"] = hook_calls
                            step_alignment_evidence["framework_host_counts_match"] = counts_match
                        if framework_run.timed_out:
                            retain_failure(
                                "framework_timeout",
                                RuntimeError("framework reached its bounded driver deadline"),
                            )
                        elif framework_run.error:
                            retain_failure(
                                "framework_error",
                                RuntimeError("framework ended with a non-completing step failure"),
                            )
                except Exception as error:
                    retain_failure("framework_driver_exception", error)
                try:
                    host.assert_episode_guard()
                    episode_guard_pre_validator = True
                except Exception as error:
                    retain_failure("episode_guard_pre_validator_failed", error)
                validator_completed = False
                if episode_guard_pre_validator:
                    try:
                        # No browser or framework operation is allowed between
                        # the sticky identity assertion and official reward.
                        reward, terminated, task_info = episode.validate()
                        validator_completed = True
                    except Exception as error:
                        retain_failure("official_validator_failed", error)
                    try:
                        # Keep every sticky listener armed through the official
                        # validator, then attest the same loader/document/
                        # environment boundary again before disarming.
                        host.assert_episode_guard()
                        episode_guard_post_validator = True
                    except Exception as error:
                        # Episode-integrity loss outranks a simultaneous
                        # validator exception in the public failure category.
                        promote_failure("episode_guard_post_validator_failed", error)
                try:
                    host.disarm_episode_guard(require_stable=True)
                    episode_guard_disarm_clean = True
                except Exception as error:
                    retain_failure("episode_guard_disarm_failed", error)
                episode_guard_stable = (
                    episode_guard_pre_validator
                    and episode_guard_post_validator
                    and episode_guard_disarm_clean
                )
                if validator_completed and episode_guard_stable:
                    try:
                        task_info_path = host.attempt_root / "official_validator_task_info.json"
                        task_info_path.write_text(
                            json.dumps(
                                {
                                    "schema_version": "cui-week1-private-validator-info-v1",
                                    "attempt_id": attempt["attempt_id"],
                                    "reward": reward,
                                    "terminated": terminated,
                                    "task_info": task_info,
                                },
                                indent=2,
                                ensure_ascii=True,
                            )
                            + "\n",
                            encoding="utf-8",
                        )
                    except Exception as error:
                        retain_failure("validator_artifact_write_failed", error)
                try:
                    episode.save_final_screenshot(host.attempt_root / "final.png")
                except Exception as error:
                    retain_failure("final_screenshot_failed", error)
                try:
                    recorder_stop = host.stop_recorder()
                except Exception as error:
                    retain_failure("recorder_stop_failed", error)

    assert host is not None
    trace_path = host.attempt_root / "recorder_trace.json"
    if not trace_path.is_file():
        fallback_trace = host.profile_root / "interrupted_trace.json"
        if fallback_trace.is_file():
            trace_path = fallback_trace
    trace_artifact = optional_private_artifact_descriptor(trace_path, "recorder-trace")
    screenshot_artifact = optional_private_artifact_descriptor(
        host.attempt_root / "final.png", "final-screenshot"
    )
    task_setup_artifact = optional_private_artifact_descriptor(
        host.attempt_root / "official_task_setup.json", "official-task-setup"
    )
    task_info_artifact = optional_private_artifact_descriptor(
        host.attempt_root / "official_validator_task_info.json", "official-validator-info"
    )
    recorder_status = str(
        (recorder_stop or {}).get("status") or ("interrupted" if recorder_start else "unknown")
    )
    recorder_session = (recorder_stop or {}).get("session_id") or (recorder_start or {}).get(
        "session_id"
    )
    if recorder_stop and recorder_session != recorder_start.get("session_id"):
        retain_failure(
            "recorder_session_mismatch",
            AdapterError("recorder_session_mismatch", "recorder start/stop session IDs differ"),
        )
    observed_viewport_value = host.episode_guard_final.get("viewport")
    observed_viewport = (
        {
            "width": int(observed_viewport_value["width"]),
            "height": int(observed_viewport_value["height"]),
        }
        if isinstance(observed_viewport_value, dict)
        and isinstance(observed_viewport_value.get("width"), int)
        and isinstance(observed_viewport_value.get("height"), int)
        else None
    )
    if (
        trace_artifact is not None
        and recorder_stop
        and recorder_session
        and official_target
        and observed_viewport is not None
    ):
        try:
            trace_validation = validate_formal_trace(
                trace_path,
                expected_session_id=str(recorder_session),
                expected_event_count=int(recorder_stop.get("event_count") or 0),
                expected_url=official_target["url"],
                expected_viewport=observed_viewport,
            )
            aligned_natural = int(trace_validation.get("aligned_natural_event_count") or 0)
            natural_total = int(trace_validation.get("natural_non_recorder_event_count") or 0)
            step_alignment_evidence["aligned_natural_event_count"] = aligned_natural
            step_alignment_evidence["unaligned_natural_event_count"] = max(
                0, natural_total - aligned_natural
            )
            step_alignment_evidence["alignment_coverage_percent"] = (
                round(100 * aligned_natural / natural_total, 2) if natural_total else 0.0
            )
            step_alignment_evidence["trace_step_id_count"] = int(
                trace_validation.get("step_id_count") or 0
            )
        except Exception as error:
            retain_failure("recorder_trace_invalid", error)
    if screenshot_artifact is not None and observed_viewport is not None:
        try:
            screenshot_validation = validate_png_screenshot(
                host.attempt_root / "final.png",
                observed_viewport,
            )
        except Exception as error:
            retain_failure("final_screenshot_invalid", error)
    if framework_run and (
        framework_run.action_count < 0
        or framework_run.action_count > attempt["limits"]["max_actions"]
    ):
        promote_failure(
            "action_cap_violation",
            AdapterError(
                "action_cap_violation",
                (
                    f"framework reported {framework_run.action_count} actions; "
                    f"limit is {attempt['limits']['max_actions']}"
                ),
            ),
        )
    published_action_count = (
        min(max(framework_run.action_count, 0), attempt["limits"]["max_actions"])
        if framework_run
        else 0
    )
    action_count_censored = bool(
        framework_run and published_action_count != framework_run.action_count
    )
    if recorder_status != "complete" or framework_run is None:
        status = "error"
    elif (
        framework_run.timed_out
        and failure_code == "framework_timeout"
        and not secondary_failure_codes
    ):
        status = "timeout"
    elif failure_code or framework_run.error:
        status = "error"
    else:
        status = "success" if reward == 1 and terminated else "failure"
    if failure_code is None and framework_run:
        if framework_run.timed_out:
            failure_code = "framework_timeout"
        elif framework_run.error:
            failure_code = "framework_error"
    provenance_complete = (
        recorder_status == "complete"
        and trace_artifact is not None
        and screenshot_artifact is not None
        and trace_validation is not None
        and screenshot_validation is not None
        and episode_guard_stable
    )
    artifact_bundle_complete = (
        provenance_complete
        and framework_run is not None
        and reward is not None
        and task_setup_artifact is not None
        and task_info_artifact is not None
        and len(dependency_snapshots) == len(FRAMEWORK_ENVIRONMENT_NAMES[framework])
    )
    # A task-level failure or timeout remains a scientifically valid matrix
    # outcome when its complete trace, screenshot, official reward envelope,
    # and identity evidence were retained. Integrity loss remains ineligible;
    # task success is evaluated separately from bundle completeness.
    formal_matrix_eligible = (
        artifact_bundle_complete
        and bool(trace_validation and trace_validation.get("capture_lossless") is True)
        and not secondary_failure_codes
    )
    action_step_alignment_eligible = bool(
        step_alignment_evidence.get("supported") is True
        and step_alignment_evidence.get("support_status") == "supported"
        and step_alignment_evidence.get("accepted_requests", 0) > 0
        and step_alignment_evidence.get("rejected_requests") == 0
        and step_alignment_evidence.get("clear_acknowledged") is True
        and step_alignment_evidence.get("framework_host_counts_match") is True
        and step_alignment_evidence.get("aligned_natural_event_count", 0) > 0
        and step_alignment_evidence.get("trace_step_id_count", 0)
        <= step_alignment_evidence.get("accepted_requests", 0)
    )
    step_alignment_evidence["analysis_eligible"] = action_step_alignment_eligible
    analysis_eligible = formal_matrix_eligible and action_step_alignment_eligible
    if framework_run is not None:
        # The runner's public boundary already recursively whitelists
        # framework evidence. Mirror this secret-free summary there so the
        # counters survive publication even if older readers ignore the new
        # recorder sub-object.
        framework_run.artifacts["action_step_alignment"] = dict(step_alignment_evidence)
    baseline_guard = host.episode_guard_baseline
    final_guard = host.episode_guard_final
    episode_guard_public = {
        "stable": episode_guard_stable,
        "sticky_lifecycle_monitoring": True,
        "pre_validator_attested": episode_guard_pre_validator,
        "post_validator_attested": episode_guard_post_validator,
        "clean_disarm_attested": episode_guard_disarm_clean,
        "attestation_count": len(host.episode_guard_attestations),
        "violation_count": 0 if episode_guard_stable else None,
        "target_id_unchanged": bool(
            baseline_guard
            and final_guard
            and baseline_guard.get("target_id_verified")
            and final_guard.get("target_id_verified")
        ),
        "loader_id_unchanged": bool(
            baseline_guard
            and baseline_guard.get("loader_id_sha256") == final_guard.get("loader_id_sha256")
        ),
        "document_token_unchanged": bool(
            baseline_guard
            and baseline_guard.get("document_token_sha256")
            == final_guard.get("document_token_sha256")
        ),
        "time_origin_unchanged": bool(
            baseline_guard
            and baseline_guard.get("time_origin_ms") == final_guard.get("time_origin_ms")
        ),
        "episode_start_unchanged": bool(
            baseline_guard
            and baseline_guard.get("episode_started_at_ms")
            == final_guard.get("episode_started_at_ms")
        ),
    }

    result = {
        "schema_version": ADAPTER_RESULT_SCHEMA,
        "attempt_id": attempt["attempt_id"],
        "status": status,
        "agent": {
            "framework": framework,
            "framework_version": attempt["framework"]["version"],
            "model": attempt["model"],
            "action_count": published_action_count,
            "action_count_observed": framework_run is not None,
            "action_count_censored": action_count_censored,
            "action_history": compact_actions(framework_run.action_history)
            if framework_run
            else [],
            "framework_completed": framework_run.completed if framework_run else False,
        },
        "validator": {
            "source": "browsergym_official_reward",
            "reward": reward,
            "terminated": terminated,
            "truncated": bool(
                framework_run
                and (
                    framework_run.timed_out
                    or (
                        framework_run.action_count == attempt["limits"]["max_actions"]
                        and not terminated
                    )
                )
            ),
            "task_setup_artifact": task_setup_artifact,
            "task_info_artifact": task_info_artifact,
        },
        "recorder": {
            "capture_adapter": "browser_mv3",
            "source_class": "cui_agent",
            "status": recorder_status,
            "session_id": recorder_session,
            "event_count": int((recorder_stop or {}).get("event_count") or 0),
            "trace_artifact": trace_artifact,
            "artifact_validation": trace_validation,
            "action_step_alignment": step_alignment_evidence,
        },
        "browser": {
            "product_version": host.browser_version,
            "viewport": observed_viewport,
            "device_pixel_ratio": host.episode_guard_final.get("device_pixel_ratio"),
            "locale": host.episode_guard_final.get("locale"),
            "timezone": host.episode_guard_final.get("timezone"),
            "environment_guard": host.environment_guard,
            "episode_guard": episode_guard_public,
            "fresh_profile": True,
        },
        "artifacts": {
            "final_screenshot": screenshot_artifact,
            "final_screenshot_validation": screenshot_validation,
            "framework": _safe_public_artifacts(_json_safe(framework_run.artifacts))
            if framework_run
            else {},
        },
        "error": (
            {
                "code": failure_code,
                "secondary_codes": secondary_failure_codes,
            }
            if failure_code
            else None
        ),
        "provenance_complete": provenance_complete,
        "artifact_bundle_complete": artifact_bundle_complete,
        "formal_matrix_eligible": formal_matrix_eligible,
        "analysis_eligible": analysis_eligible,
        "dependencies": {"snapshots": dependency_snapshots},
    }
    return result


def adapter_cli(framework: str, driver: Callable[[dict[str, Any], str, str], FrameworkRun]) -> int:
    if "--self-test" in sys.argv[1:]:
        diagnostic = common_diagnostics(framework)
        print(json.dumps(diagnostic, indent=2, ensure_ascii=True))
        return 0 if diagnostic["ready"] else 2
    attempt: dict[str, Any] | None = None
    try:
        attempt = read_attempt(sys.stdin, framework)
        result = run_formal_adapter(attempt, framework, driver)
    except AdapterError as error:
        if attempt is None:
            print(json.dumps({"ready": False, "error": error.as_dict()}, indent=2), file=sys.stderr)
            return 2
        print(
            json.dumps({"code": error.code, "detail": error.detail, "hint": error.hint}),
            file=sys.stderr,
        )
        result = error_result(attempt, framework, error)
    except Exception as error:  # framework libraries expose heterogeneous exception types
        diagnostic = AdapterError("unexpected_adapter_error", f"{type(error).__name__}: {error}")
        if attempt is None:
            print(
                json.dumps({"ready": False, "error": diagnostic.as_dict()}, indent=2),
                file=sys.stderr,
            )
            return 2
        print(json.dumps({"code": diagnostic.code, "detail": diagnostic.detail}), file=sys.stderr)
        result = error_result(attempt, framework, diagnostic)
    print(json.dumps(result, ensure_ascii=True))
    return 0
