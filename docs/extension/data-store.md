# Local data store

The recorder uses `chrome.storage.local` only: no database, server, account or
network connection. One root record indexes sessions, one metadata record
describes each session, and events are split into chunks of at most 500.
Storage is restricted to trusted extension contexts
(`setAccessLevel("TRUSTED_CONTEXTS")`) before the first write, so web pages and
content scripts cannot read it.

## Key layout

The key names keep the historical `cui-` namespace so that sessions stored by
earlier versions remain readable.

| Key | Value | Cardinality |
|---|---|---:|
| `cui-recorder-root` | Active-session pointer and newest-first list of session IDs | exactly 1 |
| `cui-session-<session_id>` | One session's metadata and lifecycle state | 1 per session |
| `cui-events-<session_id>-<chunk_index>` | Ordered array of normalized events | 0 or more per session |

`<session_id>` is a random UUID; `<chunk_index>` starts at `0`. Messages that
name a session must use a bounded opaque token
(`^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$`), so a message cannot address other keys.

## Limits

| Limit | Value |
|---|---:|
| Sessions in the root index | 250 (starting another fails; nothing is evicted) |
| Events per session | 100,000 |
| Approximate stored size per session | 64 MiB, measured as twice the JSON string length of each stored event |
| Total extension storage | 512 MiB (`getBytesInUse`) |
| Events per incoming batch | 1,000 |
| Events per chunk | 500 |
| Distinct step ids per session | 512 |

Events over a limit are rejected and counted in `rejected_event_count` and
`quota_rejection_count`; existing data is never deleted to make room.

## Example

```json
{
  "cui-recorder-root": {
    "activeSessionId": null,
    "sessionIds": ["7d52...", "118a..."]
  },
  "cui-session-7d52...": {
    "schema_version": "cui-trace-v2",
    "session_id": "7d52...",
    "actor_type": "agent",
    "capture_adapter": "browser_mv3",
    "source_class": "synthetic_coverage",
    "task_id": "e2e-safe-happy-path",
    "status": "complete",
    "event_count": 642,
    "event_seq_last": 642,
    "document_epoch_count": 2,
    "rejected_event_count": 0,
    "chunk_count": 2
  },
  "cui-events-7d52...-0": [
    { "schema_version": "cui-trace-v2", "event_seq": 1, "document_epoch": 0, "t_session_ms": 0, "t_document_ms": 12.4, "event_id": "...", "type": "navigation" }
  ],
  "cui-events-7d52...-1": [
    { "schema_version": "cui-trace-v2", "event_seq": 501, "document_epoch": 1, "t_session_ms": 8104.2, "t_document_ms": 44.1, "event_id": "...", "type": "click" }
  ]
}
```

The shortened events show the key relationship only; see the
[trace schema](data-schema.md) for every field. While a session is active its
metadata also holds internal bookkeeping (`tab_id`, current document epoch and
step, known step ids, approximate byte count) that is removed from exports.

## Write lifecycle

1. **Start** (extension page only): validate consent and metadata, inject into
   the selected tab, then commit the session metadata and the root index in
   one storage write. Content scripts receive only a reduced session view
   (id, status, scope origin, privacy flags, document epoch, step id).
2. **Append** (content script of the recording tab only): bounded batches. The
   service worker re-sanitizes every event, rejects recently seen event IDs,
   assigns the authoritative V2 sequence and session clock, and writes the
   updated chunk and metadata in one storage write.
3. **Stop** (extension page only): wait for queued writes and the final flush
   from every injected frame, then mark the session `complete` and clear the
   active pointer in one write. Leaving the scoped origin, closing the tab or
   a failed final flush yields `interrupted` or `incomplete` instead. A browser
   restart finalizes a still-active session as `interrupted`.
4. **Read/export** (extension page only): load the metadata and chunks
   `0..chunk_count-1` in order. A failed earlier write does not block later
   reads.
5. **Delete** (extension page only; not the active session): remove the
   metadata record, the chunk keys `0..chunk_count-1` (the count is capped at
   the per-session maximum), and any orphaned `cui-events-<session_id>-*` key
   found through `storage.local.getKeys()`, which lists keys without loading
   values. The worker never calls `storage.local.get(null)`; 0.4.0 did, which
   loaded the entire store (up to 512 MiB) into the service worker on every
   delete. Deletion is verified by reading the removed keys back.

## Consistency rules

- Only the tab that owns the active session may append events or report
  dropped events, and only content scripts may do so.
- Same-origin frames present at start and frames added later are injected in
  the owning tab only. Frame-discovery notifications carry no URL or content;
  the worker rechecks the sender tab and scoped origin before reinjecting.
- Sequence and session time are assigned by the worker; a page cannot choose
  its final `event_seq`, `document_epoch` or `t_session_ms`.
- Step ids attached to events must have been registered by an extension page.

## Inspecting the store

Use the dashboard: **Inspect** shows sanitized metadata, the summary and a
sample of events before export. For engineering diagnostics on an unpacked
build you can open the service worker console from `chrome://extensions` and
call `await chrome.storage.local.getKeys()` (or read one key with
`chrome.storage.local.get("cui-recorder-root")`). Do not paste a participant's
store into issues or chat; export only the intended pseudonymized session.
