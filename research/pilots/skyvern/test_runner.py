import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from research.pilots.skyvern import runner

ARCHIVED_RUNS = runner.EVIDENCE_ROOT / "pilots" / "skyvern" / "sanitized_runs.json"
AUDIT_ARGS = ["--repetitions", "3", "--engine", "skyvern-2.0", "--max-steps", "7", "--cost-cap-usd", "2.5"]


def passing_runs() -> list[dict]:
    rows = []
    for task in runner.TASKS:
        for repetition in range(1, 4):
            rows.append(
                {
                    "run_id": f"{task.id}-r{repetition}",
                    "provider_run_id": f"provider-{task.id}-{repetition}",
                    "task_id": task.id,
                    "task": task.public_description,
                    "repetition": repetition,
                    "status": "success",
                    "success": True,
                    "validator_passed": True,
                    "validator_checks": [{"name": "fixture", "passed": True}],
                    "duration_seconds": 10.0,
                    "steps": 3,
                    "reported_cost_usd": 0.01,
                }
            )
    return rows


def aggregate(rows: list[dict]) -> dict:
    return runner.build_aggregate(
        experiment_id="fixture",
        repetitions=3,
        max_steps=7,
        cost_cap=2.5,
        runs=rows,
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:10:00Z",
        engine="skyvern-2.0",
    )


class AggregateAcceptanceTests(unittest.TestCase):
    def test_complete_unique_matrix_passes(self) -> None:
        result = aggregate(passing_runs())
        self.assertEqual(result["completed_runs"], 9)
        self.assertEqual(result["successful_runs"], 9)
        self.assertTrue(result["all_planned_runs_completed"])
        self.assertTrue(result["run_matrix_complete"])
        self.assertTrue(result["provider_run_ids_unique"])
        self.assertTrue(result["all_validator_checks_passed"])
        self.assertEqual(result["runs_with_unreported_cost"], 0)

    def test_incomplete_matrix_does_not_pass(self) -> None:
        result = aggregate(passing_runs()[:-1])
        self.assertFalse(result["all_planned_runs_completed"])
        self.assertFalse(result["run_matrix_complete"])

    def test_duplicate_cell_does_not_pass(self) -> None:
        rows = passing_runs()
        rows[-1]["task_id"] = rows[-2]["task_id"]
        rows[-1]["repetition"] = rows[-2]["repetition"]
        result = aggregate(rows)
        self.assertFalse(result["run_matrix_complete"])

    def test_unreported_cost_is_counted(self) -> None:
        rows = passing_runs()
        rows[0]["reported_cost_usd"] = None
        result = aggregate(rows)
        self.assertEqual(result["runs_with_unreported_cost"], 1)


class ParserTests(unittest.TestCase):
    def test_finds_nested_structured_value(self) -> None:
        value = {"output": {"heading": "Example Domains"}}
        self.assertEqual(
            runner.find_output_value(value, "heading"),
            "Example Domains",
        )

    def test_exact_url_match_ignores_trailing_slash_only(self) -> None:
        self.assertTrue(
            runner.url_seen(
                ["https://www.iana.org/domains/root/"],
                "https://www.iana.org/domains/root",
            )
        )
        self.assertFalse(
            runner.url_seen(
                ["https://www.iana.org/domains"],
                "https://www.iana.org/domains/root",
            )
        )


class OfflineAuditTests(unittest.TestCase):
    def audit(self, *argv: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            runner.main([*argv, *AUDIT_ARGS])
        code = raised.exception.code
        return (code if isinstance(code, int) else 1), output.getvalue()

    def test_audit_of_archived_evidence_is_read_only_and_reproduces_the_aggregate(self) -> None:
        files = sorted(ARCHIVED_RUNS.parent.glob("*.json"))
        before = {path.name: path.read_bytes() for path in files}
        code, output = self.audit("--audit-existing", str(ARCHIVED_RUNS))
        self.assertEqual(code, 0, output)
        self.assertIn("9/9 completed, 9 passed", output)
        self.assertIn("matches aggregate.json", output)
        self.assertIn("writes_performed=False", output)
        self.assertEqual({path.name: path.read_bytes() for path in files}, before)

    def test_write_is_refused_inside_archived_evidence(self) -> None:
        before = ARCHIVED_RUNS.read_bytes()
        with self.assertRaises(SystemExit) as raised:
            runner.main(["--audit-existing", str(ARCHIVED_RUNS), "--write", *AUDIT_ARGS])
        self.assertIn("read-only", str(raised.exception.code))
        self.assertEqual(ARCHIVED_RUNS.read_bytes(), before)

    def test_write_rewrites_a_private_copy_and_detects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary) / "sanitized_runs.json"
            copy.write_bytes(ARCHIVED_RUNS.read_bytes())
            (Path(temporary) / "aggregate.json").write_bytes(
                (ARCHIVED_RUNS.parent / "aggregate.json").read_bytes()
            )
            code, output = self.audit("--audit-existing", str(copy), "--write")
            self.assertEqual(code, 0, output)
            self.assertIn("writes_performed=True", output)
            aggregate_path = Path(temporary) / "aggregate.json"
            tampered = json.loads(aggregate_path.read_text(encoding="utf-8"))
            tampered["successful_runs"] = 8
            aggregate_path.write_text(json.dumps(tampered), encoding="utf-8")
            code, output = self.audit("--audit-existing", str(copy))
            self.assertEqual(code, 1)
            self.assertIn("differs from aggregate.json in: successful_runs", output)

    def test_write_requires_audit_mode_and_live_runs_require_output_dir(self) -> None:
        for argv in (["--write"], []):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    runner.parse_args(argv)
        with self.assertRaisesRegex(SystemExit, "read-only"):
            runner.require_writable_output(runner.EVIDENCE_ROOT / "pilots" / "skyvern")


class PrivateStateTests(unittest.TestCase):
    def test_private_state_lives_under_cua_private_root_outside_the_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"CUA_PRIVATE_ROOT": temporary}):
                self.assertEqual(
                    runner.private_state_dir(),
                    (Path(temporary) / "pilots" / "skyvern").resolve(),
                )
        with mock.patch.dict(os.environ, {"CUA_PRIVATE_ROOT": str(runner.REPO_ROOT)}):
            with self.assertRaisesRegex(RuntimeError, "outside the repository"):
                runner.private_state_dir()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CUA_PRIVATE_ROOT", None)
            with self.assertRaisesRegex(RuntimeError, "CUA_PRIVATE_ROOT"):
                runner.private_state_dir()


if __name__ == "__main__":
    unittest.main()
