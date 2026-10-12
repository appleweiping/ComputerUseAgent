import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readdirSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { PACKAGER, main, pythonCandidates, resolvePython } from "./package.mjs";

// A fake spawnSync: `outcomes` maps "command arg0" to a probe result; every
// call is recorded. Unlisted commands behave like a missing executable.
function fakeSpawn(outcomes, packagerResult = { status: 0 }) {
  const calls = [];
  const spawn = (command, args) => {
    calls.push([command, ...args]);
    if (args.includes(PACKAGER)) return packagerResult;
    const key = [command, ...args.slice(0, args.indexOf("-c"))].join(" ");
    return outcomes[key] || { error: Object.assign(new Error(`spawn ${command} ENOENT`), { code: "ENOENT" }) };
  };
  return { spawn, calls };
}

function sink() {
  const chunks = [];
  return { write: (text) => chunks.push(text), text: () => chunks.join("") };
}

test("candidates honour $PYTHON alone, else python, python3 and the Windows launcher", () => {
  assert.deepEqual(pythonCandidates({ PYTHON: "/opt/py/bin/python3.12" }, "linux"), [["/opt/py/bin/python3.12"]]);
  assert.deepEqual(pythonCandidates({}, "linux"), [["python"], ["python3"]]);
  assert.deepEqual(pythonCandidates({}, "darwin"), [["python"], ["python3"]]);
  assert.deepEqual(pythonCandidates({}, "win32"), [["python"], ["python3"], ["py", "-3"]]);
});

test("a missing python, the Store alias stub and an old interpreter are skipped", () => {
  const { spawn } = fakeSpawn({
    python: { status: 9009 },
    python3: { status: 1 },
    "py -3": { status: 0 }
  });
  assert.deepEqual(resolvePython({ env: {}, platform: "win32", spawn }), ["py", "-3"]);
  assert.equal(resolvePython({ env: {}, platform: "linux", spawn }), null);

  const stockUbuntu = fakeSpawn({ python3: { status: 0 } });
  assert.deepEqual(resolvePython({ env: {}, platform: "linux", spawn: stockUbuntu.spawn }), ["python3"]);
  assert.match(stockUbuntu.calls[0].at(-1), /sys\.version_info >= \(3, 10\)/);
});

test("main runs the packager with the resolved interpreter and forwards arguments and status", () => {
  const ok = fakeSpawn({ python3: { status: 0 } });
  assert.equal(main(["--output-dir", "out"], { env: {}, spawn: ok.spawn, stderr: sink() }), 0);
  assert.deepEqual(ok.calls.at(-1), ["python3", PACKAGER, "--output-dir", "out"]);

  const failing = fakeSpawn({ python: { status: 0 } }, { status: 2 });
  assert.equal(main([], { env: {}, spawn: failing.spawn, stderr: sink() }), 2);

  const crashed = fakeSpawn({ python: { status: 0 } }, { error: new Error("EACCES") });
  const crashOutput = sink();
  assert.equal(main([], { env: {}, spawn: crashed.spawn, stderr: crashOutput }), 1);
  assert.match(crashOutput.text(), /could not run python: EACCES/);
});

test("main explains how to provide Python when none is found", () => {
  const output = sink();
  const { spawn, calls } = fakeSpawn({});
  assert.equal(main([], { env: { PYTHON: "missing-python" }, spawn, stderr: output }), 1);
  assert.deepEqual(calls.map((call) => call[0]), ["missing-python"]);
  assert.match(output.text(), /no Python >= 3\.10 found \(tried: missing-python\)/);
  assert.match(output.text(), /uv run npm --prefix extension run package/);
});

test("the real packager builds the ZIP through the resolved interpreter", { skip: !resolvePython() && "no Python >= 3.10 on PATH" }, () => {
  const outputDirectory = mkdtempSync(path.join(os.tmpdir(), "cua-package-"));
  try {
    assert.equal(main(["--output-dir", outputDirectory], { stderr: sink(), stdio: "ignore" }), 0);
    const archives = readdirSync(outputDirectory).filter((name) => name.endsWith(".zip"));
    assert.equal(archives.length, 1);
    assert.match(archives[0], /^computer-use-agent-recorder-\d+\.\d+\.\d+\.zip$/);
    assert.equal(existsSync(path.join(outputDirectory, `${archives[0]}.tmp`)), false);
  } finally {
    rmSync(outputDirectory, { recursive: true, force: true });
  }
});
