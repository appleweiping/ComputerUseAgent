import assert from "node:assert/strict";
import test from "node:test";

import { evaluateChecks } from "./validator.mjs";

const checks = [
  { id: "flag", kind: "checked_equals", expected: true },
  { id: "count", kind: "number_value_equals", expected: 2 },
  { id: "text", kind: "body_text_includes", expected: "2 of 2" }
];

test("validator accepts the exact requested state", () => {
  const result = evaluateChecks(checks, { flag: true, count: 2, text: true });
  assert.equal(result.passed, true);
  assert.equal(result.checks.every((row) => row.passed), true);
});

test("validator rejects a wrong scientific state", () => {
  const result = evaluateChecks(checks, { flag: false, count: 1, text: false });
  assert.equal(result.passed, false);
  assert.deepEqual(result.checks.map((row) => row.passed), [false, false, false]);
});

test("validator reports a missing selector as a failed check", () => {
  const result = evaluateChecks(checks, { flag: { missing: true }, count: 2, text: true });
  assert.equal(result.passed, false);
  assert.equal(result.checks[0].observed, "missing");
});

test("validator rejects unknown check kinds", () => {
  assert.throws(
    () => evaluateChecks([{ id: "x", kind: "pixel_magic", expected: true }], { x: true }),
    /Unsupported validator check kind/
  );
});

test("PhET ammonia oracle requires the complete independent state", () => {
  const expected = {
    project: "balancing-chemical-equations",
    version: "1.3.1",
    coefficients: [1, 3, 2],
    scale_counts: [2, 2, 6, 6],
    element_labels: ["H", "N"]
  };
  const result = evaluateChecks(
    [{ id: "chem", kind: "phet_ammonia_balance_oracle", expected }],
    { chem: structuredClone(expected) }
  );
  assert.equal(result.passed, true);
});

test("NetLogo oracle hashes the full patch state before comparison", () => {
  const patchBits = "101001";
  const expected = {
    rule: 90,
    row: -64,
    ticks: 128,
    on_cell_count: 3,
    patch_bit_count: 6,
    patch_state_sha256: "4639346f504e511dca2c51d4429f26f1e9bd758a65d87135fe8bbdcb54c033a1",
    go_pressed: false
  };
  const result = evaluateChecks(
    [{ id: "ca", kind: "netlogo_rule90_oracle", expected }],
    { ca: { rule: 90, row: -64, ticks: 128, on_cell_count: 3, patch_bits: patchBits, go_pressed: false } }
  );
  assert.equal(result.passed, true);
  assert.equal(result.checks[0].observed.patch_bits, undefined);
});
