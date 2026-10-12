"""Registry of stage-specific provenance validators for detector datasets.

A detector dataset may carry a ``provenance`` object that binds its records to
the immutable evidence of one research stage (for example a formal
ScienceBoard batch). The core package cannot know every stage, and must not
import research code, so stages register a validator here instead:

.. code-block:: python

    from computer_use_agent.detector.provenance import register_provenance_validator

    register_provenance_validator("scienceboard", validate_scienceboard_dataset)

A validator receives the complete dataset payload -- a mapping with
``schema_version``, ``records``, ``audit`` and ``provenance`` -- as a detached
JSON copy. It returns ``None`` to accept the dataset and raises
:class:`ValueError` to reject it. A dataset whose ``provenance.source_stage``
has no registered validator **fails closed**.

Callers that load datasets from files make the validator available by
importing the stage's module first, either directly or with
:func:`load_provenance_modules` (the ``--provenance-module`` option of the
pipeline and of ``computer-use-agent analyze``).

Two optional hooks refine how an accepted dataset is analyzed:

``analysis_provenance(payload)``
    Returns the publication-safe binding embedded in the result bundle as
    ``source_provenance``. Without it the pipeline embeds a generic binding
    (stage, dataset hash and a SHA-256 of the provenance object).
``human_data_policy(payload)``
    Returns the :class:`~computer_use_agent.study.policy.HumanDataPolicy` used
    to re-authorize formal human records inside every evaluation fold.
    Without it formal human records are denied
    (:data:`~computer_use_agent.study.policy.DENY_FORMAL_HUMAN_DATA`);
    accepting provenance never authorizes human-research use by itself.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from computer_use_agent.study.policy import HumanDataPolicy

ProvenanceValidator = Callable[[Mapping[str, Any]], None]
AnalysisProvenanceBuilder = Callable[[Mapping[str, Any]], Mapping[str, Any]]
HumanDataPolicyFactory = Callable[[Mapping[str, Any]], HumanDataPolicy]

GENERIC_ANALYSIS_PROVENANCE_SCHEMA = "cui-detector-provenance-binding-v1"
SOURCE_STAGE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
MODULE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")

__all__ = [
    "GENERIC_ANALYSIS_PROVENANCE_SCHEMA",
    "AnalysisProvenanceBuilder",
    "HumanDataPolicyFactory",
    "ProvenanceError",
    "ProvenanceRegistration",
    "ProvenanceValidator",
    "generic_analysis_provenance",
    "get_provenance_registration",
    "get_provenance_validator",
    "load_provenance_modules",
    "register_provenance_validator",
    "registered_source_stages",
    "require_provenance_registration",
    "source_stage_of",
    "unregister_provenance_validator",
    "validate_dataset_provenance",
]


class ProvenanceError(ValueError):
    """Raised when dataset provenance cannot be validated (fail closed)."""


@dataclass(frozen=True)
class ProvenanceRegistration:
    """One registered stage: its validator and optional analysis hooks."""

    source_stage: str
    validator: ProvenanceValidator
    analysis_provenance: AnalysisProvenanceBuilder | None = None
    human_data_policy: HumanDataPolicyFactory | None = None


_REGISTRY: dict[str, ProvenanceRegistration] = {}
_LOCK = threading.Lock()


def _identity(function: Callable[..., Any] | None) -> tuple[str, str] | None:
    """Identify a callable by its defining source file and qualified name.

    The file (not the module name) is used so that a stage module that is run
    as ``__main__`` and also imported under its package name counts as the
    same validator.
    """

    if function is None:
        return None
    code = getattr(function, "__code__", None)
    origin = getattr(code, "co_filename", None) or str(getattr(function, "__module__", ""))
    name = getattr(function, "__qualname__", None) or type(function).__qualname__
    return (str(origin), str(name))


def _check_stage(source_stage: object) -> str:
    if not isinstance(source_stage, str) or SOURCE_STAGE.fullmatch(source_stage) is None:
        raise ProvenanceError(
            "provenance source_stage must be a lowercase token "
            f"([a-z0-9][a-z0-9_.-]{{0,63}}); got {source_stage!r}"
        )
    return source_stage


def register_provenance_validator(
    source_stage: str,
    validator: ProvenanceValidator,
    *,
    analysis_provenance: AnalysisProvenanceBuilder | None = None,
    human_data_policy: HumanDataPolicyFactory | None = None,
) -> None:
    """Register the validator for datasets whose provenance names ``source_stage``.

    Registering the same functions again (for example when a module is
    re-imported) is a no-op update; registering a *different* validator for
    an already registered stage raises :class:`ProvenanceError`.
    """

    stage = _check_stage(source_stage)
    if not callable(validator):
        raise ProvenanceError("provenance validator must be callable")
    for hook in (analysis_provenance, human_data_policy):
        if hook is not None and not callable(hook):
            raise ProvenanceError("provenance hooks must be callable")
    registration = ProvenanceRegistration(
        source_stage=stage,
        validator=validator,
        analysis_provenance=analysis_provenance,
        human_data_policy=human_data_policy,
    )
    with _LOCK:
        existing = _REGISTRY.get(stage)
        if existing is not None and _identity(existing.validator) != _identity(validator):
            raise ProvenanceError(
                f"a different provenance validator is already registered for {stage!r}"
            )
        _REGISTRY[stage] = registration


def unregister_provenance_validator(source_stage: str) -> None:
    """Remove a registration (used by tests); unknown stages are ignored."""

    with _LOCK:
        _REGISTRY.pop(source_stage, None)


def get_provenance_registration(source_stage: str) -> ProvenanceRegistration | None:
    with _LOCK:
        return _REGISTRY.get(source_stage)


def get_provenance_validator(source_stage: str) -> ProvenanceValidator | None:
    """Return the registered validator for ``source_stage`` or ``None``."""

    registration = get_provenance_registration(source_stage)
    return registration.validator if registration is not None else None


def registered_source_stages() -> tuple[str, ...]:
    with _LOCK:
        return tuple(sorted(_REGISTRY))


def load_provenance_modules(names: Iterable[str]) -> None:
    """Import modules so that they can register their provenance validators."""

    for name in names:
        if not isinstance(name, str) or MODULE_NAME.fullmatch(name) is None:
            raise ProvenanceError(f"invalid provenance module name: {name!r}")
        try:
            importlib.import_module(name)
        except ImportError as error:
            raise ProvenanceError(f"cannot import provenance module {name!r}: {error}") from error


def source_stage_of(provenance: object) -> str:
    """Return the ``source_stage`` declared by a provenance object."""

    if not isinstance(provenance, Mapping):
        raise ProvenanceError("detector dataset provenance must be an object")
    return _check_stage(provenance.get("source_stage"))


def _detached(payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        copy = json.loads(json.dumps(dict(payload), allow_nan=False, sort_keys=True))
    except (TypeError, ValueError) as error:
        raise ProvenanceError(f"detector dataset is not plain JSON: {error}") from error
    if not isinstance(copy, dict):  # pragma: no cover - dict() above guarantees an object
        raise ProvenanceError("detector dataset root must be an object")
    return copy


def require_provenance_registration(provenance: object) -> ProvenanceRegistration:
    """Return the registration for a provenance object's stage; fail closed if absent."""

    stage = source_stage_of(provenance)
    registration = get_provenance_registration(stage)
    if registration is None:
        known = ", ".join(registered_source_stages()) or "none"
        raise ProvenanceError(
            f"no provenance validator is registered for source_stage {stage!r} "
            f"(registered: {known}); datasets with unvalidated provenance are rejected. "
            "Import the stage's module first, e.g. --provenance-module <module>."
        )
    return registration


def validate_dataset_provenance(payload: Mapping[str, Any]) -> ProvenanceRegistration:
    """Run the registered validator for ``payload``; fail closed when none exists."""

    registration = require_provenance_registration(payload.get("provenance"))
    registration.validator(_detached(payload))
    return registration


def generic_analysis_provenance(
    payload: Mapping[str, Any], *, audit: Mapping[str, Any]
) -> dict[str, Any]:
    """Publication-safe binding used when a stage registers no builder."""

    provenance = payload.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ProvenanceError("detector dataset provenance must be an object")
    rendered = json.dumps(
        dict(provenance), allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    return {
        "schema_version": GENERIC_ANALYSIS_PROVENANCE_SCHEMA,
        "source_stage": source_stage_of(provenance),
        "provenance_sha256": hashlib.sha256(rendered).hexdigest(),
        "dataset_sha256": audit["dataset_sha256"],
        "record_count": audit["record_count"],
        "raw_sequences_embedded": False,
    }
