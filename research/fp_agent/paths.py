"""Locations, provenance constants and path guards for the FP-Agent analyses.

In-tree locations are resolved relative to the ``research`` package, which is
only importable from a source checkout. Private inputs are never guessed from
the checkout: the restricted FP-Agent corpus lives under an explicit data root
(``--data-root`` or ``$CUA_PRIVATE_ROOT/fp_agent``) and the upstream FP-Agent
clone is passed with ``--upstream-repo`` (or ``$CUA_FP_AGENT_UPSTREAM_REPO``).
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
RESEARCH_ROOT = PACKAGE_DIR.parent
CHECKOUT_ROOT = RESEARCH_ROOT.parent
EVIDENCE_ROOT = RESEARCH_ROOT / "evidence"
EVIDENCE_DIR = EVIDENCE_ROOT / "fp_agent"

PRIVATE_ROOT_ENV = "CUA_PRIVATE_ROOT"
UPSTREAM_REPO_ENV = "CUA_FP_AGENT_UPSTREAM_REPO"
PRIVATE_SUBDIRECTORY = "fp_agent"
OUTPUT_SUBDIRECTORY = "outputs"

# Public identifiers written into new outputs. The OSF node is reached through
# the share link published in the upstream README; that link is read at run
# time only and is never written to an output, a ledger or a log.
OSF_PROJECT_URL = "https://osf.io/j6b5p/"
UPSTREAM_REPOSITORY_URL = "https://github.com/ethanbwang/fp-agent"
USER_AGENT = (
    "ComputerUseAgent-research/fp-agent (+https://github.com/appleweiping/ComputerUseAgent)"
)

# Archived evidence records paths from the original repository layout. They are
# translated here instead of editing the hash-bound evidence bytes.
LEGACY_PATH_PREFIXES: tuple[tuple[str, str], ...] = (
    ("results/", "research/evidence/"),
    ("experiments/", "research/"),
)


class PrivatePathError(ValueError):
    """A private input or an output location violates the storage policy."""


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


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
    return _inside(path.expanduser().resolve(), EVIDENCE_ROOT.resolve())


def default_data_root() -> Path | None:
    """Return ``$CUA_PRIVATE_ROOT/fp_agent`` when the private root is configured."""

    value = os.environ.get(PRIVATE_ROOT_ENV, "").strip()
    return Path(value) / PRIVATE_SUBDIRECTORY if value else None


def default_upstream_repo() -> Path | None:
    value = os.environ.get(UPSTREAM_REPO_ENV, "").strip()
    return Path(value) if value else None


def require_data_root(value: Path | None) -> Path:
    """Resolve the private FP-Agent data root and refuse in-repository locations."""

    candidate = value if value is not None else default_data_root()
    if candidate is None:
        raise PrivatePathError(
            f"no FP-Agent data root: pass --data-root or set {PRIVATE_ROOT_ENV} "
            f"(the corpus is then read from ${PRIVATE_ROOT_ENV}/{PRIVATE_SUBDIRECTORY})"
        )
    resolved = candidate.expanduser().resolve()
    if _inside(resolved, CHECKOUT_ROOT.resolve()):
        raise PrivatePathError(
            "the FP-Agent data root must resolve outside the repository checkout; "
            "restricted third-party rows and derived row-level caches never live in the tree"
        )
    return resolved


def require_upstream_repo(value: Path | None) -> Path:
    """Resolve the upstream FP-Agent checkout (a public GitHub clone)."""

    candidate = value if value is not None else default_upstream_repo()
    if candidate is None:
        raise PrivatePathError(
            f"no upstream FP-Agent checkout: pass --upstream-repo or set {UPSTREAM_REPO_ENV} "
            f"(clone {UPSTREAM_REPOSITORY_URL})"
        )
    return candidate.expanduser().resolve()


def refuse_evidence_output(path: Path) -> Path:
    """Fail closed when a rerun would overwrite archived, hash-bound evidence."""

    resolved = path.expanduser().resolve()
    if is_evidence_path(resolved):
        raise PrivatePathError(
            f"refusing to write {resolved}: research/evidence is archival and is never "
            "overwritten by a rerun; write elsewhere and compare against the archived file"
        )
    return resolved


def default_output_dir(data_root: Path) -> Path:
    return data_root / OUTPUT_SUBDIRECTORY
