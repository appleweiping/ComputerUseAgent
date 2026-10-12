# CUI Trace Recorder - isolated extension E2E report

- Overall: **PASSED**
- Run: 2026-08-09T23:15:24.512Z to 2026-08-09T23:17:00.984Z
- Cases: 14 passed, 0 failed, 0 skipped
- Runtime: Puppeteer 25.4.0 + Chrome/151.0.7922.108 (system Chrome with a new isolated profile)
- Actor boundary: the coarse actor field is `agent`, while `source_class=synthetic_coverage`; this is not a Browser-Use, Skyvern, or Stagehand run.
- Scope boundary: deterministic loopback fixtures only; no human-vs-agent comparison is claimed.
- Isolation: a unique disposable profile was used; the user's normal Chrome/profile was not read or modified.

## Validation matrix

| ID | Result | Validation | Evidence / detail |
|---|---|---|---|
| T00 | PASSED | isolated bootstrap, manifest, and fresh storage | {"extension_id":"dagbagkhhdpjmfgdgobghjjjcgdclekl","storage_keys":["cui-recorder-root"]} |
| T01 | PASSED | consent gate and versioned disclosure | {"consent_blocked_before_storage":true,"disclosure_version":"2026-08-03-v1"} |
| T02 | PASSED | real action broad-event capture and privacy invariants | {"session_id":"0ae4cf60-1c28-469d-a273-803acfdaf6f2","event_count":527,"captured_types":["auxclick","beforeinput","change","click","compositionend","compositionstart","compositionupdate","contextmenu","copy","cut","dblclick","drag","dragend","dragenter","dragleave","dragover","dragstart","drop","focusin","focusout","input","invalid","keydown","keyup","navigation","paste","pointercancel","pointerdown","pointerenter","pointerleave","pointermove","pointerout","pointerover","pointerup","reset","resize","scroll","selectionchange","submit","toggle","touchend","touchstart","wheel"],"raw_key_capture":false} |
| T03 | PASSED | dashboard inspect, JSON/JSONL/CSV export, and delete | {"exports":[{"format":"json","file":"results/week2_recorder/extension_e2e/exports/cui-trace-e2e-safe-happy-path-agent-0ae4cf60.json","bytes":675011,"sha256":"088c892a19bc7f472bffbe88596def5a1e9f4f7c71460ced39260950ab9c14ed","chrome_download_id":1,"cdp_guid":"f4b9a419-26f2-4b47-a289-9bcf2f4c292c","cdp_completed":true},{"format":"jsonl","file":"results/week2_recorder/extension_e2e/exports/cui-trace-e2e-safe-happy-path-agent-0ae4cf60.jsonl","bytes":453832,"sha256":"1c9ef85cb00ca382e36ce8e2ffa0fd57145b753c6468e5760a765163706ff642","chrome_download_id":2,"cdp_guid":"514a196b-b789-4089-ae7f-8cb28568a2db","cdp_completed":true},{"format":"csv","file":"results/week2_recorder/extension_e2e/exports/cui-trace-e2e-safe-happy-path-agent-0ae4cf60.csv","bytes":145423,"sha256":"e6c7771b522e760f15b60dd8b8aa060df0d228f2fa3dd5c6a6289f5b624bc1ef","chrome_download_id":3,"cdp_guid":"df70cfc0-0b5c-433f-80c0-41b9e8496e8c","cdp_completed":true}],"delete_verified":true} |
| T04 | PASSED | same-origin reload resumes capture | {"navigation_events":2} |
| T05 | PASSED | cross-origin navigation interrupts and clears indicators | {"status":"interrupted","capture_error":"Capture stopped after navigation revoked the temporary activeTab grant"} |
| T06 | PASSED | recording tab closure interrupts without stranding active state | {"status":"interrupted","active_session_cleared":true} |
| T07 | PASSED | MV3 service-worker restart during active capture | {"worker_restart_observed":true,"event_count":126} |
| T08 | PASSED | storage rollover beyond one 500-event chunk | {"event_count":1405,"chunk_lengths":[500,500,405]} |
| T09 | PASSED | raw-key double confirmation and private-field invariant | {"dismissed_confirmation_created_no_session":true,"ordinary_raw_key_present":true,"private_key_redaction_verified":true,"raw_trace_exported":false,"raw_trace_deleted_immediately":true} |
| T10 | PASSED | protected browser page is not recordable | {"start_disabled":true,"active_session_created":false} |
| T11 | PASSED | event-time step boundary survives one delayed content batch | {"delayed_step_id":"step-01","next_step_id":"step-02","event_seq_order":[11,15]} |
| T12 | PASSED | dynamic same-origin iframe capture and all-frame final flush | {"captured_frame_event_count":126,"query_and_input_content_absent":true,"frame_indicator_cleared":true,"cross_origin_without_grant_excluded":true} |
| T13 | PASSED | trusted-only storage and post-stop negative control | {"trusted_context_storage_only":true,"event_count_at_stop":12,"event_count_after_negative_control":12} |

## Evidence set

- `validation_summary.json`: machine-readable case outcomes and diagnostics.
- `run_manifest.json`: pinned runtime, source hash, commit, isolation, and claim boundary.
- `storage_lifecycle_summary.json`: storage/chunk/delete assertions without raw typed content.
- `exports_manifest.json`: retained safe-mode export hashes.
- `event_coverage_summary.json`: supported, naturally observed, synthetic, and recorder-generated classes kept separate.
- `agent_extension_e2e_trace.json`: legacy-named sanitized synthetic-coverage trace produced by the real extension.
- `screenshots/`: real action popup, page indicator, dashboard, interruption, rollover, raw-mode warning, and protected-page UI.
- `exports/`: dashboard-produced safe JSON, JSONL, and CSV samples.

## Important interpretation

This report validates extension behavior and data lifecycle. It does not substitute for a real CUI-framework run, consented human data collection, scientific-simulator deployment, or a matched human-vs-agent study. Raw-key mode was tested only with fixed synthetic input; that trace was not exported and was deleted immediately.
