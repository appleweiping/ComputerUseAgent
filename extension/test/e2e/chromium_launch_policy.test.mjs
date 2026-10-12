import assert from "node:assert/strict";
import test from "node:test";

import {
  BASE_CHROMIUM_ARGS,
  CI_NO_SANDBOX_ARGS,
  resolveChromiumLaunchPolicy
} from "./chromium_launch_policy.mjs";

function assertSandboxArgsAbsent(policy) {
  for (const argument of CI_NO_SANDBOX_ARGS) {
    assert.equal(policy.args.includes(argument), false);
  }
  assert.equal(policy.ci_no_sandbox_enabled, false);
  assert.equal(policy.sandbox_mode, "browser_default");
}

test("local default preserves Chromium sandbox", () => {
  const policy = resolveChromiumLaunchPolicy({});
  assert.deepEqual(policy.args, [...BASE_CHROMIUM_ARGS]);
  assertSandboxArgsAbsent(policy);
});

test("generic CI environment does not disable Chromium sandbox", () => {
  assertSandboxArgsAbsent(resolveChromiumLaunchPolicy({ CI: "true" }));
});

test("near-match values do not activate the CI sandbox exception", () => {
  for (const value of ["", "0", "true", "TRUE", "yes", " 1 "]) {
    assertSandboxArgsAbsent(resolveChromiumLaunchPolicy({ CUA_E2E_CI_NO_SANDBOX: value }));
  }
});

test("exact explicit CI flag adds only the two sandbox exception arguments", () => {
  const policy = resolveChromiumLaunchPolicy({ CUA_E2E_CI_NO_SANDBOX: "1" });
  assert.deepEqual(policy.args, [...BASE_CHROMIUM_ARGS, ...CI_NO_SANDBOX_ARGS]);
  assert.equal(policy.ci_no_sandbox_enabled, true);
  assert.equal(policy.sandbox_mode, "disabled_by_explicit_ci_flag");
});

test("each resolution returns a fresh argument list", () => {
  const first = resolveChromiumLaunchPolicy({});
  first.args.push("--test-mutation");
  const second = resolveChromiumLaunchPolicy({});
  assert.equal(second.args.includes("--test-mutation"), false);
});

test("the retired CUI_E2E_CI_NO_SANDBOX name has no effect", () => {
  assertSandboxArgsAbsent(resolveChromiumLaunchPolicy({ CUI_E2E_CI_NO_SANDBOX: "1" }));
});
