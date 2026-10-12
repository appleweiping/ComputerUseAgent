"""Official same-process ScienceBoard task and VMware lifecycle adapter."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
import traceback
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

try:
    from .runtime_doctor import (
        RUNTIME_COMPAT_HASHES,
        activate_runtime_compat,
        source_doctor,
    )
    from .worker_contract import (
        EVALUATOR_SOURCES,
        GUEST_BUNDLE_NAME,
        GUEST_PYTHON,
        OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL,
        PINNED_COMMIT,
        VALIDATOR_SCHEMA,
        WORKER_SPEC_FIELDS,
        WORKER_SPEC_SCHEMA,
        WorkerProtocolError,
        _atomic_json,
        _safe_session_id,
        _within,
        canonical_sha256,
        sha256,
        utc_now,
        worker_source_sha256,
    )
except ImportError:  # Direct ``host_worker.py`` execution.
    from runtime_doctor import (  # type: ignore[no-redef, import-not-found]
        RUNTIME_COMPAT_HASHES,
        activate_runtime_compat,
        source_doctor,
    )
    from worker_contract import (  # type: ignore[no-redef, import-not-found]
        EVALUATOR_SOURCES,
        GUEST_BUNDLE_NAME,
        GUEST_PYTHON,
        OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL,
        PINNED_COMMIT,
        VALIDATOR_SCHEMA,
        WORKER_SPEC_FIELDS,
        WORKER_SPEC_SCHEMA,
        WorkerProtocolError,
        _atomic_json,
        _safe_session_id,
        _within,
        canonical_sha256,
        sha256,
        utc_now,
        worker_source_sha256,
    )


# Runs inside the guest under its system Python: decode the base64 transport
# file, verify the bundle bytes against the host-bound SHA-256, then install the
# zipapp atomically. A mismatch exits non-zero and nothing is installed.
GUEST_BUNDLE_INSTALLER = (
    "import base64, hashlib, os, sys\n"
    "source, target, expected = sys.argv[1:4]\n"
    "with open(source, 'rb') as handle:\n"
    "    data = base64.b64decode(handle.read().strip(), validate=True)\n"
    "if hashlib.sha256(data).hexdigest() != expected:\n"
    "    sys.exit('guest recorder bundle hash mismatch')\n"
    "temporary = target + '.tmp'\n"
    "with open(temporary, 'xb') as handle:\n"
    "    handle.write(data)\n"
    "    handle.flush()\n"
    "    os.fsync(handle.fileno())\n"
    "os.replace(temporary, target)\n"
    "os.unlink(source)\n"
)


class FakeOfficialRuntime:
    """No-VM/no-model runtime used only by unit tests for the phase protocol."""

    def __init__(self, *, event_count: int = 0, validator_passed: bool = False) -> None:
        self.calls: list[str] = []
        self.event_count = event_count
        self.validator_passed = validator_passed

    def _record(self, name: str, **extra: object) -> Mapping[str, object]:
        self.calls.append(name)
        return {"operation": name, **extra}

    def prepare(self) -> Mapping[str, object]:
        return self._record("prepare", official_task_instance="fake-same-instance")

    def initialize(self) -> Mapping[str, object]:
        return self._record("initialize", task_init=True, snapshot="sci_bench")

    def start_recorder(self) -> Mapping[str, object]:
        return self._record("start_recorder", guest_role="recorder_only")

    def run_actor(self) -> Mapping[str, object]:
        return self._record("run_actor", stop_type="TIMEOUT", stop_args=[])

    def screenshot(self) -> Mapping[str, object]:
        return self._record("screenshot", captured=True)

    def stop_recorder(self) -> Mapping[str, object]:
        return self._record("stop_recorder", event_count=self.event_count)

    def evaluate(self) -> Mapping[str, object]:
        return self._record("evaluate", passed=self.validator_passed)

    def finalize(self) -> Mapping[str, object]:
        return self._record("finalize", fetched=True)

    def teardown(self) -> None:
        self.calls.append("teardown")


class OfficialRuntime:
    """Pinned ScienceBoard adapter; imports upstream only in a formal worker."""

    def __init__(self, spec: Mapping[str, object]) -> None:
        if spec.get("schema_version") != WORKER_SPEC_SCHEMA:
            raise WorkerProtocolError("Worker spec schema mismatch")
        if set(spec) != WORKER_SPEC_FIELDS:
            missing = sorted(WORKER_SPEC_FIELDS - set(spec))
            unknown = sorted(set(spec) - WORKER_SPEC_FIELDS)
            raise WorkerProtocolError(
                f"Worker spec fields differ from v3; missing={missing}; unknown={unknown}"
            )
        if spec.get("openai_chat_completions_url") != OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL:
            raise WorkerProtocolError(
                "Formal ScienceBoard worker refuses a custom or fallback model endpoint"
            )
        self.spec = dict(spec)
        self.source_root = Path(str(spec["scienceboard_root"])).resolve()
        self.session_dir = Path(str(spec["session_dir"])).resolve()
        self.task_path = _within(
            Path(str(spec["task_path"])), self.session_dir, label="task_path"
        )
        self.official_dir = self.session_dir / "official"
        self.actor = str(spec["actor"])
        self.stop_type_name = "TIMEOUT"
        self.stop_args: list[str] = []
        self.actor_error_type: str | None = None
        self.manager_entered = False
        self.torn_down = False

    def prepare(self) -> Mapping[str, object]:
        source_hash = worker_source_sha256()
        if self.spec.get("worker_source_sha256") != source_hash:
            raise WorkerProtocolError("Host worker source hash differs from the immutable spec")
        task_binding = self.spec["task_binding"]
        if not isinstance(task_binding, Mapping):
            raise WorkerProtocolError("Worker task binding is invalid")
        bindings: list[Mapping[str, object]] = [task_binding]
        report = source_doctor(
            self.source_root,
            expected_commit=str(self.spec["scienceboard_commit"]),
            task_bindings=bindings,
            guest_bundle_root=Path(str(self.spec["guest_bundle_root"])),
        )
        if report["ready"] is not True:
            raise WorkerProtocolError("Pinned ScienceBoard source/guest bundle doctor failed")
        doctor_binding = report.get("binding")
        if (
            not isinstance(doctor_binding, Mapping)
            or doctor_binding.get("worker_source_sha256") != source_hash
        ):
            raise WorkerProtocolError("Host worker aggregate changed during preparation")
        if str(self.spec["scienceboard_commit"]) != PINNED_COMMIT:
            raise WorkerProtocolError("ScienceBoard commit is not the pinned formal commit")
        task_payload = json.loads(self.task_path.read_text(encoding="utf-8"))
        if task_payload.get("snapshot") != self.spec.get("snapshot"):
            raise WorkerProtocolError("Task snapshot differs from the worker spec")
        if int(task_payload.get("steps", -1)) != int(str(self.spec["action_limit"])):
            raise WorkerProtocolError("Task step limit differs from the formal action cap")

        activate_runtime_compat()
        sys.path.insert(0, str(self.source_root))
        try:
            from sci import (  # type: ignore[import-not-found]
                AIOAgent,
                AllInOne,
                GrounderAgent,
                Model,
                OBS,
                PlannerAgent,
                SeeAct,
                Tester,
            )
        except Exception:
            sys.path.remove(str(self.source_root))
            raise

        model_name = str(self.spec["model"])
        if model_name != "gpt-5.5":
            raise WorkerProtocolError("Formal worker refuses an unpinned model")
        api_key = os.environ.get("OPENAI_API_KEY")
        if self.actor != "human" and not api_key:
            raise WorkerProtocolError("OPENAI_API_KEY is absent")
        model = Model(
            model_style="openai",
            base_url=str(
                self.spec.get(
                    "openai_chat_completions_url",
                    OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL,
                )
            ),
            model_name=model_name,
            api_key=api_key,
            max_tokens=None,
            top_p=None,
            temperature=None,
            reason_effort=str(self.spec.get("reasoning_effort", "medium")),
        )
        if self.actor == "aio":
            community = AllInOne(AIOAgent(model))
        elif self.actor == "seeact":
            community = SeeAct(PlannerAgent(model), GrounderAgent(model))
        elif self.actor == "human":
            # A concrete Community is required, but manual runs never call it.
            community = AllInOne(AIOAgent(model))
        else:
            raise WorkerProtocolError(f"Unsupported actor: {self.actor}")

        self.official_dir.mkdir(parents=True, exist_ok=False)
        self.tester = Tester(
            tasks_path=str(self.task_path),
            logs_path=str(self.official_dir / "tester"),
            community=community,
            obs_types={OBS.screenshot},
            vm_path=str(self.spec["vm_path"]),
            headless=False,
            ignore=False,
            debug=False,
            optimize=False,
        )
        if len(self.tester.task_info) != 1:
            raise WorkerProtocolError("Official Tester did not load exactly one task")
        self.task = self.tester.task_info[0].task
        self.manager = self.task.manager
        self.manager.HETERO_TIMEOUT = int(str(self.spec["timeout_seconds"]))
        self.tester.log.trigger(str(self.official_dir), log_name="official")
        return {
            "scienceboard_commit": str(self.spec["scienceboard_commit"]),
            "task_class": f"{self.task.__class__.__module__}.{self.task.__class__.__name__}",
            "manager_class": (
                f"{self.manager.__class__.__module__}.{self.manager.__class__.__name__}"
            ),
            "community_class": community.__class__.__name__,
            "worker_source_sha256": source_hash,
            "runtime_compat": {
                "implementation": "research/scienceboard/runtime_compat/desktop_env",
                "provenance_sha256": RUNTIME_COMPAT_HASHES["provenance.json"],
                "upstream_reference": "desktop-env==0.1.22",
                "installed_desktop_env_distribution": False,
            },
        }

    def initialize(self) -> Mapping[str, object]:
        self.manager.__enter__()
        self.manager_entered = True
        if not self.task.init():
            raise RuntimeError("Official Task.init() failed after snapshot reset")
        return {
            "task_init": True,
            "snapshot": str(self.spec["snapshot"]),
            "reset_implementation": "sci.vm.vtask.VTask._init",
        }

    def _guest_path(self, suffix: str) -> str:
        root = PurePosixPath(str(self.spec["guest_data_root"]))
        if root.parts[:3] != ("/", "home", "user"):
            raise WorkerProtocolError("Guest data root must be under /home/user")
        return str(root / _safe_session_id(self.spec["session_id"]) / suffix)

    def _guest_execute(self, argv: list[str]) -> None:
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise WorkerProtocolError("Guest argv must be a non-empty string list")
        if self.task._execute(command=argv, shell=False) is not True:  # noqa: SLF001
            raise RuntimeError(f"Guest argv failed: {argv[0]}")

    def start_recorder(self) -> Mapping[str, object]:
        if self.actor == "human" and self.spec.get("self_consent_confirmed") is not True:
            raise WorkerProtocolError("Manual recording lacks explicit self-consent confirmation")
        bundle_root = Path(str(self.spec["guest_bundle_root"])).resolve()
        guest_session = str(PurePosixPath(self._guest_path("trace.jsonl")).parent)
        guest_bundle_dir = str(PurePosixPath(guest_session) / "recorder")
        guest_control = str(
            PurePosixPath(str(self.spec["guest_control_root"]))
            / str(self.spec["session_id"])
        )
        if not guest_control.startswith("/home/user/"):
            raise WorkerProtocolError("Guest control root must be under /home/user")
        configured_hashes = self.spec["guest_bundle_hashes"]
        if not isinstance(configured_hashes, Mapping) or set(configured_hashes) != {
            GUEST_BUNDLE_NAME
        }:
            raise WorkerProtocolError(
                f"Guest recorder deployment must be exactly one hash-bound {GUEST_BUNDLE_NAME}"
            )
        expected_hash = str(configured_hashes[GUEST_BUNDLE_NAME])
        source = _within(
            bundle_root / GUEST_BUNDLE_NAME, bundle_root, label="guest recorder bundle"
        )
        payload = source.read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected_hash:
            raise WorkerProtocolError("Guest recorder bundle hash mismatch")
        self._guest_execute(
            ["/usr/bin/mkdir", "-p", guest_session, guest_bundle_dir, guest_control]
        )

        # The guest file API transfers text, so the zipapp travels as base64 and
        # is decoded, hash-verified and installed by the guest's own Python.
        guest_bundle = str(PurePosixPath(guest_bundle_dir) / GUEST_BUNDLE_NAME)
        transport = guest_bundle + ".b64"
        encoded = base64.b64encode(payload).decode("ascii")
        if self.manager.write_file(transport, encoded) is not True:
            raise RuntimeError("Could not deploy the guest recorder bundle")
        observed = self.manager.read_file(transport)
        if observed is None or observed.strip() != encoded:
            raise RuntimeError("Guest recorder bundle read-back mismatch")
        self._guest_execute(
            [GUEST_PYTHON, "-c", GUEST_BUNDLE_INSTALLER, transport, guest_bundle, expected_hash]
        )

        argv = [
            GUEST_PYTHON,
            guest_bundle,
            "start-recorder",
            "--control-dir", guest_control,
            "--data-root", str(PurePosixPath(str(self.spec["guest_data_root"]))),
            "--output", self._guest_path("trace.jsonl"),
            "--actor", self.actor,
            "--session-id", str(self.spec["session_id"]),
            "--task-id", str(self.spec["task_id"]),
            "--protocol-version", str(self.spec["protocol_version"]),
            "--executor-version", str(self.spec["executor_version"]),
            "--guest-id", str(self.spec["guest_id"]),
            "--guest-os-id", str(self.spec["guest_os_id"]),
            "--guest-os-version", str(self.spec["guest_os_version"]),
            "--screen-width", str(self.spec["screen_width"]),
            "--screen-height", str(self.spec["screen_height"]),
            "--scienceboard-commit", str(self.spec["scienceboard_commit"]),
            "--duration-seconds", str(self.spec["timeout_seconds"]),
        ]
        if self.actor == "human":
            argv.append("--self-consent-confirmed")
        self._guest_execute(argv)
        self.guest_control = guest_control
        self.guest_bundle = guest_bundle
        return {
            "deployment": {
                "bundle": GUEST_BUNDLE_NAME,
                "sha256": expected_hash,
                "size_bytes": len(payload),
                "transport": "base64",
                "read_back_verified": True,
                "guest_hash_verified": True,
            },
            "capture": {"adapter": "x11_vm", "trace": self._guest_path("trace.jsonl")},
        }

    def _manual_stop(self) -> tuple[str, list[str]]:
        signal_path = _within(
            Path(str(self.spec["manual_stop_file"])),
            self.session_dir,
            label="manual stop file",
        )
        deadline = time.monotonic() + float(str(self.spec["timeout_seconds"]))
        while time.monotonic() < deadline:
            if signal_path.is_file():
                payload = json.loads(signal_path.read_text(encoding="utf-8"))
                if payload.get("session_id") != self.spec["session_id"]:
                    raise WorkerProtocolError(
                        "Manual stop signal is bound to another session"
                    )
                stop_type = str(payload.get("stop_type", "TIMEOUT"))
                stop_args = payload.get("stop_args", [])
                if stop_type not in {"DONE", "FAIL", "TIMEOUT", "ANS"}:
                    raise WorkerProtocolError(
                        "Manual stop type is not an official Primitive"
                    )
                if not isinstance(stop_args, list) or not all(
                    isinstance(value, str)
                    and len(value) <= 256
                    and "\n" not in value
                    for value in stop_args
                ):
                    raise WorkerProtocolError("Manual stop args are invalid")
                return stop_type, list(stop_args)
            time.sleep(0.1)
        return "TIMEOUT", []

    def run_actor(self) -> Mapping[str, object]:
        try:
            if self.actor == "human":
                self.stop_type_name, self.stop_args = self._manual_stop()
            else:
                stop_type, stop_args = self.task.predict()
                self.stop_type_name = stop_type.__name__
                self.stop_args = list(stop_args)
        except Exception as error:
            # Provider/model failure is retained, never replaced by a script.
            self.actor_error_type = type(error).__name__
            self.stop_type_name, self.stop_args = "TIMEOUT", []
            self.tester.log.error(
                "Actor execution failed.\n" + traceback.format_exc()
            )
        return {
            "actor": self.actor,
            "stop_type": self.stop_type_name,
            "stop_args_sha256": canonical_sha256({"stop_args": self.stop_args}),
            "actor_error_type": self.actor_error_type,
        }

    def screenshot(self) -> Mapping[str, object]:
        image = self.manager.screenshot()
        if image is None:
            raise RuntimeError("Official manager returned no final screenshot")
        expected = (
            int(str(self.spec["screen_width"])),
            int(str(self.spec["screen_height"])),
        )
        if tuple(image.size) != expected:
            raise RuntimeError(
                f"Final screenshot size {image.size} differs from {expected}"
            )
        output = self.session_dir / "final_state.png"
        if output.exists():
            raise FileExistsError("Refusing to overwrite final_state.png")
        image.save(output, format="PNG")
        return {
            "path": "final_state.png",
            "sha256": sha256(output),
            "dimensions": list(expected),
        }

    def stop_recorder(self) -> Mapping[str, object]:
        self._guest_execute(
            [
                GUEST_PYTHON,
                self.guest_bundle,
                "stop-recorder",
                "--control-dir", self.guest_control,
                "--session-id", str(self.spec["session_id"]),
            ]
        )
        return {"cooperative_stop": True, "post_stop_capture_allowed": False}

    def evaluate(self) -> Mapping[str, object]:
        from sci import Primitive

        stop_type = getattr(Primitive, self.stop_type_name)
        passed = bool(self.task.eval(stop_type, self.stop_args))
        result_path = self.official_dir / "result.out"
        if (
            not result_path.is_file()
            or result_path.read_text(encoding="utf-8") not in {"0", "1"}
        ):
            raise RuntimeError("Official Log.result_handler did not produce result.out")
        if (result_path.read_text(encoding="utf-8") == "1") != passed:
            raise RuntimeError("Official result.out disagrees with Task.eval return value")

        trajectory_path = self.official_dir / "traj.jsonl"
        trajectory_origin = "ScienceBoard Log.save"
        if not trajectory_path.exists():
            trajectory_path.touch(exist_ok=False)
            trajectory_origin = (
                "ScienceBoard Log.save produced no steps; explicit empty trajectory"
            )

        task_payload = json.loads(self.task_path.read_text(encoding="utf-8"))
        application = str(task_payload["type"])
        evaluator_relative, evaluator_hash = EVALUATOR_SOURCES[application]
        observed_evaluator_hash = sha256(self.source_root / evaluator_relative)
        if observed_evaluator_hash != evaluator_hash:
            raise WorkerProtocolError("Evaluator source changed after worker preparation")
        normalized = {
            "schema_version": VALIDATOR_SCHEMA,
            "session_id": str(self.spec["session_id"]),
            "task_id": str(self.spec["task_id"]),
            "passed": passed,
            "evaluated_at": utc_now(),
            "stop": {"type": self.stop_type_name, "args": self.stop_args},
            "evaluator": {
                "implementation": "ScienceBoard official Task.eval",
                "scienceboard_commit": str(self.spec["scienceboard_commit"]),
                "task_canonical_sha256": str(self.spec["task_canonical_sha256"]),
                "module": evaluator_relative,
                "class": (
                    f"{self.task.__class__.__module__}.{self.task.__class__.__name__}"
                ),
                "function": "eval",
                "source_sha256": evaluator_hash,
            },
            "official_evidence": {
                "result": {
                    "path": "official/result.out",
                    "sha256": sha256(result_path),
                    "value": result_path.read_text(encoding="utf-8"),
                },
                "trajectory": {
                    "path": "official/traj.jsonl",
                    "sha256": sha256(trajectory_path),
                    "size_bytes": trajectory_path.stat().st_size,
                    "origin": trajectory_origin,
                },
            },
        }
        _atomic_json(
            self.session_dir / "validator_result.json", normalized, replace=False
        )
        return {
            "passed": passed,
            "result_sha256": sha256(result_path),
            "trajectory_sha256": sha256(trajectory_path),
            "evaluator_source_sha256": evaluator_hash,
        }

    def finalize(self) -> Mapping[str, object]:
        trace_text = self.manager.read_file(self._guest_path("trace.jsonl"))
        if trace_text is None:
            raise RuntimeError("Could not fetch the finalized guest X11 trace")
        trace_path = self.session_dir / "trace.jsonl"
        temporary = trace_path.with_suffix(".jsonl.fetching")
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(trace_text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, trace_path)
        self.teardown()
        return {
            "trace_sha256": sha256(trace_path),
            "trace_size_bytes": trace_path.stat().st_size,
        }

    def teardown(self) -> None:
        if self.torn_down:
            return
        self.torn_down = True
        try:
            if hasattr(self, "tester"):
                self.tester.log.callback()
        finally:
            if self.manager_entered:
                self.manager.__exit__(None, None, None)
                self.manager_entered = False
