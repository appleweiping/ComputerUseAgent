"""Task runner for ComputerUseAgent (works the same on Windows and Linux).

    uv run nox            # default sessions: lint, typecheck, tests, node
    uv run nox -l         # list every session
    uv run nox -s fmt     # auto-format and auto-fix (ruff)
    uv run nox -s tests-3.13 -- -k trace   # extra pytest arguments after --

Python sessions install exactly what ``uv.lock`` pins (``uv sync --locked``)
into the session's own virtualenv; they never touch the project ``.venv``.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import nox

ROOT = Path(__file__).resolve().parent
EXTENSION = ROOT / "extension"
PYTHON = "3.12"
PYTHONS = ["3.12", "3.13"]
RESEARCH_MARKERS = (
    "not requires_private_data and not requires_large_artifacts and not requires_network"
)

nox.needs_version = ">=2025.2.9"
nox.options.default_venv_backend = "uv"
nox.options.sessions = ["lint", "typecheck", "tests", "node"]
nox.options.reuse_existing_virtualenvs = True


def _sync(
    session: nox.Session, *groups: str, extras: tuple[str, ...] = (), project: bool = True
) -> None:
    """Install the locked dependency groups (and optionally the project) into the session venv."""

    args = [
        "uv",
        "sync",
        "--locked",
        "--no-default-groups",
        f"--python={session.virtualenv.location}",
    ]
    if not project:
        args.append("--no-install-project")
    for group in groups:
        args.append(f"--group={group}")
    for extra in extras:
        args.append(f"--extra={extra}")
    session.run_install(*args, env={"UV_PROJECT_ENVIRONMENT": session.virtualenv.location})


def _npm(session: nox.Session, *args: str) -> None:
    npm = shutil.which("npm")
    if npm is None:
        session.error(
            "npm was not found on PATH; install Node.js >= 22.13 "
            "(ESLint 10 needs it; >= 22.12 is enough for the tests and E2E)"
        )
    session.run(npm, "--prefix", str(EXTENSION), *args, external=True)


def _npm_ci(session: nox.Session) -> None:
    if not (EXTENSION / "node_modules").is_dir() or "--fresh" in session.posargs:
        _npm(session, "ci", "--no-audit", "--no-fund")


@nox.session(python=PYTHON)
def lint(session: nox.Session) -> None:
    """ruff check, ruff format --check, pinned Actions and the secret scan."""

    _sync(session, "lint", project=False)
    session.run("ruff", "check", ".")
    session.run("ruff", "format", "--check", ".")
    session.run("python", "tools/ci/check_actions_pinned.py")
    session.run("python", "tools/ci/scan_secrets.py")


@nox.session(python=PYTHON)
def fmt(session: nox.Session) -> None:
    """Apply ruff's safe fixes and format (research/ is linted but never reformatted)."""

    _sync(session, "lint", project=False)
    session.run("ruff", "check", "--fix", ".")
    session.run("ruff", "format", ".")


@nox.session(python=PYTHON)
def typecheck(session: nox.Session) -> None:
    """mypy --strict over src/computer_use_agent (configured in pyproject.toml)."""

    _sync(session, "lint", "test")
    session.run("mypy", *session.posargs)


@nox.session(python=PYTHONS)
def tests(session: nox.Session) -> None:
    """Product tests with branch coverage.

    fail_under = 75 applies to the full 3.12 run; the gate is skipped (the report
    is still printed) when pytest arguments select a subset.
    """

    _sync(session, "test")
    if session.python == PYTHON:
        session.run("coverage", "run", "-m", "pytest", *session.posargs)
        if session.posargs:
            session.run("coverage", "report", "--fail-under=0")
        else:
            session.run("coverage", "report")
            session.run("coverage", "xml")
    else:
        session.run("pytest", *session.posargs)


@nox.session(python=PYTHON)
def research(session: nox.Session) -> None:
    """Research-area tests that need only public data (private/large/network tests deselected)."""

    _sync(session, "test", extras=("research",))
    session.run("pytest", "research", "-m", RESEARCH_MARKERS, *session.posargs)
    # Hash bindings of the notebook sources, results and figures, and the
    # retrospective policy config (standard library only, no executed notebook).
    session.run("python", "-B", "-m", "research.fp_agent.execute_notebook", "--check")
    session.run("python", "-B", "-m", "research.auxiliary_data.execute_notebook", "--check")
    session.run("python", "-B", "-m", "research.fp_agent.validate_config")
    node = shutil.which("node")
    if node is None:
        session.warn("node not found; skipping research/scientific_simulators/*.test.mjs")
        return
    tests = sorted(
        str(path) for path in (ROOT / "research" / "scientific_simulators").glob("*.test.mjs")
    )
    if tests:
        session.run(node, "--test", *tests, external=True)


@nox.session(python=False)
def node(session: nox.Session) -> None:
    """Extension: npm ci, eslint, unit tests, coverage gate and version check."""

    session.env.setdefault("PUPPETEER_SKIP_DOWNLOAD", "true")
    _npm_ci(session)
    _npm(session, "run", "lint")
    _npm(session, "test")
    _npm(session, "run", "test:coverage")
    _npm(session, "run", "version:check")


@nox.session(python=PYTHON)
def e2e(session: nox.Session) -> None:
    """Headful recorder E2E (opens a browser window; on Linux wrap in Xvfb), then validate."""

    result_dir = os.environ.get("CUA_E2E_RESULT_DIR") or str(
        Path(tempfile.gettempdir()) / "cua-recorder-e2e-results"
    )
    session.env["CUA_E2E_RESULT_DIR"] = result_dir
    session.log(f"E2E bundle directory: {result_dir}")
    _npm_ci(session)
    npm = shutil.which("npm")
    if npm is None:
        session.error(
            "npm was not found on PATH; install Node.js >= 22.13 "
            "(ESLint 10 needs it; >= 22.12 is enough for the tests and E2E)"
        )
    command = [npm, "--prefix", str(EXTENSION), "run", "e2e"]
    xvfb = shutil.which("xvfb-run")
    if sys.platform.startswith("linux") and xvfb and not os.environ.get("DISPLAY"):
        command = [xvfb, "-a", *command]
    session.run(*command, external=True)
    session.run("python", "tools/ci/validate_e2e_bundle.py", result_dir)


@nox.session(python=PYTHON)
def build(session: nox.Session) -> None:
    """Wheel + sdist, twine check, extension ZIP, version consistency and SBOM check."""

    _sync(session, "dev", project=False)
    session.run("python", "tools/ci/check_versions.py")
    dist = ROOT / "dist"
    session.run("uv", "build", "--out-dir", str(dist), external=True)
    artifacts = [str(path) for path in (*dist.glob("*.whl"), *dist.glob("*.tar.gz"))]
    session.run("twine", "check", "--strict", *artifacts)
    _npm_ci(session)
    _npm(session, "run", "package")
    session.run("python", "tools/ci/supply_chain.py", "--check")


@nox.session(python=PYTHON)
def evidence(session: nox.Session) -> None:
    """Verify research/evidence/MANIFEST.sha256 (archival bytes)."""

    script = ROOT / "tools" / "ci" / "verify_evidence.py"
    session.run("python", str(script), *session.posargs)


@nox.session(python=PYTHON)
def docs(session: nox.Session) -> None:
    """README links, commands and headline numbers (tools/ci/check_readme.py)."""

    script = ROOT / "tools" / "ci" / "check_readme.py"
    _sync(session, "test")
    session.run("python", str(script), *session.posargs)
