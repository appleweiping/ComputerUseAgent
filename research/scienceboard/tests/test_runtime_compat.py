from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from research.scienceboard.host_worker import activate_runtime_compat
from research.scienceboard.preflight import _discover_vmrun
from research.scienceboard.runtime_compat.desktop_env.controller import (
    PythonController,
)
from research.scienceboard.runtime_compat.desktop_env.desktop_env import DesktopEnv
from research.scienceboard.runtime_compat.desktop_env.vmware import (
    VMRUN_ENVIRONMENT_VARIABLE,
    VMwareProvider,
    VMwareRuntimeError,
    discover_vmrun,
)

# The provider honours an explicit VMRUN_PATH; keep the host's value out of tests.
NO_VMRUN_OVERRIDE = {VMRUN_ENVIRONMENT_VARIABLE: ""}


class FakeResponse:
    def __init__(self, *, content: bytes = b"png", payload: object = None) -> None:
        self.content = content
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class RuntimeCompatibilityTests(unittest.TestCase):
    def test_vmware_lifecycle_is_bounded_shell_free_and_path_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            vmx = (Path(temp) / "ScienceBoard.vmx").resolve()
            vmx.write_text(".encoding = utf-8\n", encoding="utf-8")
            started = False
            invocations: list[tuple[list[str], dict[str, object]]] = []

            def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                nonlocal started
                invocations.append((list(argv), dict(kwargs)))
                operation = argv[3]
                stdout = ""
                if operation == "list":
                    stdout = (
                        f"Total running VMs: {1 if started else 0}\n"
                        + (f"{vmx}\n" if started else "")
                    )
                elif operation == "start":
                    started = True
                elif operation == "getGuestIPAddress":
                    stdout = "192.168.56.101\n"
                elif operation == "stop":
                    started = False
                return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

            with mock.patch.dict(os.environ, NO_VMRUN_OVERRIDE), mock.patch(
                "research.scienceboard.runtime_compat.desktop_env.vmware.shutil.which",
                return_value="/opt/vmware/bin/vmrun",
            ), mock.patch(
                "research.scienceboard.runtime_compat.desktop_env.vmware.subprocess.run",
                side_effect=run,
            ):
                environment = DesktopEnv(
                    path_to_vm=str(vmx),
                    snapshot_name="sci_bench",
                    action_space="pyautogui",
                    headless=True,
                )
                self.assertEqual(environment.controller.vm_ip, "192.168.56.101")
                environment._revert_to_snapshot()
                environment._start_emulator()
                environment.close()

            operations = [argv[3] for argv, _ in invocations]
            self.assertIn("start", operations)
            self.assertIn("revertToSnapshot", operations)
            self.assertIn("getGuestIPAddress", operations)
            self.assertIn("stop", operations)
            for argv, kwargs in invocations:
                self.assertIs(kwargs["shell"], False)
                self.assertIsInstance(argv, list)
                self.assertEqual(argv[1:3], ["-T", "ws"])

    def test_vmware_rejects_public_guest_controller_address(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            vmx = Path(temp) / "ScienceBoard.vmx"
            vmx.write_text("vmx", encoding="utf-8")
            with mock.patch.dict(os.environ, NO_VMRUN_OVERRIDE), mock.patch(
                "research.scienceboard.runtime_compat.desktop_env.vmware.shutil.which",
                return_value="/opt/vmware/bin/vmrun",
            ):
                provider = VMwareProvider()
            with mock.patch.object(provider, "_run", return_value="8.8.8.8"):
                with self.assertRaisesRegex(VMwareRuntimeError, "private"):
                    provider.get_ip_address(str(vmx))

    def test_guest_controller_sends_shell_false_json_with_timeouts(self) -> None:
        response = FakeResponse(payload={"output": "ok"})
        with mock.patch(
            "research.scienceboard.runtime_compat.desktop_env.controller.requests.Session.request",
            return_value=response,
        ) as request:
            controller = PythonController("192.168.56.101")
            result = controller.execute_python_command("pyautogui.click(10, 20)")
            self.assertFalse(controller._session.trust_env)
            controller.close()
        self.assertEqual(result, {"output": "ok"})
        _, url = request.call_args.args
        kwargs = request.call_args.kwargs
        self.assertEqual(url, "http://192.168.56.101:5000/execute")
        self.assertEqual(kwargs["json"]["shell"], False)
        self.assertIsInstance(kwargs["json"]["command"], list)
        self.assertEqual(kwargs["timeout"], (5.0, 90.0))

    def test_non_idempotent_guest_action_is_never_retried(self) -> None:
        with mock.patch(
            "research.scienceboard.runtime_compat.desktop_env.controller.requests.Session.request",
            side_effect=requests.ConnectionError("response lost"),
        ) as request:
            controller = PythonController("192.168.56.101")
            with self.assertRaisesRegex(RuntimeError, "after 1 attempts"):
                controller.execute_python_command("pyautogui.click(10, 20)")
            controller.close()
        self.assertEqual(request.call_count, 1)

    def test_preflight_and_runtime_share_one_vmrun_discovery_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            explicit = Path(temp) / "vmrun-explicit"
            explicit.write_bytes(b"")
            on_path = str(Path(temp) / "vmrun-on-path")
            cases = (
                ({VMRUN_ENVIRONMENT_VARIABLE: str(explicit)}, None, explicit.resolve()),
                ({VMRUN_ENVIRONMENT_VARIABLE: str(Path(temp) / "missing")}, on_path, None),
                (NO_VMRUN_OVERRIDE, on_path, Path(on_path).resolve()),
                (NO_VMRUN_OVERRIDE, None, None),
            )
            for environment, which, expected in cases:
                with (
                    self.subTest(environment=environment, which=which),
                    mock.patch.dict(os.environ, environment),
                    mock.patch("shutil.which", return_value=which),
                ):
                    self.assertEqual(discover_vmrun(), expected)
                    self.assertEqual(_discover_vmrun(), expected)
                    if expected is None:
                        with self.assertRaisesRegex(VMwareRuntimeError, "VMRUN_PATH or PATH"):
                            VMwareProvider()
                    else:
                        self.assertEqual(VMwareProvider().executable, str(expected))

    def test_verified_package_resolves_inside_runtime_compat(self) -> None:
        root = activate_runtime_compat()
        import desktop_env.desktop_env as module  # type: ignore[import-not-found]

        self.assertEqual(
            Path(module.__file__).resolve(),
            root / "desktop_env" / "desktop_env.py",
        )


if __name__ == "__main__":
    unittest.main()
