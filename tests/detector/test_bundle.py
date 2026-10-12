from __future__ import annotations

import hashlib
import json
import zipfile

from computer_use_agent.detector.bundle import verify_bundle, write_bundle
from computer_use_agent.detector.evaluation import EvaluationConfig
from computer_use_agent.detector.pipeline import run_pipeline


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_bundle_is_self_checking_and_byte_deterministic(tmp_path, synthetic_records):
    config = EvaluationConfig(
        epochs=30,
        calibration_epochs=30,
        bootstrap_repetitions=5,
        permutation_repetitions=1,
    )
    result = run_pipeline(synthetic_records, config=config)
    first = tmp_path / "first"
    second = tmp_path / "second"
    hashes = write_bundle(result, first)
    write_bundle(result, second)
    verified = verify_bundle(first)
    assert verified["result.json"] == hashes["result.json"]
    assert _hash(first / "detector-result-bundle.zip") == _hash(
        second / "detector-result-bundle.zip"
    )
    result_payload = json.loads((first / "result.json").read_text(encoding="utf-8"))
    trajectory = first / "trajectory_projection.json"
    projection = json.loads(trajectory.read_text(encoding="utf-8"))
    assert hashes["trajectory_projection.json"] == _hash(trajectory)
    assert result_payload["trajectory_artifact"]["sha256"] == _hash(trajectory)
    assert projection["privacy"]["raw_event_sequences_embedded"] is False
    assert "trajectory_projection.json" in (first / "MANIFEST.sha256").read_text(encoding="ascii")
    with zipfile.ZipFile(first / "detector-result-bundle.zip") as archive:
        assert archive.namelist() == [
            "data_card.md",
            "model_card.md",
            "result.json",
            "trajectory_projection.json",
            "MANIFEST.sha256",
        ]
    assert "Synthetic pipeline check only" in (first / "model_card.md").read_text(encoding="utf-8")
    assert "Hedges-g" in (first / "data_card.md").read_text(encoding="utf-8")


def test_bundle_refuses_to_mix_with_existing_directory(tmp_path, synthetic_records):
    config = EvaluationConfig(
        epochs=20,
        calibration_epochs=20,
        bootstrap_repetitions=2,
        permutation_repetitions=1,
    )
    result = run_pipeline(synthetic_records, config=config)
    destination = tmp_path / "existing"
    destination.mkdir()
    try:
        write_bundle(result, destination)
    except FileExistsError:
        pass
    else:
        raise AssertionError("Existing result directory was overwritten")


def test_bundle_verifier_detects_tampering(tmp_path, synthetic_records):
    config = EvaluationConfig(
        epochs=15,
        calibration_epochs=15,
        bootstrap_repetitions=1,
        permutation_repetitions=1,
    )
    destination = tmp_path / "bundle"
    write_bundle(run_pipeline(synthetic_records, config=config), destination)
    with (destination / "trajectory_projection.json").open("ab") as handle:
        handle.write(b'\n{"events":[]}')
    try:
        verify_bundle(destination)
    except ValueError as error:
        assert "digest mismatch" in str(error)
    else:
        raise AssertionError("Tampered bundle passed verification")
