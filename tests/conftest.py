"""Shared pytest configuration for the product test suite.

* Hypothesis never writes into the repository: the example database is
  disabled (``database=None``) and its other caches (constants, Unicode
  tables) go to ``HYPOTHESIS_STORAGE_DIRECTORY`` or, when that is unset, a
  directory under the system temporary directory.
* ``synthetic_records`` provides the deterministic detector fixture from
  :mod:`tests.fixtures.synthetic`.
* Provenance registrations made by a test are removed afterwards.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import settings
from hypothesis.configuration import set_hypothesis_home_dir

from computer_use_agent.detector import provenance
from tests.fixtures.synthetic import make_records

if not os.environ.get("HYPOTHESIS_STORAGE_DIRECTORY"):
    set_hypothesis_home_dir(Path(tempfile.gettempdir()) / "computer-use-agent-hypothesis")
settings.register_profile("computer-use-agent", database=None, print_blob=True)
settings.load_profile("computer-use-agent")


@pytest.fixture
def synthetic_records() -> list[dict[str, Any]]:
    return make_records()


@pytest.fixture
def isolated_provenance_registry() -> Iterator[None]:
    """Snapshot and restore the process-wide provenance registry."""

    saved = dict(provenance._REGISTRY)
    try:
        yield
    finally:
        with provenance._LOCK:
            provenance._REGISTRY.clear()
            provenance._REGISTRY.update(saved)
