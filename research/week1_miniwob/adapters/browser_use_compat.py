"""Narrow Browser-Use 0.11.5 compatibility for an externally owned CDP browser.

Browser-Use 0.11.5 configures ``waitForDebuggerOnStart=False`` but still calls
``Runtime.runIfWaitingForDebugger`` unconditionally whenever it retrieves the
focused CDP session.  Chrome can leave that redundant command unanswered on an
external, already-running browser.  There is no BrowserProfile/config switch
for this behavior in 0.11.5.

This module changes no installed file.  It temporarily skips only the known
unconditional caller, bounds the event-driven genuinely-waiting caller, and
normalizes the owned Chrome window immediately before ScreenshotWatchdog
captures. Exact dependency versions, callable signatures, module paths, and
source hashes are checked before the in-memory patch is installed.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import inspect
import json
import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator


EXPECTED_BROWSER_USE_VERSION = "0.11.5"
EXPECTED_CDP_USE_VERSION = "1.4.5"
RESUME_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True)
class SourceGuard:
    module_suffix: str
    signature: str
    source_sha256: str
    wrapper_module_suffix: str | None = None
    wrapper_file_sha256: str | None = None


SOURCE_GUARDS = {
    "BrowserSession.start": SourceGuard(
        module_suffix="browser_use/browser/session.py",
        signature="(self) -> None",
        source_sha256="29b435f9e40fb3db53ee94d024d2186146fbb03cea67e99d326bdfd920df2081",
        wrapper_module_suffix="browser_use/observability.py",
        wrapper_file_sha256="7ecaf85209270b8495a0a6c1d9bb39f8a6af1a102e3a9ea050b5000869135b03",
    ),
    "BrowserSession.stop": SourceGuard(
        module_suffix="browser_use/browser/session.py",
        signature="(self) -> None",
        source_sha256="3170030ca4dcda0752f745700cacc05dc15bfcc9d0e5da6a1030694b4098ce85",
    ),
    "BrowserSession.get_tabs": SourceGuard(
        module_suffix="browser_use/browser/session.py",
        signature="(self) -> list[browser_use.browser.views.TabInfo]",
        source_sha256="25504cb808822a096c858bbdccfbe5ea9a2fd052c93b108d4bb39df4bef42974",
    ),
    "BrowserSession.get_current_target_info": SourceGuard(
        module_suffix="browser_use/browser/session.py",
        signature="(self) -> cdp_use.cdp.target.types.TargetInfo | None",
        source_sha256="7ee70440a08f51c65409fe4feeb7efa16f5e8fa1fb528a7f38df2263bfe3d90e",
    ),
    "BrowserSession.get_or_create_cdp_session": SourceGuard(
        module_suffix="browser_use/browser/session.py",
        signature=(
            "(self, target_id: str | None = None, focus: bool = True) "
            "-> browser_use.browser.session.CDPSession"
        ),
        source_sha256="6698867b83b4efbdc6a397b0df82f1d78dfc914dc6d97bf57a625fd2dd84a76c",
    ),
    "SessionManager._handle_target_attached": SourceGuard(
        module_suffix="browser_use/browser/session_manager.py",
        signature=(
            "(self, event: cdp_use.cdp.target.events.AttachedToTargetEvent) -> None"
        ),
        source_sha256="189ee3cc4aaf00e12a5600c6ad28c7eb27e21bb36eb1aaadf8eb0570d24e72b9",
    ),
    "RuntimeClient.runIfWaitingForDebugger": SourceGuard(
        module_suffix="cdp_use/cdp/runtime/library.py",
        signature=(
            "(self, params: None = None, session_id: Optional[str] = None) "
            "-> 'Dict[str, Any]'"
        ),
        source_sha256="058342a8e57877e7e41869a53edde7b8de25dc2b06ec4e88462d4084de11a05c",
    ),
    "CDPClient.send_raw": SourceGuard(
        module_suffix="cdp_use/client.py",
        signature=(
            "(self, method: str, params: Optional[Any] = None, "
            "session_id: Optional[str] = None) -> dict[str, typing.Any]"
        ),
        source_sha256="e4cf09f79f25715e3c302141739294138c61c7d4cc40b14698f08b1547843f58",
    ),
    "ScreenshotWatchdog.on_ScreenshotEvent": SourceGuard(
        module_suffix="browser_use/browser/watchdogs/screenshot_watchdog.py",
        signature="(self, event: browser_use.browser.events.ScreenshotEvent) -> str",
        source_sha256="2e64a87fd57d7d399ed099cc712ccb62f59d8c55b83c5f114d18e63424d21063",
        wrapper_module_suffix="browser_use/observability.py",
        wrapper_file_sha256="7ecaf85209270b8495a0a6c1d9bb39f8a6af1a102e3a9ea050b5000869135b03",
    ),
}


class BrowserUseCompatibilityError(RuntimeError):
    """Raised when the exact guarded dependency contract is not present."""


@dataclass
class CompatibilityStats:
    skipped_unconditional_resumes: int = 0
    bounded_resume_timeouts: int = 0
    screenshot_window_checks: int = 0
    window_normalizations: int = 0
    bring_to_front_calls: int = 0


_SKIP_UNCONDITIONAL_RESUME: ContextVar[bool] = ContextVar(
    "cui_skip_browser_use_unconditional_resume", default=False
)
_PATCH_LOCK = threading.Lock()
_WINDOW_STATES = {"normal", "minimized", "maximized", "fullscreen"}


def _source_sha256(value: Callable[..., Any]) -> str:
    return hashlib.sha256(inspect.getsource(value).encode("utf-8")).hexdigest()


def _verify_callable(name: str, value: Callable[..., Any], guard: SourceGuard) -> dict[str, str]:
    implementation = inspect.unwrap(value)
    source_file = inspect.getsourcefile(implementation)
    if not source_file:
        raise BrowserUseCompatibilityError(f"{name} has no inspectable source file")
    normalized_path = Path(source_file).as_posix().lower()
    if not normalized_path.endswith(guard.module_suffix.lower()):
        raise BrowserUseCompatibilityError(
            f"{name} source path is {normalized_path!r}, expected *{guard.module_suffix!r}"
        )
    signature = str(inspect.signature(value))
    if signature != guard.signature:
        raise BrowserUseCompatibilityError(
            f"{name} signature changed: {signature!r} != {guard.signature!r}"
        )
    source_hash = _source_sha256(implementation)
    if source_hash != guard.source_sha256:
        raise BrowserUseCompatibilityError(
            f"{name} source hash changed: {source_hash} != {guard.source_sha256}"
        )
    verified = {
        "source": guard.module_suffix,
        "signature": signature,
        "source_sha256": source_hash,
    }
    if guard.wrapper_module_suffix or guard.wrapper_file_sha256:
        wrapper_file = inspect.getsourcefile(value)
        if value is implementation or not wrapper_file:
            raise BrowserUseCompatibilityError(f"{name} expected an exact observability wrapper")
        normalized_wrapper = Path(wrapper_file).as_posix().lower()
        if not normalized_wrapper.endswith(str(guard.wrapper_module_suffix).lower()):
            raise BrowserUseCompatibilityError(
                f"{name} wrapper path is {normalized_wrapper!r}, expected *{guard.wrapper_module_suffix!r}"
            )
        wrapper_hash = hashlib.sha256(Path(wrapper_file).read_bytes()).hexdigest()
        if wrapper_hash != guard.wrapper_file_sha256:
            raise BrowserUseCompatibilityError(
                f"{name} wrapper file hash changed: {wrapper_hash} != {guard.wrapper_file_sha256}"
            )
        verified["wrapper_source"] = str(guard.wrapper_module_suffix)
        verified["wrapper_file_sha256"] = wrapper_hash
    elif value is not implementation:
        raise BrowserUseCompatibilityError(f"{name} gained an unreviewed wrapper")
    return verified


def verify_browser_use_external_cdp_compatibility() -> dict[str, Any]:
    """Fail closed unless the exact reviewed Browser-Use/CDP source is installed."""

    browser_use_version = importlib.metadata.version("browser-use")
    cdp_use_version = importlib.metadata.version("cdp-use")
    if browser_use_version != EXPECTED_BROWSER_USE_VERSION:
        raise BrowserUseCompatibilityError(
            f"browser-use is {browser_use_version}, expected {EXPECTED_BROWSER_USE_VERSION}"
        )
    if cdp_use_version != EXPECTED_CDP_USE_VERSION:
        raise BrowserUseCompatibilityError(
            f"cdp-use is {cdp_use_version}, expected {EXPECTED_CDP_USE_VERSION}"
        )

    from browser_use.browser.session import BrowserSession
    from browser_use.browser.session_manager import SessionManager
    from browser_use.browser.watchdogs.screenshot_watchdog import ScreenshotWatchdog
    from cdp_use.cdp.runtime.library import RuntimeClient
    from cdp_use.client import CDPClient

    callables = {
        "BrowserSession.start": BrowserSession.start,
        "BrowserSession.stop": BrowserSession.stop,
        "BrowserSession.get_tabs": BrowserSession.get_tabs,
        "BrowserSession.get_current_target_info": BrowserSession.get_current_target_info,
        "BrowserSession.get_or_create_cdp_session": BrowserSession.get_or_create_cdp_session,
        "SessionManager._handle_target_attached": SessionManager._handle_target_attached,
        "RuntimeClient.runIfWaitingForDebugger": RuntimeClient.runIfWaitingForDebugger,
        "CDPClient.send_raw": CDPClient.send_raw,
        "ScreenshotWatchdog.on_ScreenshotEvent": ScreenshotWatchdog.on_ScreenshotEvent,
    }
    verified = {
        name: _verify_callable(name, value, SOURCE_GUARDS[name])
        for name, value in callables.items()
    }
    return {
        "browser_use_version": browser_use_version,
        "cdp_use_version": cdp_use_version,
        "resume_timeout_seconds": RESUME_TIMEOUT_SECONDS,
        "guards": verified,
    }


def _cleanup_exact_pending_request(client: Any, request_id: Any, future: Any) -> bool:
    """Remove one request only when both its ID and future identity match."""

    pending = getattr(client, "pending_requests", None)
    if not isinstance(pending, dict):
        return False
    if pending.get(request_id) is not future:
        return False
    pending.pop(request_id)
    return True


async def _bounded_exact_resume_send(
    client: Any,
    *,
    params: None,
    session_id: str | None,
    timeout_seconds: float,
) -> tuple[dict[str, Any] | None, bool]:
    """Send the pinned CDP command while retaining its exact request identity.

    The pinned ``cdp-use==1.4.5`` implementation does not clean a pending
    future after caller cancellation.  Reproducing its small, source-guarded
    send sequence here lets the timeout path remove precisely this resume
    request, even when a screenshot is issued concurrently.
    """

    if getattr(client, "ws", None) is None:
        raise RuntimeError("Client is not started. Call start() first or use as async context manager.")
    pending = getattr(client, "pending_requests", None)
    msg_id = getattr(client, "msg_id", None)
    if not isinstance(pending, dict) or not isinstance(msg_id, int):
        raise BrowserUseCompatibilityError("cdp-use client bookkeeping contract is unavailable")
    request_id = msg_id + 1
    client.msg_id = request_id
    message: dict[str, Any] = {
        "id": request_id,
        "method": "Runtime.runIfWaitingForDebugger",
        "params": params or {},
    }
    if session_id:
        message["sessionId"] = session_id
    future = asyncio.get_running_loop().create_future()
    pending[request_id] = future
    try:
        await client.ws.send(json.dumps(message))
        try:
            result = await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError:
            return None, _cleanup_exact_pending_request(client, request_id, future)
        return result, False
    except BaseException:
        _cleanup_exact_pending_request(client, request_id, future)
        if not future.done():
            future.cancel()
        raise


def _require_exact_official_target(browser_session: Any, expected_target: dict[str, str]) -> Any:
    """Return the sole focused page only when it matches the formal target."""

    focused = browser_session.get_focused_target()
    pages = browser_session.get_page_targets()
    if (
        focused is None
        or getattr(focused, "target_type", None) not in {"page", "tab"}
        or getattr(focused, "target_id", None) != expected_target["target_id"]
        or getattr(focused, "url", None) != expected_target["url"]
        or len(pages) != 1
        or getattr(pages[0], "target_id", None) != expected_target["target_id"]
        or getattr(pages[0], "url", None) != expected_target["url"]
    ):
        raise BrowserUseCompatibilityError("screenshot target identity is not the exact official page")
    return focused


def _window_geometry(bounds: Any) -> dict[str, int] | None:
    if not isinstance(bounds, dict):
        return None
    values = {key: bounds.get(key) for key in ("left", "top", "width", "height")}
    if any(not isinstance(value, int) or isinstance(value, bool) for value in values.values()):
        return None
    if values["width"] <= 0 or values["height"] <= 0:
        return None
    return {key: int(value) for key, value in values.items()}


async def _prepare_stable_screenshot_window(
    browser_session: Any,
    expected_target: dict[str, str],
    stats: CompatibilityStats,
    normal_window_bounds: dict[int, dict[str, int]],
) -> None:
    """Normalize and re-attest the exact formal page immediately before capture."""

    target = _require_exact_official_target(browser_session, expected_target)
    root_client = browser_session.cdp_client
    target_id = str(target.target_id)
    window = await root_client.send_raw(
        method="Browser.getWindowForTarget",
        params={"targetId": target_id},
    )
    window_id = window.get("windowId") if isinstance(window, dict) else None
    bounds = window.get("bounds") if isinstance(window, dict) else None
    state = bounds.get("windowState") if isinstance(bounds, dict) else None
    if not isinstance(window_id, int) or isinstance(window_id, bool) or state not in _WINDOW_STATES:
        raise BrowserUseCompatibilityError("screenshot window state could not be measured")
    stats.screenshot_window_checks += 1
    geometry = _window_geometry(bounds)
    if state == "normal" and geometry is not None:
        normal_window_bounds[window_id] = geometry
    elif state != "normal":
        preferred_geometry = normal_window_bounds.get(window_id)
        await root_client.send_raw(
            method="Browser.setWindowBounds",
            params={"windowId": window_id, "bounds": {"windowState": "normal"}},
        )
        # Chrome restores its last normal rectangle when leaving a minimized,
        # maximized, or fullscreen state. Reapply the last measured normal
        # rectangle when one is available so the formal viewport stays fixed.
        if preferred_geometry is not None:
            await root_client.send_raw(
                method="Browser.setWindowBounds",
                params={"windowId": window_id, "bounds": preferred_geometry},
            )
        stats.window_normalizations += 1

    page_session = await browser_session.get_or_create_cdp_session(target_id, focus=True)
    await page_session.cdp_client.send_raw(
        method="Page.bringToFront",
        params={},
        session_id=page_session.session_id,
    )
    stats.bring_to_front_calls += 1

    # Re-attest through both the root CDP connection and Browser-Use's session
    # manager after the window mutation/focus operation, immediately before the
    # original ScreenshotWatchdog performs Page.captureScreenshot.
    target_result = await root_client.send_raw(
        method="Target.getTargetInfo",
        params={"targetId": target_id},
    )
    target_info = target_result.get("targetInfo") if isinstance(target_result, dict) else None
    if (
        not isinstance(target_info, dict)
        or target_info.get("targetId") != expected_target["target_id"]
        or target_info.get("url") != expected_target["url"]
        or target_info.get("type") not in {"page", "tab"}
    ):
        raise BrowserUseCompatibilityError("screenshot target changed during window normalization")
    _require_exact_official_target(browser_session, expected_target)
    final_window = await root_client.send_raw(
        method="Browser.getWindowForTarget",
        params={"targetId": target_id},
    )
    final_window_id = final_window.get("windowId") if isinstance(final_window, dict) else None
    final_bounds = final_window.get("bounds") if isinstance(final_window, dict) else None
    if (
        final_window_id != window_id
        or not isinstance(final_bounds, dict)
        or final_bounds.get("windowState") != "normal"
    ):
        raise BrowserUseCompatibilityError("screenshot window did not normalize to a stable state")
    final_geometry = _window_geometry(final_bounds)
    if final_geometry is not None:
        normal_window_bounds[window_id] = final_geometry


@contextmanager
def _external_cdp_patch(
    browser_session_class: type[Any],
    runtime_client_class: type[Any],
    screenshot_watchdog_class: type[Any],
    *,
    expected_target: dict[str, str],
    timeout_seconds: float = RESUME_TIMEOUT_SECONDS,
) -> Iterator[CompatibilityStats]:
    """Install the reviewed in-memory patch; exposed separately for unit tests."""

    if timeout_seconds <= 0:
        raise BrowserUseCompatibilityError("resume timeout must be positive")
    if (
        not isinstance(expected_target, dict)
        or not isinstance(expected_target.get("target_id"), str)
        or not expected_target["target_id"]
        or not isinstance(expected_target.get("url"), str)
        or not expected_target["url"]
    ):
        raise BrowserUseCompatibilityError("an exact official screenshot target is required")
    original_get_session = browser_session_class.get_or_create_cdp_session
    original_resume = runtime_client_class.runIfWaitingForDebugger
    original_screenshot = screenshot_watchdog_class.on_ScreenshotEvent
    stats = CompatibilityStats()
    logger = logging.getLogger("cui.week1.browser_use_compat")
    normal_window_bounds: dict[int, dict[str, int]] = {}
    screenshot_lock = asyncio.Lock()

    @wraps(original_get_session)
    async def guarded_get_session(self: Any, target_id: Any = None, focus: bool = True) -> Any:
        token = _SKIP_UNCONDITIONAL_RESUME.set(True)
        try:
            return await original_get_session(self, target_id=target_id, focus=focus)
        finally:
            _SKIP_UNCONDITIONAL_RESUME.reset(token)

    @wraps(original_resume)
    async def guarded_resume(self: Any, params: None = None, session_id: str | None = None) -> dict[str, Any]:
        if _SKIP_UNCONDITIONAL_RESUME.get():
            stats.skipped_unconditional_resumes += 1
            logger.debug(
                "Skipped Browser-Use 0.11.5 redundant Runtime.runIfWaitingForDebugger "
                "for an externally attached focused session"
            )
            return {}

        client = getattr(self, "_client", None)
        result, cleaned = await _bounded_exact_resume_send(
            client,
            params=params,
            session_id=session_id,
            timeout_seconds=timeout_seconds,
        )
        if result is None:
            stats.bounded_resume_timeouts += 1
            logger.warning(
                "Timed out Runtime.runIfWaitingForDebugger after %.3fs; continuing because "
                "Browser-Use configured waitForDebuggerOnStart=False (exact_request_cleaned=%s)",
                timeout_seconds,
                cleaned,
            )
            return {}
        return result

    @wraps(original_screenshot)
    async def guarded_screenshot(self: Any, event: Any) -> str:
        async with screenshot_lock:
            await _prepare_stable_screenshot_window(
                self.browser_session,
                expected_target,
                stats,
                normal_window_bounds,
            )
            return await original_screenshot(self, event)

    if not _PATCH_LOCK.acquire(blocking=False):
        raise BrowserUseCompatibilityError("Browser-Use compatibility patch is already active")
    try:
        browser_session_class.get_or_create_cdp_session = guarded_get_session
        runtime_client_class.runIfWaitingForDebugger = guarded_resume
        screenshot_watchdog_class.on_ScreenshotEvent = guarded_screenshot
        try:
            yield stats
        finally:
            get_session_unchanged = browser_session_class.get_or_create_cdp_session is guarded_get_session
            resume_unchanged = runtime_client_class.runIfWaitingForDebugger is guarded_resume
            screenshot_unchanged = screenshot_watchdog_class.on_ScreenshotEvent is guarded_screenshot
            browser_session_class.get_or_create_cdp_session = original_get_session
            runtime_client_class.runIfWaitingForDebugger = original_resume
            screenshot_watchdog_class.on_ScreenshotEvent = original_screenshot
            if not get_session_unchanged or not resume_unchanged or not screenshot_unchanged:
                raise BrowserUseCompatibilityError(
                    "Browser-Use compatibility methods changed while the guarded patch was active"
                )
    finally:
        _PATCH_LOCK.release()


@contextmanager
def browser_use_external_cdp_compatibility(
    expected_target: dict[str, str],
) -> Iterator[CompatibilityStats]:
    """Verify and install the exact 0.11.5 external-CDP compatibility layer."""

    verify_browser_use_external_cdp_compatibility()
    from browser_use.browser.session import BrowserSession
    from browser_use.browser.watchdogs.screenshot_watchdog import ScreenshotWatchdog
    from cdp_use.cdp.runtime.library import RuntimeClient

    with _external_cdp_patch(
        BrowserSession,
        RuntimeClient,
        ScreenshotWatchdog,
        expected_target=expected_target,
    ) as stats:
        yield stats
