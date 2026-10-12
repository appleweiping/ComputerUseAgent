from __future__ import annotations

import ast
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from computer_use_agent.capture.x11.bundle import build as build_guest_bundle
from research.scienceboard import host_worker
from research.scienceboard.host_worker import (
    DESKTOP_ENV_INIT_ARGUMENTS,
    HOST_DEPENDENCY_VERSIONS,
    HOST_WORKER_SOURCE_FILES,
    OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL,
    PHASE_RESPONSE_MARGIN_SECONDS,
    RUNTIME_COMPAT_HASHES,
    TRANSITIONS,
    WORKER_SPEC_FIELDS,
    FakeOfficialRuntime,
    OfficialRuntime,
    WorkerProtocolError,
    WorkerStateMachine,
    call_phase,
    inspect_source_signatures,
    phase_client_timeout,
    runtime_compat_doctor,
    serve,
    signal_manual_stop,
    source_doctor,
    validate_state,
    worker_source_manifest,
    worker_source_sha256,
)
from research.scienceboard.runtime_doctor import guest_bundle_doctor


SCIENCEBOARD_ROOT = Path(__file__).resolve().parents[1]


class FakeManager:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def reset(self) -> None:
        self.calls.append("reset:sci_bench")


class FakeOfficialTask:
    def __init__(self, manager: FakeManager) -> None:
        self.manager = manager
        self.calls: list[str] = []
        self.stop: tuple[str, list[str]] = ("TIMEOUT", [])

    def init(self) -> bool:
        self.calls.append("init")
        self.manager.reset()
        return True

    def predict(self) -> tuple[str, list[str]]:
        self.calls.append("predict")
        self.stop = ("DONE", [])
        return self.stop

    def eval(self, stop_type: str, stop_args: list[str]) -> bool:
        self.calls.append("eval")
        return (stop_type, stop_args) == self.stop


class FakeLifecycle:
    """Fake manager/evaluator proves one object spans init through eval."""

    def __init__(self) -> None:
        self.manager = FakeManager()
        self.task = FakeOfficialTask(self.manager)
        self.task_identity = id(self.task)
        self.calls: list[str] = []

    def prepare(self) -> dict[str, object]:
        self.calls.append("prepare")
        return {"task_identity": self.task_identity}

    def initialize(self) -> dict[str, object]:
        self.calls.append("initialize")
        return {"initialized": self.task.init(), "task_identity": id(self.task)}

    def start_recorder(self) -> dict[str, object]:
        self.calls.append("start_recorder")
        return {"guest_role": "recorder_only", "task_identity": id(self.task)}

    def run_actor(self) -> dict[str, object]:
        self.calls.append("run_actor")
        stop_type, stop_args = self.task.predict()
        return {"stop_type": stop_type, "stop_args": stop_args, "task_identity": id(self.task)}

    def screenshot(self) -> dict[str, object]:
        self.calls.append("screenshot")
        return {"captured": True, "task_identity": id(self.task)}

    def stop_recorder(self) -> dict[str, object]:
        self.calls.append("stop_recorder")
        return {"event_count": 0, "task_identity": id(self.task)}

    def evaluate(self) -> dict[str, object]:
        self.calls.append("evaluate")
        passed = self.task.eval(*self.task.stop)
        return {"passed": passed, "task_identity": id(self.task)}

    def finalize(self) -> dict[str, object]:
        self.calls.append("finalize")
        return {"fetched": True, "task_identity": id(self.task)}


class HostWorkerContractTests(unittest.TestCase):
    def _machine(self, runtime: FakeLifecycle) -> WorkerStateMachine:
        return WorkerStateMachine(
            session_id="sb-kalgebra-a01-aio-r01",
            nonce="n" * 48,
            endpoint={"host": "127.0.0.1", "port": 12345},
            pid=123,
            prepared_evidence=runtime.prepare(),
        )

    def _advance(
        self,
        machine: WorkerStateMachine,
        runtime: FakeLifecycle,
        command: str,
    ) -> dict[str, object]:
        action = getattr(runtime, command)
        return dict(
            machine.transition(
                nonce="n" * 48,
                request_id=f"session:{command}",
                command=command,
                expected_state_hash=str(machine.state["state_hash"]),
                action=action,
            )
        )

    def test_exact_order_keeps_one_task_for_init_predict_and_eval(self) -> None:
        runtime = FakeLifecycle()
        machine = self._machine(runtime)
        evidence = []
        for command, _ in TRANSITIONS.values():
            evidence.append(self._advance(machine, runtime, command))
            validate_state(machine.state)
        self.assertEqual(machine.state["phase"], "finalized")
        self.assertEqual(runtime.manager.calls, ["reset:sci_bench"])
        self.assertEqual(runtime.task.calls, ["init", "predict", "eval"])
        self.assertTrue(evidence[-2]["passed"])
        self.assertEqual(
            {item["task_identity"] for item in evidence}, {runtime.task_identity}
        )
        self.assertLess(
            runtime.calls.index("initialize"), runtime.calls.index("start_recorder")
        )

    def test_wrong_nonce_replay_and_out_of_order_requests_fail_closed(self) -> None:
        runtime = FakeLifecycle()
        machine = self._machine(runtime)
        with self.assertRaisesRegex(WorkerProtocolError, "nonce"):
            machine.transition(
                nonce="x" * 48,
                request_id="bad-nonce",
                command="initialize",
                expected_state_hash=str(machine.state["state_hash"]),
                action=runtime.initialize,
            )
        with self.assertRaisesRegex(WorkerProtocolError, "invalid from phase"):
            machine.transition(
                nonce="n" * 48,
                request_id="out-of-order",
                command="start_recorder",
                expected_state_hash=str(machine.state["state_hash"]),
                action=runtime.start_recorder,
            )
        previous_hash = str(machine.state["state_hash"])
        machine.transition(
            nonce="n" * 48,
            request_id="once",
            command="initialize",
            expected_state_hash=previous_hash,
            action=runtime.initialize,
        )
        with self.assertRaisesRegex(WorkerProtocolError, "Replayed"):
            machine.transition(
                nonce="n" * 48,
                request_id="once",
                command="start_recorder",
                expected_state_hash=str(machine.state["state_hash"]),
                action=runtime.start_recorder,
            )

    def test_state_tampering_is_detected(self) -> None:
        runtime = FakeLifecycle()
        machine = self._machine(runtime)
        machine.state["phase"] = "evaluated"
        with self.assertRaisesRegex(WorkerProtocolError, "hash mismatch"):
            validate_state(machine.state)

    def test_ast_signature_probe_reports_exact_arguments_and_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "contract.py"
            path.write_text(
                "class Task:\n"
                "    def init(self):\n"
                "        return True\n"
                "    def eval(self, stop_type, stop_args):\n"
                "        return False\n",
                encoding="utf-8",
            )
            report = inspect_source_signatures(
                path,
                {
                    "Task.init": ("self",),
                    "Task.eval": ("self", "stop_type", "stop_args"),
                },
            )
            self.assertTrue(report["passed"])
            self.assertEqual(report["symbols"]["Task.eval"]["line"], 4)
            mismatch = inspect_source_signatures(
                path, {"Task.eval": ("self", "wrong")}
            )
            self.assertFalse(mismatch["passed"])

    def test_host_dependency_versions_and_runtime_compat_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def version(name: str) -> str:
                return str(HOST_DEPENDENCY_VERSIONS[name])

            with mock.patch(
                "research.scienceboard.runtime_doctor.importlib_metadata.version",
                side_effect=version,
            ), mock.patch(
                "research.scienceboard.runtime_doctor._git",
                side_effect=RuntimeError("fixture stops after dependency check"),
            ):
                report = source_doctor(
                    root,
                    expected_commit="c8d5010bdba32afc5dfb16472873bed148c39a45",
                    task_bindings=[],
                    guest_bundle_root=root,
                )
            dependency = next(
                item for item in report["checks"]
                if item["name"] == "host_dependency_contract"
            )
            self.assertTrue(dependency["passed"])
            self.assertEqual(
                report["binding"]["worker_source_sha256"],
                worker_source_sha256(),
            )
            self.assertEqual(
                set(report["binding"]["worker_source_files"]),
                set(HOST_WORKER_SOURCE_FILES),
            )

            with mock.patch(
                "research.scienceboard.runtime_doctor.importlib_metadata.version",
                return_value="0.1.22",
            ), mock.patch(
                "research.scienceboard.runtime_doctor._git",
                side_effect=RuntimeError("fixture stops after dependency check"),
            ):
                drifted = source_doctor(
                    root,
                    expected_commit="c8d5010bdba32afc5dfb16472873bed148c39a45",
                    task_bindings=[],
                    guest_bundle_root=root,
                )
            drifted_dependency = next(
                item for item in drifted["checks"]
                if item["name"] == "host_dependency_contract"
            )
            self.assertFalse(drifted_dependency["passed"])

    def test_runtime_compat_is_source_hash_and_signature_pinned(self) -> None:
        report = runtime_compat_doctor()
        self.assertTrue(report["passed"])
        self.assertTrue(report["provenance_contract"])
        self.assertNotIn("root", report)
        desktop = report["files"]["desktop_env/desktop_env.py"]
        self.assertEqual(
            desktop["signatures"]["symbols"]["DesktopEnv.__init__"][
                "expected_arguments"
            ],
            list(DESKTOP_ENV_INIT_ARGUMENTS),
        )
        with mock.patch.dict(
            RUNTIME_COMPAT_HASHES,
            {"desktop_env/desktop_env.py": "0" * 64},
        ):
            self.assertFalse(runtime_compat_doctor()["passed"])

    def test_manual_stop_requires_confirmation_nonce_and_active_human_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = FakeLifecycle()
            machine = self._machine(runtime)
            self._advance(machine, runtime, "initialize")
            self._advance(machine, runtime, "start_recorder")
            spec: dict[str, object] = {
                field: None for field in WORKER_SPEC_FIELDS
            }
            spec.update(
                {
                    "schema_version": "scienceboard-host-worker-spec-v3",
                    "session_id": "sb-kalgebra-a01-aio-r01",
                    "actor": "human",
                    "session_dir": str(root),
                    "manual_stop_file": str(root / ".manual-stop.json"),
                }
            )
            spec_path, state_path, nonce_path = (
                root / "worker_spec.json",
                root / "worker_state.json",
                root / ".worker-nonce",
            )
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            state_path.write_text(
                json.dumps(machine.state), encoding="utf-8"
            )
            nonce_path.write_text("n" * 48, encoding="utf-8")
            with self.assertRaisesRegex(WorkerProtocolError, "confirm"):
                signal_manual_stop(
                    spec_path,
                    state_path,
                    nonce_path,
                    stop_type="DONE",
                    stop_args=[],
                    confirmed=False,
                )
            result = signal_manual_stop(
                spec_path,
                state_path,
                nonce_path,
                stop_type="DONE",
                stop_args=[],
                confirmed=True,
            )
            self.assertEqual(result["stop_type"], "DONE")
            payload = json.loads(
                (root / ".manual-stop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["session_id"], spec["session_id"])

    def test_worker_split_preserves_cli_and_binds_every_executable_module(self) -> None:
        manifest = worker_source_manifest()
        self.assertEqual(set(manifest), set(HOST_WORKER_SOURCE_FILES))
        self.assertTrue(all(len(value) == 64 for value in manifest.values()))

        entrypoint = SCIENCEBOARD_ROOT / "host_worker.py"
        tree = ast.parse(entrypoint.read_text(encoding="utf-8"))
        locally_defined_classes = {
            node.name for node in tree.body if isinstance(node, ast.ClassDef)
        }
        self.assertNotIn("WorkerStateMachine", locally_defined_classes)
        self.assertNotIn("OfficialRuntime", locally_defined_classes)
        self.assertLess(len(entrypoint.read_text(encoding="utf-8").splitlines()), 800)

        completed = subprocess.run(
            [sys.executable, str(entrypoint), "--help"],
            cwd=SCIENCEBOARD_ROOT.parents[1],
            text=True,
            encoding="utf-8",
            capture_output=True,
            shell=False,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for command in ("doctor", "start", "phase", "signal-manual-stop"):
            self.assertIn(command, completed.stdout)

        with tempfile.TemporaryDirectory() as temp:
            copy_root = Path(temp)
            for name in HOST_WORKER_SOURCE_FILES:
                shutil.copyfile(SCIENCEBOARD_ROOT / name, copy_root / name)
            self.assertEqual(worker_source_sha256(copy_root), worker_source_sha256())
            runtime_path = copy_root / "official_runtime.py"
            runtime_path.write_text(
                runtime_path.read_text(encoding="utf-8") + "\n# tampered\n",
                encoding="utf-8",
            )
            self.assertNotEqual(worker_source_sha256(copy_root), worker_source_sha256())

    def test_official_runtime_rejects_stale_aggregate_before_any_external_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            task_path = root / "task.json"
            task_path.write_text("{}\n", encoding="utf-8")
            spec: dict[str, object] = {
                field: None for field in WORKER_SPEC_FIELDS
            }
            spec.update(
                {
                    "schema_version": "scienceboard-host-worker-spec-v3",
                    "session_id": "sb-kalgebra-a01-aio-r01",
                    "session_dir": str(root),
                    "task_path": str(task_path),
                    "scienceboard_root": str(root),
                    "actor": "aio",
                    "openai_chat_completions_url": (
                        OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL
                    ),
                    "worker_source_sha256": "0" * 64,
                }
            )
            runtime = OfficialRuntime(spec)
            with self.assertRaisesRegex(WorkerProtocolError, "source hash"):
                runtime.prepare()


class GuestBundleDoctorTests(unittest.TestCase):
    def test_built_bundle_passes_and_tampering_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            missing = guest_bundle_doctor(root)
            self.assertFalse(missing["passed"])
            self.assertFalse(missing["present"])
            built = build_guest_bundle(root)
            report = guest_bundle_doctor(root)
            self.assertTrue(report["passed"], report)
            self.assertEqual(report["sha256"], built.sha256)
            self.assertEqual(report["unexpected_members"], [])
            with zipfile.ZipFile(built.path, "a") as archive:
                archive.writestr("computer_use_agent/detector/model.py", "x = 1\n")
            tampered = guest_bundle_doctor(root)
            self.assertFalse(tampered["passed"])
            self.assertEqual(
                tampered["unexpected_members"], ["computer_use_agent/detector/model.py"]
            )
            built.path.write_bytes(b"not a zip")
            self.assertFalse(guest_bundle_doctor(root)["passed"])

    def test_source_doctor_reports_the_bundle_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_guest_bundle(root)
            with mock.patch(
                "research.scienceboard.runtime_doctor._git",
                side_effect=RuntimeError("fixture has no checkout"),
            ):
                report = source_doctor(
                    root,
                    expected_commit="c8d5010bdba32afc5dfb16472873bed148c39a45",
                    task_bindings=[],
                    guest_bundle_root=root,
                )
        bundle = next(row for row in report["checks"] if row["name"] == "guest_recorder_bundle")
        self.assertTrue(bundle["passed"], bundle)
        self.assertFalse(report["ready"])


class WorkerServiceTimeoutTests(unittest.TestCase):
    def test_phase_client_waits_for_the_phase_budget_plus_margin(self) -> None:
        self.assertEqual(phase_client_timeout(480), 480 + PHASE_RESPONSE_MARGIN_SECONDS)
        with self.assertRaises(WorkerProtocolError):
            phase_client_timeout(0)

    def test_silent_client_cannot_wedge_the_worker_and_phases_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec_path, state_path, nonce_path = (
                root / "worker_spec.json",
                root / "worker_state.json",
                root / ".worker-nonce",
            )
            spec_path.write_text(
                json.dumps({"session_id": "sb-kalgebra-a01-aio-r01"}), encoding="utf-8"
            )
            nonce_path.write_text("n" * 48 + "\n", encoding="utf-8")
            runtime = FakeOfficialRuntime(event_count=0)
            outcome: dict[str, object] = {}

            def run() -> None:
                outcome["code"] = serve(spec_path, state_path, nonce_path)

            with (
                mock.patch.object(host_worker, "OfficialRuntime", return_value=runtime),
                mock.patch.object(host_worker, "WORKER_IO_TIMEOUT_SECONDS", 0.5),
            ):
                thread = threading.Thread(target=run, daemon=True)
                thread.start()
                deadline = time.monotonic() + 20
                while not state_path.is_file() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(state_path.is_file())
                endpoint = json.loads(state_path.read_text(encoding="utf-8"))["endpoint"]
                with socket.create_connection((endpoint["host"], endpoint["port"]), timeout=5):
                    time.sleep(1.0)  # connected but silent past the worker's I/O timeout
                for command, _ in TRANSITIONS.values():
                    response = call_phase(
                        state_path,
                        nonce_path,
                        command=command,
                        request_id=f"sb-kalgebra-a01-aio-r01:{command}",
                        timeout=5,
                    )
                    self.assertTrue(response["ok"])
                thread.join(timeout=20)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcome.get("code"), 0)
            final = json.loads(state_path.read_text(encoding="utf-8"))
            validate_state(final)
            self.assertEqual(final["phase"], "finalized")
            self.assertEqual(runtime.calls[-1], "teardown")


if __name__ == "__main__":
    unittest.main()
