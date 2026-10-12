"""Scan repository text for credentials without ever printing the matched values.

By default every file Git would track (committed plus untracked, minus
.gitignore) is scanned to EOF, including archived research evidence. Opaque
binary formats are skipped. Pass explicit paths (as pre-commit does) to scan
only those files.

Usage::

    python tools/ci/scan_secrets.py            # whole repository
    python tools/ci/scan_secrets.py FILE ...   # selected files
"""

from __future__ import annotations

import argparse
import math
import re
import subprocess
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BINARY_SUFFIXES = frozenset(
    {
        ".7z",
        ".docx",
        ".gif",
        ".gz",
        ".ico",
        ".jpeg",
        ".jpg",
        ".mp4",
        ".npz",
        ".pdf",
        ".png",
        ".pptx",
        ".pyz",
        ".sqlite",
        ".sqlite3",
        ".tar",
        ".tgz",
        ".webm",
        ".webp",
        ".whl",
        ".xlsx",
        ".zip",
    }
)
# Directories that are never repository content even when they are not ignored.
EXCLUDED_PARTS = frozenset({".git", ".venv", "node_modules", ".nox", "__pycache__"})
PLACEHOLDER_MARKERS = (
    "changeme",
    "example",
    "placeholder",
    "redacted",
    "replace",
    "your-",
    "your_",
)
DIRECT_PATTERNS = (
    ("openai-api-key", re.compile(r"\bsk-(?:proj-|svcacct-|ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("github-fine-grained-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("pypi-token", re.compile(r"\bpypi-AgE[A-Za-z0-9_-]{50,}\b")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    # Capability links grant access to private projects; publish the public URL instead.
    ("osf-view-only-link", re.compile(r"\bview_only=[0-9a-f]{16,}\b")),
)
ASSIGNMENT_RE = re.compile(
    r"(?i)[\"']?\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|passwd)"
    r"\b[\"']?\s*[:=]\s*[\"'](?P<value>[A-Za-z0-9_./+=-]{16,})[\"']"
)


@dataclass(frozen=True)
class SecretFinding:
    rule: str
    path: Path
    line: int


def _entropy(value: str) -> float:
    counts = Counter(value)
    return -sum((count / len(value)) * math.log2(count / len(value)) for count in counts.values())


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def _scan_line(path: Path, line_number: int, line: str) -> list[SecretFinding]:
    findings = [
        SecretFinding(rule, path, line_number)
        for rule, pattern in DIRECT_PATTERNS
        if pattern.search(line)
    ]
    # A provider-specific match is already decisive; do not report the same
    # value again under the generic entropy rule.
    if findings:
        return findings
    assignment = ASSIGNMENT_RE.search(line)
    if assignment is not None:
        value = assignment.group("value")
        if not _is_placeholder(value) and _entropy(value) >= 3.5:
            findings.append(SecretFinding("high-entropy-secret-assignment", path, line_number))
    return findings


def _is_excluded(relative_path: Path) -> bool:
    return any(part in EXCLUDED_PARTS for part in relative_path.parts)


def scan_paths(root: Path, relative_paths: Iterable[Path]) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for relative_path in sorted(set(relative_paths)):
        if _is_excluded(relative_path):
            continue
        path = root / relative_path
        if not path.is_file() or path.suffix.lower() in BINARY_SUFFIXES:
            continue
        with path.open("rb") as binary_handle:
            if b"\x00" in binary_handle.read(8192):
                continue
        # Scan to EOF without a size exemption: large public datasets and
        # citation tables must not make the gate size-dependent. Decoding is
        # tolerant, while known opaque formats are excluded above.
        with path.open("r", encoding="utf-8", errors="replace") as text_handle:
            for line_number, line in enumerate(text_handle, 1):
                findings.extend(_scan_line(relative_path, line_number, line))
    return findings


def git_visible_paths(root: Path = ROOT) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [Path(raw.decode("utf-8")) for raw in result.stdout.split(b"\0") if raw]


def _relative_to_root(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root)
    except ValueError:
        return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", type=Path, help="scan only these files")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.files:
        paths = [_relative_to_root(root, path) for path in args.files]
    else:
        paths = git_visible_paths(root)
    findings = scan_paths(root, paths)
    if findings:
        for finding in findings:
            # Never print the matched text or captured value.
            print(f"ERROR {finding.path.as_posix()}:{finding.line}: possible {finding.rule}")
        return 1
    print(f"PASS: no credential patterns found in {len(paths)} scanned path(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
