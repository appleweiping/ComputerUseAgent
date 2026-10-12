# Formal MiniWoB matrix: two failed launches (archived)

The formal MiniWoB study planned 27 attempts (3 frameworks x 3 tasks x 3
repetitions) and completed **0/27**. This directory keeps the only two public records:
two Browser-Use launches on the official MiniWoB `click-checkboxes` task, each with a
fresh Chrome profile and the real recorder extension. They are failure evidence, not
part of a result matrix.

In both launches the model provider returned an operational error
(`model_provider_error`) before the agent acted, so the agent made **zero actions** and
each trace held only the recorder-generated navigation record, which failed the formal
artifact gate. The first launch also hit a Browser-Use screenshot timeout; the adapter
weakness behind it was later reproduced and fixed, but its exact window state was not
recorded. The operator stopped the matrix after the zero-action pattern repeated. A
third launch was interrupted during startup and has no public record. A later provider
preflight for the same model also failed, with **HTTP 502**. No file here records an
HTTP 403.

The records were produced from source commit
`c56a7b5fe01126ef00157e123738ae280292422b`, which exists only in the private source
repository. Raw traces, framework logs, prompts and profiles are not published.

## Files

| File | Contents |
|---|---|
| `correction_manifest.json` | The claim boundary: per-attempt causes, the matrix disposition (27 planned, 0 completed, stopped early) and the failed provider preflight |
| `attempts/browser-use__click-checkboxes__r01__s20260808.json` | First launch: configuration, limits, result (`action_count` 0) and private-artifact identifiers |
| `attempts/browser-use__click-checkboxes__r02__s20260809.json` | Second launch, same structure |

Paths recorded inside these files use the original layout (`experiments/week1/...`);
see [`../../PROVENANCE.md`](../../PROVENANCE.md). The `private-...` artifact
identifiers name files that were never published. The counts and the HTTP status have
entries in [`../INDEX.json`](../INDEX.json), section `formal_studies`.
