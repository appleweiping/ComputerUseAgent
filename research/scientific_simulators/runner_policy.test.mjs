import assert from "node:assert/strict";
import test from "node:test";

import {
  PHET_ANALYTICS_ALLOWLIST,
  classifyBrowserErrors,
  runWithTimeLimit,
  sanitizeConsoleSource
} from "./runner_policy.mjs";


test("console source sanitization removes query and fragment data", () => {
  assert.deepEqual(
    sanitizeConsoleSource("https://www.google-analytics.com/g/collect?cid=private#fragment"),
    { source_origin: "https://www.google-analytics.com", source_path: "/g/collect" }
  );
});


test("PhET analytics connection closure is narrowly allowlisted", () => {
  const error = {
    kind: PHET_ANALYTICS_ALLOWLIST.error_kind,
    message: PHET_ANALYTICS_ALLOWLIST.error_message,
    source_origin: PHET_ANALYTICS_ALLOWLIST.source_origin,
    source_path: PHET_ANALYTICS_ALLOWLIST.source_path
  };
  const review = classifyBrowserErrors([error], "https://phet.colorado.edu/sims/html/example/1.0/example_en.html");
  assert.equal(review.unexpected.length, 0);
  assert.equal(review.allowlisted.length, 1);
  assert.equal(review.allowlisted[0].allowlist_id, PHET_ANALYTICS_ALLOWLIST.id);
});


test("the same network error is never allowlisted outside the exact scope", () => {
  const base = {
    kind: PHET_ANALYTICS_ALLOWLIST.error_kind,
    message: PHET_ANALYTICS_ALLOWLIST.error_message,
    source_origin: PHET_ANALYTICS_ALLOWLIST.source_origin,
    source_path: PHET_ANALYTICS_ALLOWLIST.source_path
  };
  const variants = [
    { ...base, message: "Failed to load resource: net::ERR_NAME_NOT_RESOLVED" },
    { ...base, source_origin: "https://phet.colorado.edu" },
    { ...base, source_path: "/other" },
    { ...base, kind: "pageerror" }
  ];
  for (const error of variants) {
    const review = classifyBrowserErrors([error], "https://phet.colorado.edu/sims/html/example/1.0/example_en.html");
    assert.equal(review.allowlisted.length, 0);
    assert.deepEqual(review.unexpected, [error]);
  }
  const nonPhet = classifyBrowserErrors([base], "https://netlogoweb.org/versions/2.15.0.html");
  assert.equal(nonPhet.allowlisted.length, 0);
  assert.deepEqual(nonPhet.unexpected, [base]);
});


test("task time limit returns auditable elapsed timing", async () => {
  const result = await runWithTimeLimit(async () => "done", 1, "fast-task");
  assert.equal(result.value, "done");
  assert.equal(result.limit_ms, 1_000);
  assert(result.elapsed_ms >= 0 && result.elapsed_ms <= result.limit_ms);
});


test("task time limit fails with a stable error code", async () => {
  await assert.rejects(
    runWithTimeLimit(
      () => new Promise((resolve) => setTimeout(resolve, 40)),
      0.005,
      "slow-task"
    ),
    (error) => error.code === "CUI_TASK_TIME_LIMIT_EXCEEDED" && /time_limit_seconds=0.005/.test(error.message)
  );
});
