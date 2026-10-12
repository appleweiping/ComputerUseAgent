#!/usr/bin/env node
// Verify that every published version string agrees with the extension
// manifest (extension/app/manifest.json), the single source for the extension
// version. The repository-wide release check (tools/ci/check_versions.py) treats
// pyproject.toml as canonical; both must agree, so this script compares all of:
//   extension/app/manifest.json   "version"
//   extension/package.json        "version"
//   extension/package-lock.json   "version" and packages[""].version
//   pyproject.toml                [project] version
//   CITATION.cff                  version
// and checks that no other extension file duplicates the version.
//
// Usage: node scripts/check-version.mjs [--root <repository root>]
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath, pathToFileURL } from "node:url";

const SEMVER = /^\d+\.\d+\.\d+$/;

function readText(root, relativePath) {
  return readFileSync(path.join(root, relativePath), "utf8");
}

function readJson(root, relativePath) {
  return JSON.parse(readText(root, relativePath));
}

/** Return the `version` of the [project] table, or throw if it is absent/dynamic. */
export function pyprojectVersion(text) {
  let inProject = false;
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.replace(/#.*$/, "").trim();
    if (/^\[.*\]$/.test(line)) {
      inProject = line === "[project]";
      continue;
    }
    if (!inProject) continue;
    const match = line.match(/^version\s*=\s*["']([^"']+)["']$/);
    if (match) return match[1];
  }
  throw new Error("pyproject.toml has no static [project] version");
}

/** Return the top-level `version:` of a CITATION.cff document. */
export function citationVersion(text) {
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^version:\s*["']?([^"'\s#]+)["']?\s*(#.*)?$/);
    if (match) return match[1];
  }
  throw new Error("CITATION.cff has no top-level version");
}

/** Major version of the Chrome for Testing build pinned by the installed Puppeteer, or null. */
export function puppeteerChromeMajor(extensionRoot) {
  const revisions = path.join(extensionRoot, "node_modules", "puppeteer-core", "lib", "puppeteer", "revisions.js");
  if (!existsSync(revisions)) return null;
  const match = readFileSync(revisions, "utf8").match(/chrome: '(\d+)\./);
  return match ? Number(match[1]) : null;
}

export function checkVersions(repositoryRoot) {
  const root = path.resolve(repositoryRoot);
  const extensionRoot = path.join(root, "extension");
  const errors = [];
  const rows = [];
  const record = (source, read) => {
    try {
      const value = read();
      rows.push({ source, value });
      return value;
    } catch (error) {
      errors.push(`${source}: ${error.message}`);
      rows.push({ source, value: null });
      return null;
    }
  };

  const manifest = record("extension/app/manifest.json", () => readJson(root, "extension/app/manifest.json").version);
  if (manifest !== null && !SEMVER.test(manifest)) errors.push(`manifest version ${manifest} is not MAJOR.MINOR.PATCH`);
  record("extension/package.json", () => readJson(root, "extension/package.json").version);
  record("extension/package-lock.json", () => readJson(root, "extension/package-lock.json").version);
  record("extension/package-lock.json packages[\"\"]", () => readJson(root, "extension/package-lock.json").packages[""].version);
  record("pyproject.toml [project]", () => pyprojectVersion(readText(root, "pyproject.toml")));
  record("CITATION.cff", () => citationVersion(readText(root, "CITATION.cff")));
  for (const row of rows) {
    if (manifest !== null && row.value !== null && row.value !== manifest) {
      errors.push(`${row.source} has ${row.value}; the manifest has ${manifest}`);
    }
  }

  // Files that must read the version from the manifest instead of copying it.
  try {
    const popup = readText(root, "extension/app/popup/popup.html");
    if (/\bv?\d+\.\d+\.\d+\b/.test(popup)) errors.push("extension/app/popup/popup.html hard-codes a version");
    const coverage = readJson(root, "extension/event_coverage.json");
    if ("recorder_version" in coverage) errors.push("extension/event_coverage.json duplicates the version");
  } catch (error) {
    errors.push(`duplicate-version scan: ${error.message}`);
  }

  // minimum_chrome_version must not exceed the Chrome for Testing build the E2E job runs.
  try {
    const floor = Number(readJson(root, "extension/app/manifest.json").minimum_chrome_version);
    const tested = puppeteerChromeMajor(extensionRoot);
    rows.push({ source: "minimum_chrome_version", value: String(floor) });
    if (!Number.isInteger(floor) || floor < 1) errors.push("manifest minimum_chrome_version is missing or invalid");
    if (tested !== null) {
      rows.push({ source: "Puppeteer Chrome for Testing major", value: String(tested) });
      if (floor > tested) errors.push(`minimum_chrome_version ${floor} is above the tested Chrome for Testing ${tested}`);
    }
  } catch (error) {
    errors.push(`minimum_chrome_version: ${error.message}`);
  }
  return { ok: errors.length === 0, version: manifest, rows, errors };
}

function parseRoot(argv) {
  const index = argv.indexOf("--root");
  if (index >= 0 && argv[index + 1]) return path.resolve(argv[index + 1]);
  return path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
}

export function main(argv = process.argv.slice(2), output = process.stdout) {
  const result = checkVersions(parseRoot(argv));
  for (const row of result.rows) output.write(`${row.source.padEnd(48)} ${row.value ?? "<unreadable>"}\n`);
  if (result.ok) {
    output.write(`version:check OK (${result.version})\n`);
    return 0;
  }
  for (const error of result.errors) output.write(`ERROR ${error}\n`);
  return 1;
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  process.exitCode = main();
}
