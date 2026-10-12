"""tools/ci/validate_e2e_bundle.py against synthetic bundles and the archived 0.4.0 bundle."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tools.ci.validate_e2e_bundle import (
    E2EBundleError,
    Week2BundleError,
    main,
    resolve_artifact,
    validate_bundle,
)

REPOSITORY = Path(__file__).resolve().parents[2]
ARCHIVED_BUNDLE = REPOSITORY / "research" / "evidence" / "recorder_e2e_v0.4.0"
VERSION = "0.5.0"


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _make_bundle(tmp_path: Path, *, case_count: int = 15) -> tuple[Path, Path]:
    """Build a bundle shaped like extension/test/e2e/recorder.e2e.mjs output.

    The result directory lives outside the fake repository, so recorded paths
    are repository-relative and start with ``..`` exactly as in CI.
    """

    repo = tmp_path / "repo"
    _write(
        repo / "extension" / "app" / "manifest.json", {"manifest_version": 3, "version": VERSION}
    )
    root = tmp_path / "runner-temp" / "e2e"
    rel = "../runner-temp/e2e"
    exports = []
    for fmt, body in (
        ("json", b'{"events": []}\n'),
        ("jsonl", b"{}\n"),
        ("csv", b"a,b\r\n1,2\r\n"),
    ):
        name = f"exports/cui-trace-e2e-safe-demo.{fmt}"
        (root / "exports").mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(body)
        exports.append(
            {
                "format": fmt,
                "file": f"{rel}/{name}",
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "cdp_completed": True,
            }
        )
    _write(
        root / "run_manifest.json",
        {
            "schema_version": "cui-recorder-extension-e2e-run-v1",
            "extension": {"name": "ComputerUseAgent Trace Recorder", "version": VERSION},
            "isolation": {
                "user_chrome_profile_used": False,
                "user_chrome_process_touched": False,
                "unique_profile_under_configured_runtime": True,
                "loopback_fixture_only": True,
                "fixture_origins": ["http://127.0.0.1:1"],
            },
            "actor_claim": {
                "actor_type": "agent",
                "cui_framework_claimed": False,
                "human_data_claimed": False,
                "comparison_claimed": False,
            },
            "fatal_error": None,
            "some_future_field": {"ignored": True},
        },
    )
    cases = [
        {"id": f"T{index:02d}", "name": f"case {index}", "status": "passed", "details": {}}
        for index in range(case_count)
    ]
    _write(root / "storage_lifecycle_summary.json", {"sessions": []})
    _write(root / "exports_manifest.json", {"exports": exports, "browser_download_events": []})
    _write(
        root / "event_coverage_summary.json",
        {"schema_version": "cui-recorder-e2e-event-coverage-v1"},
    )
    _write(
        root / "agent_extension_e2e_trace.json",
        {
            "metadata": {"recorder_version": VERSION, "schema_version": "cui-trace-v2"},
            "events": [{}],
        },
    )
    (root / "E2E_REPORT.md").write_text("# report\n", encoding="utf-8")
    generated = sorted(
        [
            f"{rel}/{name}"
            for name in (
                "run_manifest.json",
                "storage_lifecycle_summary.json",
                "exports_manifest.json",
                "validation_summary.json",
                "E2E_REPORT.md",
                "agent_extension_e2e_trace.json",
                "event_coverage_summary.json",
            )
        ]
        + [item["file"] for item in exports]
    )
    _write(
        root / "validation_summary.json",
        {
            "schema_version": "cui-recorder-extension-e2e-summary-v1",
            "overall_status": "passed",
            "counts": {"total": case_count, "passed": case_count, "failed": 0, "skipped": 0},
            "cases": cases,
            "browser_errors": [],
            "generated_artifacts": generated,
        },
    )
    return repo, root


def _mutate(root: Path, name: str, change: Callable[[dict[str, Any]], None]) -> None:
    path = root / name
    payload = json.loads(path.read_text(encoding="utf-8"))
    change(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _validate(repo: Path, root: Path, **kwargs: Any) -> dict[str, Any]:
    return validate_bundle(root, repo_root=repo, **kwargs)


def test_complete_bundle_passes(tmp_path: Path) -> None:
    repo, root = _make_bundle(tmp_path)
    result = _validate(repo, root)
    assert result["valid"] is True
    assert result["recorder_version"] == VERSION
    assert result["case_count"] == 15
    assert result["browser_error_count"] == 0
    assert result["browser_ui_error_count"] == 0
    assert result["export_formats"] == ["csv", "json", "jsonl"]


def test_legacy_error_name_is_kept() -> None:
    assert Week2BundleError is E2EBundleError


def test_missing_file_fails(tmp_path: Path) -> None:
    repo, root = _make_bundle(tmp_path)
    (root / "E2E_REPORT.md").unlink()
    with pytest.raises(E2EBundleError, match="missing required E2E artifacts: E2E_REPORT.md"):
        _validate(repo, root)


def test_failed_case_fails(tmp_path: Path) -> None:
    repo, root = _make_bundle(tmp_path)

    def fail_t07(summary: dict[str, Any]) -> None:
        summary["cases"][7]["status"] = "failed"
        summary["counts"]["passed"] = 14
        summary["counts"]["failed"] = 1
        summary["overall_status"] = "failed"

    _mutate(root, "validation_summary.json", fail_t07)
    with pytest.raises(E2EBundleError, match="did not pass: T07"):
        _validate(repo, root)


def test_wrong_case_count_fails(tmp_path: Path) -> None:
    repo, root = _make_bundle(tmp_path, case_count=13)
    with pytest.raises(E2EBundleError, match="expected 15 E2E cases, found 13"):
        _validate(repo, root)
    assert _validate(repo, root, expected_cases=13)["case_count"] == 13


def test_browser_errors_fail_unless_allowed(tmp_path: Path) -> None:
    repo, root = _make_bundle(tmp_path)
    _mutate(
        root,
        "validation_summary.json",
        lambda summary: summary["browser_errors"].append({"kind": "pageerror", "message": "x"}),
    )
    with pytest.raises(E2EBundleError, match="1 browser error"):
        _validate(repo, root)
    assert _validate(repo, root, allow_browser_errors=True)["browser_error_count"] == 1


def test_browser_ui_errors_do_not_count(tmp_path: Path) -> None:
    repo, root = _make_bundle(tmp_path)
    _mutate(
        root,
        "validation_summary.json",
        lambda summary: summary.update(
            browser_ui_errors=[{"page": "chrome://extensions/", "kind": "console", "message": "x"}]
        ),
    )
    assert _validate(repo, root)["browser_ui_error_count"] == 1
    _mutate(root, "validation_summary.json", lambda summary: summary.update(browser_ui_errors={}))
    with pytest.raises(E2EBundleError, match="browser_ui_errors must be a list"):
        _validate(repo, root)


def test_permissions_and_chrome_floor_bind_to_the_repository_manifest(tmp_path: Path) -> None:
    repo, root = _make_bundle(tmp_path)
    _write(
        repo / "extension" / "app" / "manifest.json",
        {
            "manifest_version": 3,
            "version": VERSION,
            "minimum_chrome_version": "151",
            "permissions": ["activeTab", "scripting", "storage", "unlimitedStorage"],
        },
    )

    def record(permissions: list[str], major: int) -> None:
        def change(run: dict[str, Any]) -> None:
            run["extension"]["permissions"] = permissions
            run["browser_runtime"] = {"major_version": major}

        _mutate(root, "run_manifest.json", change)

    record(["storage", "unlimitedStorage", "scripting", "activeTab"], 151)
    assert _validate(repo, root)["valid"] is True
    record(["activeTab", "downloads", "scripting", "storage", "unlimitedStorage"], 151)
    with pytest.raises(E2EBundleError, match="permissions do not match"):
        _validate(repo, root)
    record(["activeTab", "scripting", "storage", "unlimitedStorage"], 150)
    with pytest.raises(E2EBundleError, match="below minimum_chrome_version 151"):
        _validate(repo, root)
    # An explicit expected version (archived bundles) skips the repository binding.
    assert _validate(repo, root, expected_version=VERSION)["valid"] is True


def test_version_must_match_the_manifest(tmp_path: Path) -> None:
    repo, root = _make_bundle(tmp_path)
    _write(
        repo / "extension" / "app" / "manifest.json", {"manifest_version": 3, "version": "0.6.0"}
    )
    with pytest.raises(E2EBundleError, match="expected '0.6.0'"):
        _validate(repo, root)
    assert _validate(repo, root, expected_version=VERSION)["recorder_version"] == VERSION


def test_fatal_error_and_attestations_fail(tmp_path: Path) -> None:
    repo, root = _make_bundle(tmp_path)
    _mutate(root, "run_manifest.json", lambda run: run.update(fatal_error={"message": "boom"}))
    with pytest.raises(E2EBundleError, match="fatal error"):
        _validate(repo, root)
    repo, root = _make_bundle(tmp_path / "second")
    _mutate(
        root,
        "run_manifest.json",
        lambda run: run["isolation"].update(user_chrome_profile_used=True),
    )
    with pytest.raises(E2EBundleError, match="isolation"):
        _validate(repo, root)


def test_export_tampering_fails(tmp_path: Path) -> None:
    repo, root = _make_bundle(tmp_path)
    (root / "exports" / "cui-trace-e2e-safe-demo.csv").write_bytes(b"tampered\n")
    with pytest.raises(E2EBundleError, match="hash/size mismatch"):
        _validate(repo, root)


def test_missing_export_format_fails(tmp_path: Path) -> None:
    repo, root = _make_bundle(tmp_path)
    _mutate(
        root,
        "exports_manifest.json",
        lambda manifest: manifest.update(
            exports=[item for item in manifest["exports"] if item["format"] != "csv"]
        ),
    )
    with pytest.raises(E2EBundleError, match="JSON, JSONL, and CSV"):
        _validate(repo, root)


def test_artifacts_may_not_escape_the_bundle(tmp_path: Path) -> None:
    repo, root = _make_bundle(tmp_path)
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    with pytest.raises(E2EBundleError, match="escapes"):
        resolve_artifact(root, "../runner-temp/../outside.txt", repo_root=repo)
    _mutate(
        root,
        "validation_summary.json",
        lambda summary: summary["generated_artifacts"].append(str(tmp_path / "outside.txt")),
    )
    with pytest.raises(E2EBundleError, match="escapes"):
        _validate(repo, root)


def test_bundle_relative_and_absolute_paths_resolve(tmp_path: Path) -> None:
    repo, root = _make_bundle(tmp_path)
    expected = (root / "E2E_REPORT.md").resolve()
    assert resolve_artifact(root, "E2E_REPORT.md", repo_root=repo) == expected
    assert resolve_artifact(root, str(root / "E2E_REPORT.md"), repo_root=repo) == expected
    assert resolve_artifact(root, "../runner-temp/e2e/E2E_REPORT.md", repo_root=repo) == expected


def test_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    repo, root = _make_bundle(tmp_path)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    # The CLI resolves against this repository; pass the version explicitly.
    assert main([str(root), "--expected-version", VERSION]) == 0
    assert "15/15 cases" in capsys.readouterr().out
    (root / "run_manifest.json").unlink()
    assert main([str(root), "--expected-version", VERSION]) == 1
    assert "missing required" in capsys.readouterr().out


@pytest.mark.skipif(not ARCHIVED_BUNDLE.is_dir(), reason="archived 0.4.0 E2E evidence not present")
def test_archived_v040_bundle_still_validates() -> None:
    result = validate_bundle(ARCHIVED_BUNDLE, expected_version="0.4.0", expected_cases=14)
    assert result["case_count"] == 14
    assert result["case_ids"] == [f"T{index:02d}" for index in range(14)]
    assert result["browser_error_count"] == 0
    assert result["event_count"] == 527
