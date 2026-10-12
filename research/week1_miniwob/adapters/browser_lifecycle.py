"""Browser, recorder, step-alignment, and official MiniWoB lifecycle.

The adapter orchestration facade owns experiment policy.  This module owns
the concrete resources used by one attempt: the Chrome host process, the
authenticated step sidecar, and the official BrowserGym task connection.
"""

from __future__ import annotations

import hmac
import inspect
import json
import os
import queue
import secrets
import signal
import shutil
import subprocess
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from .adapter_contract import (
    AdapterError,
    EXTENSION_ROOT,
    NODE_HOST,
    REPO_ROOT,
    STEP_ALIGNMENT_HOOKS,
    STEP_ALIGNMENT_PATH,
    STEP_ALIGNMENT_TOKEN_PATTERN,
    STEP_ID_PATTERN,
    TASK_CLASS_NAMES,
)


def terminate_owned_process_tree(process: subprocess.Popen[Any]) -> None:
    """Stop only the BrowserHost process group created for this attempt."""

    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=15,
            )
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class BrowserHost:
    """JSONL RPC client for the Puppeteer process that owns Chrome and the MV3 UI."""

    def __init__(
        self,
        attempt: dict[str, Any],
        private_root: Path,
        *,
        terminate_process_tree_fn: Callable[
            [subprocess.Popen[Any]], None
        ] = terminate_owned_process_tree,
    ):
        self.attempt = attempt
        self.private_root = private_root
        self.attempt_root = private_root / "week1" / attempt["attempt_id"]
        self.profile_root = private_root / "profiles" / attempt["attempt_id"]
        if self.attempt_root.exists():
            raise AdapterError(
                "private_attempt_exists",
                "a private artifact directory already exists for this attempt ID",
                "Use a new formal batch/attempt ID or explicitly archive the prior private evidence.",
            )
        if self.profile_root.exists():
            raise AdapterError(
                "fresh_profile_exists",
                "the requested Chrome profile path already exists",
                "Use a new formal batch/attempt ID; the adapter never deletes or reuses profiles.",
            )
        self.attempt_root.mkdir(parents=True, exist_ok=False)
        self.profile_root.mkdir(parents=True, exist_ok=False)
        self.process: subprocess.Popen[str] | None = None
        self.stderr_tail: deque[str] = deque(maxlen=80)
        self.rpc_counter = 0
        self.cdp_url = ""
        self.browser_version = ""
        self.environment_guard: dict[str, Any] = {}
        self.episode_guard_baseline: dict[str, Any] = {}
        self.episode_guard_final: dict[str, Any] = {}
        self.episode_guard_attestations: list[dict[str, Any]] = []
        self._stderr_log_path = self.attempt_root / "browser_host.stderr.log"
        self._terminate_process_tree = terminate_process_tree_fn

    def __enter__(self) -> "BrowserHost":
        node = shutil.which("node")
        chrome = os.environ.get("CUI_WEEK1_CHROME_EXECUTABLE")
        if not node or not chrome:
            raise AdapterError(
                "browser_host_unready", "Node or CUI_WEEK1_CHROME_EXECUTABLE is missing"
            )
        viewport = self.attempt["browser"]["viewport"]
        command = [
            node,
            str(NODE_HOST),
            "--chrome",
            str(Path(chrome).resolve()),
            "--extension",
            str(EXTENSION_ROOT.resolve()),
            "--profile",
            str(self.profile_root.resolve()),
            "--attempt-id",
            self.attempt["attempt_id"],
            "--width",
            str(viewport["width"]),
            "--height",
            str(viewport["height"]),
        ]
        process_options: dict[str, Any] = {}
        if os.name == "nt":
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            process_options["start_new_session"] = True
        self.process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            **process_options,
        )
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        try:
            ready = self._read_message(timeout=45)
            if ready.get("type") != "ready":
                raise AdapterError(
                    "browser_host_start_failed", f"unexpected host response: {ready}"
                )
            self.cdp_url = str(ready.get("cdp_url") or "")
            self.browser_version = str(ready.get("browser_version") or "")
            self.environment_guard = (
                ready.get("environment_guard")
                if isinstance(ready.get("environment_guard"), dict)
                else {}
            )
            if not self.cdp_url.startswith(("ws://", "wss://", "http://", "https://")):
                raise AdapterError(
                    "browser_host_start_failed", "host did not return a CDP endpoint"
                )
            expected_browser = f"Chrome/{self.attempt['browser']['product_version']}"
            if self.browser_version != expected_browser:
                raise AdapterError(
                    "chrome_product_version_mismatch",
                    f"browser host reported {self.browser_version or 'unknown'}; expected {expected_browser}",
                )
            if (
                ready.get("locale") != self.attempt["browser"].get("locale")
                or ready.get("timezone") != self.attempt["browser"].get("timezone")
                or self.environment_guard.get("verified") is not True
                or self.environment_guard.get("capture_stability_verified") is not True
                or self.environment_guard.get("capture_stability_switch_count") != 3
                or self.environment_guard.get("native_window_occlusion_disabled") is not True
            ):
                raise AdapterError(
                    "browser_environment_mismatch",
                    "browser host did not attest the pinned locale/timezone/capture-stability contract",
                )
        except BaseException:
            self._terminate_process_tree(self.process)
            raise
        return self

    def _drain_stderr(self) -> None:
        if not self.process or not self.process.stderr:
            return
        with self._stderr_log_path.open("a", encoding="utf-8") as private_log:
            for line in self.process.stderr:
                self.stderr_tail.append(line.rstrip())
                private_log.write(line)
                private_log.flush()

    def _read_message(self, timeout: float) -> dict[str, Any]:
        if not self.process or not self.process.stdout:
            raise AdapterError("browser_host_protocol", "browser host stdout is unavailable")
        result_queue: queue.Queue[str] = queue.Queue(maxsize=1)

        def read_one() -> None:
            assert self.process and self.process.stdout
            result_queue.put(self.process.stdout.readline())

        threading.Thread(target=read_one, daemon=True).start()
        try:
            line = result_queue.get(timeout=timeout)
        except queue.Empty as error:
            raise AdapterError(
                "browser_host_timeout", f"browser host did not answer within {timeout:g}s"
            ) from error
        if not line:
            detail = " | ".join(self.stderr_tail) or "browser host exited without a message"
            raise AdapterError("browser_host_exited", detail[-3000:])
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise AdapterError(
                "browser_host_protocol", f"invalid host JSON: {line[:500]}"
            ) from error
        if not isinstance(value, dict):
            raise AdapterError("browser_host_protocol", "host response must be an object")
        if value.get("ok") is False:
            raise AdapterError(
                "browser_host_operation_failed", str(value.get("error") or "unknown host error")
            )
        return value

    def rpc(
        self, operation: str, payload: dict[str, Any] | None = None, timeout: float = 30
    ) -> dict[str, Any]:
        if not self.process or not self.process.stdin:
            raise AdapterError("browser_host_protocol", "browser host stdin is unavailable")
        self.rpc_counter += 1
        request_id = f"rpc-{self.rpc_counter:04d}"
        request = {"id": request_id, "operation": operation, "payload": payload or {}}
        self.process.stdin.write(json.dumps(request, ensure_ascii=True) + "\n")
        self.process.stdin.flush()
        response = self._read_message(timeout)
        if response.get("id") != request_id:
            raise AdapterError(
                "browser_host_protocol", f"host response id mismatch for {operation}"
            )
        return response

    def start_recorder(self, instruction: str) -> dict[str, Any]:
        attempt = self.attempt
        metadata = {
            "actorType": "agent",
            "participantCode": f"agent-{attempt['framework']['id']}",
            "pairId": (f"{attempt['protocol_version']}:{attempt['task']['id']}:s{attempt['seed']}")[
                :80
            ],
            "taskId": attempt["task"]["environment_id"],
            "protocolVersion": attempt["protocol_version"],
            "actorLabel": attempt["framework"]["id"],
            "executor": attempt["framework"]["id"],
            "executorVersion": attempt["framework"]["version"],
            "captureAdapter": "browser_mv3",
            "environmentScope": "browsergym_miniwob",
            "sourceClass": "cui_agent",
            "rawKeyCapture": False,
            "taskInstructionLength": len(instruction),
        }
        return self.rpc("start_recorder", metadata, timeout=30)

    def pin_post_setup_environment(self) -> dict[str, Any]:
        result = self.rpc("pin_post_setup_environment", timeout=20)
        if (
            result.get("pinned") is not True
            or result.get("viewport") != self.attempt["browser"]["viewport"]
            or result.get("device_pixel_ratio") != 1
            or result.get("locale") != self.attempt["browser"].get("locale")
            or result.get("timezone") != self.attempt["browser"].get("timezone")
        ):
            raise AdapterError(
                "browser_environment_mismatch",
                "browser host could not verify post-setup viewport/DPR/locale/timezone",
            )
        return result

    def stop_recorder(self) -> dict[str, Any]:
        trace_path = self.attempt_root / "recorder_trace.json"
        return self.rpc("stop_recorder", {"trace_path": str(trace_path.resolve())}, timeout=45)

    def set_recorder_step(self, step_id: str | None) -> dict[str, Any]:
        """Atomically change the background-owned step token for future events."""

        if step_id is not None and STEP_ID_PATTERN.fullmatch(step_id) is None:
            raise AdapterError("step_alignment_invalid", "step ID is not a bounded opaque token")
        result = self.rpc("set_recorder_step", {"step_id": step_id}, timeout=15)
        if result.get("step_id") != step_id:
            raise AdapterError(
                "step_alignment_failed", "recorder did not acknowledge the requested step"
            )
        return result

    def arm_episode_guard(self, official_target: dict[str, str]) -> dict[str, Any]:
        payload = {
            "target_id": official_target["target_id"],
            "url": official_target["url"],
        }
        result = self.rpc("arm_episode_guard", payload, timeout=20)
        baseline = result.get("baseline")
        if result.get("armed") is not True or not isinstance(baseline, dict):
            raise AdapterError(
                "episode_guard_arm_failed", "browser host did not return a guarded baseline"
            )
        self.episode_guard_baseline = baseline
        return result

    def assert_episode_guard(self) -> dict[str, Any]:
        result = self.rpc("assert_episode_guard", timeout=20)
        current = result.get("current")
        sequence = result.get("attestation_sequence")
        if (
            result.get("stable") is not True
            or not isinstance(current, dict)
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence != len(self.episode_guard_attestations) + 1
        ):
            raise AdapterError(
                "episode_guard_invalid", "browser host did not confirm stable episode identity"
            )
        self.episode_guard_final = current
        self.episode_guard_attestations.append(result)
        return result

    def disarm_episode_guard(self, *, require_stable: bool = False) -> dict[str, Any]:
        result = self.rpc("disarm_episode_guard", timeout=10)
        if require_stable and (
            result.get("disarmed") is not True
            or result.get("violation_count") != 0
            or result.get("final_attestation_sequence") != len(self.episode_guard_attestations)
        ):
            raise AdapterError(
                "episode_guard_disarm_unstable",
                "lifecycle violation appeared after the final validator-boundary attestation",
            )
        return result

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        if not self.process:
            return
        try:
            if self.process.poll() is None:
                self.rpc("shutdown", timeout=15)
        except Exception:
            pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._terminate_process_tree(self.process)


class StepAlignmentSidecar:
    """Authenticated loopback bridge from framework hooks to BrowserHost.

    The framework process receives a random bearer token and can submit only
    the next deterministic opaque step ID. Free text, selectors, action
    payloads, and arbitrary metadata are rejected at this boundary.
    """

    def __init__(self, host: BrowserHost, framework: str, max_steps: int):
        hook_type = STEP_ALIGNMENT_HOOKS.get(framework)
        if hook_type is None:
            raise AdapterError(
                "step_alignment_unsupported", f"{framework} has no reviewed pre-action hook"
            )
        if (
            not isinstance(max_steps, int)
            or isinstance(max_steps, bool)
            or not 1 <= max_steps <= 999
        ):
            raise AdapterError(
                "step_alignment_invalid", "step alignment requires a bounded step cap"
            )
        self.host = host
        self.framework = framework
        self.hook_type = hook_type
        self.max_steps = max_steps
        self.token = secrets.token_urlsafe(48)
        self.request_count = 0
        self.accepted_count = 0
        self.rejected_count = 0
        self.clear_acknowledged = False
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def descriptor(self) -> dict[str, Any]:
        if self._server is None:
            raise AdapterError("step_alignment_unready", "step sidecar has not started")
        host, port = self._server.server_address[:2]
        return {
            "endpoint": f"http://{host}:{port}{STEP_ALIGNMENT_PATH}",
            "bearer_token": self.token,
            "framework": self.framework,
            "max_steps": self.max_steps,
        }

    def public_evidence(self) -> dict[str, Any]:
        return {
            "supported": True,
            "support_status": "supported",
            "hook_type": self.hook_type,
            "transport_type": "authenticated_loopback_http",
            "max_steps": self.max_steps,
            "request_count": self.request_count,
            "accepted_requests": self.accepted_count,
            "rejected_requests": self.rejected_count,
            "clear_acknowledged": self.clear_acknowledged,
        }

    def _handle_step(self, authorization: str, body: bytes) -> tuple[int, dict[str, Any]]:
        with self._lock:
            self.request_count += 1
            expected_authorization = f"Bearer {self.token}"
            if not hmac.compare_digest(authorization, expected_authorization):
                self.rejected_count += 1
                return 401, {"ok": False, "code": "unauthorized"}
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.rejected_count += 1
                return 400, {"ok": False, "code": "invalid_json"}
            if not isinstance(payload, dict) or set(payload) != {"step_id"}:
                self.rejected_count += 1
                return 400, {"ok": False, "code": "invalid_shape"}
            step_id = payload.get("step_id")
            matched = STEP_ID_PATTERN.fullmatch(step_id) if isinstance(step_id, str) else None
            expected_index = self.accepted_count + 1
            if (
                matched is None
                or int(matched.group(1)) != expected_index
                or expected_index > self.max_steps
            ):
                self.rejected_count += 1
                return 409, {"ok": False, "code": "step_sequence_invalid"}
            try:
                self.host.set_recorder_step(step_id)
            except Exception:
                self.rejected_count += 1
                return 502, {"ok": False, "code": "recorder_rejected"}
            self.accepted_count = expected_index
            return 200, {"ok": True, "step_id": step_id}

    def __enter__(self) -> "StepAlignmentSidecar":
        sidecar = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "CUI-Step-Alignment/1"
            sys_version = ""

            def _send(self, status: int, payload: dict[str, Any]) -> None:
                encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode(
                    "ascii"
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(encoded)

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
                if self.path != STEP_ALIGNMENT_PATH:
                    self._send(404, {"ok": False, "code": "not_found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    length = -1
                if not 2 <= length <= 160:
                    with sidecar._lock:
                        sidecar.request_count += 1
                        sidecar.rejected_count += 1
                    self._send(413, {"ok": False, "code": "invalid_size"})
                    return
                body = self.rfile.read(length)
                status, response = sidecar._handle_step(self.headers.get("Authorization", ""), body)
                self._send(status, response)

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
                self._send(405, {"ok": False, "code": "method_not_allowed"})

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"cui-step-sidecar-{self.framework}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, _value: Any, _traceback: Any) -> None:
        clear_error: Exception | None = None
        try:
            self.host.set_recorder_step(None)
            self.clear_acknowledged = True
        except Exception as error:  # retain the original framework exception, if any
            clear_error = error
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.token = ""
        if clear_error is not None and exc_type is None:
            raise AdapterError(
                "step_alignment_clear_failed", "recorder step token could not be cleared"
            ) from clear_error


def validate_step_alignment_descriptor(value: Any, framework: str) -> dict[str, Any]:
    """Validate a private runtime descriptor without returning free-form data."""

    if not isinstance(value, dict) or set(value) != {
        "endpoint",
        "bearer_token",
        "framework",
        "max_steps",
    }:
        raise AdapterError("step_alignment_invalid", "step alignment descriptor shape changed")
    if value.get("framework") != framework or framework not in STEP_ALIGNMENT_HOOKS:
        raise AdapterError("step_alignment_invalid", "step alignment framework does not match")
    endpoint = value.get("endpoint")
    parsed = urlparse(endpoint) if isinstance(endpoint, str) else None
    if (
        parsed is None
        or parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.path != STEP_ALIGNMENT_PATH
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise AdapterError(
            "step_alignment_invalid", "step alignment endpoint is not exact loopback HTTP"
        )
    token = value.get("bearer_token")
    if not isinstance(token, str) or STEP_ALIGNMENT_TOKEN_PATTERN.fullmatch(token) is None:
        raise AdapterError("step_alignment_invalid", "step alignment bearer token is malformed")
    max_steps = value.get("max_steps")
    if not isinstance(max_steps, int) or isinstance(max_steps, bool) or not 1 <= max_steps <= 999:
        raise AdapterError("step_alignment_invalid", "step alignment step cap is malformed")
    return dict(value)


def post_step_alignment(descriptor: Any, framework: str, step_index: int) -> None:
    """Submit exactly one opaque pre-action step token to the local sidecar."""

    value = validate_step_alignment_descriptor(descriptor, framework)
    if (
        not isinstance(step_index, int)
        or isinstance(step_index, bool)
        or not 1 <= step_index <= value["max_steps"]
    ):
        raise AdapterError(
            "step_alignment_invalid", "framework emitted a step outside the formal cap"
        )
    # Deliberately framework-neutral: the trace must not gain a classifier
    # shortcut through a provenance label embedded in step_id.
    step_id = f"step-{step_index:03d}"
    body = json.dumps({"step_id": step_id}, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )
    request = Request(
        value["endpoint"],
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {value['bearer_token']}",
            "Content-Type": "application/json",
        },
    )
    # Do not let ambient HTTP(S)_PROXY configuration forward this private
    # bearer token away from loopback. Node's native fetch ignores those
    # variables by default; Python explicitly installs the same local-only
    # transport guarantee.
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:  # noqa: S310 - exact loopback URL validated above
            payload = json.loads(response.read(512).decode("ascii"))
    except HTTPError as error:
        error.close()
        raise AdapterError(
            "step_alignment_sidecar_failed", "pre-action step was not acknowledged"
        ) from error
    except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AdapterError(
            "step_alignment_sidecar_failed", "pre-action step was not acknowledged"
        ) from error
    if payload != {"ok": True, "step_id": step_id}:
        raise AdapterError(
            "step_alignment_sidecar_failed", "pre-action step acknowledgement changed"
        )


class OfficialMiniwobEpisode:
    """Official MiniWoB task setup and reward validation on the shared CDP page."""

    def __init__(self, attempt: dict[str, Any], host: BrowserHost, base_url: str):
        self.attempt = attempt
        self.host = host
        self.base_url = base_url
        self.playwright = None
        self.browser = None
        self.page = None
        self.task = None
        self.instruction = ""
        self.initial_info: dict[str, Any] = {}
        self.official_target: dict[str, str] = {}

    def __enter__(self) -> "OfficialMiniwobEpisode":
        from browsergym.miniwob import all as miniwob_tasks
        from browsergym.miniwob.base import AbstractMiniwobTask
        from playwright.sync_api import sync_playwright

        task_class = getattr(
            miniwob_tasks, TASK_CLASS_NAMES[self.attempt["task"]["environment_id"]]
        )
        assert_miniwob_timer_is_milliseconds(AbstractMiniwobTask)
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.connect_over_cdp(self.host.cdp_url, timeout=30_000)
        contexts = self.browser.contexts
        if len(contexts) != 1:
            raise AdapterError(
                "browser_context_mismatch",
                f"expected one persistent context, observed {len(contexts)}",
            )
        marker = f"about:blank#cui-{self.attempt['attempt_id']}"
        candidates = [page for page in contexts[0].pages if page.url == marker]
        if len(candidates) != 1:
            raise AdapterError(
                "task_page_mismatch", f"could not identify the single task page ({marker})"
            )
        self.page = candidates[0]
        base_url = self.base_url
        if not base_url.endswith("/"):
            base_url += "/"
        self.task = task_class(
            seed=self.attempt["seed"],
            base_url=base_url,
            episode_max_time=self.attempt["limits"]["timeout_seconds"] * 1000,
            remove_human_display=True,
        )
        instruction, info = self.task.setup(self.page)
        if not isinstance(instruction, str) or not instruction.strip():
            raise AdapterError(
                "task_setup_failed", "official MiniWoB setup returned no text instruction"
            )
        # BrowserGym's Playwright setup can reset viewport emulation on an
        # externally-owned Chrome target. Re-pin and observe the complete
        # viewport/DPR/locale/timezone contract before any recorder event or
        # framework action is allowed.
        self.host.pin_post_setup_environment()
        self.instruction = instruction
        self.initial_info = _json_safe(info)
        cdp_session = contexts[0].new_cdp_session(self.page)
        try:
            target_info = cdp_session.send("Target.getTargetInfo").get("targetInfo") or {}
        finally:
            cdp_session.detach()
        target_id = str(target_info.get("targetId") or "")
        if not target_id or str(target_info.get("type") or "") != "page":
            raise AdapterError(
                "official_target_unavailable", "could not resolve the official task page target ID"
            )
        if str(target_info.get("url") or "") != self.page.url:
            raise AdapterError(
                "official_target_unavailable",
                "official task page URL disagrees with CDP target info",
            )
        self.official_target = {"target_id": target_id, "url": self.page.url}
        return self

    def validate(self) -> tuple[float, bool, dict[str, Any]]:
        if not self.task or not self.page:
            raise AdapterError("validator_unavailable", "official task was not initialized")
        reward, done, _message, info = self.task.validate(self.page, [])
        numeric_reward = float(reward)
        if not 0 <= numeric_reward <= 1:
            raise AdapterError(
                "validator_invalid", f"official reward outside [0,1]: {numeric_reward}"
            )
        return numeric_reward, bool(done), _json_safe(info)

    def save_final_screenshot(self, destination: Path) -> None:
        if not self.page:
            raise AdapterError("screenshot_unavailable", "task page is unavailable")
        self.page.screenshot(path=str(destination), full_page=False)

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        # ``BrowserHost`` owns the persistent Chrome process.  Closing the
        # Playwright ``Browser`` returned by ``connect_over_cdp`` would also
        # close that process before the recorder/export host can shut down
        # cleanly.  Stopping Playwright disconnects this client while leaving
        # Chrome under the Node host's lifecycle control.
        if self.playwright:
            self.playwright.stop()


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, default=str))


def assert_miniwob_timer_is_milliseconds(task_base: type[Any]) -> None:
    """Fail closed if the pinned BrowserGym timer contract changes units."""
    signature = inspect.signature(task_base.__init__)
    parameter = signature.parameters.get("episode_max_time")
    documentation = (inspect.getdoc(task_base.__init__) or "").lower()
    if parameter is None or parameter.default != 1_000_000 or "milliseconds" not in documentation:
        raise AdapterError(
            "browsergym_timer_contract_changed",
            "BrowserGym no longer exposes the verified millisecond episode_max_time contract",
        )
