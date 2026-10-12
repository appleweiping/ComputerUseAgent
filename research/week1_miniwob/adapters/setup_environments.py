"""Create isolated, pinned Week 1 adapter environments outside the repository."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ADAPTER_ROOT = Path(__file__).resolve().parent
REPO_ROOT = ADAPTER_ROOT.parents[2]
ENVIRONMENT_REQUIREMENTS = {
    "orchestrator": ADAPTER_ROOT / "requirements-orchestrator.txt",
    "browser-use": ADAPTER_ROOT / "requirements-browser-use.txt",
    "skyvern-local": ADAPTER_ROOT / "requirements-skyvern.txt",
}
FRAMEWORK_ENVIRONMENTS = {
    "browser-use": ("browser-use",),
    # BrowserGym/Playwright 1.44 owns setup+validation; Skyvern local runs in a
    # separate process with Playwright 1.62.  They cannot share one venv.
    "skyvern": ("orchestrator", "skyvern-local"),
}
DEPENDENCY_FREEZE_FILENAME = "cui-week1-dependency-freeze.txt"
DEPENDENCY_FREEZE_DIGEST_FILENAME = "cui-week1-dependency-freeze.sha256"


def default_root() -> Path | None:
    explicit = os.environ.get("CUI_WEEK1_ADAPTER_ENV_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    private = os.environ.get("CUI_WEEK1_PRIVATE_ROOT")
    if private:
        return Path(private).expanduser().resolve() / "environments"
    return None


def venv_python(path: Path) -> Path:
    return path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--framework", choices=["all", *FRAMEWORK_ENVIRONMENTS], default="all")
    parser.add_argument("--env-root", type=Path, default=default_root())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-node", action="store_true")
    parser.add_argument(
        "--snapshot-existing",
        action="store_true",
        help="Verify existing private environments and refresh only their dependency freeze evidence.",
    )
    return parser.parse_args()


def _normalized_freeze(value: str) -> str:
    rows = sorted(line.strip() for line in value.splitlines() if line.strip())
    return "\n".join(rows) + "\n"


def snapshot_environment(python: Path, destination: Path) -> dict[str, object]:
    completed = subprocess.run(
        [str(python), "-m", "pip", "freeze", "--all", "--local"],
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"pip freeze failed for {destination.name}")
    normalized = _normalized_freeze(completed.stdout)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    freeze_path = destination / DEPENDENCY_FREEZE_FILENAME
    digest_path = destination / DEPENDENCY_FREEZE_DIGEST_FILENAME
    freeze_path.write_text(normalized, encoding="utf-8", newline="\n")
    digest_path.write_text(digest + "\n", encoding="ascii", newline="\n")
    return {
        "environment": destination.name,
        "dependency_freeze_sha256": digest,
        "dependency_count": len(normalized.splitlines()),
    }


def main() -> int:
    args = parse_args()
    if not ((3, 11) <= sys.version_info[:2] < (3, 14)):
        print("Use Python >=3.11,<3.14 (for example: py -3.12 setup_environments.py).", file=sys.stderr)
        return 2
    if args.env_root is None:
        print("Set CUI_WEEK1_PRIVATE_ROOT or pass --env-root outside the repository.", file=sys.stderr)
        return 2
    env_root = args.env_root.expanduser().resolve()
    try:
        env_root.relative_to(REPO_ROOT.resolve())
    except ValueError:
        pass
    else:
        print("Adapter environments must be outside the Git repository.", file=sys.stderr)
        return 2
    selected_frameworks = list(FRAMEWORK_ENVIRONMENTS) if args.framework == "all" else [args.framework]
    selected = list(
        dict.fromkeys(
            environment
            for framework in selected_frameworks
            for environment in FRAMEWORK_ENVIRONMENTS[framework]
        )
    )
    plan = {
        "schema_version": "cui-week1-adapter-environment-plan-v1",
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "environment_root": str(env_root),
        "frameworks": selected_frameworks,
        "environments": selected,
        "operation": "snapshot-existing" if args.snapshot_existing else "create",
        "node_install": not args.skip_node and not args.snapshot_existing,
        "writes_performed": False,
    }
    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return 0

    env_root.mkdir(parents=True, exist_ok=True)
    completed_envs = []
    for environment in selected:
        destination = env_root / environment
        python = venv_python(destination)
        if args.snapshot_existing:
            if not python.is_file():
                print(f"Existing private environment is missing: {destination}", file=sys.stderr)
                return 2
        else:
            if destination.exists() and (
                not destination.is_dir() or any(destination.iterdir())
            ):
                print(
                    f"Refusing to reuse non-empty adapter environment: {destination}. "
                    "Choose a fresh --env-root or archive it explicitly.",
                    file=sys.stderr,
                )
                return 2
            subprocess.run([sys.executable, "-m", "venv", str(destination)], check=True)
            subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--quiet",
                    "--disable-pip-version-check",
                    "--requirement",
                    str(ENVIRONMENT_REQUIREMENTS[environment]),
                ],
                check=True,
                cwd=REPO_ROOT,
            )
        check = subprocess.run(
            [str(python), "-m", "pip", "check"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if check.returncode != 0:
            print(f"Dependency check failed for {environment}:\n{check.stdout}", file=sys.stderr)
            return 2
        snapshot = snapshot_environment(python, destination)
        completed_envs.append({"environment": environment, "python": str(python), **snapshot})
    if not args.skip_node and not args.snapshot_existing:
        npm = shutil.which("npm")
        if not npm:
            print("npm is required for the pinned browser host.", file=sys.stderr)
            return 2
        subprocess.run([npm, "ci", "--prefix", str(ADAPTER_ROOT / "node")], check=True, cwd=REPO_ROOT)
    plan["writes_performed"] = True
    plan["completed_environments"] = completed_envs
    print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
