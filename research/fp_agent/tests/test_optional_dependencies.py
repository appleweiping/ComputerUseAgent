"""The FP-Agent modules import, and the LOAFO pure-function tests run, without xgboost.

Each check runs in a fresh interpreter in which the named modules are blocked
(``sys.modules[name] = None`` makes ``import name`` raise ImportError), which
simulates an environment without the ``research`` extra.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from research.fp_agent.paths import CHECKOUT_ROOT

LOAFO_TESTS = Path(__file__).with_name("test_leave_one_family_out.py")


def _run_blocked(blocked: tuple[str, ...], lines: list[str]) -> subprocess.CompletedProcess[str]:
    prelude = ["import sys", *(f"sys.modules[{name!r}] = None" for name in blocked)]
    return subprocess.run(
        [sys.executable, "-B", "-c", "\n".join([*prelude, *lines])],
        cwd=CHECKOUT_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


def _pytest_loafo(
    blocked: tuple[str, ...], imports: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    # --color=no: CI sets FORCE_COLOR, whose ANSI codes would split "4 passed, 1 skipped".
    arguments = [
        str(LOAFO_TESTS),
        "-q",
        "-rs",
        "--color=no",
        "-p",
        "no:cacheprovider",
        "-p",
        "no:faulthandler",
    ]
    lines = [f"import {module}" for module in imports]
    lines += ["import pytest", f"raise SystemExit(pytest.main({arguments!r}))"]
    return _run_blocked(blocked, lines)


def test_modules_import_and_loafo_tests_pass_without_xgboost() -> None:
    # The retrospective modules need numpy and scikit-learn (but not xgboost).
    if any(importlib.util.find_spec(name) is None for name in ("numpy", "sklearn")):
        pytest.skip("needs numpy and scikit-learn from the research extra")
    result = _pytest_loafo(
        ("xgboost",),
        imports=(
            "research.fp_agent.leave_one_family_out",
            "research.fp_agent.evaluate_fp_agent_classifiers",
            "research.fp_agent.retrospective_evaluation",
            "research.fp_agent.execute_notebook",
        ),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "5 passed" in result.stdout


def test_loafo_tests_run_without_the_numerical_stack() -> None:
    result = _pytest_loafo(("xgboost", "numpy", "sklearn", "scipy"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed, 1 skipped" in result.stdout
    assert "needs numpy from the research extra" in result.stdout
