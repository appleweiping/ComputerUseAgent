"""Fail when a GitHub workflow or composite action uses a mutable action reference.

Every third-party ``uses:`` reference must be pinned to a full 40-character
commit SHA and carry a human-readable version comment, for example::

    uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1

Local actions (``./path``) are allowed. Docker references must be pinned by
``@sha256:<digest>``. Dependabot updates the SHA and the comment together.

The check reads lines, not YAML, so it needs only the standard library (it
runs as a pre-commit hook and in the dependency-free lint job). It fails
closed: a ``uses`` key it cannot read as ``uses: owner/repo@<sha> # vX.Y.Z`` on
one line (a flow mapping, a quoted key, a value on the next line, a block
scalar, an alias, an explicit ``? key`` or an escaped key) is a violation, so
an unpinned reference cannot pass by being spelled differently.

Usage::

    python tools/ci/check_actions_pinned.py                 # .github/workflows and .github/actions
    python tools/ci/check_actions_pinned.py FILE [FILE ...]  # explicit files (pre-commit)
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
ACTIONS_DIR = ROOT / ".github" / "actions"
# The one accepted spelling: a block-mapping `uses:` key with the reference on the
# same line, optionally quoted, optionally followed by a comment and trailing
# whitespace.
USES_RE = re.compile(
    r"^\s*(?:-\s*)?uses:\s*[\"']?(?P<action>[^@\s\"']+)(?:@(?P<ref>[^\s#\"']+))?[\"']?"
    r"(?:\s*#\s*(?P<comment>.*?))?\s*$"
)
# Any other spelling of a `uses` key: in a flow mapping (`{name: x, uses: y}`),
# quoted (`"uses":`), with a tag or anchor before it, or with the value on the
# next line. Lines that match this but not USES_RE fail closed.
USES_KEY_RE = re.compile(r"""(?:^|[\s{,\[?])["']?uses["']?\s*:""")
# Key forms the line scanner cannot read at all; workflows never need them.
# Explicit `? key` entries (the key may span lines) and double-quoted keys with
# escape sequences (`"u\x73es"` is `uses`).
EXPLICIT_KEY_RE = re.compile(r"^\s*(?:-\s+)*\?(?:\s|$)|[{,]\s*\?(?:\s|$)")
ESCAPED_KEY_RE = re.compile(r'"[^"]*\\[^"]*"\s*:')
# A YAML node property or indicator where the action should start: a block
# scalar (`>`/`|`), an alias (`*name`), an anchor (`&name`), a tag (`!x`) or a
# flow collection.
YAML_INDICATORS = (">", "|", "*", "&", "!", "{", "[")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DOCKER_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
VERSION_COMMENT_RE = re.compile(r"\bv?\d+(?:\.\d+){0,2}\b")
UNRECOGNISED = (
    "unrecognised `uses:` form; write `uses: owner/repo@<40-char SHA> # vX.Y.Z` on one line"
)


@dataclass(frozen=True)
class PinViolation:
    path: Path
    line: int
    message: str


@dataclass(frozen=True)
class UsesLine:
    """One ``uses:`` reference the line scanner read."""

    line: int
    action: str
    ref: str | None
    comment: str

    @property
    def value(self) -> str:
        """The reference as YAML would read it (``action`` or ``action@ref``)."""

        return self.action if self.ref is None else f"{self.action}@{self.ref}"


def _scan(path: Path) -> tuple[list[UsesLine], list[PinViolation]]:
    references: list[UsesLine] = []
    unreadable: list[PinViolation] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        match = USES_RE.match(line)
        if match is not None and not match.group("action").startswith(YAML_INDICATORS):
            references.append(
                UsesLine(
                    line_number,
                    match.group("action"),
                    match.group("ref"),
                    match.group("comment") or "",
                )
            )
        elif match is not None or any(
            pattern.search(line) for pattern in (USES_KEY_RE, EXPLICIT_KEY_RE, ESCAPED_KEY_RE)
        ):
            unreadable.append(PinViolation(path, line_number, UNRECOGNISED))
    return references, unreadable


def scan_uses(path: Path) -> list[UsesLine]:
    """Return the ``uses:`` references in ``path`` that are written in the accepted form."""

    return _scan(path)[0]


def check_workflow(path: Path) -> list[PinViolation]:
    """Return the pinning violations in one workflow or action metadata file."""

    references, violations = _scan(path)
    for reference in references:
        action = reference.action
        if action.startswith("./"):
            continue
        if action.startswith("docker://"):
            if reference.ref is None or DOCKER_DIGEST_RE.fullmatch(reference.ref) is None:
                violations.append(
                    PinViolation(path, reference.line, f"{action} is not pinned by sha256 digest")
                )
            continue
        if reference.ref is None or SHA_RE.fullmatch(reference.ref) is None:
            violations.append(
                PinViolation(path, reference.line, f"{action} is not pinned to a 40-character SHA")
            )
        if VERSION_COMMENT_RE.search(reference.comment) is None:
            violations.append(
                PinViolation(
                    path, reference.line, f"{action} SHA lacks a human-readable version comment"
                )
            )
    return sorted(violations, key=lambda violation: violation.line)


def _yaml_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted((*directory.rglob("*.yml"), *directory.rglob("*.yaml")))


def check_workflows(workflow_dir: Path = WORKFLOW_DIR) -> list[PinViolation]:
    """Check every workflow under ``workflow_dir``; an empty directory is a violation."""

    paths = _yaml_files(workflow_dir)
    if not paths:
        return [PinViolation(workflow_dir, 0, "no GitHub workflow files found")]
    return [violation for path in paths for violation in check_workflow(path)]


def check_files(paths: Iterable[Path]) -> list[PinViolation]:
    return [violation for path in paths for violation in check_workflow(path)]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", type=Path, help="explicit workflow/action files")
    parser.add_argument("--workflow-dir", type=Path, default=WORKFLOW_DIR)
    parser.add_argument("--actions-dir", type=Path, default=ACTIONS_DIR)
    args = parser.parse_args(argv)
    if args.files:
        violations = check_files(args.files)
        scope = f"{len(args.files)} file(s)"
    else:
        violations = check_workflows(args.workflow_dir)
        violations.extend(check_files(_yaml_files(args.actions_dir)))
        scope = str(args.workflow_dir)
    if violations:
        for violation in violations:
            location = (
                f"{violation.path}:{violation.line}" if violation.line else str(violation.path)
            )
            print(f"ERROR {location}: {violation.message}")
        return 1
    print(f"PASS: all third-party Actions in {scope} use immutable SHAs with version comments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
