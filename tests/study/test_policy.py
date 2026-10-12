"""Formal-study policy tests.

The first group is data-driven: it reads the shipped
``research/configs/formal-study.json`` and checks the pinned protocol values
(previously ``scripts/validate_formal_study.py``) through a table of JSON
pointers. The code under test only enforces structure and fail-closed
invariants; the expected values live here, next to the data.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from computer_use_agent.study import policy
from computer_use_agent.study.ledger import ledger_payload
from computer_use_agent.study.policy import (
    DENY_FORMAL_HUMAN_DATA,
    DETERMINATION_RECORD_REFERENCE,
    REQUIRED_PROHIBITED_FEATURES,
    ConfigHumanDataPolicy,
    FormalStudyError,
    HumanDataPolicy,
    file_sha256,
    load_binding,
    load_config,
    private_root,
    require_human_research_use,
    require_private_path,
    resolve_config_path,
    snapshot_sha256,
    stage_manifest,
    validate_capsule,
    validate_config,
    validate_human_research_policy,
    validate_ledger_binding_snapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "research" / "configs" / "formal-study.json"

# JSON pointer -> pinned value of the historical (2026-08) formal protocol.
EXPECTED_PINS: dict[str, Any] = {
    "/schema_version": "cui-formal-study-config-v1",
    "/protocol_status": "draft_fail_closed",
    "/budget/currency": "USD",
    "/budget/hard_total_cap": 200.0,
    "/budget/model_entitlement_probe_required": True,
    "/budget/mixed_model_or_provider_fallback_allowed": False,
    "/budget/stop_if_projected_total_exceeds_cap": True,
    "/budget/entitlement_certificate/provider": "openai",
    "/budget/entitlement_certificate/endpoint": "https://api.openai.com/v1/responses",
    "/budget/entitlement_certificate/requested_model": "gpt-5.5",
    "/budget/entitlement_certificate/required_response_model": "gpt-5.5-2026-04-23",
    "/budget/entitlement_certificate/environment_reference": "env:CUI_WEEK1_PROVIDER_CERT",
    "/budget/entitlement_certificate/tools_enabled": False,
    "/week1_miniwob/planned_attempts": 27,
    "/week1_miniwob/manifest": "../week1_miniwob/benchmark_manifest.json",
    "/scienceboard/planned_sessions": 27,
    "/scienceboard/manifest": "../scienceboard/tasks/manifest.json",
    "/week2_human_qa/planned_self_qa_sessions": 3,
    "/week2_human_qa/other_participants_allowed": False,
    "/week2_human_qa/raw_trace_publication_allowed": False,
    "/human_research_policy/determination_status": "not_recorded",
    "/human_research_policy/determination_record_reference": DETERMINATION_RECORD_REFERENCE,
    "/human_research_policy/self_human_formal_research_allowed": False,
    "/human_research_policy/other_participants_formal_research_allowed": False,
    "/human_research_policy/detector_training_allowed": False,
    "/analysis/split": "leave_one_task_out",
}
# matrix pointer -> (dimension names, planned-count pointer, expected size)
EXPECTED_MATRICES = {
    "/week1_miniwob/matrix": (("frameworks", "tasks"), "/week1_miniwob/planned_attempts", 27),
    "/scienceboard/matrix": (("actors", "tasks"), "/scienceboard/planned_sessions", 27),
}


def pointer(document: Any, path: str) -> Any:
    value = document
    for token in path.lstrip("/").split("/"):
        value = value[token.replace("~1", "/").replace("~0", "~")]
    return value


def walk(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


@pytest.fixture(scope="module")
def shipped_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def config(shipped_config: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(shipped_config)


# ------------------------------------------------- data-driven config pins --


@pytest.mark.parametrize(("path", "expected"), sorted(EXPECTED_PINS.items()))
def test_shipped_config_pins(shipped_config, path, expected):
    assert pointer(shipped_config, path) == expected


@pytest.mark.parametrize("matrix_path", sorted(EXPECTED_MATRICES))
def test_shipped_matrices_expand_to_the_planned_count(shipped_config, matrix_path):
    dimensions, planned_path, size = EXPECTED_MATRICES[matrix_path]
    matrix = pointer(shipped_config, matrix_path)
    observed = len(matrix[dimensions[0]]) * len(matrix[dimensions[1]]) * matrix["repetitions"]
    assert observed == pointer(shipped_config, planned_path) == size


def test_shipped_config_is_valid_fail_closed_and_secret_free(shipped_config):
    assert validate_config(shipped_config) == []
    assert validate_human_research_policy(shipped_config["human_research_policy"]) == []
    assert set(shipped_config["analysis"]["prohibited_features"]) >= REQUIRED_PROHIBITED_FEATURES
    references = shipped_config["secrets"]["required_environment_references"]
    assert references and all(item.startswith("env:") for item in references)
    for value in walk(shipped_config):
        if isinstance(value, str):
            assert not value.startswith("sk-")
    rendered = json.dumps(shipped_config).lower()
    for marker in ("umn", "mentor", "d:\\", "d:/", "minnesota", "drive.google", "slack"):
        assert marker not in rendered, marker


def test_shipped_manifests_resolve_relative_to_the_config():
    binding = load_binding(CONFIG_PATH, reference_root=REPO_ROOT)
    assert binding.config_reference == "research/configs/formal-study.json"
    assert stage_manifest(binding, "week1") == (
        REPO_ROOT / "research" / "week1_miniwob" / "benchmark_manifest.json"
    )
    assert stage_manifest(binding, "scienceboard") == (
        REPO_ROOT / "research" / "scienceboard" / "tasks" / "manifest.json"
    )


# ------------------------------------------------------ config validation --


def _set(document: dict[str, Any], path: str, value: Any) -> None:
    *parents, last = path.lstrip("/").split("/")
    target = document
    for token in parents:
        target = target[token]
    if value is _DELETE:
        del target[last]
    else:
        target[last] = value


_DELETE = object()
CONFIG_MUTATIONS = [
    ("/schema_version", "other", "schema_version"),
    ("/budget", None, "budget policy is missing"),
    ("/budget/currency", "usd", "three-letter"),
    ("/budget/hard_total_cap", 0, "hard_total_cap"),
    ("/budget/hard_total_cap", True, "hard_total_cap"),
    ("/budget/model_entitlement_probe_required", False, "entitlement probe"),
    ("/budget/mixed_model_or_provider_fallback_allowed", True, "fallback"),
    ("/budget/stop_if_projected_total_exceeds_cap", False, "stop gate"),
    ("/budget/entitlement_certificate/tools_enabled", True, "tools disabled"),
    ("/budget/entitlement_certificate/endpoint", "http://x", "https URL"),
    ("/budget/entitlement_certificate/maximum_age_hours", 0, "maximum_age_hours"),
    ("/budget/entitlement_certificate/provider", " ", "provider"),
    ("/budget/entitlement_certificate/environment_reference", "CERT", "environment-referenced"),
    ("/budget/entitlement_certificate/extra", 1, "closed schema"),
    ("/week1_miniwob", None, "week1_miniwob policy is missing"),
    ("/week1_miniwob/manifest", "/abs/manifest.json", "relative path"),
    ("/week1_miniwob/manifest", "C:/manifest.json", "relative path"),
    ("/week1_miniwob/planned_attempts", 26, "planned_attempts"),
    ("/scienceboard/matrix/repetitions", 2, "planned_sessions"),
    ("/week2_human_qa/planned_self_qa_sessions", 0, "self-QA"),
    ("/week2_human_qa/other_participants_allowed", True, "fail-closed gates"),
    ("/human_research_policy/detector_training_allowed", True, "fail closed before"),
    ("/human_research_policy/determination_record_reference", "env:OTHER", "reference"),
    ("/human_research_policy/claim_boundary", " ", "claim boundary"),
    ("/human_research_policy/self_consent_confirmation_required", False, "consent"),
    ("/human_research_policy/self_human_engineering_qa_allowed", False, "engineering QA"),
    ("/human_research_policy/determination_status", "maybe", "determination_status"),
    ("/human_research_policy/extra", 1, "closed schema"),
    ("/human_research_policy", None, "human_research_policy is missing"),
    ("/analysis", None, "analysis policy is missing"),
    ("/analysis/split", "random", "leave_one_task_out"),
    ("/analysis/prohibited_features", ["actor"], "prohibited-feature"),
    ("/secrets/required_environment_references", [], "secret references are missing"),
    ("/secrets/required_environment_references", ["OPENAI_API_KEY"], "env:NAME"),
    ("/secrets/required_environment_references", ["env:OPENAI_API_KEY"], "provider certificate"),
    ("/project/research_question", "sk-" + "a" * 30, "inline API key"),
]


@pytest.mark.parametrize(("path", "value", "message"), CONFIG_MUTATIONS)
def test_config_violations_are_reported(config, path, value, message):
    _set(config, path, value)
    errors = validate_config(config)
    assert any(message in error for error in errors), errors


def test_recorded_policy_requires_scope_and_hash(config):
    original = copy.deepcopy(config["human_research_policy"])
    config["human_research_policy"].update(
        {"determination_status": "recorded", "determination_scope": None}
    )
    errors = validate_human_research_policy(config["human_research_policy"])
    assert any("non-empty decision scope" in error for error in errors)
    assert any("private-record SHA-256" in error for error in errors)
    assert validate_human_research_policy("not a dict") == ["human_research_policy is missing"]
    unrecorded = copy.deepcopy(original)
    unrecorded["determination_scope"] = "x"
    assert any("cannot carry" in error for error in validate_human_research_policy(unrecorded))
    unrecorded = copy.deepcopy(original)
    unrecorded["detector_training_allowed"] = "no"
    assert any("explicit booleans" in e for e in validate_human_research_policy(unrecorded))


# ------------------------------------------------------------ config path --


def test_config_path_is_explicit_or_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.delenv(policy.ENV_FORMAL_STUDY_CONFIG, raising=False)
    with pytest.raises(FormalStudyError, match="CUA_FORMAL_STUDY_CONFIG"):
        resolve_config_path()
    assert resolve_config_path(CONFIG_PATH) == CONFIG_PATH.resolve()
    monkeypatch.setenv(policy.ENV_FORMAL_STUDY_CONFIG, str(CONFIG_PATH))
    assert resolve_config_path() == CONFIG_PATH.resolve()
    assert load_config()["schema_version"] == "cui-formal-study-config-v1"
    with pytest.raises(FormalStudyError, match="unavailable"):
        resolve_config_path(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(FormalStudyError, match="could not read"):
        load_config(broken)
    listed = tmp_path / "list.json"
    listed.write_text("[]", encoding="utf-8")
    with pytest.raises(FormalStudyError, match="one object"):
        load_config(listed)
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"schema_version": "x"}), encoding="utf-8")
    with pytest.raises(FormalStudyError, match="schema_version"):
        load_config(invalid)


def test_external_config_copy_validates_like_the_original(tmp_path):
    copied = tmp_path / "formal-study.json"
    copied.write_bytes(CONFIG_PATH.read_bytes())
    binding = load_binding(copied)
    assert binding.config_sha256 == snapshot_sha256(json.loads(CONFIG_PATH.read_text("utf-8")))
    assert binding.config_reference == "formal-study.json"
    # The manifests are resolved relative to the copy, so a copied config
    # cannot silently bind the repository's task manifests.
    with pytest.raises(FormalStudyError, match="manifest is unavailable"):
        stage_manifest(binding, "week1")
    with pytest.raises(FormalStudyError, match="no formal manifest binding"):
        stage_manifest(binding, "week3")


# -------------------------------------------------------- private storage --


@pytest.fixture
def private_dir(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "private"
    root.mkdir()
    monkeypatch.setenv(policy.ENV_PRIVATE_ROOT, str(root))
    return root.resolve()


def test_private_root_rules(monkeypatch, tmp_path):
    monkeypatch.delenv(policy.ENV_PRIVATE_ROOT, raising=False)
    with pytest.raises(FormalStudyError, match="CUA_PRIVATE_ROOT is not set"):
        private_root()
    monkeypatch.setenv(policy.ENV_PRIVATE_ROOT, "relative/private")
    with pytest.raises(FormalStudyError, match="absolute"):
        private_root()
    work_tree = tmp_path / "checkout"
    (work_tree / ".git").mkdir(parents=True)
    monkeypatch.setenv(policy.ENV_PRIVATE_ROOT, str(work_tree / "private"))
    with pytest.raises(FormalStudyError, match="Git work tree"):
        private_root()
    monkeypatch.setenv(policy.ENV_PRIVATE_ROOT, str(tmp_path / "research" / "evidence" / "x"))
    with pytest.raises(FormalStudyError, match="research/evidence"):
        private_root()
    repository = tmp_path / "zip-checkout"
    monkeypatch.setenv(policy.ENV_PRIVATE_ROOT, str(repository / "private"))
    with pytest.raises(FormalStudyError, match="inside the repository"):
        private_root(repository_root=repository)
    with pytest.raises(FormalStudyError, match="inside the repository"):
        policy.check_private_location(REPO_ROOT / "private_data")


def test_private_paths_stay_inside_the_root(private_dir):
    assert private_root() == private_dir
    assert require_private_path("scienceboard/raw") == private_dir / "scienceboard" / "raw"
    assert require_private_path(private_dir / "a") == private_dir / "a"
    with pytest.raises(FormalStudyError, match="inside CUA_PRIVATE_ROOT"):
        require_private_path("../escape")


# ------------------------------------------------------ human-data policy --


def _recorded(config: dict[str, Any], record: Path | None = None, **decisions: bool) -> None:
    config["human_research_policy"].update(
        {
            "determination_status": "recorded",
            "determination_scope": "single self-participant study",
            "determination_record_sha256": file_sha256(record) if record else "e" * 64,
            "self_human_formal_research_allowed": True,
            "detector_training_allowed": True,
            **decisions,
        }
    )


def test_use_and_scope_decisions_fail_closed(config):
    with pytest.raises(FormalStudyError, match="determination is not recorded"):
        require_human_research_use(config, use="detector_training")
    broken = copy.deepcopy(config)
    broken["human_research_policy"]["claim_boundary"] = ""
    with pytest.raises(FormalStudyError, match="claim boundary"):
        require_human_research_use(broken, use="detector_training")
    _recorded(config)
    checks = [
        ({"participant_scope": "approved_multi_participant"}, "other participants"),
        ({"participant_scope": "public_dataset"}, "unsupported formal human participant"),
        ({"use": "marketing"}, "unsupported human-data use"),
    ]
    for kwargs, message in checks:
        arguments = {"use": "detector_training", "verify_private_record": False, **kwargs}
        with pytest.raises(FormalStudyError, match=message):
            require_human_research_use(config, **arguments)
    require_human_research_use(config, use="scienceboard_formal", verify_private_record=False)
    no_self = copy.deepcopy(config)
    no_self["human_research_policy"]["self_human_formal_research_allowed"] = False
    with pytest.raises(FormalStudyError, match="formal self-human research"):
        require_human_research_use(no_self, use="detector_training", verify_private_record=False)
    no_training = copy.deepcopy(config)
    no_training["human_research_policy"]["detector_training_allowed"] = False
    with pytest.raises(FormalStudyError, match="does not allow detector training"):
        require_human_research_use(
            no_training, use="detector_training", verify_private_record=False
        )
    multi = copy.deepcopy(config)
    multi["human_research_policy"]["other_participants_formal_research_allowed"] = True
    require_human_research_use(
        multi,
        use="detector_training",
        participant_scope="approved_multi_participant",
        verify_private_record=False,
    )


def test_private_determination_record_is_bound_by_hash(config, private_dir, monkeypatch):
    record = private_dir / "determination.txt"
    record.write_text("approved fixture determination\n", encoding="utf-8")
    _recorded(config, record)
    monkeypatch.delenv(policy.ENV_DETERMINATION_RECORD, raising=False)
    with pytest.raises(FormalStudyError, match="record path is missing"):
        require_human_research_use(config, use="detector_training")
    monkeypatch.setenv(policy.ENV_DETERMINATION_RECORD, str(record))
    require_human_research_use(config, use="detector_training")
    ConfigHumanDataPolicy(config).authorize(
        use="detector_training", participant_scope="single_self_participant"
    )
    tampered = copy.deepcopy(config)
    tampered["human_research_policy"]["determination_record_sha256"] = "0" * 64
    with pytest.raises(FormalStudyError, match="SHA-256 does not match"):
        require_human_research_use(tampered, use="detector_training")
    monkeypatch.setenv(policy.ENV_DETERMINATION_RECORD, str(private_dir / "missing.txt"))
    with pytest.raises(FormalStudyError, match="unavailable"):
        require_human_research_use(config, use="detector_training")


def test_private_determination_record_location_rules(config, private_dir, tmp_path, monkeypatch):
    outside = tmp_path / "elsewhere" / "determination.txt"
    outside.parent.mkdir()
    outside.write_text("fixture", encoding="utf-8")
    _recorded(config, outside)
    monkeypatch.setenv(policy.ENV_DETERMINATION_RECORD, str(outside))
    with pytest.raises(FormalStudyError, match="must be stored under CUA_PRIVATE_ROOT"):
        require_human_research_use(config, use="detector_training")
    repository_record = Path(__file__).resolve()
    _recorded(config, repository_record)
    monkeypatch.setenv(policy.ENV_DETERMINATION_RECORD, str(repository_record))
    with pytest.raises(FormalStudyError, match="must not be stored inside the repository"):
        require_human_research_use(config, use="detector_training")


def test_human_data_policies(config):
    assert isinstance(DENY_FORMAL_HUMAN_DATA, HumanDataPolicy)
    with pytest.raises(FormalStudyError, match="denied by default"):
        DENY_FORMAL_HUMAN_DATA.authorize(
            use="detector_training", participant_scope="single_self_participant"
        )
    with pytest.raises(FormalStudyError, match="determination is not recorded"):
        ConfigHumanDataPolicy(config).authorize(
            use="detector_training", participant_scope="single_self_participant"
        )
    _recorded(config)
    permissive = ConfigHumanDataPolicy(config, verify_private_record=False)
    assert isinstance(permissive, HumanDataPolicy)
    permissive.authorize(use="detector_training", participant_scope="single_self_participant")


# ------------------------------------------------- run capsule and binding --


def _capsule(root: Path, *, sealed: bool = True) -> tuple[Path, dict[str, Any]]:
    inputs = root / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    pinned = inputs / "pinned.txt"
    pinned.write_text("pinned input\n", encoding="utf-8")
    capsule = {
        "schema_version": "cui-run-capsule-v2",
        "sealed": sealed,
        "capsule_role": "immutable_pre_run_provenance",
        "run_name": "synthetic-run",
        "pins": {"model": "fixture-model"},
        "immutable_inputs": {
            "inputs/pinned.txt": {
                "path": "inputs/pinned.txt",
                "bytes": pinned.stat().st_size,
                "sha256": hashlib.sha256(pinned.read_bytes()).hexdigest(),
            }
        },
        "required_environment_references": ["env:OPENAI_API_KEY", DETERMINATION_RECORD_REFERENCE],
    }
    path = root / "capsule.json"
    path.write_text(json.dumps(capsule), encoding="utf-8")
    return path, capsule


def test_dry_run_binding_needs_no_capsule_and_formal_runs_do():
    binding = load_binding(CONFIG_PATH)
    assert binding.capsule is None and binding.capsule_sha256 is None
    with pytest.raises(FormalStudyError, match="requires a run capsule"):
        binding.ledger_binding()
    with pytest.raises(FormalStudyError, match="archived 2026-08 capsule"):
        load_binding(CONFIG_PATH, require_sealed=True)


def test_sealed_capsule_binding_round_trips_through_a_ledger(tmp_path):
    capsule_path, capsule = _capsule(tmp_path)
    binding = load_binding(
        CONFIG_PATH,
        capsule_path,
        require_sealed=True,
        inputs_root=tmp_path,
        reference_root=tmp_path,
    )
    assert binding.capsule == capsule
    assert binding.capsule_reference == "capsule.json"
    ledger_binding = binding.ledger_binding()
    ledger = ledger_payload(
        stage="scienceboard",
        binding=ledger_binding,
        protocol_sha256="c" * 64,
        planned_ids=["cell-01"],
    )
    assert validate_ledger_binding_snapshot(ledger["binding"]) == []
    assert (
        validate_ledger_binding_snapshot(
            ledger["binding"], require_canonical_config=True, config_path=CONFIG_PATH
        )
        == []
    )


def test_formal_capsule_gates(tmp_path):
    capsule_path, capsule = _capsule(tmp_path, sealed=False)
    load_binding(CONFIG_PATH, capsule_path)  # dry run: structure only
    with pytest.raises(FormalStudyError, match="sealed immutable run capsule"):
        load_binding(CONFIG_PATH, capsule_path, require_sealed=True, inputs_root=tmp_path)
    capsule_path, capsule = _capsule(tmp_path, sealed=True)
    with pytest.raises(FormalStudyError, match="explicit inputs_root"):
        load_binding(CONFIG_PATH, capsule_path, require_sealed=True)
    (tmp_path / "inputs" / "pinned.txt").write_text("drifted\n", encoding="utf-8")
    with pytest.raises(FormalStudyError, match="capsule input drift"):
        load_binding(CONFIG_PATH, capsule_path, require_sealed=True, inputs_root=tmp_path)
    with pytest.raises(FormalStudyError, match="run capsule is unavailable"):
        load_binding(CONFIG_PATH, tmp_path / "missing.json")


CAPSULE_MUTATIONS = [
    ("schema_version", "other", "schema_version"),
    ("sealed", "yes", "explicit boolean"),
    ("capsule_role", "other", "provenance role"),
    ("run_name", "", "run_name"),
    ("result_counts", {}, "post-run mutable"),
    ("pins", {}, "pins are missing"),
    ("pins", {"model": ""}, "non-empty strings"),
    ("immutable_inputs", {}, "immutable_inputs are missing"),
    ("required_environment_references", ["OPENAI"], "env:NAME"),
]


@pytest.mark.parametrize(("field", "value", "message"), CAPSULE_MUTATIONS)
def test_capsule_structure_violations(tmp_path, field, value, message):
    _, capsule = _capsule(tmp_path)
    capsule[field] = value
    errors = validate_capsule(capsule, verify_files=False)
    assert any(message in error for error in errors), errors


def test_capsule_input_descriptors(tmp_path):
    _, capsule = _capsule(tmp_path)
    assert validate_capsule(capsule, verify_files=True, inputs_root=tmp_path) == []
    assert any("explicit inputs_root" in e for e in validate_capsule(capsule))
    descriptor = capsule["immutable_inputs"]["inputs/pinned.txt"]
    cases = {
        "../escape.txt": ({**descriptor, "path": "../escape.txt"}, "escapes inputs_root"),
        "inputs/other.txt": ({**descriptor, "path": "inputs/other.txt"}, "unavailable"),
        "bad-path": ({**descriptor}, "key/path mismatch"),
        "bad-hash": ({**descriptor, "path": "bad-hash", "sha256": "x"}, "invalid SHA-256"),
        "bad-bytes": ({**descriptor, "path": "bad-bytes", "bytes": 0}, "invalid byte count"),
        "not-a-dict": ("x", "descriptor is invalid"),
    }
    for key, (value, message) in cases.items():
        mutated = copy.deepcopy(capsule)
        mutated["immutable_inputs"] = {key: value}
        errors = validate_capsule(mutated, verify_files=True, inputs_root=tmp_path)
        assert any(message in error for error in errors), (key, errors)


def test_ledger_binding_snapshot_rejections(tmp_path, config):
    capsule_path, capsule = _capsule(tmp_path)
    binding = load_binding(CONFIG_PATH, capsule_path).ledger_binding()
    assert validate_ledger_binding_snapshot(binding) == []
    assert validate_ledger_binding_snapshot("x") == [
        "ledger binding does not match the closed formal-study schema"
    ]
    cases = [
        ("formal_config", "C:/abs.json", "safe relative reference"),
        ("formal_config_sha256", "x", "not a SHA-256"),
        ("formal_config_snapshot", None, "snapshot is missing"),
        ("release_capsule_snapshot", None, "snapshot is missing"),
        ("formal_config_sha256", "0" * 64, "hash does not match"),
        ("release_capsule_sha256", "0" * 64, "hash does not match"),
    ]
    for field, value, message in cases:
        mutated = copy.deepcopy(binding)
        mutated[field] = value
        errors = validate_ledger_binding_snapshot(mutated)
        assert any(message in error for error in errors), (field, errors)
    unsealed = copy.deepcopy(binding)
    unsealed["release_capsule_snapshot"]["sealed"] = False
    unsealed["release_capsule_sha256"] = snapshot_sha256(unsealed["release_capsule_snapshot"])
    assert any("sealed" in e for e in validate_ledger_binding_snapshot(unsealed))
    assert validate_ledger_binding_snapshot(unsealed, require_sealed=False) == []
    simulated = copy.deepcopy(binding)
    _recorded(simulated["formal_config_snapshot"])
    simulated["formal_config_sha256"] = snapshot_sha256(simulated["formal_config_snapshot"])
    errors = validate_ledger_binding_snapshot(
        simulated, require_canonical_config=True, config_path=CONFIG_PATH
    )
    assert any("differs from the canonical formal config" in e for e in errors)
    simulated["release_capsule_snapshot"]["required_environment_references"] = ["env:X"]
    simulated["release_capsule_sha256"] = snapshot_sha256(simulated["release_capsule_snapshot"])
    errors = validate_ledger_binding_snapshot(
        simulated, require_canonical_config=True, config_path=CONFIG_PATH
    )
    assert any("human-determination record reference" in e for e in errors)
    errors = validate_ledger_binding_snapshot(
        binding, require_canonical_config=True, config_path=tmp_path / "missing.json"
    )
    assert any("canonical formal config is unavailable" in e for e in errors)
