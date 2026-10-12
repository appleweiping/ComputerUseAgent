import crypto from "node:crypto";

export const VALIDATOR_SCHEMA_VERSION = "cui-task-validator-result-v1";

function requireSupportedCheck(check) {
  if (![
    "checked_equals",
    "number_value_equals",
    "body_text_includes",
    "phet_ammonia_balance_oracle",
    "netlogo_rule90_oracle"
  ].includes(check.kind)) {
    throw new Error(`Unsupported validator check kind: ${check.kind}`);
  }
}

export async function observeChecks(page, checks) {
  for (const check of checks) requireSupportedCheck(check);
  return page.evaluate((specs) => {
    const observations = {};
    const stripBidi = (value) => (value || "")
      .replace(/[\u202A-\u202E\u2066-\u2069]/g, "")
      .replace(/\u00a0/g, " ")
      .trim();
    const visible = (element) => {
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    };
    for (const spec of specs) {
      if (spec.kind === "body_text_includes") {
        observations[spec.id] = stripBidi(document.body.innerText).includes(stripBidi(spec.expected));
        continue;
      }
      if (spec.kind === "phet_ammonia_balance_oracle") {
        const coefficientRows = [...document.querySelectorAll("svg text")]
          .filter((element) => {
            const rect = element.getBoundingClientRect();
            const controlRect = element.parentElement?.parentElement?.getBoundingClientRect();
            return visible(element) && rect.y > innerHeight * 0.5 && /^\d+$/.test(stripBidi(element.textContent)) &&
              parseFloat(element.getAttribute("font-size") || "0") >= 30 &&
              controlRect && controlRect.width >= 50 && controlRect.width <= 80 &&
              controlRect.height >= 90 && controlRect.height <= 120;
          })
          .sort((a, b) => a.getBoundingClientRect().x - b.getBoundingClientRect().x)
          .map((element) => Number(stripBidi(element.textContent)));
        const scaleCounts = [...document.querySelectorAll("svg text")]
          .filter((element) => {
            const rect = element.getBoundingClientRect();
            return visible(element) && rect.y < innerHeight * 0.4 && /^\d+$/.test(element.textContent.trim());
          })
          .sort((a, b) => a.getBoundingClientRect().x - b.getBoundingClientRect().x)
          .map((element) => Number(element.textContent.trim()));
        const elementLabels = [...document.querySelectorAll("svg text")]
          .filter((element) => {
            const rect = element.getBoundingClientRect();
            return visible(element) && rect.y < innerHeight * 0.4 && ["N", "H"].includes(element.textContent);
          })
          .map((element) => element.textContent)
          .sort();
        observations[spec.id] = {
          project: window.phet?.chipper?.project || null,
          version: window.phet?.chipper?.version || null,
          coefficients: coefficientRows,
          scale_counts: scaleCounts,
          element_labels: elementLabels
        };
        continue;
      }
      if (spec.kind === "netlogo_rule90_oracle") {
        const patches = window.world?._patches || [];
        const patchBits = patches.map((patch) => patch.getVariable("on?") ? "1" : "0").join("");
        const go = [...document.querySelectorAll("label.netlogo-forever-button[role='button']")]
          .find((element) => element.textContent.trim() === "go");
        observations[spec.id] = {
          rule: window.world?.observer?.getGlobal("rule"),
          row: window.world?.observer?.getGlobal("row"),
          ticks: window.world?.ticker?._count,
          on_cell_count: patches.filter((patch) => patch.getVariable("on?")).length,
          patch_bits: patchBits,
          go_pressed: go?.getAttribute("aria-pressed") === "true" || Boolean(go?.querySelector("input[type='checkbox']")?.checked)
        };
        continue;
      }
      const element = document.querySelector(spec.selector);
      if (!element) {
        observations[spec.id] = { missing: true };
        continue;
      }
      if (spec.kind === "checked_equals") observations[spec.id] = Boolean(element.checked);
      if (spec.kind === "number_value_equals") observations[spec.id] = Number(element.value);
    }
    return observations;
  }, checks);
}

export function evaluateChecks(checks, observations) {
  const results = checks.map((check) => {
    requireSupportedCheck(check);
    const observed = observations[check.id];
    const expected = check.kind === "body_text_includes" ? true : check.expected;
    let publicObserved = observed && typeof observed === "object" && observed.missing ? "missing" : observed;
    let passed;
    if (check.kind === "number_value_equals") {
      passed = Number.isFinite(observed) && observed === Number(expected);
    } else if (check.kind === "phet_ammonia_balance_oracle") {
      passed = JSON.stringify(observed) === JSON.stringify(expected);
    } else if (check.kind === "netlogo_rule90_oracle") {
      const patchBits = typeof observed?.patch_bits === "string" ? observed.patch_bits : "";
      publicObserved = {
        rule: observed?.rule,
        row: observed?.row,
        ticks: observed?.ticks,
        on_cell_count: observed?.on_cell_count,
        patch_bit_count: patchBits.length,
        patch_state_sha256: crypto.createHash("sha256").update(patchBits).digest("hex"),
        go_pressed: observed?.go_pressed
      };
      passed = JSON.stringify(publicObserved) === JSON.stringify(expected);
    } else {
      passed = observed === expected;
    }
    return {
      id: check.id,
      kind: check.kind,
      passed,
      expected,
      observed: publicObserved
    };
  });
  return {
    passed: results.every((row) => row.passed),
    checks: results
  };
}

export async function validateTaskPage(page, task, provenance = {}) {
  const startedAt = new Date().toISOString();
  const observations = await observeChecks(page, task.validator.checks);
  const evaluated = evaluateChecks(task.validator.checks, observations);
  return {
    schema_version: VALIDATOR_SCHEMA_VERSION,
    task_id: task.task_id,
    task_version: task.task_version,
    validator_id: task.validator.validator_id,
    validator_version: task.validator.validator_version,
    started_at: startedAt,
    finished_at: new Date().toISOString(),
    passed: evaluated.passed,
    checks: evaluated.checks,
    provenance
  };
}
