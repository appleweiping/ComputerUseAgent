"""Invariants of the repository's CI, packaging and governance configuration.

actionlint checks workflow syntax in CI; these tests pin the policy decisions
(least privilege, timeouts, no repository-name guards, required jobs, matching
tool pins) so that a later edit cannot silently drop them.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is part of the test dependency group")

REPOSITORY = Path(__file__).resolve().parents[2]
WORKFLOWS = REPOSITORY / ".github" / "workflows"
PYPROJECT = tomllib.loads((REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"))


def _load(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), path
    return payload


def _workflow(name: str) -> dict[str, Any]:
    return _load(WORKFLOWS / name)


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # YAML 1.1 parses the bare key `on` as boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    return triggers


def _steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow["jobs"].values() for step in job.get("steps", [])]


WORKFLOW_FILES = sorted(WORKFLOWS.glob("*.yml"))


def test_expected_workflows_exist() -> None:
    assert {path.name for path in WORKFLOW_FILES} >= {
        "ci.yml",
        "e2e.yml",
        "codeql.yml",
        "audit.yml",
        "release.yml",
    }


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda path: path.name)
def test_workflow_is_least_privilege_and_bounded(path: Path) -> None:
    workflow = _load(path)
    text = path.read_text(encoding="utf-8")
    assert workflow.get("permissions") == {"contents": "read"}, "top-level permissions"
    assert "concurrency" in workflow
    assert "github.repository ==" not in text and "github.repository !=" not in text
    assert "pull_request_target" not in _triggers(workflow)
    for job_id, job in workflow["jobs"].items():
        assert isinstance(job.get("timeout-minutes"), int), f"{job_id} has no timeout"
        assert "write-all" not in str(job.get("permissions", ""))
    for step in _steps(workflow):
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            assert step.get("with", {}).get("persist-credentials") is False


def test_ci_has_the_required_jobs_and_matrix() -> None:
    workflow = _workflow("ci.yml")
    jobs = workflow["jobs"]
    assert set(jobs) >= {
        "lint",
        "typecheck",
        "test-python",
        "test-research",
        "test-node",
        "evidence",
        "docs",
        "build",
    }
    matrix = jobs["test-python"]["strategy"]["matrix"]
    assert set(matrix["os"]) == {"ubuntu-latest", "windows-latest"}
    assert set(matrix["python"]) == {"3.12", "3.13"}
    assert set(jobs["test-node"]["strategy"]["matrix"]["os"]) == {"ubuntu-latest", "windows-latest"}
    commands = "\n".join(str(step.get("run", "")) for step in _steps(workflow))
    for expected in (
        "tools/ci/check_actions_pinned.py",
        "tools/ci/scan_secrets.py",
        "tools/ci/check_versions.py",
        "tools/ci/verify_evidence.py",
        "tools/ci/check_readme.py",
        "tools/ci/supply_chain.py --check",
        "actionlint",
        "coverage run -m pytest",
        "npm run test:coverage",
        "npm run lint",
        "uv build",
    ):
        assert expected in commands, expected


def test_e2e_runs_headful_under_xvfb_and_validates_the_bundle() -> None:
    workflow = _workflow("e2e.yml")
    steps = workflow["jobs"]["e2e-linux"]["steps"]
    run_step = next(step for step in steps if "npm run e2e" in str(step.get("run", "")))
    assert run_step["run"].startswith("xvfb-run -a")
    assert run_step["env"]["CUA_E2E_CI_NO_SANDBOX"] == "1"
    assert run_step["env"]["CUA_E2E_RESULT_DIR"] == "${{ runner.temp }}/e2e"
    commands = "\n".join(str(step.get("run", "")) for step in steps)
    assert "tools/ci/validate_e2e_bundle.py" in commands
    assert any(str(step.get("uses", "")).startswith("actions/upload-artifact@") for step in steps)


def test_codeql_covers_python_javascript_and_actions() -> None:
    job = _workflow("codeql.yml")["jobs"]["analyze"]
    assert set(job["strategy"]["matrix"]["language"]) == {
        "python",
        "javascript-typescript",
        "actions",
    }
    assert job["permissions"]["security-events"] == "write"


def test_release_is_tag_driven_with_gated_pypi() -> None:
    workflow = _workflow("release.yml")
    assert _triggers(workflow)["push"]["tags"] == ["v*.*.*"]
    jobs = workflow["jobs"]
    assert "vars.PUBLISH_TO_PYPI == 'true'" in jobs["pypi"]["if"]
    # A manual run started from a tag also has ref_type == 'tag'; only a tag
    # push may attest, draft a release or publish, so workflow_dispatch is
    # always a dry run.
    for job_id in ("github-release", "pypi"):
        condition = " ".join(jobs[job_id]["if"].split())
        assert "github.event_name == 'push' && github.ref_type == 'tag'" in condition, job_id
    assert "if" not in jobs["build"]
    assert jobs["pypi"]["permissions"] == {"id-token": "write"}
    assert jobs["github-release"]["permissions"]["attestations"] == "write"
    commands = "\n".join(str(step.get("run", "")) for step in _steps(workflow))
    for expected in (
        "check_versions.py --tag",
        "uv build",
        "supply_chain.py --write",
        "SHA256SUMS",
        "--draft",
    ):
        assert expected in commands, expected


def test_dependabot_covers_uv_npm_and_actions() -> None:
    config = _load(REPOSITORY / ".github" / "dependabot.yml")
    assert config["version"] == 2
    ecosystems = {(item["package-ecosystem"], item["directory"]) for item in config["updates"]}
    assert ecosystems == {("uv", "/"), ("npm", "/extension"), ("github-actions", "/")}
    assert all(item["schedule"]["interval"] == "weekly" for item in config["updates"])
    # ruff is also pinned as the ruff-pre-commit rev, which no configured
    # ecosystem updates; a uv-only bump would break
    # test_pre_commit_pins_match_pyproject_and_exclude_evidence.
    uv = next(item for item in config["updates"] if item["package-ecosystem"] == "uv")
    assert {"dependency-name": "ruff"} in uv.get("ignore", [])


def _cache_enabled(value: object) -> bool:
    # setup-uv's enable-cache defaults to "auto" (on for GitHub-hosted runners).
    return str(value if value is not None else "auto").lower() != "false"


def test_ci_caches_are_keyed_per_job_and_saved_only_by_jobs_that_install() -> None:
    # setup-uv and setup-node key their caches by OS, Python and lockfile hash
    # only, and restore exact matches only, across workflows. A job that shares
    # a key with another job restores that job's cache and never saves its own,
    # so the first job to finish decides the cache for all of them.
    uv_keys: dict[tuple[str, str], str] = {}
    for path in WORKFLOW_FILES:
        workflow = _load(path)
        for job_id, job in workflow["jobs"].items():
            steps = job.get("steps", [])
            commands = "\n".join(str(step.get("run", "")) for step in steps)
            for step in steps:
                uses = str(step.get("uses", ""))
                options = step.get("with", {})
                where = f"{path.name}:{job_id}"
                if uses.startswith("astral-sh/setup-uv@") and _cache_enabled(
                    options.get("enable-cache")
                ):
                    assert "uv sync" in commands, f"{where} caches uv but installs nothing"
                    assert options.get("cache-suffix") == "${{ github.job }}", where
                    python = str(options.get("python-version", ""))
                    key = (job_id, python)
                    assert key not in uv_keys, f"{where} shares a uv cache key with {uv_keys[key]}"
                    uv_keys[key] = where
                if uses.startswith("actions/setup-node@") and options.get("cache"):
                    npm_ci = re.search(r"\bnpm (?:--prefix \S+ )?ci\b", commands)
                    assert npm_ci is not None, f"{where} caches npm but never runs npm ci"
    evidence = _workflow("ci.yml")["jobs"]["evidence"]
    setup_uv = next(
        step
        for step in evidence["steps"]
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
    )
    assert setup_uv["with"]["enable-cache"] is False


def test_research_npm_audit_runs_after_the_failing_pip_audit() -> None:
    # The frozen research Python locks carry known advisories, so the pip-audit
    # step always fails; the npm steps must not be skipped because of it.
    job = _workflow("audit.yml")["jobs"]["research-locks"]
    assert job["continue-on-error"] is True
    steps = job["steps"]
    pip_audit = next(i for i, step in enumerate(steps) if "pip-audit" in str(step.get("run", "")))
    later = steps[pip_audit + 1 :]
    assert any("npm audit" in str(step.get("run", "")) for step in later)
    for step in later:
        assert step.get("if") == "${{ !cancelled() }}", step.get("name")


def test_sdist_ships_no_repository_only_trees() -> None:
    sdist = PYPROJECT["tool"]["hatch"]["build"]["targets"]["sdist"]
    # tests/ needs a checkout (tools/, research/, extension/, .github/), so a
    # shipped suite would fail from the unpacked sdist.
    for tree in ("/tests", "/tools", "/research", "/extension", "/.github"):
        assert tree not in sdist["include"], tree
    build = _workflow("ci.yml")["jobs"]["build"]
    contents = next(step for step in build["steps"] if step["name"] == "Wheel and sdist contents")
    for tree in ("research", "extension", "tests", "tools"):
        assert f'["{tree}"]' in contents["run"], tree


def test_nox_gates_fail_rather_than_skip_when_their_script_is_missing() -> None:
    tree = ast.parse((REPOSITORY / "noxfile.py").read_text(encoding="utf-8"))
    sessions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in {"evidence", "docs"}
    }
    assert set(sessions) == {"evidence", "docs"}
    for name, function in sessions.items():
        calls = [
            node.func.attr
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        assert "skip" not in calls, name


def test_issue_templates_route_vulnerabilities_privately() -> None:
    config = _load(REPOSITORY / ".github" / "ISSUE_TEMPLATE" / "config.yml")
    assert config["blank_issues_enabled"] is False
    assert any("security/advisories/new" in link["url"] for link in config["contact_links"])
    for name in ("bug_report.yml", "feature_request.yml"):
        template = _load(REPOSITORY / ".github" / "ISSUE_TEMPLATE" / name)
        assert template["body"], name


def test_pre_commit_pins_match_pyproject_and_exclude_evidence() -> None:
    config = _load(REPOSITORY / ".pre-commit-config.yaml")
    exclude = config["exclude"]
    for path in (
        "research/evidence/fp_agent/x.json",
        "research/literature/generated/citation_qa.json",
        "research/scienceboard/runtime_compat/provenance.json",
    ):
        assert re.match(exclude, path, re.VERBOSE), path
    assert not re.match(exclude, "src/computer_use_agent/cli.py", re.VERBOSE)
    remote = [repo for repo in config["repos"] if repo["repo"] != "local"]
    assert all(re.fullmatch(r"[0-9a-f]{40}", repo["rev"]) for repo in remote)
    text = (REPOSITORY / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    ruff_tag = re.search(r"ruff-pre-commit\n\s+rev: [0-9a-f]{40} # frozen: v(?P<v>[\d.]+)", text)
    assert ruff_tag is not None
    lint = PYPROJECT["dependency-groups"]["lint"]
    assert f"ruff=={ruff_tag.group('v')}" in lint


def test_pyproject_contract() -> None:
    project = PYPROJECT["project"]
    assert project["name"] == "computer-use-agent"
    assert project["license"] == "MIT"
    assert project["scripts"] == {"computer-use-agent": "computer_use_agent.cli:main"}
    assert project["urls"]["Repository"] == "https://github.com/appleweiping/ComputerUseAgent"
    assert set(project["optional-dependencies"]) == {"x11", "research", "notebooks"}
    lint = PYPROJECT["dependency-groups"]["lint"]
    assert all("==" in requirement for requirement in lint)
    pytest_options = PYPROJECT["tool"]["pytest"]["ini_options"]
    markers = {marker.split(":", 1)[0] for marker in pytest_options["markers"]}
    assert markers == {
        "slow",
        "requires_private_data",
        "requires_large_artifacts",
        "requires_network",
        "requires_x11",
    }
    coverage = PYPROJECT["tool"]["coverage"]
    assert coverage["run"]["branch"] is True
    assert coverage["report"]["fail_under"] >= 75
    assert PYPROJECT["tool"]["mypy"]["strict"] is True


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_gitignore_protects_secrets_without_hiding_sources() -> None:
    def ignored(path: str) -> bool:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", path],
            cwd=REPOSITORY,
            capture_output=True,
            check=False,
        )
        if result.returncode not in (0, 1):
            pytest.skip("not a git checkout")
        return result.returncode == 0

    for path in (
        ".env",
        ".env.local",
        "deploy.pem",
        "id_rsa",
        "credentials.json",
        "dist/x.whl",
        # The README quick start writes its result bundle to out/run1.
        "out/run1/result.json",
    ):
        assert ignored(path), path
    for path in (
        ".env.example",
        "tests/tools/test_scan_secrets.py",
        "tools/ci/scan_secrets.py",
        "docs/secret-handling.md",
        "src/computer_use_agent/credentials_policy.py",
        # /out/ is anchored to the root: a nested `out` package stays visible.
        "src/computer_use_agent/out/__init__.py",
    ):
        assert not ignored(path), path
