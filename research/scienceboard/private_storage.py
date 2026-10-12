"""Private storage root for raw ScienceBoard evidence (``CUA_PRIVATE_ROOT``).

Raw session bundles, worker specs and state, calibration evidence, VM hash
certificates, private driver configurations and detector datasets can contain
human-derived or otherwise private data. They are written only below the
directory named by the ``CUA_PRIVATE_ROOT`` environment variable.

The rule itself lives in :mod:`computer_use_agent.study.policy` (an absolute
path outside this repository, outside any Git work tree and outside
``research/evidence``; relative paths are taken from the root). This module
binds it to this repository and adds ScienceBoard-specific labels, so every
ScienceBoard entry point applies exactly the same rule as the human-data
policy. There is no drive-letter rule.
"""

from __future__ import annotations

import os
from pathlib import Path

from computer_use_agent.study.policy import ENV_PRIVATE_ROOT, FormalStudyError
from computer_use_agent.study.policy import private_root as _policy_private_root
from computer_use_agent.study.policy import require_private_path as _policy_require_private_path

PRIVATE_ROOT_ENV = ENV_PRIVATE_ROOT
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class PrivateStorageError(FormalStudyError):
    """Raised when a path would place private evidence outside the private root."""


def private_root() -> Path:
    """Return the validated private root (it may not exist yet) or fail closed."""

    try:
        return _policy_private_root(repository_root=REPOSITORY_ROOT)
    except FormalStudyError as error:
        raise PrivateStorageError(str(error)) from error


def require_private_path(path: str | os.PathLike[str], *, label: str = "private path") -> Path:
    """Resolve ``path`` inside ``CUA_PRIVATE_ROOT`` or raise :class:`PrivateStorageError`."""

    try:
        return _policy_require_private_path(path, repository_root=REPOSITORY_ROOT)
    except FormalStudyError as error:
        raise PrivateStorageError(f"{label}: {error}") from error


def is_private_path(path: str | os.PathLike[str] | None) -> bool:
    """Boolean form of :func:`require_private_path` for preflight checks."""

    if path is None:
        return False
    try:
        require_private_path(path)
    except PrivateStorageError:
        return False
    return True


def default_private_path(*parts: str) -> Path:
    """Return ``CUA_PRIVATE_ROOT/<parts>`` (fails closed when the root is unusable)."""

    return private_root().joinpath(*parts)
