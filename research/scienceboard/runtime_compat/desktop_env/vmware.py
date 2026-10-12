"""Shell-free VMware lifecycle used by the ScienceBoard compatibility layer."""

from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path


class VMwareRuntimeError(RuntimeError):
    """Raised when ``vmrun`` violates the pinned runtime contract."""


VMRUN_ENVIRONMENT_VARIABLE = "VMRUN_PATH"


def discover_vmrun() -> Path | None:
    """Return the ``vmrun`` executable, or ``None`` when it cannot be found.

    This is the single discovery rule shared by the host preflight and the
    runtime provider: an explicit ``VMRUN_PATH`` wins (and must name an existing
    file); otherwise ``vmrun`` must be on ``PATH``. No install directory or drive
    letter is guessed.
    """

    configured = os.environ.get(VMRUN_ENVIRONMENT_VARIABLE, "").strip()
    if configured:
        path = Path(os.path.expandvars(os.path.expanduser(configured)))
        return path.resolve() if path.is_file() else None
    executable = shutil.which("vmrun")
    return Path(executable).resolve() if executable else None


class VMwareProvider:
    """Bounded VMware Workstation provider for an already-provisioned VMX."""

    COMMAND_TIMEOUT_SECONDS = 300.0
    START_POLL_ATTEMPTS = 30
    START_POLL_INTERVAL_SECONDS = 2.0

    def __init__(self, region: str | None = None) -> None:
        if region is not None:
            raise ValueError("The local VMware provider does not accept a cloud region")
        executable = discover_vmrun()
        if executable is None:
            raise VMwareRuntimeError("vmrun was not found via VMRUN_PATH or PATH")
        self.executable = str(executable)

    def _run(
        self,
        *arguments: str,
        accepted_return_codes: Iterable[int] = (0,),
    ) -> str:
        if not arguments or not all(isinstance(item, str) and item for item in arguments):
            raise ValueError("vmrun arguments must be non-empty strings")
        completed = subprocess.run(
            [self.executable, "-T", "ws", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.COMMAND_TIMEOUT_SECONDS,
            shell=False,
            check=False,
        )
        if completed.returncode not in set(accepted_return_codes):
            detail = completed.stderr.strip().replace("\r", " ").replace("\n", " ")[:500]
            raise VMwareRuntimeError(
                f"vmrun {arguments[0]} failed with code {completed.returncode}: {detail}"
            )
        return completed.stdout.strip()

    @staticmethod
    def _vmx(path_to_vm: str) -> Path:
        path = Path(os.path.expandvars(os.path.expanduser(path_to_vm))).resolve()
        if path.suffix.lower() != ".vmx" or not path.is_file():
            raise VMwareRuntimeError("VM path must be an existing .vmx file")
        return path

    def _running_vms(self) -> set[Path]:
        output = self._run("list")
        lines = output.splitlines()[1:] if output else []
        return {Path(line.strip()).resolve() for line in lines if line.strip()}

    def start_emulator(self, path_to_vm: str, headless: bool, os_type: str) -> None:
        if os_type != "Ubuntu":
            raise VMwareRuntimeError("Pinned ScienceBoard supports only the Ubuntu guest")
        vmx = self._vmx(path_to_vm)
        if vmx not in self._running_vms():
            arguments = ["start", str(vmx)]
            if headless:
                arguments.append("nogui")
            self._run(*arguments)
        for attempt in range(self.START_POLL_ATTEMPTS):
            if vmx in self._running_vms():
                return
            if attempt + 1 < self.START_POLL_ATTEMPTS:
                time.sleep(self.START_POLL_INTERVAL_SECONDS)
        raise VMwareRuntimeError("VM did not reach the running set before the bounded deadline")

    def get_ip_address(self, path_to_vm: str) -> str:
        vmx = self._vmx(path_to_vm)
        raw = self._run("getGuestIPAddress", str(vmx), "-wait").strip()
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as error:
            raise VMwareRuntimeError("vmrun returned an invalid guest IP address") from error
        if (
            not isinstance(address, ipaddress.IPv4Address)
            or not address.is_private
            or address.is_loopback
            or address.is_multicast
            or address.is_unspecified
        ):
            raise VMwareRuntimeError("Guest controller address is not a private non-loopback IPv4")
        return str(address)

    def revert_to_snapshot(self, path_to_vm: str, snapshot_name: str) -> str:
        vmx = self._vmx(path_to_vm)
        if not snapshot_name or any(char in snapshot_name for char in "\r\n\0"):
            raise ValueError("Unsafe snapshot name")
        self._run("revertToSnapshot", str(vmx), snapshot_name)
        return str(vmx)

    def stop_emulator(self, path_to_vm: str) -> None:
        vmx = self._vmx(path_to_vm)
        if vmx in self._running_vms():
            self._run("stop", str(vmx), "hard")
