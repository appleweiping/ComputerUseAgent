# Event coverage: support is not observation

The extension listens for 59 browser event types and creates one additional
`navigation` record. Four statements are kept separate:

| Term | Meaning | Where the exact value lives |
|---|---|---|
| Supported | The content script has a listener and a normalization path | [`extension/event_coverage.json`](../../extension/event_coverage.json): 59 browser types plus `navigation` |
| Naturally observed in the fixture | The browser emitted the event with `is_trusted=true` during one E2E run | That run's `event_coverage_summary.json` |
| Synthetically exercised | The fixture dispatched the event (`is_trusted=false`) to reach a less common code path | That run's `event_coverage_summary.json` |
| Recorder generated | The recorder created a lifecycle record (`is_trusted=null`) | That run's `event_coverage_summary.json` |

Observed counts are evidence, not capabilities, so they live next to the trace
and run manifest that produced them; a later run cannot inherit them.
`event_coverage.json` deliberately carries no version number (the extension
manifest is the only version source).

## Retained 0.4.0 run

The archived bundle `research/evidence/recorder_e2e_v0.4.0/` (recorder 0.4.0,
2026-08-09, 14/14 cases) contains 527 events: 516 natural, 10 synthetic and 1
recorder-generated `navigation`. They cover 42 of the 59 supported types (35
naturally, 10 synthetically, 3 both ways). 17 supported types never occur in
that run: `beforetoggle`, `cancel`, `close`, `fullscreenchange`,
`fullscreenerror`, `gotpointercapture`, `hashchange`, `lostpointercapture`,
`offline`, `online`, `pagehide`, `pageshow`, `popstate`, `select`,
`touchcancel`, `touchmove`, `visibilitychange`. Their listeners are covered by
unit tests only.

Current-version runs write their own `event_coverage_summary.json` into the E2E
bundle (see [E2E](e2e.md)); bundles are CI artifacts and are not committed.

## Important boundary

`is_trusted` describes the input path, not the actor. Chrome marks
DevTools-protocol input as trusted, while page-dispatched synthetic events are
untrusted. It is excluded from detector features.
