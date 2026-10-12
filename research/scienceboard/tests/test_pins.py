"""Drift tests: every ScienceBoard pin has one source, and pinned bytes are LF-only."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from computer_use_agent.capture.x11 import bundle, guest_controller, recorder
from research.scienceboard import artifacts, orchestrate, preflight, runtime_doctor, vm_provenance
from research.scienceboard import worker_contract as contract

SCIENCEBOARD = Path(__file__).resolve().parents[1]
REPO = SCIENCEBOARD.parents[1]
CAPTURE = REPO / "src" / "computer_use_agent" / "capture"
DRIVE_LITERAL = re.compile(r"""["'][A-Za-z]:[\\/]""")


class PinDriftTests(unittest.TestCase):
    def test_commit_pin_has_one_value_everywhere(self) -> None:
        manifest = json.loads((SCIENCEBOARD / "tasks" / "manifest.json").read_text("utf-8"))
        values = {
            contract.PINNED_COMMIT,
            getattr(preflight, "PINNED_SCIENCEBOARD_COMMIT"),
            recorder.DEFAULT_SCIENCEBOARD_COMMIT,
            guest_controller.PINNED_COMMIT,
            manifest["upstream"]["commit"],
            manifest["upstream"]["source_ref"],
        }
        self.assertEqual(values, {contract.PINNED_COMMIT})

    def test_endpoint_vm_and_evaluator_pins_are_shared(self) -> None:
        self.assertIs(
            getattr(orchestrate, "OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL"),
            contract.OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL,
        )
        self.assertIs(
            getattr(preflight, "OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL"),
            contract.OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL,
        )
        for module in (vm_provenance, preflight):
            self.assertIs(getattr(module, "EXPECTED_VM_ZIP_SHA256"), contract.EXPECTED_VM_ZIP_SHA256)
        self.assertIs(getattr(runtime_doctor, "EVALUATOR_SOURCES"), contract.EVALUATOR_SOURCES)
        self.assertEqual(
            artifacts.EVALUATOR_SOURCE_HASHES,
            {
                application: {"module": module, "sha256": digest}
                for application, (module, digest) in contract.EVALUATOR_SOURCES.items()
            },
        )

    def test_guest_bundle_identity_and_paths(self) -> None:
        self.assertEqual(contract.GUEST_BUNDLE_NAME, bundle.BUNDLE_NAME)
        for member in runtime_doctor.REQUIRED_GUEST_BUNDLE_MEMBERS:
            self.assertIn(member, bundle.bundle_members())
            self.assertRegex(member, runtime_doctor.GUEST_BUNDLE_MEMBER_PATTERN)
        for member in bundle.bundle_members():
            self.assertRegex(member, runtime_doctor.GUEST_BUNDLE_MEMBER_PATTERN)
        for root in (contract.DEFAULT_GUEST_DATA_ROOT, contract.DEFAULT_GUEST_CONTROL_ROOT):
            self.assertTrue(root.startswith("/home/user/"), root)

    def test_task_selection_is_still_pending(self) -> None:
        manifest = json.loads((SCIENCEBOARD / "tasks" / "manifest.json").read_text("utf-8"))
        self.assertNotEqual(manifest["selection_status"], preflight.TASK_SELECTION_CONFIRMED_STATUS)


class PinnedBytesTests(unittest.TestCase):
    def test_hash_pinned_sources_are_lf_only(self) -> None:
        pinned = [SCIENCEBOARD / "runtime_compat" / name for name in runtime_doctor.RUNTIME_COMPAT_HASHES]
        pinned += [SCIENCEBOARD / name for name in contract.HOST_WORKER_SOURCE_FILES]
        pinned += sorted((CAPTURE / "x11").glob("*.py"))
        for path in pinned:
            with self.subTest(path=path.name):
                self.assertNotIn(b"\r", path.read_bytes())

    def test_runtime_compat_pins_match_the_checked_in_bytes(self) -> None:
        report = runtime_doctor.runtime_compat_doctor()
        self.assertTrue(report["passed"], report)
        self.assertEqual(
            report["implementation"], "research/scienceboard/runtime_compat/desktop_env"
        )

    def test_no_drive_letter_paths_in_scienceboard_or_capture(self) -> None:
        roots = (SCIENCEBOARD, CAPTURE)
        suffixes = {".py", ".json", ".md", ".txt", ".example"}
        offenders: list[str] = []
        for root in roots:
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix not in suffixes:
                    continue
                if "__pycache__" in path.parts or path == Path(__file__).resolve():
                    continue
                text = path.read_text(encoding="utf-8")
                offenders.extend(
                    f"{path.relative_to(REPO).as_posix()}: {match.group(0)}"
                    for match in DRIVE_LITERAL.finditer(text)
                )
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
