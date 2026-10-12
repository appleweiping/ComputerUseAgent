"""Compatibility implementation of the DesktopEnv surface ScienceBoard uses.

The pinned benchmark owns task setup, actions, observations, and evaluation.
This class owns only the VMware lifecycle and OSWorld guest controller that the
pinned ``sci.vm.vmanager.VManager`` accesses.  It is intentionally not a
general replacement for the upstream ``desktop-env`` distribution.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Tuple

from .controller import PythonController
from .vmware import VMwareProvider


class DesktopEnv:
    """Exact constructor surface required by ScienceBoard commit ``c8d5010b``."""

    def __init__(
        self,
        provider_name: str = "vmware",
        region: str | None = None,
        path_to_vm: str | None = None,
        snapshot_name: str = "init_state",
        action_space: str = "computer_13",
        cache_dir: str = "cache",
        screen_size: Tuple[int, int] = (1920, 1080),
        headless: bool = False,
        require_a11y_tree: bool = True,
        require_terminal: bool = False,
        os_type: str = "Ubuntu",
    ) -> None:
        if provider_name != "vmware":
            raise ValueError("The ScienceBoard runtime compatibility layer is VMware-only")
        if path_to_vm is None:
            raise ValueError("ScienceBoard must provide an existing VMX path")
        if action_space != "pyautogui":
            raise ValueError("Pinned ScienceBoard requires the pyautogui action space")
        if (
            not isinstance(screen_size, tuple)
            or len(screen_size) != 2
            or not all(isinstance(value, int) and value > 0 for value in screen_size)
        ):
            raise ValueError("screen_size must contain two positive integers")

        self.region = region
        self.path_to_vm = str(
            Path(os.path.expandvars(os.path.expanduser(path_to_vm))).resolve()
        )
        self.snapshot_name = snapshot_name
        self.action_space = action_space
        self.cache_dir_base = cache_dir
        self.screen_size = screen_size
        self.headless = headless
        self.require_a11y_tree = require_a11y_tree
        self.require_terminal = require_terminal
        self.os_type = os_type
        self.server_port = 5000
        self.provider = VMwareProvider(region)
        self._start_emulator()

    def _start_emulator(self) -> None:
        self.provider.start_emulator(self.path_to_vm, self.headless, self.os_type)
        vm_ip = self.provider.get_ip_address(self.path_to_vm)
        previous = getattr(self, "controller", None)
        self.vm_ip = vm_ip
        self.controller = PythonController(vm_ip, self.server_port)
        if previous is not None:
            previous.close()

    def _revert_to_snapshot(self) -> None:
        observed = self.provider.revert_to_snapshot(
            self.path_to_vm,
            self.snapshot_name,
        )
        if Path(observed).resolve() != Path(self.path_to_vm).resolve():
            raise RuntimeError("Local VMware snapshot revert unexpectedly changed the VMX path")

    def close(self) -> None:
        try:
            self.controller.close()
        finally:
            self.provider.stop_emulator(self.path_to_vm)
