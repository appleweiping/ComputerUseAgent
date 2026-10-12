"""Fail-closed formal-study policy: config loading, human-data authorization
and private-storage rules.

Nothing in this module derives a location from where the package is
installed:

* The formal-study config is an explicit argument or the
  ``CUA_FORMAL_STUDY_CONFIG`` environment variable. Research callers pass
  ``research/configs/formal-study.json``.
* Private data live under ``CUA_PRIVATE_ROOT``, which must be an absolute
  path outside every Git work tree (including this repository) and outside
  ``research/evidence``. No drive letter is special.
* The private human-research determination record is named by
  ``CUA_HUMAN_RESEARCH_DETERMINATION_RECORD`` and must live under
  ``CUA_PRIVATE_ROOT``; its SHA-256 must match the config.

Expected values (budget caps, matrix sizes, model pins) are data in the
config, not constants in code: this module checks structure and the
fail-closed invariants only. Human data used by the detector go through an
injectable :class:`HumanDataPolicy`; the default denies formal human data.

A formal execution additionally requires an explicit, sealed run capsule.
The repository's archived 2026-08 capsule is historical and unsealable; this
module never reads it on its own.

The config files use JSON syntax (which is also valid YAML) so no YAML parser
is needed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

ENV_FORMAL_STUDY_CONFIG = "CUA_FORMAL_STUDY_CONFIG"
ENV_PRIVATE_ROOT = "CUA_PRIVATE_ROOT"
ENV_DETERMINATION_RECORD = "CUA_HUMAN_RESEARCH_DETERMINATION_RECORD"
DETERMINATION_RECORD_REFERENCE = f"env:{ENV_DETERMINATION_RECORD}"

FORMAL_CONFIG_SCHEMA = "cui-formal-study-config-v1"
HUMAN_POLICY_SCHEMA = "cui-human-research-policy-v1"
RUN_CAPSULE_SCHEMA = "cui-run-capsule-v2"
STAGE_SECTIONS = {"week1": "week1_miniwob", "scienceboard": "scienceboard"}
HUMAN_DATA_USES = frozenset({"detector_training", "scienceboard_formal"})

HEX_SHA256 = re.compile(r"[a-f0-9]{64}")
ENV_REFERENCE = re.compile(r"env:[A-Z][A-Z0-9_]{0,127}")
CURRENCY = re.compile(r"[A-Z]{3}")
REQUIRED_PROHIBITED_FEATURES = {
    "actor",
    "framework",
    "executor",
    "task_id",
    "url",
    "selector",
    "session_uuid",
    "absolute_time",
    "os_version",
    "browser_version",
    "validator_outcome",
    "is_trusted",
    "action_text",
    "task_description",
}
ENTITLEMENT_FIELDS = frozenset(
    {
        "schema_version",
        "environment_reference",
        "provider",
        "endpoint",
        "requested_model",
        "required_response_model",
        "maximum_age_hours",
        "tools_enabled",
    }
)
HUMAN_POLICY_FIELDS = {
    "schema_version",
    "determination_status",
    "determination_scope",
    "determination_record_reference",
    "determination_record_sha256",
    "self_human_engineering_qa_allowed",
    "self_human_formal_research_allowed",
    "other_participants_formal_research_allowed",
    "detector_training_allowed",
    "self_consent_confirmation_required",
    "claim_boundary",
}
LEDGER_BINDING_FIELDS = frozenset(
    {
        "formal_config",
        "formal_config_sha256",
        "release_capsule",
        "release_capsule_sha256",
        "formal_config_snapshot",
        "release_capsule_snapshot",
    }
)


class FormalStudyError(ValueError):
    """Raised when a formal invocation or human-data use is not authorized."""


# --------------------------------------------------------------- utilities --


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_sha256(payload: Mapping[str, Any]) -> str:
    """Hash a parsed safe snapshot independently of JSON whitespace."""

    rendered = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _read_json_yaml(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FormalStudyError(f"could not read {label} {path}: {error}") from error
    if not isinstance(payload, dict):
        raise FormalStudyError(f"{label} must contain one object")
    return payload


def _raise_errors(errors: Iterable[str]) -> None:
    rows = list(errors)
    if rows:
        raise FormalStudyError("; ".join(rows))


def _is_safe_relative_reference(value: object) -> bool:
    """A forward-slash relative path: no drive, no root, no empty segment."""

    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        return False
    if value.startswith("/"):
        return False
    return all(part not in {"", "."} for part in value.split("/"))


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


# ------------------------------------------------------------ config path --


def resolve_config_path(config_path: str | os.PathLike[str] | None = None) -> Path:
    """Return the formal-study config: the explicit path, else ``CUA_FORMAL_STUDY_CONFIG``."""

    if config_path is None or not str(config_path):
        value = os.environ.get(ENV_FORMAL_STUDY_CONFIG, "")
        if not value:
            raise FormalStudyError(
                "no formal-study config: pass the config path explicitly or set "
                f"{ENV_FORMAL_STUDY_CONFIG}"
            )
        config_path = value
    path = Path(config_path).expanduser()
    try:
        return path.resolve(strict=True)
    except OSError as error:
        raise FormalStudyError(f"formal-study config is unavailable: {path}: {error}") from error


def load_config(config_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Read and validate the formal-study config (explicit path or environment)."""

    path = resolve_config_path(config_path)
    config = _read_json_yaml(path, label="formal config")
    _raise_errors(validate_config(config))
    return config


# -------------------------------------------------------- private storage --


def _inside_git_work_tree(path: Path) -> bool:
    return any((candidate / ".git").exists() for candidate in (path, *path.parents))


def _inside_research_evidence(path: Path) -> bool:
    parts = [part.lower() for part in path.parts]
    return any(
        first == "research" and second == "evidence"
        for first, second in zip(parts, parts[1:], strict=False)
    )


def check_private_location(
    path: str | os.PathLike[str],
    *,
    repository_root: str | os.PathLike[str] | None = None,
    label: str = "private path",
) -> Path:
    """Resolve ``path`` and reject repository, Git work-tree and evidence locations."""

    resolved = Path(path).expanduser().resolve()
    if repository_root is not None and resolved.is_relative_to(
        Path(repository_root).expanduser().resolve()
    ):
        raise FormalStudyError(f"{label} must not be stored inside the repository")
    if _inside_git_work_tree(resolved):
        raise FormalStudyError(
            f"{label} must not be stored inside the repository or any other Git work tree"
        )
    if _inside_research_evidence(resolved):
        raise FormalStudyError(f"{label} must not be stored under research/evidence")
    return resolved


def private_root(*, repository_root: str | os.PathLike[str] | None = None) -> Path:
    """Return the validated ``CUA_PRIVATE_ROOT`` directory (it may not exist yet)."""

    value = os.environ.get(ENV_PRIVATE_ROOT, "")
    if not value:
        raise FormalStudyError(
            f"{ENV_PRIVATE_ROOT} is not set; private data need an explicit storage root "
            "outside the repository"
        )
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise FormalStudyError(f"{ENV_PRIVATE_ROOT} must be an absolute path")
    return check_private_location(raw, repository_root=repository_root, label=ENV_PRIVATE_ROOT)


def require_private_path(
    path: str | os.PathLike[str],
    *,
    repository_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve ``path`` inside ``CUA_PRIVATE_ROOT``; relative paths are taken from that root."""

    root = private_root(repository_root=repository_root)
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise FormalStudyError(f"private path must be inside {ENV_PRIVATE_ROOT}")
    return resolved


# ------------------------------------------------------ human-data policy --


def validate_human_research_policy(policy: Any) -> list[str]:
    """Validate the closed, public-safe human-data decision boundary."""

    if not isinstance(policy, dict):
        return ["human_research_policy is missing"]
    errors: list[str] = []
    if set(policy) != HUMAN_POLICY_FIELDS:
        errors.append("human_research_policy does not match the closed schema")
    if policy.get("schema_version") != HUMAN_POLICY_SCHEMA:
        errors.append("human_research_policy has an unexpected schema_version")
    status = policy.get("determination_status")
    if status not in {"not_recorded", "recorded"}:
        errors.append("human-research determination_status is invalid")
    if policy.get("self_human_engineering_qa_allowed") is not True:
        errors.append("private self-human engineering QA must remain explicitly allowed")
    if policy.get("self_consent_confirmation_required") is not True:
        errors.append("self-human capture must require an explicit consent confirmation")
    decision_fields = (
        "self_human_formal_research_allowed",
        "other_participants_formal_research_allowed",
        "detector_training_allowed",
    )
    if any(type(policy.get(field)) is not bool for field in decision_fields):
        errors.append("human-research use decisions must be explicit booleans")
    digest = policy.get("determination_record_sha256")
    scope = policy.get("determination_scope")
    if policy.get("determination_record_reference") != DETERMINATION_RECORD_REFERENCE:
        errors.append(
            "human-research decision record must use the reference "
            f"{DETERMINATION_RECORD_REFERENCE}"
        )
    if status == "not_recorded":
        if digest is not None or scope is not None:
            errors.append("an unrecorded determination cannot carry a record hash or scope")
        if any(policy.get(field) is not False for field in decision_fields):
            errors.append("formal human research/training must fail closed before determination")
    else:
        if not isinstance(scope, str) or not scope.strip():
            errors.append("a recorded determination requires a non-empty decision scope")
        if not isinstance(digest, str) or HEX_SHA256.fullmatch(digest) is None:
            errors.append("a recorded determination requires a bound private-record SHA-256")
    claim_boundary = policy.get("claim_boundary")
    if not isinstance(claim_boundary, str) or not claim_boundary.strip():
        errors.append("human_research_policy requires an explicit claim boundary")
    return errors


def require_human_research_use(
    config: dict[str, Any],
    *,
    use: str,
    participant_scope: str = "single_self_participant",
    verify_private_record: bool = True,
    repository_root: str | os.PathLike[str] | None = None,
) -> None:
    """Fail closed unless the config's recorded policy permits this human-data use."""

    policy = config.get("human_research_policy")
    errors = validate_human_research_policy(policy)
    if errors:
        raise FormalStudyError("; ".join(errors))
    assert isinstance(policy, dict)
    if policy["determination_status"] != "recorded":
        raise FormalStudyError(
            "human-research determination is not recorded; human data are engineering QA only"
        )
    if participant_scope == "approved_multi_participant":
        if policy["other_participants_formal_research_allowed"] is not True:
            raise FormalStudyError("the recorded policy does not allow other participants")
    elif participant_scope != "single_self_participant":
        raise FormalStudyError("unsupported formal human participant scope")
    elif policy["self_human_formal_research_allowed"] is not True:
        raise FormalStudyError("the recorded policy does not allow formal self-human research")
    if use == "detector_training":
        if policy["detector_training_allowed"] is not True:
            raise FormalStudyError("the recorded policy does not allow detector training")
    elif use not in HUMAN_DATA_USES:
        raise FormalStudyError(f"unsupported human-data use: {use}")
    if not verify_private_record:
        return
    reference = str(policy["determination_record_reference"])
    environment_name = reference.removeprefix("env:")
    private_value = os.environ.get(environment_name, "")
    if not private_value:
        raise FormalStudyError(f"private determination record path is missing from {reference}")
    record_path = check_private_location(
        private_value,
        repository_root=repository_root,
        label="private determination record",
    )
    if not record_path.is_file():
        raise FormalStudyError("private determination record is unavailable")
    root = private_root(repository_root=repository_root)
    if not record_path.is_relative_to(root):
        raise FormalStudyError(
            f"private determination record must be stored under {ENV_PRIVATE_ROOT}"
        )
    if file_sha256(record_path) != policy["determination_record_sha256"]:
        raise FormalStudyError("private determination record SHA-256 does not match policy")


@runtime_checkable
class HumanDataPolicy(Protocol):
    """Decides whether formal human data may be used.

    ``authorize`` returns ``None`` to permit the use and raises
    :class:`FormalStudyError` to deny it.
    """

    def authorize(self, *, use: str, participant_scope: str) -> None: ...


@dataclass(frozen=True)
class DenyFormalHumanData:
    """The default policy: formal human data are never authorized."""

    def authorize(self, *, use: str, participant_scope: str) -> None:
        raise FormalStudyError(
            "formal human data are denied by default: no human-research determination is "
            f"bound to this analysis (use={use}, participant_scope={participant_scope}); "
            "supply an explicit HumanDataPolicy"
        )


@dataclass(frozen=True, eq=False)
class ConfigHumanDataPolicy:
    """Authorize through a formal-study config's recorded ``human_research_policy``."""

    config: Mapping[str, Any]
    verify_private_record: bool = True
    repository_root: Path | None = None

    def authorize(self, *, use: str, participant_scope: str) -> None:
        # Detach from caller mutation and reject non-JSON values.
        snapshot = json.loads(json.dumps(dict(self.config), allow_nan=False))
        require_human_research_use(
            snapshot,
            use=use,
            participant_scope=participant_scope,
            verify_private_record=self.verify_private_record,
            repository_root=self.repository_root,
        )


DENY_FORMAL_HUMAN_DATA: HumanDataPolicy = DenyFormalHumanData()


# ------------------------------------------------------ config validation --


def _matrix_size(matrix: Any, first: str, second: str) -> int:
    if not isinstance(matrix, dict):
        return 0
    first_values = matrix.get(first)
    second_values = matrix.get(second)
    repetitions = matrix.get("repetitions")
    if (
        not isinstance(first_values, list)
        or not isinstance(second_values, list)
        or not isinstance(repetitions, int)
        or isinstance(repetitions, bool)
    ):
        return 0
    return len(first_values) * len(second_values) * repetitions


def _budget_errors(budget: object) -> list[str]:
    if not isinstance(budget, dict):
        return ["budget policy is missing"]
    errors: list[str] = []
    currency = budget.get("currency")
    if not isinstance(currency, str) or CURRENCY.fullmatch(currency) is None:
        errors.append("budget currency must be a three-letter code")
    cap = budget.get("hard_total_cap")
    if (
        isinstance(cap, bool)
        or not isinstance(cap, int | float)
        or not math.isfinite(cap)
        or cap <= 0
    ):
        errors.append("formal study must keep a positive, finite hard_total_cap")
    if budget.get("model_entitlement_probe_required") is not True:
        errors.append("model entitlement probe must remain required")
    if budget.get("mixed_model_or_provider_fallback_allowed") is not False:
        errors.append("mixed model/provider fallback must remain disabled")
    if budget.get("stop_if_projected_total_exceeds_cap") is not True:
        errors.append("projected-cost stop gate must remain enabled")
    entitlement = budget.get("entitlement_certificate")
    if not isinstance(entitlement, dict) or set(entitlement) != ENTITLEMENT_FIELDS:
        errors.append("provider entitlement certificate policy does not match the closed schema")
        return errors
    for field in ("schema_version", "provider", "requested_model", "required_response_model"):
        value = entitlement.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"provider entitlement {field} must be a non-empty string")
    reference = entitlement.get("environment_reference")
    if not isinstance(reference, str) or ENV_REFERENCE.fullmatch(reference) is None:
        errors.append("provider entitlement certificate must be environment-referenced")
    endpoint = entitlement.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        errors.append("provider entitlement endpoint must be an https URL")
    if not _positive_int(entitlement.get("maximum_age_hours")):
        errors.append("provider entitlement maximum_age_hours must be a positive integer")
    if entitlement.get("tools_enabled") is not False:
        errors.append("provider entitlement probe must keep tools disabled")
    return errors


def _stage_errors(section: object, *, label: str, first: str, planned_field: str) -> list[str]:
    if not isinstance(section, dict):
        return [f"{label} policy is missing"]
    errors: list[str] = []
    if not _is_safe_relative_reference(section.get("manifest")):
        errors.append(f"{label} manifest must be a relative path (relative to the config file)")
    planned = section.get(planned_field)
    size = _matrix_size(section.get("matrix"), first, "tasks")
    if not _positive_int(planned) or size != planned:
        errors.append(f"{label} matrix must expand to exactly {planned_field}")
    return errors


def validate_config(payload: dict[str, Any]) -> list[str]:
    """Return every structural or fail-closed violation in a formal-study config."""

    errors: list[str] = []
    if payload.get("schema_version") != FORMAL_CONFIG_SCHEMA:
        errors.append("unexpected formal-study schema_version")
    errors.extend(_budget_errors(payload.get("budget")))
    errors.extend(
        _stage_errors(
            payload.get("week1_miniwob"),
            label="week1_miniwob",
            first="frameworks",
            planned_field="planned_attempts",
        )
    )
    errors.extend(
        _stage_errors(
            payload.get("scienceboard"),
            label="scienceboard",
            first="actors",
            planned_field="planned_sessions",
        )
    )
    week2 = payload.get("week2_human_qa")
    if not isinstance(week2, dict) or not _positive_int(week2.get("planned_self_qa_sessions")):
        errors.append("week2_human_qa must plan a positive number of self-QA sessions")
    elif (
        week2.get("other_participants_allowed") is not False
        or week2.get("raw_trace_publication_allowed") is not False
    ):
        errors.append("week2_human_qa participant/privacy fail-closed gates changed")

    errors.extend(validate_human_research_policy(payload.get("human_research_policy")))

    analysis = payload.get("analysis")
    if not isinstance(analysis, dict):
        errors.append("analysis policy is missing")
    else:
        if analysis.get("split") != "leave_one_task_out":
            errors.append("primary analysis split must remain leave_one_task_out")
        prohibited = analysis.get("prohibited_features")
        observed = set(prohibited) if isinstance(prohibited, list) else set()
        missing_features = sorted(REQUIRED_PROHIBITED_FEATURES - observed)
        if missing_features:
            errors.append(
                "analysis prohibited-feature policy is missing: " + ", ".join(missing_features)
            )

    secrets = payload.get("secrets")
    references = (
        secrets.get("required_environment_references") if isinstance(secrets, dict) else None
    )
    if not isinstance(references, list) or not references:
        errors.append("environment-only secret references are missing")
    elif any(
        not isinstance(item, str) or ENV_REFERENCE.fullmatch(item) is None for item in references
    ):
        errors.append("secret references must use env:NAME syntax")
    else:
        budget = payload.get("budget")
        entitlement = budget.get("entitlement_certificate") if isinstance(budget, dict) else None
        reference = (
            entitlement.get("environment_reference") if isinstance(entitlement, dict) else None
        )
        if isinstance(reference, str) and reference not in references:
            errors.append(
                "the provider certificate reference must be a required environment reference"
            )
    if "sk-" in json.dumps(payload, sort_keys=True):
        errors.append("formal config appears to contain an inline API key")
    return errors


# ------------------------------------------------------------ run capsule --


def validate_capsule(
    payload: dict[str, Any],
    *,
    verify_files: bool = True,
    require_sealed: bool = False,
    inputs_root: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Validate a run capsule's structure; optionally re-hash its immutable inputs.

    Pins and the immutable-input list are capsule data, not code. Inputs are
    verified relative to an explicit ``inputs_root``; ``verify_files=True``
    without one is an error.
    """

    errors: list[str] = []
    if payload.get("schema_version") != RUN_CAPSULE_SCHEMA:
        errors.append("unexpected run-capsule schema_version")
    if not isinstance(payload.get("sealed"), bool):
        errors.append("run capsule sealed must be an explicit boolean")
    elif require_sealed and payload.get("sealed") is not True:
        errors.append("formal execution requires a sealed immutable run capsule")
    if payload.get("capsule_role") != "immutable_pre_run_provenance":
        errors.append("run capsule has the wrong provenance role")
    run_name = payload.get("run_name")
    if not isinstance(run_name, str) or not run_name:
        errors.append("run capsule requires a stable run_name")
    forbidden_mutable_fields = sorted(
        {"formal_evidence", "source_commit", "result_counts"}.intersection(payload)
    )
    if forbidden_mutable_fields:
        errors.append(
            "run capsule contains post-run mutable fields: " + ", ".join(forbidden_mutable_fields)
        )
    pins = payload.get("pins")
    if not isinstance(pins, dict) or not pins:
        errors.append("run capsule pins are missing")
    elif any(
        not isinstance(key, str) or not isinstance(value, str) or not value
        for key, value in pins.items()
    ):
        errors.append("run capsule pins must map names to non-empty strings")
    immutable = payload.get("immutable_inputs")
    if not isinstance(immutable, dict) or not immutable:
        errors.append("run capsule immutable_inputs are missing")
        immutable = {}
    root: Path | None = None
    if verify_files:
        if inputs_root is None:
            errors.append("capsule input verification requires an explicit inputs_root")
        else:
            root = Path(inputs_root).expanduser().resolve()
    for key, descriptor in immutable.items():
        if not isinstance(key, str) or not isinstance(descriptor, dict):
            errors.append("run capsule immutable input descriptor is invalid")
            continue
        if descriptor.get("path") != key or not _is_safe_relative_reference(key):
            errors.append(f"capsule input key/path mismatch or unsafe path: {key}")
            continue
        expected_hash = descriptor.get("sha256")
        expected_bytes = descriptor.get("bytes")
        if not isinstance(expected_hash, str) or HEX_SHA256.fullmatch(expected_hash) is None:
            errors.append(f"capsule input has invalid SHA-256: {key}")
            continue
        if not _positive_int(expected_bytes):
            errors.append(f"capsule input has invalid byte count: {key}")
            continue
        if root is None:
            continue
        candidate = (root / key).resolve()
        if not candidate.is_relative_to(root):
            errors.append(f"capsule immutable input escapes inputs_root: {key}")
            continue
        try:
            actual_bytes = candidate.stat().st_size
            actual_hash = file_sha256(candidate)
        except OSError as error:
            errors.append(f"capsule input unavailable: {key}: {error}")
            continue
        if actual_bytes != expected_bytes or actual_hash != expected_hash:
            errors.append(f"capsule input drift: {key}")
    references = payload.get("required_environment_references")
    if not isinstance(references, list) or any(
        not isinstance(item, str) or ENV_REFERENCE.fullmatch(item) is None for item in references
    ):
        errors.append("capsule environment references must use env:NAME syntax")
    return errors


# ---------------------------------------------------------------- binding --


def _reference(path: Path, reference_root: str | os.PathLike[str] | None) -> str:
    if reference_root is not None:
        root = Path(reference_root).expanduser().resolve()
        if path.is_relative_to(root):
            return path.relative_to(root).as_posix()
    return path.name


@dataclass(frozen=True)
class FormalStudyBinding:
    """Validated formal-study inputs used to initialize an immutable run ledger."""

    config: dict[str, Any]
    capsule: dict[str, Any] | None
    config_path: Path
    capsule_path: Path | None
    config_sha256: str
    capsule_sha256: str | None
    config_reference: str = ""
    capsule_reference: str | None = None

    def ledger_binding(self) -> dict[str, Any]:
        if self.capsule is None or self.capsule_sha256 is None:
            raise FormalStudyError("a study ledger binding requires a run capsule snapshot")
        return {
            "formal_config": self.config_reference or self.config_path.name,
            "formal_config_sha256": self.config_sha256,
            "release_capsule": self.capsule_reference
            or (self.capsule_path.name if self.capsule_path else "run-capsule.json"),
            "release_capsule_sha256": self.capsule_sha256,
            # The immutable ledger retains the exact safe inputs, not only
            # pointers to mutable repository files.  This makes provenance
            # review and resume independent of later status/report updates.
            "formal_config_snapshot": json.loads(
                json.dumps(self.config, sort_keys=True, ensure_ascii=True)
            ),
            "release_capsule_snapshot": json.loads(
                json.dumps(self.capsule, sort_keys=True, ensure_ascii=True)
            ),
        }


def load_binding(
    config_path: str | os.PathLike[str] | None = None,
    capsule_path: str | os.PathLike[str] | None = None,
    *,
    verify_capsule_inputs: bool = True,
    require_sealed: bool = False,
    inputs_root: str | os.PathLike[str] | None = None,
    reference_root: str | os.PathLike[str] | None = None,
) -> FormalStudyBinding:
    """Load the formal-study config and an optional run capsule.

    ``config_path`` falls back to ``CUA_FORMAL_STUDY_CONFIG``. With
    ``require_sealed=True`` (a formal execution) the capsule is mandatory, must
    be sealed and, when ``verify_capsule_inputs`` is true, its immutable
    inputs are re-hashed under ``inputs_root``. Dry runs may omit the capsule.
    ``reference_root`` makes the ledger references relative paths.
    """

    resolved_config = resolve_config_path(config_path)
    config = _read_json_yaml(resolved_config, label="formal config")
    _raise_errors(validate_config(config))
    capsule: dict[str, Any] | None = None
    resolved_capsule: Path | None = None
    if capsule_path is None or not str(capsule_path):
        if require_sealed:
            raise FormalStudyError(
                "formal execution requires an explicit sealed run capsule; the archived "
                "2026-08 capsule is historical and cannot be sealed"
            )
    else:
        try:
            resolved_capsule = Path(capsule_path).expanduser().resolve(strict=True)
        except OSError as error:
            raise FormalStudyError(f"run capsule is unavailable: {error}") from error
        capsule = _read_json_yaml(resolved_capsule, label="run capsule")
        verify = verify_capsule_inputs and inputs_root is not None
        errors = validate_capsule(
            capsule,
            verify_files=verify,
            require_sealed=require_sealed,
            inputs_root=inputs_root,
        )
        if require_sealed and verify_capsule_inputs and inputs_root is None:
            errors.append(
                "formal execution must verify capsule inputs under an explicit inputs_root"
            )
        _raise_errors(errors)
    return FormalStudyBinding(
        config=config,
        capsule=capsule,
        config_path=resolved_config,
        capsule_path=resolved_capsule,
        config_sha256=snapshot_sha256(config),
        capsule_sha256=snapshot_sha256(capsule) if capsule is not None else None,
        config_reference=_reference(resolved_config, reference_root),
        capsule_reference=(
            _reference(resolved_capsule, reference_root) if resolved_capsule is not None else None
        ),
    )


def stage_manifest(binding: FormalStudyBinding, stage: str) -> Path:
    """Resolve a stage's task manifest; config paths are relative to the config file."""

    section = STAGE_SECTIONS.get(stage)
    if section is None:
        raise FormalStudyError(f"stage has no formal manifest binding: {stage}")
    policy = binding.config.get(section)
    relative = policy.get("manifest") if isinstance(policy, dict) else None
    if not _is_safe_relative_reference(relative):
        raise FormalStudyError(f"{stage} config manifest is not a safe relative path")
    assert isinstance(relative, str)
    try:
        return (binding.config_path.parent / relative).resolve(strict=True)
    except OSError as error:
        raise FormalStudyError(f"{stage} config manifest is unavailable: {error}") from error


def validate_ledger_binding_snapshot(
    binding: Any,
    *,
    require_sealed: bool = True,
    require_canonical_config: bool = False,
    config_path: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Validate the config and capsule snapshots embedded in an immutable ledger.

    The snapshots must validate on their own and match their recorded
    hashes. ``require_canonical_config`` additionally requires the config
    snapshot to equal the current config (``config_path`` or
    ``CUA_FORMAL_STUDY_CONFIG``), so a simulated policy cannot become a claim.
    """

    if not isinstance(binding, dict) or set(binding) != LEDGER_BINDING_FIELDS:
        return ["ledger binding does not match the closed formal-study schema"]
    errors: list[str] = []
    for field in ("formal_config", "release_capsule"):
        if not _is_safe_relative_reference(binding.get(field)):
            errors.append(f"ledger binding {field} is not a safe relative reference")
    for field in ("formal_config_sha256", "release_capsule_sha256"):
        value = binding.get(field)
        if not isinstance(value, str) or HEX_SHA256.fullmatch(value) is None:
            errors.append(f"ledger binding {field} is not a SHA-256")
    config = binding.get("formal_config_snapshot")
    capsule = binding.get("release_capsule_snapshot")
    if not isinstance(config, dict):
        errors.append("ledger formal-config snapshot is missing")
    else:
        errors.extend(f"ledger config: {item}" for item in validate_config(config))
        if binding.get("formal_config_sha256") != snapshot_sha256(config):
            errors.append("ledger formal-config hash does not match its embedded snapshot")
        if require_canonical_config:
            try:
                canonical_config = _read_json_yaml(
                    resolve_config_path(config_path), label="formal config"
                )
            except FormalStudyError as error:
                errors.append(f"canonical formal config is unavailable: {error}")
            else:
                if config != canonical_config:
                    errors.append(
                        "ledger formal-config snapshot differs from the canonical formal config"
                    )
    if not isinstance(capsule, dict):
        errors.append("ledger release-capsule snapshot is missing")
    else:
        errors.extend(
            f"ledger capsule: {item}"
            for item in validate_capsule(capsule, verify_files=False, require_sealed=require_sealed)
        )
        if binding.get("release_capsule_sha256") != snapshot_sha256(capsule):
            errors.append("ledger release-capsule hash does not match its embedded snapshot")
        policy = config.get("human_research_policy") if isinstance(config, dict) else None
        if (
            require_canonical_config
            and isinstance(policy, dict)
            and policy.get("determination_status") == "recorded"
        ):
            references = capsule.get("required_environment_references")
            if (
                not isinstance(references, list)
                or policy.get("determination_record_reference") not in references
            ):
                errors.append(
                    "ledger capsule does not retain the private human-determination "
                    "record reference"
                )
    return errors
