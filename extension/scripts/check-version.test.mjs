import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { checkVersions, citationVersion, main, pyprojectVersion } from "./check-version.mjs";

function fixture({
  manifest = "0.5.0",
  pkg = "0.5.0",
  lock = "0.5.0",
  pyproject = "0.5.0",
  citation = "0.5.0",
  popupFooter = '<span id="recorder-version">v?</span>',
  coverage = {},
  floor = "151",
  cftMajor = 151
} = {}) {
  const root = mkdtempSync(path.join(os.tmpdir(), "cua-version-check-"));
  const write = (relativePath, text) => {
    const target = path.join(root, relativePath);
    mkdirSync(path.dirname(target), { recursive: true });
    writeFileSync(target, text, "utf8");
  };
  write("extension/app/manifest.json", JSON.stringify({ version: manifest, minimum_chrome_version: floor }));
  write("extension/package.json", JSON.stringify({ version: pkg }));
  write("extension/package-lock.json", JSON.stringify({ version: lock, packages: { "": { version: lock } } }));
  write("pyproject.toml", `[build-system]\nrequires = ["hatchling"]\n\n[project]\nname = "x"\nversion = "${pyproject}" # canonical\n\n[tool.x]\nversion = "9.9.9"\n`);
  write("CITATION.cff", `cff-version: 1.2.0\nversion: "${citation}"\n`);
  write("extension/app/popup/popup.html", `<footer>${popupFooter}</footer>`);
  write("extension/event_coverage.json", JSON.stringify(coverage));
  if (cftMajor !== null) {
    write(
      "extension/node_modules/puppeteer-core/lib/puppeteer/revisions.js",
      `export const PUPPETEER_REVISIONS = Object.freeze({ chrome: '${cftMajor}.0.1.2' });\n`
    );
  }
  return root;
}

test("consistent versions pass", () => {
  const root = fixture();
  try {
    const result = checkVersions(root);
    assert.deepEqual(result.errors, []);
    assert.equal(result.ok, true);
    assert.equal(result.version, "0.5.0");
    const lines = [];
    assert.equal(main(["--root", root], { write: (line) => lines.push(line) }), 0);
    assert.match(lines.join(""), /version:check OK \(0\.5\.0\)/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("any disagreeing source fails and is named", () => {
  for (const [field, source] of [
    ["pkg", "extension/package.json"],
    ["lock", "extension/package-lock.json"],
    ["pyproject", "pyproject.toml"],
    ["citation", "CITATION.cff"]
  ]) {
    const root = fixture({ [field]: "0.4.0" });
    try {
      const result = checkVersions(root);
      assert.equal(result.ok, false, field);
      assert(result.errors.some((error) => error.startsWith(source) && error.includes("0.4.0")), result.errors.join("\n"));
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  }
});

test("hard-coded duplicates, a high Chrome floor, and unreadable files fail", () => {
  const cases = [
    [{ popupFooter: "<span>v0.5.0</span>" }, /popup\.html hard-codes/],
    [{ coverage: { recorder_version: "0.5.0" } }, /duplicates the version/],
    [{ floor: "152" }, /above the tested Chrome for Testing 151/],
    [{ floor: "abc" }, /minimum_chrome_version is missing/],
    [{ manifest: "0.5" }, /not MAJOR\.MINOR\.PATCH/]
  ];
  for (const [options, pattern] of cases) {
    const root = fixture(options);
    try {
      const result = checkVersions(root);
      assert.equal(result.ok, false);
      assert(result.errors.some((error) => pattern.test(error)), result.errors.join("\n"));
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  }
  const root = fixture({ cftMajor: null });
  try {
    rmSync(path.join(root, "CITATION.cff"));
    const lines = [];
    assert.equal(main(["--root", root], { write: (line) => lines.push(line) }), 1);
    assert.match(lines.join(""), /ERROR CITATION\.cff:/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("pyproject and CITATION parsers read only the intended field", () => {
  assert.equal(pyprojectVersion('[tool.a]\nversion = "1.0.0"\n[project]\nname = "x"\nversion = \'0.5.0\'\n'), "0.5.0");
  assert.throws(() => pyprojectVersion('[project]\ndynamic = ["version"]\n'), /no static/);
  assert.equal(citationVersion("title: x\nversion: 0.5.0\n"), "0.5.0");
  assert.equal(citationVersion("version: '0.5.0' # release\n"), "0.5.0");
  assert.throws(() => citationVersion("  version: 0.5.0\n"), /no top-level version/);
});
