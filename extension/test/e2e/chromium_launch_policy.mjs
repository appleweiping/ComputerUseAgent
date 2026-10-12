const BASE_CHROMIUM_ARGS = Object.freeze([
  "--no-first-run",
  "--no-default-browser-check",
  "--disable-sync",
  "--disable-background-networking",
  "--disable-component-update",
  "--window-size=1440,1000"
]);

const CI_NO_SANDBOX_ARGS = Object.freeze([
  "--no-sandbox",
  "--disable-setuid-sandbox"
]);

/**
 * Keep Chromium's sandbox enabled unless the dedicated CI escape hatch is
 * explicitly set to the exact value "1". The CI-specific name and strict
 * value check prevent a generic CI=true environment from weakening local or
 * hosted runs unintentionally.
 */
export function resolveChromiumLaunchPolicy(env = process.env) {
  const ciNoSandboxEnabled = env.CUA_E2E_CI_NO_SANDBOX === "1";
  return {
    args: [
      ...BASE_CHROMIUM_ARGS,
      ...(ciNoSandboxEnabled ? CI_NO_SANDBOX_ARGS : [])
    ],
    ci_no_sandbox_enabled: ciNoSandboxEnabled,
    sandbox_mode: ciNoSandboxEnabled ? "disabled_by_explicit_ci_flag" : "browser_default"
  };
}

export { BASE_CHROMIUM_ARGS, CI_NO_SANDBOX_ARGS };
