"""The checked-in synthetic example dataset stays in sync with its generator."""

from __future__ import annotations

from computer_use_agent.detector.contract import canonical_json
from computer_use_agent.detector.pipeline import load_dataset_payload
from tests.fixtures.synthetic import EXAMPLE_DATASET, synthetic_dataset_payload


def test_example_dataset_is_the_generator_output():
    assert EXAMPLE_DATASET.read_bytes() == canonical_json(synthetic_dataset_payload()), (
        "regenerate with: python -m tests.fixtures.synthetic "
        "examples/detector/synthetic-dataset.json"
    )
    records, provenance = load_dataset_payload(EXAMPLE_DATASET)
    assert len(records) == 18 and provenance is None
