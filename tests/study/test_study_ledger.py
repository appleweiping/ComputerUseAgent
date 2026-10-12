from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from computer_use_agent.study.ledger import (
    LedgerError,
    active_claims,
    atomic_write_json,
    claim,
    initialize_or_resume,
    ledger_payload,
    validate_ledger,
)


class StudyLedgerTests(unittest.TestCase):
    def expected(self) -> dict:
        return ledger_payload(
            stage="week1",
            binding={
                "formal_config": "configs/formal-study.yaml",
                "formal_config_sha256": "a" * 64,
                "release_capsule": "configs/release-capsule.yaml",
                "release_capsule_sha256": "b" * 64,
                "formal_config_snapshot": {"schema_version": "cui-formal-study-config-v1"},
                "release_capsule_snapshot": {
                    "schema_version": "cui-run-capsule-v2",
                    "sealed": True,
                },
            },
            protocol_sha256="c" * 64,
            planned_ids=["attempt-01", "attempt-02"],
        )

    def test_initialize_resume_is_immutable_and_detects_binding_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "ledger"
            expected = self.expected()
            initialize_or_resume(root, expected, resume=False)
            observed = initialize_or_resume(root, expected, resume=True)
            self.assertEqual(observed["planned_ids"], expected["planned_ids"])
            self.assertEqual(
                observed["binding"]["release_capsule_snapshot"],
                expected["binding"]["release_capsule_snapshot"],
            )
            with self.assertRaisesRegex(LedgerError, "use --resume"):
                initialize_or_resume(root, expected, resume=False)
            changed = json.loads(json.dumps(expected))
            changed["protocol_sha256"] = "d" * 64
            with self.assertRaisesRegex(LedgerError, "binding mismatch"):
                initialize_or_resume(root, changed, resume=True)
            changed_snapshot = json.loads(json.dumps(expected))
            changed_snapshot["binding"]["release_capsule_snapshot"]["sealed"] = False
            with self.assertRaisesRegex(LedgerError, "binding mismatch"):
                initialize_or_resume(root, changed_snapshot, resume=True)

    def test_resume_rejects_timestamp_mutation_and_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "ledger"
            expected = self.expected()
            initialize_or_resume(root, expected, resume=False)
            ledger_path = root / "study_ledger.json"
            observed = json.loads(ledger_path.read_text(encoding="utf-8"))
            observed["created_at"] = "2000-01-01T00:00:00Z"
            ledger_path.write_text(json.dumps(observed), encoding="utf-8")
            with self.assertRaisesRegex(LedgerError, "integrity mismatch"):
                initialize_or_resume(root, self.expected(), resume=True)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "ledger"
            expected = self.expected()
            initialize_or_resume(root, expected, resume=False)
            ledger_path = root / "study_ledger.json"
            observed = json.loads(ledger_path.read_text(encoding="utf-8"))
            observed["operator_note"] = "misleading mutable text"
            ledger_path.write_text(json.dumps(observed), encoding="utf-8")
            with self.assertRaisesRegex(LedgerError, "closed immutable schema"):
                initialize_or_resume(root, self.expected(), resume=True)

    def test_partial_root_and_corrupt_ledger_are_never_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "ledger"
            root.mkdir()
            (root / "partial.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(LedgerError, "refusing to adopt"):
                initialize_or_resume(root, self.expected(), resume=True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "ledger"
            root.mkdir()
            (root / "study_ledger.json").write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(LedgerError, "cannot read"):
                initialize_or_resume(root, self.expected(), resume=True)

    def test_claim_excludes_concurrent_writer_and_releases_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                claim(root, "attempt-01"),
                self.assertRaisesRegex(LedgerError, "already claimed"),
                claim(root, "attempt-01"),
            ):
                self.fail("second claim must not be entered")
            with claim(root, "attempt-01"):
                pass

    def test_immutable_atomic_publication_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "entry.json"
            atomic_write_json(path, {"value": 1}, replace=False)
            with self.assertRaisesRegex(LedgerError, "already exists"):
                atomic_write_json(path, {"value": 2}, replace=False)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"value": 1})


class LedgerContractTests(unittest.TestCase):
    def binding(self) -> dict:
        return {
            "formal_config": "formal-study.json",
            "formal_config_sha256": "a" * 64,
            "release_capsule": "run-capsule.json",
            "release_capsule_sha256": "b" * 64,
            "formal_config_snapshot": {},
            "release_capsule_snapshot": {},
        }

    def test_payload_rejects_unsafe_identifiers_and_bindings(self) -> None:
        good = {"stage": "week1", "binding": self.binding(), "protocol_sha256": "c" * 64}
        ledger_payload(**good, planned_ids=["a-01"])
        cases = [
            ({**good, "stage": "bad stage"}, ["a"], "stable token"),
            (good, [], "planned IDs"),
            (good, ["a", "a"], "planned IDs"),
            ({**good, "protocol_sha256": "C" * 64}, ["a"], "protocol_sha256"),
            ({**good, "binding": {"formal_config": "x"}}, ["a"], "exact config and capsule"),
            (
                {**good, "binding": {**self.binding(), "formal_config": "sk-" + "x" * 30}},
                ["a"],
                "secret-like",
            ),
        ]
        for kwargs, ids, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(LedgerError, message):
                ledger_payload(**kwargs, planned_ids=ids)

    def test_validate_ledger_rejects_tampering(self) -> None:
        ledger = ledger_payload(
            stage="week1", binding=self.binding(), protocol_sha256="c" * 64, planned_ids=["a"]
        )
        validate_ledger(ledger, ledger)
        for field, value, message in (
            ("integrity_sha256", "x", "integrity hash is invalid"),
            ("created_at", 5, "integrity mismatch"),
        ):
            changed = json.loads(json.dumps(ledger))
            changed[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(LedgerError, message):
                validate_ledger(changed, ledger)
        naive = json.loads(json.dumps(ledger))
        naive["created_at"] = "2026-01-01T00:00:00"
        content = {key: value for key, value in naive.items() if key != "integrity_sha256"}
        naive["integrity_sha256"] = ledger_payload.__globals__["_integrity_sha256"](content)
        with self.assertRaisesRegex(LedgerError, "timezone"):
            validate_ledger(naive, ledger)

    def test_claims_are_listed_and_unsafe_ids_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(active_claims(root), [])
            with claim(root, "attempt-02"):
                self.assertEqual(active_claims(root), ["attempt-02"])
            self.assertEqual(active_claims(root), [])
            with self.assertRaisesRegex(LedgerError, "safe stable token"), claim(root, "../x"):
                pass


if __name__ == "__main__":
    unittest.main()
