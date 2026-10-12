"""The X11 guest zipapp is deterministic, closed and runs on the standard library."""

from __future__ import annotations

import ast
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from computer_use_agent.capture.x11 import bundle
from computer_use_agent.capture.x11.__main__ import COMMANDS
from computer_use_agent.capture.x11.__main__ import main as dispatch

PACKAGE_ROOT = Path(bundle.__file__).resolve().parents[2]


def _expected_members() -> list[str]:
    members = [
        "__main__.py",
        "computer_use_agent/__init__.py",
        "computer_use_agent/capture/__init__.py",
    ]
    for relative in ("trace", "capture/x11"):
        members.extend(
            f"computer_use_agent/{relative}/{path.name}"
            for path in (PACKAGE_ROOT / relative).glob("*.py")
        )
    return sorted(members)


def _run(*argv: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    # -I: isolated (no user site, no PYTHON* variables); -S: no site-packages,
    # so only the standard library and the archive itself are importable.
    return subprocess.run(
        [sys.executable, "-B", "-I", "-S", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )


class GuestBundleBuildTests(unittest.TestCase):
    _temp: tempfile.TemporaryDirectory[str]
    root: Path
    first: bundle.BuiltBundle
    second: bundle.BuiltBundle

    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temp.name)
        cls.first = bundle.build(cls.root / "first" / bundle.BUNDLE_NAME)
        (cls.root / "second").mkdir()
        cls.second = bundle.build(cls.root / "second")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def test_two_builds_are_byte_identical(self) -> None:
        self.assertEqual(self.first.sha256, self.second.sha256)
        self.assertEqual(self.first.path.read_bytes(), self.second.path.read_bytes())
        self.assertEqual(self.second.path.name, bundle.BUNDLE_NAME)
        self.assertEqual(
            hashlib.sha256(self.first.path.read_bytes()).hexdigest(), self.first.sha256
        )
        self.assertEqual(self.first.size_bytes, self.first.path.stat().st_size)
        self.assertEqual(bundle.bundle_bytes(), self.first.path.read_bytes())

    def test_members_are_exactly_the_guest_closure(self) -> None:
        with zipfile.ZipFile(self.first.path) as archive:
            names = sorted(archive.namelist())
            self.assertIsNone(archive.testzip())
            infos = archive.infolist()
        self.assertEqual(names, _expected_members())
        self.assertEqual(list(self.first.members), names)
        for forbidden in ("detector", "viewer", "contracts", "features", "study", "schemas"):
            self.assertFalse(any(f"/{forbidden}/" in name for name in names), forbidden)
        for info in infos:
            self.assertEqual(info.date_time, bundle.ZIP_TIMESTAMP)
            self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
            self.assertEqual(info.create_system, bundle.ZIP_CREATOR_UNIX)
            self.assertEqual(info.external_attr >> 16, bundle.FILE_MODE)

    def test_archive_starts_with_interpreter_line_and_has_lf_sources(self) -> None:
        payload = self.first.path.read_bytes()
        self.assertTrue(payload.startswith(bundle.INTERPRETER_LINE))
        with zipfile.ZipFile(self.first.path) as archive:
            for name in archive.namelist():
                self.assertNotIn(b"\r\n", archive.read(name), name)

    def test_crlf_checkout_produces_the_same_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / "computer_use_agent"
            shutil.copytree(PACKAGE_ROOT, copy, ignore=shutil.ignore_patterns("__pycache__"))
            target = copy / "trace" / "privacy.py"
            target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))
            self.assertEqual(bundle.bundle_bytes(copy), self.first.path.read_bytes())

    def test_output_must_be_a_pyz_file(self) -> None:
        with self.assertRaises(ValueError):
            bundle.build(self.root / "bundle.zip")

    def test_cli_builds_and_reports_identity(self) -> None:
        output = self.root / "cli"
        output.mkdir()
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "computer_use_agent.capture.x11.bundle",
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["sha256"], self.first.sha256)
        self.assertEqual(report["name"], bundle.BUNDLE_NAME)


class GuestBundleRuntimeTests(unittest.TestCase):
    """The archive runs with ``python3 file.pyz`` and needs no third-party package."""

    _temp: tempfile.TemporaryDirectory[str]
    archive: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = tempfile.TemporaryDirectory()
        cls.archive = bundle.build(Path(cls._temp.name)).path

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def test_help_lists_every_command(self) -> None:
        completed = _run(str(self.archive), "--help")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for command in COMMANDS:
            self.assertIn(command, completed.stdout)
        self.assertEqual(_run(str(self.archive)).returncode, 2)
        self.assertEqual(_run(str(self.archive), "no-such-command").returncode, 2)

    def test_calibration_plan_and_recorder_doctor_run_from_the_archive(self) -> None:
        plan = _run(str(self.archive), "calibrate", "--dry-run")
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertEqual(
            json.loads(plan.stdout)["schema_version"], "scienceboard-x11-calibration-plan-v1"
        )
        doctor = _run(str(self.archive), "record", "--doctor")
        report = json.loads(doctor.stdout)
        self.assertIn(doctor.returncode, {0, 2})
        self.assertEqual(doctor.returncode == 0, report["ready"])
        self.assertIn("python_xlib", {check["name"] for check in report["checks"]})

    def test_guest_modules_import_from_the_archive_without_site_packages(self) -> None:
        probe = (
            "import json, sys\n"
            f"sys.path.insert(0, {str(self.archive)!r})\n"
            "import computer_use_agent\n"
            "from computer_use_agent.capture.x11 import guest_controller, recorder\n"
            "import computer_use_agent.trace.io\n"
            "print(json.dumps({\n"
            "    'package': computer_use_agent.__file__,\n"
            "    'version': computer_use_agent.__version__,\n"
            "    'command': guest_controller.recorder_command(),\n"
            "    'loaded': sorted(m for m in sys.modules\n"
            "                     if m.split('.')[0] in {'jsonschema', 'research', 'Xlib'}),\n"
            "}))\n"
        )
        completed = _run("-c", probe)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["package"].startswith(str(self.archive)))
        self.assertEqual(report["loaded"], [])
        self.assertTrue(report["version"])
        self.assertEqual(report["command"][1:], [str(self.archive.resolve()), "record"])


def _guest_python() -> str | None:
    """A Python 3.10 interpreter to smoke-test the bundle with, if one is available.

    ``CUA_GUEST_PYTHON`` names it explicitly; otherwise ``python3.10`` on PATH.
    """

    configured = os.environ.get("CUA_GUEST_PYTHON", "").strip()
    return configured or shutil.which("python3.10")


class GuestPythonCompatibilityTests(unittest.TestCase):
    """The pinned guest is Ubuntu 22.04, whose system ``python3`` is 3.10."""

    # Standard-library names added after Python 3.10. The bundle entry point
    # also backfills ``datetime.UTC`` as a safety net, but bundled modules must
    # not rely on it.
    NEWER_MODULES = frozenset({"tomllib", "wsgiref.types"})
    NEWER_NAMES = frozenset(
        {
            ("datetime", "UTC"),
            ("typing", "Self"),
            ("typing", "LiteralString"),
            ("typing", "Never"),
            ("typing", "assert_never"),
            ("typing", "reveal_type"),
            ("enum", "StrEnum"),
            ("hashlib", "file_digest"),
            ("asyncio", "TaskGroup"),
            ("asyncio", "timeout"),
            ("contextlib", "chdir"),
        }
    )
    NEWER_BUILTINS = frozenset({"ExceptionGroup", "BaseExceptionGroup"})

    def test_bundle_sources_parse_and_avoid_newer_stdlib_names(self) -> None:
        for name, path in bundle.bundle_sources():
            with self.subTest(member=name):
                tree = ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 10))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            self.assertNotIn(alias.name, self.NEWER_MODULES)
                    elif isinstance(node, ast.ImportFrom):
                        self.assertNotIn(node.module, self.NEWER_MODULES)
                        for alias in node.names:
                            self.assertNotIn((node.module, alias.name), self.NEWER_NAMES)
                    elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                        self.assertNotIn((node.value.id, node.attr), self.NEWER_NAMES)
                    elif isinstance(node, ast.Name):
                        self.assertNotIn(node.id, self.NEWER_BUILTINS)
                    self.assertNotIsInstance(node, ast.TryStar)

    def test_entry_point_backfills_datetime_utc_for_python_310(self) -> None:
        namespace: dict[str, object] = {}
        source = bundle.ROOT_MAIN.split("from computer_use_agent")[0]
        with mock.patch.object(datetime, "UTC", create=False):
            delattr(datetime, "UTC")
            exec(compile(source, "__main__.py", "exec"), namespace)
            self.assertIs(datetime.UTC, datetime.timezone.utc)  # noqa: UP017

    def test_bundle_runs_on_a_python_310_interpreter(self) -> None:
        interpreter = _guest_python()
        if not interpreter:
            self.skipTest(
                "no Python 3.10 interpreter (set CUA_GUEST_PYTHON or put python3.10 on PATH)"
            )
        version = subprocess.run(
            [interpreter, "-c", "import sys; print(sys.version_info[:2])"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout.strip()
        self.assertEqual(version, "(3, 10)")
        with tempfile.TemporaryDirectory() as temp:
            archive = bundle.build(Path(temp)).path
            trace = Path(temp) / "trace.jsonl"

            def run(*argv: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [interpreter, "-B", "-I", "-S", *argv],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=120,
                    check=False,
                )

            self.assertEqual(run(str(archive), "--help").returncode, 0)
            plan = run(str(archive), "calibrate", "--dry-run")
            self.assertEqual(plan.returncode, 0, plan.stderr)
            doctor = run(str(archive), "record", "--doctor")
            self.assertIn("checks", json.loads(doctor.stdout), doctor.stderr)
            # No datetime.UTC backfill here: the bundled modules must run on
            # Python 3.10 without the entry point's safety net.
            round_trip = (
                "import argparse, json, sys\n"
                f"sys.path.insert(0, {str(archive)!r})\n"
                "from computer_use_agent.capture.x11.recorder import build_session_metadata\n"
                "from computer_use_agent.trace.io import TraceWriter, load_trace\n"
                "args = argparse.Namespace(actor='xtest', session_id='s-1', participant_code='p',\n"
                "    pair_id=None, task_id='t-1', protocol_version='p-1', executor_version='x',\n"
                "    consent_confirmed=False, controller_confirmed=True, guest_id='g-1',\n"
                "    guest_os_id='ubuntu', guest_os_version='22.04', screen_width=1920,\n"
                "    screen_height=1080, scienceboard_commit='0' * 40)\n"
                f"path = {str(trace)!r}\n"
                "with TraceWriter(path, build_session_metadata(args)) as writer:\n"
                "    pass\n"
                "metadata, events, terminal = load_trace(path)\n"
                "print(json.dumps([metadata['schema_version'], len(events), terminal['status']]))\n"
            )
            completed = run("-c", round_trip)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout), ["cui-trace-v2", 0, "complete"])


class DispatcherTests(unittest.TestCase):
    def test_source_checkout_dispatch(self) -> None:
        self.assertEqual(dispatch(["--help"]), 0)
        self.assertEqual(dispatch([]), 2)
        self.assertEqual(dispatch(["unknown"]), 2)
        self.assertEqual(dispatch(["calibrate", "--dry-run"]), 0)


if __name__ == "__main__":
    unittest.main()
