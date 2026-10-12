"""Validate a recorder Chromium E2E result bundle.

``npm --prefix extension run e2e`` writes a bundle into ``CUA_E2E_RESULT_DIR``.
This checker accepts it only when it is complete and clean:

* every required file is present (``run_manifest.json``,
  ``validation_summary.json``, ``storage_lifecycle_summary.json``,
  ``exports_manifest.json``, ``event_coverage_summary.json``,
  ``agent_extension_e2e_trace.json``, ``E2E_REPORT.md``);
* all expected cases (15 by default, T00-T14) passed, none failed or skipped;
* zero browser errors were recorded (errors raised by Chrome's own
  ``chrome://`` pages are kept apart in ``browser_ui_errors`` and do not count);
* the bundle was produced by the extension version in
  ``extension/app/manifest.json`` (or ``--expected-version``); when checked
  against the repository manifest, the recorded permissions must equal the
  manifest's and the browser must meet ``minimum_chrome_version``;
* the isolation and claim-boundary attestations hold, every generated artifact
  exists inside the bundle, and every export matches its recorded size and
  SHA-256.

Unknown additional fields are ignored, so the harness may add diagnostics
without breaking this gate. The archived 0.4.0 bundle (14 cases, T00-T13) is
checked with ``--expected-version 0.4.0 --expected-cases 14``. Standard library
only.

Usage::

    python tools/ci/validate_e2e_bundle.py [RESULT_DIR] [--expected-cases 15]
        [--expected-version X.Y.Z] [--json]

RESULT_DIR defaults to ``$CUA_E2E_RESULT_DIR``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "extension" / "app" / "manifest.json"
EXPECTED_CASE_COUNT = 15
REQUIRED_FILES = (
    "run_manifest.json",
    "validation_summary.json",
    "storage_lifecycle_summary.json",
    "exports_manifest.json",
    "event_coverage_summary.json",
    "agent_extension_e2e_trace.json",
    "E2E_REPORT.md",
)
REQUIRED_EXPORT_FORMATS = frozenset({"json", "jsonl", "csv"})
RUN_SCHEMA_RE = re.compile(r"^cui-recorder-extension-e2e-run-v\d+$")
SUMMARY_SCHEMA_RE = re.compile(r"^cui-recorder-extension-e2e-summary-v\d+$")
COVERAGE_SCHEMA_RE = re.compile(r"^cui-recorder-e2e-event-coverage-v\d+$")
# Paths recorded inside archived bundles use the pre-restructure layout.
LEGACY_PREFIXES = ("results/week2_recorder/extension_e2e/",)
ISOLATION_ATTESTATION = {
    "user_chrome_profile_used": False,
    "user_chrome_process_touched": False,
    "unique_profile_under_configured_runtime": True,
    "loopback_fixture_only": True,
}
CLAIM_BOUNDARY = {
    "cui_framework_claimed": False,
    "human_data_claimed": False,
    "comparison_claimed": False,
}


class E2EBundleError(ValueError):
    """Raised when an E2E directory is partial, stale, failed or corrupt."""


# Backwards-compatible alias for the pre-restructure name (scripts/week2_e2e_bundle.py).
Week2BundleError = E2EBundleError


def _json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise E2EBundleError(f"cannot read {path.name}: {error}") from error
    if not isinstance(payload, dict):
        raise E2EBundleError(f"{path.name} must contain one JSON object")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _repository_manifest(manifest_path: Path) -> dict[str, Any]:
    manifest = _json(manifest_path)
    if manifest.get("manifest_version") != 3:
        raise E2EBundleError("repository extension is not Manifest V3")
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        raise E2EBundleError("repository extension manifest has no version")
    return manifest


def current_recorder_version(manifest_path: Path = MANIFEST_PATH) -> str:
    return str(_repository_manifest(manifest_path)["version"])


def _major(value: object) -> int | None:
    """Return the leading integer of a Chrome version (``151`` or ``"151.0.1"``)."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        match = re.match(r"^(?:Chrome/)?(\d+)", value.strip())
        if match:
            return int(match.group(1))
    return None


def _check_against_repository_manifest(run: dict[str, Any], manifest: dict[str, Any]) -> None:
    """Bind the run to the checked-out extension where the bundle records enough."""

    extension = run.get("extension")
    assert isinstance(extension, dict)
    recorded_permissions = extension.get("permissions")
    if recorded_permissions is not None:
        if not isinstance(recorded_permissions, list):
            raise E2EBundleError("run_manifest extension.permissions must be a list")
        expected = manifest.get("permissions", [])
        if sorted(map(str, recorded_permissions)) != sorted(map(str, expected)):
            raise E2EBundleError(
                "E2E bundle permissions do not match extension/app/manifest.json: "
                f"{sorted(map(str, recorded_permissions))} != {sorted(map(str, expected))}"
            )
    minimum = _major(manifest.get("minimum_chrome_version"))
    runtime = run.get("browser_runtime")
    if minimum is not None and isinstance(runtime, dict):
        major = _major(runtime.get("major_version", runtime.get("product_version")))
        if major is not None and major < minimum:
            raise E2EBundleError(
                f"E2E browser major version {major} is below minimum_chrome_version {minimum}"
            )


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return False
    return True


def resolve_artifact(root: Path, value: str, *, repo_root: Path = REPO_ROOT) -> Path:
    """Resolve a path recorded in the bundle and require it to stay inside ``root``.

    The harness records paths relative to the repository root of the machine
    that ran it (which, for a result directory outside the checkout, starts
    with ``..``); archived bundles use the pre-restructure ``results/...``
    prefix; a harness may also record paths relative to the bundle or as
    absolute paths. When a recorded path does not resolve here (the bundle was
    downloaded from CI, say), the longest existing suffix inside ``root`` is
    used. Nothing outside ``root`` is ever returned.
    """

    root = root.resolve()
    text = value.replace("\\", "/")
    for prefix in LEGACY_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :]
            candidates = [root / text]
            break
    else:
        candidate = Path(text)
        if candidate.is_absolute():
            candidates = [candidate]
        else:
            candidates = [repo_root / candidate, root / candidate]
    for candidate in candidates:
        if candidate.is_file() and _inside(candidate, root):
            return candidate.resolve()
    parts = [part for part in Path(text).parts if part not in ("/", "\\")]
    for index in range(len(parts)):
        if parts[index] in ("..", ".") or ":" in parts[index]:
            continue
        suffix = root.joinpath(*parts[index:])
        if suffix.is_file() and _inside(suffix, root):
            return suffix.resolve()
    for candidate in candidates:
        if _inside(candidate, root):
            return candidate.resolve()
    raise E2EBundleError(f"generated artifact escapes the E2E result directory: {value}")


def validate_bundle(
    root: Path,
    *,
    expected_version: str | None = None,
    expected_cases: int = EXPECTED_CASE_COUNT,
    allow_browser_errors: bool = False,
    repo_root: Path = REPO_ROOT,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Validate the bundle in ``root`` and return a summary, or raise E2EBundleError."""

    root = Path(root).resolve()
    if not root.is_dir():
        raise E2EBundleError(f"E2E result directory does not exist: {root}")
    missing = sorted(name for name in REQUIRED_FILES if not (root / name).is_file())
    if missing:
        raise E2EBundleError("missing required E2E artifacts: " + ", ".join(missing))
    repository_manifest: dict[str, Any] | None = None
    if expected_version:
        version = expected_version
    else:
        repository_manifest = _repository_manifest(
            manifest_path or repo_root / "extension" / "app" / "manifest.json"
        )
        version = str(repository_manifest["version"])

    run = _json(root / "run_manifest.json")
    if not RUN_SCHEMA_RE.fullmatch(str(run.get("schema_version"))):
        raise E2EBundleError("run_manifest schema is invalid")
    extension = run.get("extension")
    if not isinstance(extension, dict) or extension.get("version") != version:
        found = extension.get("version") if isinstance(extension, dict) else None
        raise E2EBundleError(f"E2E bundle was produced by recorder {found!r}, expected {version!r}")
    if run.get("fatal_error") is not None:
        raise E2EBundleError("E2E run recorded a fatal error")
    if repository_manifest is not None:
        _check_against_repository_manifest(run, repository_manifest)
    isolation = run.get("isolation")
    if not isinstance(isolation, dict) or any(
        isolation.get(field) is not expected for field, expected in ISOLATION_ATTESTATION.items()
    ):
        raise E2EBundleError("E2E isolation attestation is incomplete")
    actor_claim = run.get("actor_claim")
    if not isinstance(actor_claim, dict) or any(
        actor_claim.get(field) is not expected for field, expected in CLAIM_BOUNDARY.items()
    ):
        raise E2EBundleError("E2E claim boundary is invalid")

    summary = _json(root / "validation_summary.json")
    if not SUMMARY_SCHEMA_RE.fullmatch(str(summary.get("schema_version"))):
        raise E2EBundleError("validation_summary schema is invalid")
    counts = summary.get("counts")
    cases = summary.get("cases")
    if not isinstance(counts, dict) or not isinstance(cases, list) or not cases:
        raise E2EBundleError("validation_summary has no case ledger")
    if any(not isinstance(row, dict) for row in cases):
        raise E2EBundleError("validation_summary case rows must be objects")
    case_ids = [row.get("id") for row in cases]
    if len(set(case_ids)) != len(case_ids) or not all(isinstance(i, str) for i in case_ids):
        raise E2EBundleError("E2E case IDs are missing or duplicated")
    not_passed = sorted(str(row.get("id")) for row in cases if row.get("status") != "passed")
    if not_passed:
        raise E2EBundleError("E2E cases did not pass: " + ", ".join(not_passed))
    if (
        summary.get("overall_status") != "passed"
        or counts.get("total") != len(cases)
        or counts.get("passed") != len(cases)
        or counts.get("failed") != 0
        or counts.get("skipped") != 0
    ):
        raise E2EBundleError("E2E case ledger is not fully passed")
    if len(cases) != expected_cases:
        raise E2EBundleError(f"expected {expected_cases} E2E cases, found {len(cases)}")
    browser_errors = summary.get("browser_errors", [])
    if not isinstance(browser_errors, list):
        raise E2EBundleError("validation_summary browser_errors must be a list")
    if browser_errors and not allow_browser_errors:
        raise E2EBundleError(f"E2E run recorded {len(browser_errors)} browser error(s)")
    # Errors from Chrome's own chrome:// pages (0.5.0+) are diagnostics only.
    browser_ui_errors = summary.get("browser_ui_errors", [])
    if not isinstance(browser_ui_errors, list):
        raise E2EBundleError("validation_summary browser_ui_errors must be a list")

    generated = summary.get("generated_artifacts")
    if not isinstance(generated, list) or not generated:
        raise E2EBundleError("validation_summary does not enumerate generated artifacts")
    for value in generated:
        if (
            not isinstance(value, str)
            or not resolve_artifact(root, value, repo_root=repo_root).is_file()
        ):
            raise E2EBundleError(f"generated artifact is missing: {value!r}")

    exports = _json(root / "exports_manifest.json").get("exports")
    if not isinstance(exports, list) or any(not isinstance(item, dict) for item in exports):
        raise E2EBundleError("exports_manifest has no export list")
    formats = {item.get("format") for item in exports}
    if not formats >= REQUIRED_EXPORT_FORMATS:
        raise E2EBundleError("JSON, JSONL, and CSV export evidence is required")
    for item in exports:
        if item.get("cdp_completed") is not True:
            raise E2EBundleError("export completion attestation is missing")
        file_value = item.get("file")
        if not isinstance(file_value, str):
            raise E2EBundleError("export path is invalid")
        path = resolve_artifact(root, file_value, repo_root=repo_root)
        if not path.is_file():
            raise E2EBundleError(f"export is missing: {file_value}")
        if path.stat().st_size != item.get("bytes") or _sha256(path) != item.get("sha256"):
            raise E2EBundleError(f"export hash/size mismatch: {file_value}")

    coverage = _json(root / "event_coverage_summary.json")
    if not COVERAGE_SCHEMA_RE.fullmatch(str(coverage.get("schema_version"))):
        raise E2EBundleError("event coverage schema is invalid")
    trace = _json(root / "agent_extension_e2e_trace.json")
    metadata = trace.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(trace.get("events"), list):
        raise E2EBundleError("agent_extension_e2e_trace.json is not a trace envelope")
    if "recorder_version" in metadata and metadata["recorder_version"] != version:
        raise E2EBundleError("the exported trace was not produced by the expected recorder version")

    return {
        "schema_version": "cua-e2e-bundle-validation-v1",
        "valid": True,
        "recorder_version": version,
        "case_count": len(cases),
        "case_ids": case_ids,
        "browser_error_count": len(browser_errors),
        "browser_ui_error_count": len(browser_ui_errors),
        "export_formats": sorted(str(value) for value in formats),
        "event_count": len(trace["events"]),
        "result_root": str(root),
    }


def _write_step_summary(result: dict[str, Any]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        "### Recorder E2E bundle",
        "",
        f"- Recorder version: `{result['recorder_version']}`",
        f"- Cases passed: {result['case_count']}/{result['case_count']}",
        f"- Browser errors: {result['browser_error_count']}",
        f"- Exported trace events: {result['event_count']}",
        "",
    ]
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("result_dir", nargs="?", type=Path, help="default: $CUA_E2E_RESULT_DIR")
    parser.add_argument("--expected-version", help="default: extension/app/manifest.json version")
    parser.add_argument("--expected-cases", type=int, default=EXPECTED_CASE_COUNT)
    parser.add_argument(
        "--allow-browser-errors",
        action="store_true",
        help="do not fail on recorded browser console/page errors (local debugging only)",
    )
    parser.add_argument("--json", action="store_true", help="print the validation result as JSON")
    args = parser.parse_args(argv)
    result_dir = args.result_dir or (
        Path(os.environ["CUA_E2E_RESULT_DIR"]) if os.environ.get("CUA_E2E_RESULT_DIR") else None
    )
    if result_dir is None:
        parser.error("pass RESULT_DIR or set CUA_E2E_RESULT_DIR")
    try:
        result = validate_bundle(
            result_dir,
            expected_version=args.expected_version,
            expected_cases=args.expected_cases,
            allow_browser_errors=args.allow_browser_errors,
        )
    except E2EBundleError as error:
        print(f"ERROR: {error}")
        return 1
    _write_step_summary(result)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    print(
        f"PASS: recorder {result['recorder_version']} E2E bundle, "
        f"{result['case_count']}/{result['case_count']} cases, "
        f"{result['browser_error_count']} browser errors"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
