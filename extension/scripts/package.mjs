#!/usr/bin/env node
// `npm run package`: build dist/computer-use-agent-recorder-<version>.zip with
// tools/package_extension.py, which needs only the Python standard library.
//
// A bare `python` command exists inside an activated virtual environment, on
// Windows and where python-is-python3 is installed, but stock Ubuntu, Debian and
// macOS ship only `python3` (and a Windows virtual environment has no
// `python3`). So the interpreter is resolved here, in this order:
//   1. $PYTHON, when set (used alone: an explicit choice that fails is an error);
//   2. `python` (an activated venv, `uv run`, CI, Windows);
//   3. `python3`;
//   4. `py -3` (the Windows launcher).
// Each candidate is probed first, so a missing command or the Windows Store
// alias stub is skipped instead of being reported as a packaging failure.
//
// Usage: node scripts/package.mjs [tools/package_extension.py arguments, e.g. --output-dir DIR]
import { spawnSync } from "node:child_process";
import path from "node:path";
import process from "node:process";
import { fileURLToPath, pathToFileURL } from "node:url";

export const MINIMUM_PYTHON = Object.freeze([3, 10]);
const PROBE = `import sys; sys.exit(0 if sys.version_info >= (${MINIMUM_PYTHON.join(", ")}) else 1)`;
const REPOSITORY_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
export const PACKAGER = path.join(REPOSITORY_ROOT, "tools", "package_extension.py");

/** Interpreter commands to try, each as [executable, ...leading arguments]. */
export function pythonCandidates(env = process.env, platform = process.platform) {
  if (env.PYTHON) return [[env.PYTHON]];
  const candidates = [["python"], ["python3"]];
  if (platform === "win32") candidates.push(["py", "-3"]);
  return candidates;
}

/** Return the first candidate that runs and is new enough, or null. */
export function resolvePython({ env = process.env, platform = process.platform, spawn = spawnSync } = {}) {
  for (const [command, ...leading] of pythonCandidates(env, platform)) {
    const probe = spawn(command, [...leading, "-c", PROBE], { stdio: "ignore", windowsHide: true });
    if (!probe.error && probe.status === 0) return [command, ...leading];
  }
  return null;
}

export function main(
  argv = process.argv.slice(2),
  { env = process.env, spawn = spawnSync, stderr = process.stderr, stdio = "inherit" } = {}
) {
  const python = resolvePython({ env, spawn });
  if (!python) {
    const tried = pythonCandidates(env).map((candidate) => candidate.join(" ")).join(", ");
    stderr.write(
      `error: no Python >= ${MINIMUM_PYTHON.join(".")} found (tried: ${tried}). Set PYTHON to an ` +
        "interpreter, activate the project's .venv, or run `uv run npm --prefix extension run package` " +
        "from the repository root.\n"
    );
    return 1;
  }
  const [command, ...leading] = python;
  const result = spawn(command, [...leading, PACKAGER, ...argv], { stdio, windowsHide: true });
  if (result.error) {
    stderr.write(`error: could not run ${command}: ${result.error.message}\n`);
    return 1;
  }
  return result.status ?? 1;
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  process.exitCode = main();
}
