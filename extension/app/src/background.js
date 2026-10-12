importScripts("core.js");

const ROOT_KEY = "cui-recorder-root";
const SESSION_PREFIX = "cui-session-";
const CHUNK_PREFIX = "cui-events-";
const CHUNK_SIZE = 500;
const MAX_SESSIONS = 250;
const MAX_BATCH_SIZE = 1000;
const MAX_EVENTS_PER_SESSION = 100000;
const MAX_APPROX_SESSION_BYTES = 64 * 1024 * 1024;
const MAX_TOTAL_STORAGE_BYTES = 512 * 1024 * 1024;
const MAX_STEP_IDS = 512;
const MAX_CHUNKS_PER_SESSION = Math.ceil(MAX_EVENTS_PER_SESSION / CHUNK_SIZE) + 1;
const MIN_TIME_INCREMENT_MS = 0.001;
const SESSION_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/;

// Message authority. Extension pages (popup, dashboard, any chrome-extension://
// page of this extension, even when it is opened in a tab) may control
// sessions. Content scripts run inside web-page renderer processes and may only
// stream events for the tab that owns the active session and read a reduced
// state projection. Everything else is rejected.
const EXTENSION_PAGE_MESSAGES = new Set([
  "RECORDER_GET_STATE",
  "RECORDER_START",
  "RECORDER_STOP",
  "RECORDER_SET_STEP",
  "RECORDER_LIST_SESSIONS",
  "RECORDER_GET_SESSION",
  "RECORDER_DELETE_SESSION"
]);
const CONTENT_SCRIPT_MESSAGES = new Set([
  "RECORDER_GET_STATE",
  "RECORDER_EVENT_BATCH",
  "RECORDER_REPORT_DROPPED",
  "RECORDER_FRAME_DISCOVERED"
]);
let writeQueue = Promise.resolve();
let lifecycleQueue = Promise.resolve();
const dynamicFrameRefreshes = new Map();

async function setRecordingBadge(tabId, active) {
  if (!chrome.action?.setBadgeText) return;
  try {
    await chrome.action.setBadgeText({ tabId, text: active ? "REC" : "" });
    if (active && chrome.action.setBadgeBackgroundColor) {
      await chrome.action.setBadgeBackgroundColor({ tabId, color: "#b42318" });
    }
  } catch (_error) {
    // The page-level indicator remains the primary visible recording signal.
  }
}

function enqueueLifecycle(operation) {
  const queued = lifecycleQueue.catch(() => undefined).then(operation);
  lifecycleQueue = queued;
  return queued;
}

function enqueueWrite(operation) {
  const queued = writeQueue.catch(() => undefined).then(operation);
  writeQueue = queued;
  return queued;
}

async function ensureRecorderInjected(tabId) {
  const results = await chrome.scripting.executeScript({
    target: { tabId, allFrames: true },
    files: ["src/core.js", "src/content.js"]
  });
  const frameIds = Array.isArray(results)
    ? [...new Set(results.map((row) => row?.frameId).filter(Number.isInteger))]
    : [];
  return frameIds.length ? frameIds : [0];
}

async function sendRecorderMessageToFrames(tabId, message, frameIds = null) {
  const targets = frameIds || await ensureRecorderInjected(tabId);
  const outcomes = await Promise.allSettled(
    targets.map(async (frameId) => ({
      frameId,
      response: await chrome.tabs.sendMessage(tabId, message, { frameId })
    }))
  );
  const top = outcomes.find(
    (row) => row.status === "fulfilled" && row.value.frameId === 0
  );
  if (!top) {
    const rejectedTop = outcomes.find(
      (row, index) => targets[index] === 0 && row.status === "rejected"
    );
    throw rejectedTop?.reason || new Error("Recorder top-frame listener was unavailable");
  }
  const rejectedFrames = outcomes.filter((row) => row.status === "rejected");
  if (rejectedFrames.length) {
    throw new Error(
      `Recorder frame operation did not reach ${rejectedFrames.length} target frame(s)`
    );
  }
  const explicitFailure = outcomes.find(
    (row) => row.status === "fulfilled" && !row.value.response?.ok
  );
  if (explicitFailure) {
    throw new Error(explicitFailure.value.response?.error || "Recorder frame operation failed");
  }
  return {
    ok: true,
    reachedFrameCount: outcomes.length
  };
}

async function hardenStorageAccess() {
  if (typeof chrome.storage.local.setAccessLevel !== "function") {
    throw new Error("This Chrome build cannot restrict recorder storage to trusted extension contexts");
  }
  await chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
}

async function storageGet(keys) {
  return chrome.storage.local.get(keys);
}

async function storageSet(values) {
  return chrome.storage.local.set(values);
}

// Never fall back to reading values: storage.local.get(null) would load the
// whole store into the service worker. Fail closed instead.
async function storageBytesInUse(keys = null) {
  if (typeof chrome.storage.local.getBytesInUse !== "function") {
    throw new Error("This Chrome build cannot report recorder storage usage");
  }
  return chrome.storage.local.getBytesInUse(keys);
}

async function loadRoot() {
  const value = (await storageGet(ROOT_KEY))[ROOT_KEY];
  if (!value || typeof value !== "object") return { activeSessionId: null, sessionIds: [] };
  const sessionIds = Array.isArray(value.sessionIds)
    ? [...new Set(value.sessionIds.filter((id) => typeof id === "string" && id))]
    : [];
  return {
    activeSessionId: typeof value.activeSessionId === "string" ? value.activeSessionId : null,
    sessionIds
  };
}

async function saveRoot(root) {
  await storageSet({ [ROOT_KEY]: root });
}

async function getMetadata(sessionId) {
  return (await storageGet(`${SESSION_PREFIX}${sessionId}`))[`${SESSION_PREFIX}${sessionId}`] || null;
}

async function setMetadata(metadata) {
  await storageSet({ [`${SESSION_PREFIX}${metadata.session_id}`]: metadata });
}

async function commitMetadataAndRoot(metadata, root) {
  await storageSet({
    [`${SESSION_PREFIX}${metadata.session_id}`]: metadata,
    [ROOT_KEY]: root
  });
}

function isQuotaError(error) {
  return /quota|quota_bytes|storage limit/i.test(String(error?.message || error || ""));
}

function chunkKey(sessionId, index) {
  return `${CHUNK_PREFIX}${sessionId}-${index}`;
}

function requireSessionId(value) {
  if (typeof value !== "string" || !SESSION_ID_PATTERN.test(value)) {
    throw new Error("sessionId must be a bounded opaque token");
  }
  return value;
}

function extensionOrigin() {
  return `chrome-extension://${chrome.runtime.id}`;
}

function senderOrigin(sender) {
  // Chrome reports `origin` for every frame; an opaque origin ("null", e.g. a
  // sandboxed iframe) is never trusted. `url` is only a fallback for callers
  // that omit `origin`.
  if (typeof sender?.origin === "string") {
    return sender.origin && sender.origin !== "null" ? sender.origin : null;
  }
  const url = String(sender?.url || "");
  const extensionBase = `${extensionOrigin()}/`;
  if (url.startsWith(extensionBase)) return extensionOrigin();
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.origin : null;
  } catch (_error) {
    return null;
  }
}

/**
 * Classify a runtime.onMessage sender as "extension_page", "content_script",
 * or "untrusted". Chrome fills `sender.id`, `sender.origin`, `sender.url` and
 * `sender.tab` in the browser process, so a compromised page cannot forge them.
 * An extension page opened in a tab (the dashboard) still has the extension
 * origin; a content script has the web page origin and a tab.
 */
function classifySender(sender) {
  if (!sender || sender.id !== chrome.runtime.id) return "untrusted";
  const origin = senderOrigin(sender);
  if (origin === extensionOrigin()) return "extension_page";
  if (origin && Number.isInteger(sender.tab?.id) && sender.tab.id >= 0) return "content_script";
  return "untrusted";
}

function originMatchesScope(origin, scopeOrigin) {
  try {
    return Boolean(origin) && origin === new URL(scopeOrigin).origin;
  } catch (_error) {
    return false;
  }
}

// The only session fields a content script needs. Participant, task, actor,
// executor, notes, and lifecycle counters never enter a web-page renderer.
function contentSessionView(metadata) {
  return {
    schema_version: metadata.schema_version,
    session_id: metadata.session_id,
    status: metadata.status,
    scope_origin: metadata.scope_origin,
    raw_key_capture: Boolean(metadata.raw_key_capture),
    include_query: Boolean(metadata.include_query),
    current_document_epoch: Number(metadata.current_document_epoch || 0),
    current_step_id: metadata.current_step_id || null
  };
}

async function startSession(input) {
  const request = input || {};
  const clean = CUIRecorderCore.validateSessionInput(request);
  if (!Number.isInteger(request.tabId) || request.tabId < 0) throw new Error("tabId must identify a browser tab");
  await hardenStorageAccess();
  const root = await loadRoot();
  if (root.activeSessionId) {
    const activeMetadata = await getMetadata(root.activeSessionId);
    if (activeMetadata?.status === "recording") {
      let recordingTabExists = true;
      try {
        await chrome.tabs.get(activeMetadata.tab_id);
      } catch (_error) {
        recordingTabExists = false;
      }
      if (recordingTabExists) {
        throw new Error("A recording session is already active");
      } else {
        await finalizeSession(root.activeSessionId, "interrupted", "Recording tab was no longer available");
      }
    } else {
      root.activeSessionId = null;
      await saveRoot(root);
    }
  }
  const refreshedRoot = await loadRoot();
  if (refreshedRoot.sessionIds.length >= MAX_SESSIONS) {
    throw new Error(`Local session limit (${MAX_SESSIONS}) reached; export and delete a session before recording`);
  }
  if ((await storageBytesInUse(null)) >= MAX_TOTAL_STORAGE_BYTES) {
    throw new Error("Local recorder byte quota reached; export and delete sessions before recording");
  }
  const frameIds = await ensureRecorderInjected(request.tabId);
  const sessionId = crypto.randomUUID();
  const metadata = {
    ...clean,
    schema_version: CUIRecorderCore.CURRENT_SCHEMA_VERSION,
    session_id: sessionId,
    started_at: new Date().toISOString(),
    stopped_at: null,
    status: "recording",
    event_count: 0,
    chunk_count: 0,
    timing_model: "session-global-monotonic-v1",
    event_seq_last: 0,
    document_epoch_count: 1,
    dropped_event_count: 0,
    rejected_event_count: 0,
    flush_failure_count: 0,
    quota_rejection_count: 0,
    approx_event_bytes: 0,
    current_document_epoch: 0,
    last_event_document_epoch: -1,
    last_t_session_ms: -MIN_TIME_INCREMENT_MS,
    started_epoch_ms: Date.now(),
    current_step_id: null,
    known_step_ids: [],
    tab_id: request.tabId
  };
  refreshedRoot.activeSessionId = sessionId;
  refreshedRoot.sessionIds = [sessionId, ...refreshedRoot.sessionIds.filter((id) => id !== sessionId)];
  try {
    await commitMetadataAndRoot(metadata, refreshedRoot);
  } catch (error) {
    await chrome.storage.local.remove(`${SESSION_PREFIX}${sessionId}`);
    throw error;
  }
  try {
    await sendRecorderMessageToFrames(
      request.tabId,
      { type: "RECORDER_SESSION_STARTED", session: contentSessionView(metadata) },
      frameIds
    );
    await setRecordingBadge(request.tabId, true);
  } catch (error) {
    try {
      await sendRecorderMessageToFrames(request.tabId, { type: "RECORDER_SESSION_STOPPED" }, frameIds);
    } catch (_stopError) {
      // The initial injection may have failed before a listener existed.
    }
    await finalizeSession(sessionId, "incomplete", `Could not start page capture: ${error.message}`);
    throw error;
  }
  return metadata;
}

async function appendEvents(sessionId, incoming, senderTabId) {
  const root = await loadRoot();
  if (root.activeSessionId !== sessionId) return { accepted: 0, rejected: 0, inactive: true };
  const metadata = await getMetadata(sessionId);
  if (!metadata || metadata.status !== "recording") return { accepted: 0 };
  if (senderTabId !== metadata.tab_id) throw new Error("Event batch does not match the recording tab");
  const recentStart = Math.max(0, metadata.chunk_count - 3);
  const recentKeys = Array.from(
    { length: metadata.chunk_count - recentStart },
    (_value, index) => `${CHUNK_PREFIX}${sessionId}-${recentStart + index}`
  );
  const recentChunks = recentKeys.length ? await storageGet(recentKeys) : {};
  const seenEventIds = new Set(
    recentKeys.flatMap((key) => recentChunks[key] || []).map((event) => event?.event_id).filter(Boolean)
  );
  const incomingList = Array.isArray(incoming) ? incoming : [];
  const batchCandidates = incomingList.slice(0, MAX_BATCH_SIZE);
  const remainingCapacity = Math.max(0, MAX_EVENTS_PER_SESSION - metadata.event_count);
  const candidates = batchCandidates.slice(0, remainingCapacity);
  const events = [];
  let rejected = Math.max(0, incomingList.length - candidates.length);
  let quotaRejected = metadata.event_count + batchCandidates.length > MAX_EVENTS_PER_SESSION
    ? Math.max(0, batchCandidates.length - remainingCapacity)
    : 0;
  let approximateBytes = Number(metadata.approx_event_bytes || 0);
  let remainingGlobalBytes = Math.max(0, MAX_TOTAL_STORAGE_BYTES - await storageBytesInUse(null));
  let lastSessionTime = Number(metadata.last_t_session_ms);
  const lastStoredDocumentEpoch = Number(metadata.last_event_document_epoch ?? -1);
  if (!Number.isFinite(lastSessionTime)) lastSessionTime = -MIN_TIME_INCREMENT_MS;
  for (const candidate of candidates) {
    try {
      const clean = CUIRecorderCore.sanitizeNormalizedEvent(candidate, {
          rawKeyCapture: metadata.raw_key_capture,
          includeQuery: metadata.include_query
        });
      if (seenEventIds.has(clean.event_id)) {
        rejected += 1;
        continue;
      }
      const requestedDocumentEpoch = Number(candidate?.document_epoch);
      const documentEpoch = Number.isInteger(requestedDocumentEpoch)
        ? requestedDocumentEpoch
        : metadata.current_document_epoch;
      if (
        documentEpoch < 0 ||
        documentEpoch !== metadata.current_document_epoch ||
        documentEpoch < lastStoredDocumentEpoch ||
        (events.length && documentEpoch < events.at(-1).document_epoch)
      ) {
        rejected += 1;
        continue;
      }
      const wallRelative = Math.max(0, Number(clean.epoch_ms) - Number(metadata.started_epoch_ms));
      const tSessionMs = Math.max(
        lastSessionTime + MIN_TIME_INCREMENT_MS,
        Number.isFinite(wallRelative) ? wallRelative : lastSessionTime + MIN_TIME_INCREMENT_MS
      );
      const event = CUIRecorderCore.enrichEventV2(clean, {
        rawKeyCapture: metadata.raw_key_capture,
        includeQuery: metadata.include_query,
        eventSeq: metadata.event_count + events.length + 1,
        documentEpoch,
        tSessionMs,
        tDocumentMs: candidate?.t_document_ms ?? clean.monotonic_ms,
        stepId: resolveEventStepId(candidate, metadata)
      });
      const eventBytes = JSON.stringify(event).length * 2;
      if (
        approximateBytes + eventBytes > MAX_APPROX_SESSION_BYTES ||
        eventBytes > remainingGlobalBytes
      ) {
        rejected += 1;
        quotaRejected += 1;
        continue;
      }
      seenEventIds.add(event.event_id);
      lastSessionTime = event.t_session_ms;
      approximateBytes += eventBytes;
      remainingGlobalBytes -= eventBytes;
      events.push(event);
    } catch (_error) {
      rejected += 1;
    }
  }
  if (!events.length) {
    if (rejected) {
      metadata.rejected_event_count += rejected;
      metadata.quota_rejection_count += quotaRejected;
      await setMetadata(metadata);
    }
    return { accepted: 0, rejected, eventCount: metadata.event_count };
  }

  let chunkIndex = Math.max(0, metadata.chunk_count - 1);
  let chunkKey = `${CHUNK_PREFIX}${sessionId}-${chunkIndex}`;
  let chunk = recentChunks[chunkKey] || [];
  if (metadata.chunk_count === 0) {
    metadata.chunk_count = 1;
  }
  const updates = {};

  for (const event of events) {
    if (chunk.length >= CHUNK_SIZE) {
      updates[chunkKey] = chunk;
      chunkIndex += 1;
      chunkKey = `${CHUNK_PREFIX}${sessionId}-${chunkIndex}`;
      chunk = [];
      metadata.chunk_count = chunkIndex + 1;
    }
    chunk.push(event);
  }
  const nextMetadata = {
    ...metadata,
    event_count: metadata.event_count + events.length,
    event_seq_last: metadata.event_count + events.length,
    last_t_session_ms: lastSessionTime,
    last_event_document_epoch: events.at(-1).document_epoch,
    approx_event_bytes: approximateBytes,
    rejected_event_count: metadata.rejected_event_count + rejected,
    quota_rejection_count: metadata.quota_rejection_count + quotaRejected
  };
  updates[chunkKey] = chunk;
  updates[`${SESSION_PREFIX}${metadata.session_id}`] = nextMetadata;
  try {
    await storageSet(updates);
  } catch (error) {
    const failureMetadata = {
      ...metadata,
      flush_failure_count: metadata.flush_failure_count + 1,
      quota_rejection_count:
        metadata.quota_rejection_count + (isQuotaError(error) ? events.length : 0)
    };
    try {
      await setMetadata(failureMetadata);
    } catch (_metricsError) {
      // A full quota may also prevent the bounded metrics update. The caller
      // still receives the original failure and retains its batch for retry.
    }
    throw error;
  }
  return { accepted: events.length, rejected, eventCount: nextMetadata.event_count };
}

async function finalizeSession(sessionId, status, captureError = null) {
  const root = await loadRoot();
  const metadata = await getMetadata(sessionId);
  if (!metadata) {
    if (root.activeSessionId === sessionId) {
      root.activeSessionId = null;
      await saveRoot(root);
    }
    throw new Error("Active session metadata is missing");
  }
  metadata.stopped_at = new Date().toISOString();
  metadata.status = status;
  metadata.capture_error = captureError ? String(captureError).slice(0, 300) : null;
  await setRecordingBadge(metadata.tab_id, false);
  if (root.activeSessionId === sessionId) root.activeSessionId = null;
  await commitMetadataAndRoot(metadata, root);
  return metadata;
}

async function setActiveStep(stepId) {
  const root = await loadRoot();
  if (!root.activeSessionId) throw new Error("No recording session is active");
  const metadata = await getMetadata(root.activeSessionId);
  if (!metadata || metadata.status !== "recording") throw new Error("Active session metadata is missing");
  const requested = String(stepId || "").trim();
  if (requested && !CUIRecorderCore.STEP_ID_PATTERN.test(requested)) {
    throw new Error("stepId must be an opaque token containing only letters, digits, dot, underscore, colon, or hyphen");
  }
  const cleanStepId = requested || null;
  const knownStepIds = Array.isArray(metadata.known_step_ids)
    ? metadata.known_step_ids.filter((value) => typeof value === "string")
    : [];
  if (cleanStepId && !knownStepIds.includes(cleanStepId)) {
    if (knownStepIds.length >= MAX_STEP_IDS) {
      throw new Error(`Session step-id limit (${MAX_STEP_IDS}) reached`);
    }
    knownStepIds.push(cleanStepId);
  }
  metadata.known_step_ids = knownStepIds;
  metadata.current_step_id = cleanStepId;
  await setMetadata(metadata);
  await sendRecorderMessageToFrames(metadata.tab_id, {
    type: "RECORDER_STEP_CHANGED",
    stepId: cleanStepId
  });
  return { step_id: cleanStepId };
}

async function reportDroppedEvents(sessionId, count, senderTabId) {
  const root = await loadRoot();
  if (root.activeSessionId !== sessionId) throw new Error("Dropped-event report does not match the active session");
  const metadata = await getMetadata(sessionId);
  if (!metadata || metadata.status !== "recording") throw new Error("Active session metadata is missing");
  if (metadata.tab_id !== senderTabId) throw new Error("Dropped-event report does not match the recording tab");
  const dropped = Math.round(Number(count));
  if (!Number.isInteger(dropped) || dropped < 1 || dropped > MAX_EVENTS_PER_SESSION) {
    throw new Error("Dropped-event count is outside the session bound");
  }
  metadata.dropped_event_count = Number(metadata.dropped_event_count || 0) + dropped;
  await setMetadata(metadata);
  return { dropped_event_count: metadata.dropped_event_count };
}

async function stopSession() {
  const root = await loadRoot();
  const sessionId = root.activeSessionId;
  if (!sessionId) throw new Error("No recording session is active");
  const metadata = await getMetadata(sessionId);
  if (!metadata) return finalizeSession(sessionId, "incomplete", "Active session metadata was missing");

  let captureError = null;
  try {
    await sendRecorderMessageToFrames(metadata.tab_id, { type: "RECORDER_SESSION_STOPPED" });
  } catch (error) {
    captureError = `Final flush could not be confirmed: ${error.message}`;
  }
  return finalizeSession(sessionId, captureError ? "incomplete" : "complete", captureError);
}

function resolveEventStepId(candidate, metadata) {
  if (!Object.prototype.hasOwnProperty.call(candidate || {}, "step_id")) {
    return metadata.current_step_id || null;
  }
  if (candidate.step_id === null || candidate.step_id === "") return null;
  const requested = String(candidate.step_id).trim();
  if (!CUIRecorderCore.STEP_ID_PATTERN.test(requested)) return null;
  const known = Array.isArray(metadata.known_step_ids) ? metadata.known_step_ids : [];
  return known.includes(requested) ? requested : null;
}

async function refreshDynamicFrames(sessionId, senderTabId, senderUrl) {
  const root = await loadRoot();
  if (!root.activeSessionId || root.activeSessionId !== sessionId) {
    return { ignored: true, reason: "inactive_session" };
  }
  const metadata = await getMetadata(sessionId);
  if (!metadata || metadata.status !== "recording" || metadata.tab_id !== senderTabId) {
    return { ignored: true, reason: "wrong_tab" };
  }
  if (senderUrl) {
    try {
      if (new URL(senderUrl).origin !== new URL(metadata.scope_origin).origin) {
        return { ignored: true, reason: "outside_scope" };
      }
    } catch (_error) {
      return { ignored: true, reason: "invalid_sender_url" };
    }
  }
  const frameIds = await ensureRecorderInjected(metadata.tab_id);
  await sendRecorderMessageToFrames(
    metadata.tab_id,
    { type: "RECORDER_SESSION_STARTED", session: contentSessionView(metadata) },
    frameIds
  );
  return { ignored: false, reached_frame_count: frameIds.length };
}

function scheduleDynamicFrameRefresh(sessionId, senderTabId, senderUrl) {
  if (!Number.isInteger(senderTabId)) {
    return Promise.resolve({ ignored: true, reason: "missing_sender_tab" });
  }
  const existing = dynamicFrameRefreshes.get(senderTabId);
  if (existing) return existing;
  const operation = enqueueLifecycle(
    () => refreshDynamicFrames(sessionId, senderTabId, senderUrl)
  ).finally(() => {
    if (dynamicFrameRefreshes.get(senderTabId) === operation) {
      dynamicFrameRefreshes.delete(senderTabId);
    }
  });
  dynamicFrameRefreshes.set(senderTabId, operation);
  return operation;
}

async function reconcileBrowserStartup() {
  await hardenStorageAccess();
  await writeQueue.catch(() => undefined);
  const root = await loadRoot();
  if (!root.activeSessionId) {
    await saveRoot(root);
    return;
  }
  const metadata = await getMetadata(root.activeSessionId);
  if (metadata?.status === "recording") {
    await finalizeSession(
      metadata.session_id,
      "interrupted",
      "Capture was finalized after the browser restarted or recovered from a crash"
    );
    return;
  }
  root.activeSessionId = null;
  await saveRoot(root);
}

async function readSession(sessionId) {
  const metadata = await getMetadata(sessionId);
  if (!metadata) throw new Error("Session not found");
  const keys = Array.from({ length: metadata.chunk_count }, (_value, index) => `${CHUNK_PREFIX}${sessionId}-${index}`);
  const chunks = keys.length ? await storageGet(keys) : {};
  const events = keys.flatMap((key) => chunks[key] || []);
  return {
    metadata,
    summary: CUIRecorderCore.summarizeEvents(events),
    events
  };
}

async function listSessions() {
  const root = await loadRoot();
  const rows = await Promise.all(root.sessionIds.map((id) => getMetadata(id)));
  const existing = rows.filter(Boolean);
  if (existing.length !== root.sessionIds.length) {
    root.sessionIds = existing.map((metadata) => metadata.session_id);
    if (root.activeSessionId && !root.sessionIds.includes(root.activeSessionId)) root.activeSessionId = null;
    await saveRoot(root);
  }
  return existing;
}

// Never read the whole store: `storage.local.get(null)` would load every
// session (up to 512 MiB) into the service worker. The chunk keys are derived
// from the metadata `chunk_count` (bounded by the per-session event ceiling),
// and `storage.local.getKeys()` (keys only, no values) also sweeps orphaned
// chunks left behind by an older recorder or an interrupted write.
async function sessionStorageKeys(sessionId) {
  const metadata = await getMetadata(sessionId);
  const declared = Math.round(Number(metadata?.chunk_count));
  const chunkCount = Number.isFinite(declared)
    ? Math.min(Math.max(0, declared), MAX_CHUNKS_PER_SESSION)
    : 0;
  const keys = new Set([`${SESSION_PREFIX}${sessionId}`]);
  for (let index = 0; index < chunkCount; index += 1) keys.add(chunkKey(sessionId, index));
  if (typeof chrome.storage.local.getKeys === "function") {
    const chunkStem = `${CHUNK_PREFIX}${sessionId}-`;
    for (const key of await chrome.storage.local.getKeys()) {
      if (key.startsWith(chunkStem)) keys.add(key);
    }
  }
  return [...keys];
}

async function deleteSession(sessionId) {
  const root = await loadRoot();
  if (root.activeSessionId === sessionId) throw new Error("Stop the active session before deleting it");
  const keys = await sessionStorageKeys(sessionId);
  await chrome.storage.local.remove(keys);
  const residue = await chrome.storage.local.get(keys);
  if (Object.keys(residue).length) throw new Error("Session deletion could not remove every indexed and orphaned chunk");
  root.sessionIds = root.sessionIds.filter((id) => id !== sessionId);
  await saveRoot(root);
  return { deleted: sessionId, removed_key_count: keys.length };
}

// Read-only, reduced state for content scripts: only the tab that owns the
// active session, in a frame of the scoped origin, learns the session view.
async function getContentState(sender) {
  const root = await loadRoot();
  if (!root.activeSessionId) return { activeSession: null };
  const metadata = await getMetadata(root.activeSessionId);
  if (
    !metadata ||
    metadata.status !== "recording" ||
    metadata.tab_id !== sender.tab.id ||
    !originMatchesScope(senderOrigin(sender), metadata.scope_origin)
  ) {
    return { activeSession: null };
  }
  return { activeSession: contentSessionView(metadata) };
}

async function getState() {
  const root = await loadRoot();
  const activeSession = root.activeSessionId ? await getMetadata(root.activeSessionId) : null;
  if (root.activeSessionId && (!activeSession || activeSession.status !== "recording")) {
    root.sessionIds = root.sessionIds.filter((id) => id !== root.activeSessionId || Boolean(activeSession));
    root.activeSessionId = null;
    await saveRoot(root);
  }
  return {
    activeSession: activeSession?.status === "recording" ? activeSession : null,
    sessionCount: root.sessionIds.length
  };
}

function authorizeMessage(type, sender) {
  if (!EXTENSION_PAGE_MESSAGES.has(type) && !CONTENT_SCRIPT_MESSAGES.has(type)) {
    throw new Error("Unknown recorder message");
  }
  const senderClass = classifySender(sender);
  const allowed = senderClass === "extension_page"
    ? EXTENSION_PAGE_MESSAGES
    : senderClass === "content_script"
      ? CONTENT_SCRIPT_MESSAGES
      : null;
  if (!allowed?.has(type)) {
    throw new Error(`${type} is not accepted from this sender`);
  }
  return senderClass;
}

async function handleMessage(message, sender) {
  const type = message?.type;
  const senderClass = authorizeMessage(type, sender);
  switch (type) {
    case "RECORDER_GET_STATE":
      return senderClass === "extension_page" ? getState() : getContentState(sender);
    case "RECORDER_START":
      return enqueueLifecycle(() => startSession(message.input));
    case "RECORDER_EVENT_BATCH": {
      const sessionId = requireSessionId(message.sessionId);
      return enqueueWrite(() => appendEvents(sessionId, message.events, sender.tab.id));
    }
    case "RECORDER_STOP":
      return enqueueLifecycle(async () => {
        await writeQueue.catch(() => undefined);
        return stopSession();
      });
    case "RECORDER_SET_STEP":
      return enqueueLifecycle(() => enqueueWrite(() => setActiveStep(message.stepId)));
    case "RECORDER_REPORT_DROPPED": {
      const sessionId = requireSessionId(message.sessionId);
      return enqueueWrite(() => reportDroppedEvents(sessionId, message.count, sender.tab.id));
    }
    case "RECORDER_FRAME_DISCOVERED":
      return scheduleDynamicFrameRefresh(
        requireSessionId(message.sessionId),
        sender.tab.id,
        sender.url
      );
    case "RECORDER_LIST_SESSIONS":
      return listSessions();
    case "RECORDER_GET_SESSION": {
      const sessionId = requireSessionId(message.sessionId);
      // A failed earlier write must not poison every later read.
      await writeQueue.catch(() => undefined);
      return readSession(sessionId);
    }
    case "RECORDER_DELETE_SESSION": {
      const sessionId = requireSessionId(message.sessionId);
      return enqueueLifecycle(() => deleteSession(sessionId));
    }
  }
  // authorizeMessage() rejects every type that is not handled above.
  throw new Error("Unknown recorder message");
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const run = async () => handleMessage(message, sender);

  run()
    .then((data) => sendResponse({ ok: true, data }))
    .catch((error) => sendResponse({ ok: false, error: error.message }));
  return true;
});

chrome.runtime.onInstalled.addListener((details) => {
  void enqueueLifecycle(async () => {
    await hardenStorageAccess();
    await writeQueue.catch(() => undefined);
    const root = await loadRoot();
    if (details?.reason === "update" && root.activeSessionId) {
      const metadata = await getMetadata(root.activeSessionId);
      if (
        metadata?.status === "recording" &&
        metadata.schema_version !== CUIRecorderCore.CURRENT_SCHEMA_VERSION
      ) {
        await finalizeSession(
          metadata.session_id,
          "interrupted",
          `Recorder upgraded from ${details.previousVersion || "an earlier version"}; start a new V2 session`
        );
        return;
      }
    }
    await saveRoot(root);
  }).catch((error) => console.error("Recorder installation migration failed", error));
});

chrome.runtime.onStartup.addListener(() => {
  void enqueueLifecycle(reconcileBrowserStartup)
    .catch((error) => console.error("Recorder browser-startup reconciliation failed", error));
});

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  if (changeInfo.status !== "complete") return;
  await enqueueLifecycle(async () => {
    // A navigation can race the old document's pagehide flush. Drain every
    // batch already queued, then reload metadata so an epoch update cannot
    // overwrite a newer event_count/chunk_count snapshot.
    const transition = await enqueueWrite(async () => {
      const root = await loadRoot();
      if (!root.activeSessionId) return null;
      const metadata = await getMetadata(root.activeSessionId);
      if (!metadata || metadata.tab_id !== tabId || metadata.status !== "recording") return null;
      if (tab.url && new URL(tab.url).origin !== new URL(metadata.scope_origin).origin) {
        await finalizeSession(metadata.session_id, "interrupted", "Capture stopped when the tab left the scoped origin");
        return null;
      }
      metadata.current_document_epoch = Number(metadata.current_document_epoch || 0) + 1;
      metadata.document_epoch_count = Math.max(
        Number(metadata.document_epoch_count || 1),
        metadata.current_document_epoch + 1
      );
      await setMetadata(metadata);
      return metadata;
    });
    if (!transition) return;
    const metadata = transition;
    try {
      const frameIds = await ensureRecorderInjected(tabId);
      await sendRecorderMessageToFrames(
        tabId,
        { type: "RECORDER_SESSION_STARTED", session: contentSessionView(metadata) },
        frameIds
      );
    } catch (error) {
      try {
        await sendRecorderMessageToFrames(tabId, { type: "RECORDER_SESSION_STOPPED" });
      } catch (_stopError) {
        // Navigation may have removed the content-script listener.
      }
      const reason = tab.url
        ? `Capture could not resume after navigation: ${error.message}`
        : "Capture stopped after navigation revoked the temporary activeTab grant";
      await finalizeSession(metadata.session_id, tab.url ? "incomplete" : "interrupted", reason);
    }
  });
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  await enqueueLifecycle(async () => {
    await writeQueue.catch(() => undefined);
    const root = await loadRoot();
    if (!root.activeSessionId) return;
    const metadata = await getMetadata(root.activeSessionId);
    if (!metadata || metadata.tab_id !== tabId || metadata.status !== "recording") return;
    await finalizeSession(metadata.session_id, "interrupted", "Capture stopped because the recording tab closed");
  });
});
