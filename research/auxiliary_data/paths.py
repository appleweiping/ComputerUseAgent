"""In-tree locations and path guards for the auxiliary calibration tracks.

Locations are resolved relative to the ``research`` package, which is only
importable from a source checkout. Archived evidence records paths from the
original repository layout (``results/...``, ``experiments/...``); they are
translated here instead of editing the hash-bound evidence bytes.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
RESEARCH_ROOT = PACKAGE_DIR.parent
CHECKOUT_ROOT = RESEARCH_ROOT.parent
EVIDENCE_ROOT = RESEARCH_ROOT / "evidence"
EVIDENCE_DIR = EVIDENCE_ROOT / "auxiliary_data"

LEGACY_PATH_PREFIXES: tuple[tuple[str, str], ...] = (
    ("results/", "research/evidence/"),
    ("experiments/", "research/"),
)


def resolve_recorded_path(recorded: str) -> Path:
    """Resolve a repository-relative path recorded in evidence or a manifest."""

    for old, new in LEGACY_PATH_PREFIXES:
        if recorded.startswith(old):
            recorded = new + recorded[len(old) :]
            break
    return CHECKOUT_ROOT / recorded


def display_path(path: Path) -> str:
    """Checkout-relative POSIX path when inside the checkout, else absolute POSIX."""

    resolved = path.resolve()
    try:
        return resolved.relative_to(CHECKOUT_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def is_evidence_path(path: Path) -> bool:
    return path.resolve().is_relative_to(EVIDENCE_ROOT.resolve())


def refuse_evidence_output(path: Path) -> Path:
    """Fail closed when a rerun would overwrite archived, hash-bound evidence."""

    resolved = path.expanduser().resolve()
    if is_evidence_path(resolved):
        raise ValueError(
            f"refusing to write {resolved}: research/evidence is archival and is never "
            "overwritten by a rerun; write elsewhere and compare against the archived files"
        )
    return resolved
