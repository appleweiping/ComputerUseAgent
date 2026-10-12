"""The installable package never imports research or repository tooling code."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "computer_use_agent"
FORBIDDEN_ROOTS = {"research", "tools", "tests", "scripts", "experiments"}


def _modules_loaded_after(imports: list[str]) -> list[str]:
    code = (
        "import importlib, json, sys\n"
        f"for name in {imports!r}:\n"
        "    importlib.import_module(name)\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    # Run from the repository root, where ``research`` *is* importable, so an
    # accidental import would succeed and be detected.
    completed = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return json.loads(completed.stdout)


def _product_modules() -> list[str]:
    names = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PACKAGE_ROOT.parent).with_suffix("")
        parts = list(relative.parts)
        if "capture" in parts or parts[-1] == "__main__":
            continue
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.append(".".join(parts))
    return names


def test_importing_the_package_and_detector_loads_no_research_module():
    loaded = _modules_loaded_after(["computer_use_agent", "computer_use_agent.detector"])
    leaked = [name for name in loaded if name.split(".")[0] in FORBIDDEN_ROOTS]
    assert leaked == []
    assert "computer_use_agent.detector.provenance" in loaded


def test_importing_every_product_module_loads_no_research_module():
    modules = _product_modules()
    assert "computer_use_agent.detector.pipeline" in modules
    assert "computer_use_agent.cli" in modules
    loaded = _modules_loaded_after(modules)
    leaked = [name for name in loaded if name.split(".")[0] in FORBIDDEN_ROOTS]
    assert leaked == []


def test_no_product_source_references_research_modules():
    offenders = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if "capture" in path.relative_to(PACKAGE_ROOT).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.startswith(("research.", "tools.ci"))
            ):
                names = [node.value]
            offenders.extend(
                f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {name}"
                for name in names
                if name.split(".")[0] in FORBIDDEN_ROOTS
            )
    assert offenders == []
