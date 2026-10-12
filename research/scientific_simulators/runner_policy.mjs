export const PHET_ANALYTICS_ALLOWLIST = Object.freeze({
  id: "phet-google-analytics-connection-closed-v1",
  simulator_origin: "https://phet.colorado.edu",
  error_kind: "console.error",
  error_message: "Failed to load resource: net::ERR_CONNECTION_CLOSED",
  source_origin: "https://www.google-analytics.com",
  source_path: "/g/collect",
  rationale:
    "The optional Google Analytics collection request can be closed by the local network; simulator state and first-party assets remain validated independently."
});

export function sanitizeConsoleSource(rawUrl) {
  if (!rawUrl) return { source_origin: null, source_path: null };
  try {
    const parsed = new URL(rawUrl);
    if (!["http:", "https:"].includes(parsed.protocol)) {
      return { source_origin: null, source_path: null };
    }
    return { source_origin: parsed.origin, source_path: parsed.pathname };
  } catch (_error) {
    return { source_origin: null, source_path: null };
  }
}

export function classifyBrowserErrors(errors, simulatorUrl) {
  const simulatorOrigin = new URL(simulatorUrl).origin;
  const allowlisted = [];
  const unexpected = [];
  for (const error of errors) {
    const matches =
      simulatorOrigin === PHET_ANALYTICS_ALLOWLIST.simulator_origin &&
      error?.kind === PHET_ANALYTICS_ALLOWLIST.error_kind &&
      error?.message === PHET_ANALYTICS_ALLOWLIST.error_message &&
      error?.source_origin === PHET_ANALYTICS_ALLOWLIST.source_origin &&
      error?.source_path === PHET_ANALYTICS_ALLOWLIST.source_path;
    if (matches) {
      allowlisted.push({ ...error, allowlist_id: PHET_ANALYTICS_ALLOWLIST.id });
    } else {
      unexpected.push(error);
    }
  }
  return {
    policy: PHET_ANALYTICS_ALLOWLIST,
    allowlisted,
    unexpected
  };
}

export async function runWithTimeLimit(operation, timeLimitSeconds, label) {
  if (!Number.isFinite(timeLimitSeconds) || timeLimitSeconds <= 0) {
    throw new TypeError(`Invalid time limit for ${label}: ${timeLimitSeconds}`);
  }
  const limitMs = timeLimitSeconds * 1_000;
  const startedAt = Date.now();
  let timer;
  const timeout = new Promise((_resolve, reject) => {
    timer = setTimeout(() => {
      const error = new Error(`${label} exceeded time_limit_seconds=${timeLimitSeconds}`);
      error.code = "CUI_TASK_TIME_LIMIT_EXCEEDED";
      reject(error);
    }, limitMs);
  });
  timer.unref?.();
  try {
    const value = await Promise.race([Promise.resolve().then(operation), timeout]);
    const elapsedMs = Date.now() - startedAt;
    if (elapsedMs > limitMs) {
      const error = new Error(`${label} exceeded time_limit_seconds=${timeLimitSeconds}`);
      error.code = "CUI_TASK_TIME_LIMIT_EXCEEDED";
      throw error;
    }
    return { value, elapsed_ms: elapsedMs, limit_ms: limitMs };
  } finally {
    clearTimeout(timer);
  }
}
