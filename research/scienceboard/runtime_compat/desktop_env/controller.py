"""Minimal OSWorld guest-controller client required by ScienceBoard.

This intentionally implements only the methods called by ScienceBoard's pinned
``VManager``.  Every network request is bounded and is sent only to the private
IPv4 address returned by ``vmrun getGuestIPAddress``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests


class GuestControllerError(RuntimeError):
    """Raised when the guest controller cannot complete a bounded request."""


class PythonController:
    """Small, fail-closed subset of OSWorld's ``PythonController``."""

    RETRY_ATTEMPTS = 3
    RETRY_INTERVAL_SECONDS = 1.0
    CONNECT_TIMEOUT_SECONDS = 5.0
    READ_TIMEOUT_SECONDS = 90.0

    def __init__(self, vm_ip: str, server_port: int = 5000) -> None:
        if not vm_ip or not isinstance(server_port, int) or not 1 <= server_port <= 65535:
            raise ValueError("Invalid guest controller endpoint")
        self.vm_ip = vm_ip
        self.http_server = f"http://{vm_ip}:{server_port}"
        self._session = requests.Session()
        # Private guest-controller traffic must never inherit a host proxy.
        self._session.trust_env = False

    @property
    def _timeout(self) -> tuple[float, float]:
        return self.CONNECT_TIMEOUT_SECONDS, self.READ_TIMEOUT_SECONDS

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        if method not in {"GET", "POST"} or not path.startswith("/"):
            raise ValueError("Unsafe guest-controller request")
        # Retrying an action POST after a lost response can duplicate a click or
        # keystroke. Only read-only GET operations are retried.
        attempts = self.RETRY_ATTEMPTS if method == "GET" else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self._session.request(
                    method,
                    self.http_server + path,
                    timeout=self._timeout,
                    **kwargs,
                )
                response.raise_for_status()
                return response
            except requests.RequestException as error:
                last_error = error
                if attempt + 1 < attempts:
                    time.sleep(self.RETRY_INTERVAL_SECONDS)
        raise GuestControllerError(
            f"Guest controller {method} {path} failed after {attempts} attempts"
        ) from last_error

    def get_screenshot(self) -> bytes:
        return bytes(self._request("GET", "/screenshot").content)

    def get_accessibility_tree(self) -> str:
        payload = self._request("GET", "/accessibility").json()
        value = payload.get("AT")
        if not isinstance(value, str):
            raise GuestControllerError("Guest accessibility response lacks textual AT")
        return value

    def get_terminal_output(self) -> str:
        payload = self._request("GET", "/terminal").json()
        value = payload.get("output")
        if not isinstance(value, str):
            raise GuestControllerError("Guest terminal response lacks textual output")
        return value

    def execute_python_command(self, command: str) -> dict[str, Any]:
        if not isinstance(command, str) or not command:
            raise ValueError("Python command must be a non-empty string")
        argv = [
            "python",
            "-c",
            "import pyautogui; import time; pyautogui.FAILSAFE = False; " + command,
        ]
        payload = self._request(
            "POST",
            "/execute",
            json={"command": argv, "shell": False},
        ).json()
        if not isinstance(payload, dict):
            raise GuestControllerError("Guest execute response is not an object")
        return payload

    def start_recording(self) -> None:
        self._request("POST", "/start_recording")

    def end_recording(self, destination: str) -> None:
        output = Path(destination).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        response = self._request("POST", "/end_recording", stream=True)
        with output.open("xb") as handle:
            for block in response.iter_content(chunk_size=8192):
                if block:
                    handle.write(block)

    def close(self) -> None:
        self._session.close()
