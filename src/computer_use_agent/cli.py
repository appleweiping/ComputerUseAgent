"""``computer-use-agent`` console script (also ``python -m computer_use_agent``).

Subcommands:

``validate``       validate a file against a packaged schema (traces, contracts,
                   detector datasets; ``cui-detector-dataset-v1`` read-only)
``extract``        extract a path-free detector dataset from a trace manifest
``analyze``        run the detector pipeline and write a verified result bundle
``verify-bundle``  re-verify a result bundle's hashes, ZIP and reporting contract
``view``           serve the loopback-only, privacy-projected viewer
``schema``         list or print the packaged JSON Schemas
``doctor``         check the local environment (exit 0 unless a required check fails)

The historical module entry points (``python -m
computer_use_agent.detector.pipeline`` and friends) keep working.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path

from . import __version__

PROG = "computer-use-agent"
MINIMUM_PYTHON = (3, 12)
MINIMUM_NODE = (22, 12)
# ESLint 10 (`npm run lint`, the default nox run and the pre-commit hook) needs
# a newer Node than the extension tests and the E2E suite.
MINIMUM_NODE_FOR_LINT = (22, 13)


class CommandError(Exception):
    """An expected, user-facing failure (exit status 1)."""


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


# ------------------------------------------------------------- subcommands --


def _cmd_validate(args: argparse.Namespace) -> int:
    from .contracts.validation import ContractValidationError, validate_file

    try:
        summary = validate_file(args.input, schema_version=args.schema, for_write=args.for_write)
    except ContractValidationError as error:
        raise CommandError(str(error)) from error
    _print_json(summary)
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    from .detector.extract import extract_manifest, write_dataset

    dataset = extract_manifest(args.manifest)
    write_dataset(dataset, args.output)
    _print_json(dataset["audit"])
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    from .detector.pipeline import run_analysis_arguments

    print(
        f"{PROG} analyze: profile {args.profile}; this can take a while "
        "(the default profile is about 2 minutes on 18 records)",
        file=sys.stderr,
        flush=True,
    )
    _print_json(run_analysis_arguments(args))
    return 0


def _cmd_verify_bundle(args: argparse.Namespace) -> int:
    from .detector.bundle import verify_bundle

    files = verify_bundle(args.output_dir)
    _print_json({"verified": True, "files": files})
    return 0


def _cmd_view(args: argparse.Namespace) -> int:
    from .viewer.serve import serve

    parser: argparse.ArgumentParser = args.subparser
    return serve(args, parser)


def _cmd_schema_list(args: argparse.Namespace) -> int:
    from .schemas import SCHEMA_FILES, load_schema

    rows: list[dict[str, object]] = []
    for schema_id in sorted(SCHEMA_FILES):
        schema = load_schema(schema_id)
        rows.append(
            {
                "schema": schema_id,
                "file": SCHEMA_FILES[schema_id],
                "id": schema.get("$id"),
                "title": schema.get("title"),
            }
        )
    if args.json:
        _print_json(rows)
    else:
        width = max(len(schema_id) for schema_id in SCHEMA_FILES)
        for row in rows:
            print(f"{row['schema']!s:<{width}}  {row['id']}")
    return 0


def _cmd_schema_show(args: argparse.Namespace) -> int:
    from .schemas import UnknownSchemaError, schema_text

    try:
        text = schema_text(args.schema)
    except UnknownSchemaError as error:
        raise CommandError(str(error)) from error
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


# ------------------------------------------------------------------ doctor --


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # "ok", "warn" or "fail"
    detail: str
    required: bool


def _parse_version(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups(default="0"))


def _check_python() -> Check:
    observed = sys.version_info[:3]
    rendered = ".".join(str(part) for part in observed)
    if observed >= MINIMUM_PYTHON:
        return Check("python", "ok", f"Python {rendered}", True)
    return Check("python", "fail", f"Python {rendered}; >= 3.12 is required", True)


def _check_jsonschema() -> Check:
    try:
        version = metadata.version("jsonschema")
        from jsonschema import Draft202012Validator  # noqa: F401
    except (metadata.PackageNotFoundError, ImportError) as error:
        return Check("jsonschema", "fail", f"jsonschema is not importable: {error}", True)
    return Check("jsonschema", "ok", f"jsonschema {version}", True)


def _check_package_data() -> Check:
    try:
        from .schemas import SCHEMA_FILES, load_schema
        from .viewer.serve import STATIC_FILES

        for schema_id in SCHEMA_FILES:
            load_schema(schema_id)
        for resource in STATIC_FILES.values():
            resource.read_bytes()
    except (OSError, ValueError) as error:
        return Check("package-data", "fail", f"packaged schemas/viewer assets: {error}", True)
    return Check(
        "package-data",
        "ok",
        f"{PROG} {__version__}: {len(SCHEMA_FILES)} schemas and viewer assets readable",
        True,
    )


def _run_version(executable: str) -> str:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    completed = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        creationflags=flags,
    )
    return (completed.stdout or completed.stderr).strip()


def _check_node() -> Check:
    executable = shutil.which("node")
    if executable is None:
        return Check(
            "node",
            "warn",
            "node not found; Node >= 22.12 is needed only for extension development and E2E",
            False,
        )
    try:
        output = _run_version(executable)
    except (OSError, subprocess.SubprocessError) as error:
        return Check("node", "warn", f"could not run node --version: {error}", False)
    version = _parse_version(output)
    if version is None or version < MINIMUM_NODE:
        return Check(
            "node",
            "warn",
            f"node {output or 'unknown'}; Node >= 22.12 is needed for extension tests and E2E",
            False,
        )
    if version < MINIMUM_NODE_FOR_LINT:
        return Check(
            "node",
            "ok",
            f"node {output}; ESLint 10 (npm run lint) needs Node >= 22.13",
            False,
        )
    return Check("node", "ok", f"node {output}", False)


def _chrome_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("CUA_E2E_CHROME")
    if configured:
        candidates.append(Path(configured))
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
    ):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    if os.name == "nt":
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(variable)
            if base:
                candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
                candidates.append(Path(base) / "Chromium" / "Application" / "chrome.exe")
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
        candidates.append(Path("/Applications/Chromium.app/Contents/MacOS/Chromium"))
    puppeteer_cache = Path(
        os.environ.get("PUPPETEER_CACHE_DIR", str(Path.home() / ".cache" / "puppeteer"))
    )
    if puppeteer_cache.is_dir():
        executable = "chrome.exe" if os.name == "nt" else "chrome"
        candidates.extend(sorted(puppeteer_cache.glob(f"chrome/*/chrome-*/{executable}")))
    return candidates


def _check_chrome() -> Check:
    # Never launch the browser: only report whether an executable exists.
    for candidate in _chrome_candidates():
        if candidate.is_file():
            return Check("chrome", "ok", f"Chrome/Chromium found: {candidate}", False)
    return Check(
        "chrome",
        "warn",
        "no Chrome/Chromium found (set CUA_E2E_CHROME or run npm ci in extension/ for "
        "Chrome for Testing); needed only to load the extension and for E2E",
        False,
    )


DOCTOR_CHECKS: tuple[Callable[[], Check], ...] = (
    _check_python,
    _check_jsonschema,
    _check_package_data,
    _check_node,
    _check_chrome,
)


def run_doctor() -> list[Check]:
    return [check() for check in DOCTOR_CHECKS]


def _cmd_doctor(args: argparse.Namespace) -> int:
    checks = run_doctor()
    failed = any(check.status == "fail" and check.required for check in checks)
    if args.json:
        _print_json({"ok": not failed, "checks": [asdict(check) for check in checks]})
    else:
        for check in checks:
            kind = "required" if check.required else "optional"
            print(f"[{check.status:>4}] {check.name:<12} ({kind}) {check.detail}")
        warnings = sum(check.status == "warn" for check in checks)
        print(
            f"{PROG} doctor: {'FAILED' if failed else 'ok'}"
            + (f" with {warnings} warning(s)" if warnings else "")
        )
    return 1 if failed else 0


# ------------------------------------------------------------------ parser --


def build_parser() -> argparse.ArgumentParser:
    # Keep parser construction free of jsonschema so ``doctor`` can report it missing.
    from .detector.pipeline import add_analysis_arguments
    from .schemas import SCHEMA_FILES
    from .viewer.serve import build_parser as build_view_parser

    validatable = sorted({*SCHEMA_FILES, "cui-detector-dataset-v1"})

    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Privacy-reduced interaction-trace toolkit: validate traces and contracts, "
            "extract behavioral features, run the leakage-controlled detector analysis, "
            "and inspect results in a loopback-only viewer."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    validate = commands.add_parser(
        "validate",
        help="validate a file against a packaged schema",
        description=(
            "Validate a file against a packaged schema. For cui-detector-dataset-v2 this "
            "checks schema, safety, duplicate records, formal rows without provenance and "
            "any stored audit; the "
            "leave-one-task-out gate (at least 3 tasks, both classes in every task, at "
            "least 3 records per task) is enforced by analyze."
        ),
    )
    validate.add_argument("--schema", required=True, choices=validatable)
    validate.add_argument("--input", type=Path, required=True)
    validate.add_argument(
        "--for-write",
        action="store_true",
        help="reject read-only legacy formats (cui-detector-dataset-v1)",
    )
    validate.set_defaults(handler=_cmd_validate)

    extract = commands.add_parser("extract", help="extract a detector dataset from a manifest")
    extract.add_argument("--manifest", type=Path, required=True)
    extract.add_argument("--output", type=Path, required=True, help="written atomically")
    extract.set_defaults(handler=_cmd_extract)

    analyze = commands.add_parser("analyze", help="run the detector pipeline")
    add_analysis_arguments(analyze)
    analyze.set_defaults(handler=_cmd_analyze)

    verify = commands.add_parser("verify-bundle", help="verify a detector result bundle")
    verify.add_argument("output_dir", type=Path, metavar="OUTPUT_DIR")
    verify.set_defaults(handler=_cmd_verify_bundle)

    view = commands.add_parser("view", help="serve the loopback-only viewer")
    build_view_parser(view)
    view.set_defaults(handler=_cmd_view, subparser=view)

    schema = commands.add_parser("schema", help="list or print packaged JSON Schemas")
    schema_commands = schema.add_subparsers(dest="schema_command", metavar="ACTION", required=True)
    schema_list = schema_commands.add_parser("list", help="list packaged schemas")
    schema_list.add_argument("--json", action="store_true")
    schema_list.set_defaults(handler=_cmd_schema_list)
    schema_show = schema_commands.add_parser("show", help="print one schema")
    schema_show.add_argument("schema", metavar="ID")
    schema_show.set_defaults(handler=_cmd_schema_show)

    doctor = commands.add_parser("doctor", help="check the local environment")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=_cmd_doctor)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.handler
    try:
        return handler(args)
    except (CommandError, ValueError, OSError) as error:
        print(f"{PROG} {args.command}: error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
