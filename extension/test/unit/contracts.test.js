const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const APP_ROOT = path.resolve(__dirname, "../../app");
const REPO_ROOT = path.resolve(__dirname, "../../..");

function read(relativePath) {
  return fs.readFileSync(path.join(APP_ROOT, relativePath), "utf8");
}

function readProject(relativePath) {
  return fs.readFileSync(path.resolve(REPO_ROOT, relativePath), "utf8");
}

function matchesClosedObjectShape(value, definition) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  if (definition.type !== "object" || definition.additionalProperties !== false) return false;
  const properties = definition.properties || {};
  const required = definition.required || [];
  if (!required.every((key) => Object.prototype.hasOwnProperty.call(value, key))) return false;
  if (!Object.keys(value).every((key) => Object.prototype.hasOwnProperty.call(properties, key))) return false;
  return Object.entries(value).every(([key, item]) => {
    const rule = properties[key];
    if (Object.prototype.hasOwnProperty.call(rule, "const") && item !== rule.const) return false;
    if (Array.isArray(rule.enum) && !rule.enum.includes(item)) return false;
    return true;
  });
}

function matchesTraceBranch(trace, metadataDefinition, eventDefinition) {
  return matchesClosedObjectShape(trace.metadata, metadataDefinition)
    && Array.isArray(trace.events)
    && trace.events.length > 0
    && trace.events.every((event) => matchesClosedObjectShape(event, eventDefinition))
    && trace.metadata.event_count === trace.events.length;
}

test("simple popup keeps only actor and task ahead of advanced metadata", () => {
  const html = read("popup/popup.html");
  const script = read("popup/popup.js");
  const beforeAdvanced = html.slice(0, html.indexOf('<details id="advanced-settings">'));
  assert.match(beforeAdvanced, /id="actor-type"/);
  assert.match(beforeAdvanced, /id="task-id"/);
  for (const advancedId of [
    "participant-code", "pair-id", "protocol-version", "actor-label",
    "executor", "executor-version", "capture-adapter", "environment-scope",
    "source-class"
  ]) {
    assert.doesNotMatch(beforeAdvanced, new RegExp(`id="${advancedId}"`));
    assert.match(html, new RegExp(`id="${advancedId}"`));
  }
  assert.doesNotMatch(`${html}\n${script}`, /codex|in-app-browser/i);
  assert.match(script, /agent-run-01/);
  assert.match(html, /id="persist-origin-permission"/);
  assert.match(script, /chrome\.permissions\.request\(\{ origins: \[originPattern\] \}\)/);
});

test("iframe capture and optional host access stay task-origin scoped", () => {
  const manifest = JSON.parse(read("manifest.json"));
  const background = read("src/background.js");
  assert.equal(manifest.permissions.includes("<all_urls>"), false);
  assert.deepEqual(manifest.optional_host_permissions, ["http://*/*", "https://*/*"]);
  assert.match(background, /target: \{ tabId, allFrames: true \}/);
  assert.match(background, /RECORDER_FRAME_DISCOVERED/);
  assert.match(background, /function classifySender\(sender\)/);
  assert.match(background, /sender\.tab\.id/);
  assert.match(background, /sender\.url/);
  assert.match(background, /TRUSTED_CONTEXTS/);
  assert.match(background, /chrome\.runtime\.onStartup\.addListener/);
});

test("dashboard exposes an explicit V1 compatibility export without changing V2 defaults", () => {
  const html = read("dashboard/dashboard.html");
  const script = read("dashboard/dashboard.js");
  assert.match(html, /data-format="json-v1"/);
  assert.match(script, /CUIRecorderCore\.toV1Session/);
  assert.match(html, /data-format="json">JSON</);
});

test("machine-readable coverage lists 59 unique browser listeners", () => {
  const coverage = JSON.parse(readProject("extension/event_coverage.json"));
  const content = read("src/content.js");
  assert.equal("recorder_version" in coverage, false, "the manifest is the only version source");
  assert.equal(coverage.supported_browser_event_types.length, 59);
  assert.equal(new Set(coverage.supported_browser_event_types).size, 59);
  assert.deepEqual(coverage.recorder_generated_event_types, ["navigation"]);
  for (const type of coverage.supported_browser_event_types) {
    assert.match(content, new RegExp(`"${type}"`), `content script does not register ${type}`);
  }
  assert.deepEqual(coverage.run_evidence_contract.required_groups, [
    "naturally_observed_in_fixture",
    "synthetically_exercised_in_fixture",
    "recorder_generated"
  ]);
  assert.match(coverage.run_evidence_contract.note, /specific clean-profile run/i);
  assert.match(coverage.interpretation_warning, /must not be used as an actor detector/i);
});

test("JSON schema and minimal example preserve the cui-trace-v1 contract", () => {
  const schema = JSON.parse(readProject("src/computer_use_agent/schemas/cui-trace-v1.schema.json"));
  const example = JSON.parse(readProject("examples/traces/minimal-session.json"));
  const historicalStandalone = JSON.parse(readProject(
    "extension/test/fixtures/agent_demo_trace.json"
  ));
  assert.equal(schema.$schema, "https://json-schema.org/draft/2020-12/schema");
  assert.equal(example.metadata.schema_version, "cui-trace-v1");
  assert.equal(example.events[0].schema_version, "cui-trace-v1");
  assert.equal(example.metadata.capture_adapter, "browser_mv3");
  assert.equal(example.metadata.source_class, "cui_agent");
  assert.match(example.metadata.notes, /not a human trace/i);
  for (const key of schema.required) assert.ok(key in example);
  assert.deepEqual(schema.$defs.sessionMetadata.oneOf, [
    { $ref: "#/$defs/currentSessionMetadata" },
    { $ref: "#/$defs/legacyStandaloneMetadata" }
  ]);
  assert.deepEqual(schema.$defs.event.oneOf, [
    { $ref: "#/$defs/currentEvent" },
    { $ref: "#/$defs/legacyStandaloneEvent" }
  ]);
  for (const key of schema.$defs.currentSessionMetadata.required) assert.ok(key in example.metadata, key);
  for (const key of schema.$defs.currentEvent.required) assert.ok(key in example.events[0], key);

  const currentMetadata = schema.$defs.currentSessionMetadata;
  const currentEvent = schema.$defs.currentEvent;
  const legacyMetadata = schema.$defs.legacyStandaloneMetadata;
  const legacyEvent = schema.$defs.legacyStandaloneEvent;
  assert.equal(currentMetadata.additionalProperties, false);
  assert.equal(currentEvent.additionalProperties, false);
  assert.equal(legacyMetadata.additionalProperties, false);
  assert.equal(legacyEvent.additionalProperties, false);
  assert.equal(Object.prototype.hasOwnProperty.call(currentMetadata.properties, "evidence_scope"), false);
  for (const key of [
    "pair_id", "protocol_version", "recorder_version", "scope_origin",
    "disclosure_version", "retention_policy", "chunk_count"
  ]) {
    assert.ok(currentMetadata.required.includes(key), `strict current metadata lost ${key}`);
  }

  assert.equal(matchesTraceBranch(example, currentMetadata, currentEvent), true);
  assert.equal(matchesTraceBranch(example, legacyMetadata, legacyEvent), false);
  assert.equal(matchesTraceBranch(historicalStandalone, legacyMetadata, legacyEvent), true);
  assert.equal(matchesTraceBranch(historicalStandalone, currentMetadata, currentEvent), false);
  assert.match(historicalStandalone.metadata.evidence_scope, /standalone mirror/i);
  assert.match(historicalStandalone.metadata.evidence_scope, /not extension storage/i);

  const currentWithLegacyField = structuredClone(example);
  currentWithLegacyField.metadata.evidence_scope = "legacy-only";
  assert.equal(matchesTraceBranch(currentWithLegacyField, currentMetadata, currentEvent), false);
  assert.equal(matchesTraceBranch(currentWithLegacyField, legacyMetadata, legacyEvent), false);
  const legacyWithCurrentField = structuredClone(historicalStandalone);
  legacyWithCurrentField.metadata.recorder_version = "0.3.0";
  assert.equal(matchesTraceBranch(legacyWithCurrentField, currentMetadata, currentEvent), false);
  assert.equal(matchesTraceBranch(legacyWithCurrentField, legacyMetadata, legacyEvent), false);
});

test("TraceEnvelopeV2 schema is closed and keeps V1 compatibility fields", () => {
  const schema = JSON.parse(readProject("src/computer_use_agent/schemas/cui-trace-v2.schema.json"));
  const example = JSON.parse(readProject("examples/traces/minimal-session-v2.json"));
  const extensionV04 = JSON.parse(readProject(
    "research/evidence/recorder_e2e_v0.4.0/agent_extension_e2e_trace.json"
  ));
  const metadata = schema.$defs.metadata;
  const event = schema.$defs.event;
  assert.equal(schema.additionalProperties, false);
  assert.equal(metadata.additionalProperties, false);
  assert.equal(event.additionalProperties, false);
  assert.equal(example.metadata.schema_version, "cui-trace-v2");
  // The published example tracks the current recorder (manifest.json is the single version source).
  assert.equal(example.metadata.recorder_version, JSON.parse(read("manifest.json")).version);
  assert.equal(example.events[0].schema_version, "cui-trace-v2");
  assert.equal(matchesClosedObjectShape(example.metadata, metadata), true);
  assert.equal(example.events.every((row) => matchesClosedObjectShape(row, event)), true);
  assert.equal(matchesTraceBranch(extensionV04, metadata, event), true);
  const refs = [];
  const visit = (value) => {
    if (Array.isArray(value)) return value.forEach(visit);
    if (!value || typeof value !== "object") return;
    if (typeof value.$ref === "string") refs.push(value.$ref);
    Object.values(value).forEach(visit);
  };
  visit(schema);
  assert.ok(refs.length > 0);
  assert.equal(refs.every((ref) => ref.startsWith("#/")), true, refs.join(", "));
  for (const nested of [
    "viewport", "pointer", "wheel", "scroll", "keyboard", "modifiers",
    "input", "transferSummary", "transfer", "touch", "form",
    "documentState", "target"
  ]) {
    assert.equal(schema.$defs[nested].additionalProperties, false, nested);
  }
  for (const field of [
    "event_seq", "document_epoch", "t_session_ms", "t_document_ms", "step_id"
  ]) {
    assert.ok(event.required.includes(field), field);
  }
  for (const field of [
    "timing_model", "event_seq_last", "document_epoch_count",
    "dropped_event_count", "rejected_event_count", "flush_failure_count",
    "quota_rejection_count"
  ]) {
    assert.ok(metadata.required.includes(field), field);
  }
  for (const retained of ["epoch_ms", "monotonic_ms", "url", "target", "is_trusted"]) {
    assert.ok(retained in event.properties, retained);
  }
});

const EXTENSION_ROOT = path.resolve(__dirname, "../..");

function readExtension(relativePath) {
  return fs.readFileSync(path.join(EXTENSION_ROOT, relativePath), "utf8");
}

function pngSize(filePath) {
  const bytes = fs.readFileSync(filePath);
  assert.deepEqual([...bytes.subarray(0, 8)], [137, 80, 78, 71, 13, 10, 26, 10], `${filePath} is not a PNG`);
  assert.equal(bytes.subarray(12, 16).toString("latin1"), "IHDR");
  return { width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20) };
}

function listFiles(root) {
  const rows = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const full = path.join(root, entry.name);
    if (entry.isDirectory()) rows.push(...listFiles(full));
    else rows.push(full);
  }
  return rows;
}

test("the manifest is the single version source for package metadata and UI", () => {
  const manifest = JSON.parse(read("manifest.json"));
  const pkg = JSON.parse(readExtension("package.json"));
  const lock = JSON.parse(readExtension("package-lock.json"));
  assert.match(manifest.version, /^\d+\.\d+\.\d+$/);
  assert.equal(pkg.version, manifest.version);
  assert.equal(lock.version, manifest.version);
  assert.equal(lock.packages[""].version, manifest.version);
  assert.equal(pkg.name, "computer-use-agent-recorder");
  assert.equal(pkg.private, true);
  assert.equal(pkg.engines.node, ">=22.12");
  const popupHtml = read("popup/popup.html");
  assert.doesNotMatch(popupHtml, /\bv?\d+\.\d+\.\d+\b/, "popup.html must not hard-code a version");
  assert.match(popupHtml, /id="recorder-version"/);
  assert.match(read("popup/popup.js"), /chrome\.runtime\.getManifest\(\)\.version/);
  const e2e = readExtension("test/e2e/recorder.e2e.mjs");
  assert.doesNotMatch(e2e, /RECORDER_VERSION = "\d/);
});

test("the manifest requests no download permission and the app never calls chrome.downloads", () => {
  const manifest = JSON.parse(read("manifest.json"));
  assert.deepEqual([...manifest.permissions].sort(), ["activeTab", "scripting", "storage", "unlimitedStorage"]);
  for (const file of listFiles(APP_ROOT).filter((name) => name.endsWith(".js"))) {
    assert.doesNotMatch(fs.readFileSync(file, "utf8"), /chrome\.downloads/, file);
  }
});

test("the Chrome floor is the Chrome for Testing major that the E2E job runs", () => {
  const manifest = JSON.parse(read("manifest.json"));
  assert.equal(manifest.minimum_chrome_version, "151");
  const revisions = path.join(EXTENSION_ROOT, "node_modules", "puppeteer-core", "lib", "puppeteer", "revisions.js");
  if (fs.existsSync(revisions)) {
    const match = fs.readFileSync(revisions, "utf8").match(/chrome: '(\d+)\./);
    assert(match, "could not read the Puppeteer Chrome for Testing revision");
    assert(
      Number(manifest.minimum_chrome_version) <= Number(match[1]),
      "minimum_chrome_version is above the Chrome for Testing build the E2E job runs"
    );
  }
});

test("declared icons exist as PNG files with the declared dimensions", () => {
  const manifest = JSON.parse(read("manifest.json"));
  assert.deepEqual(Object.keys(manifest.icons).sort(), ["128", "16", "32", "48"]);
  const declared = { ...manifest.icons, ...manifest.action.default_icon };
  for (const [size, relativePath] of Object.entries(declared)) {
    assert.equal(relativePath, `icons/${size}.png`);
    const dimensions = pngSize(path.join(APP_ROOT, relativePath));
    assert.deepEqual(dimensions, { width: Number(size), height: Number(size) });
  }
});

test("branding is neutral and carries no institution- or person-specific text", () => {
  const manifest = JSON.parse(read("manifest.json"));
  assert.equal(manifest.name, "ComputerUseAgent Trace Recorder");
  assert.equal(manifest.short_name, "CUA Recorder");
  assert.equal(manifest.action.default_title, manifest.name);
  const textFiles = [
    ...listFiles(APP_ROOT),
    ...listFiles(path.join(EXTENSION_ROOT, "playground"))
  ].filter((name) => /\.(html|js|css|json)$/.test(name));
  for (const file of textFiles) {
    const text = fs.readFileSync(file, "utf8");
    assert.doesNotMatch(text, /Minnesota|minnesotanlp|\bUMN\b|mentor|\bPI\b|Week ?[0-9]|CUI Trace Recorder/i, file);
  }
});

test("the disclosure is a versioned operator template matching the popup", () => {
  const html = read("privacy/disclosure.html");
  const popup = read("popup/popup.js");
  const constant = popup.match(/const DISCLOSURE_VERSION = "([^"]+)";/);
  assert(constant, "popup.js must define DISCLOSURE_VERSION");
  const meta = html.match(/<meta name="cua-disclosure-version" content="([^"]+)" \/>/);
  const visible = html.match(/<span id="disclosure-version">([^<]+)<\/span>/);
  assert.equal(meta?.[1], constant[1]);
  assert.equal(visible?.[1], constant[1]);
  assert.notEqual(constant[1], "2026-08-03-v1", "the 0.4.0 disclosure version must not be reused");
  assert.match(popup, /disclosureVersion: DISCLOSURE_VERSION/);
  assert.match(html, /Operator template/);
  assert.match(html, /\[Responsible organization\]/);
  assert.match(html.toLowerCase(), /consent/);
});

test("the E2E harness uses CUA_E2E_* settings, OS temp output, and CDP download tracking", () => {
  const e2e = readExtension("test/e2e/recorder.e2e.mjs");
  const policy = readExtension("test/e2e/chromium_launch_policy.mjs");
  assert.doesNotMatch(`${e2e}\n${policy}`, /CUI_E2E_|LEGACY_CHROME|workstreams|chrome\.downloads/);
  for (const name of ["CUA_E2E_CHROME", "CUA_E2E_RESULT_DIR", "CUA_E2E_CASES", "CUA_E2E_RUNTIME_DIR"]) {
    assert.match(e2e, new RegExp(`process\\.env\\.${name}`));
  }
  assert.match(policy, /env\.CUA_E2E_CI_NO_SANDBOX === "1"/);
  assert.match(e2e, /os\.tmpdir\(\)/);
  assert.match(e2e, /Browser\.setDownloadBehavior/);
  assert.match(e2e, /Browser\.downloadProgress/);
});

test("the playground loads the shipped core from the extension root", () => {
  const html = readExtension("playground/index.html");
  const sources = [...html.matchAll(/<script[^>]+src="([^"]+)"/g)].map((match) => match[1]);
  assert.deepEqual(sources, ["../app/src/core.js", "demo.js"]);
  const coreUrl = new URL(sources[0], "http://127.0.0.1:8765/playground/");
  assert.equal(coreUrl.pathname, "/app/src/core.js");
  assert.equal(fs.existsSync(path.join(EXTENSION_ROOT, coreUrl.pathname)), true);
});
