# Retired Stagehand 3.7.1 adapter (historical record)

Stagehand 3.7.1 was the third framework of the frozen Week 1 protocol. It was
retired before any formal attempt ran because its reviewed production dependency
graph carried unresolved advisories (recorded in the `formal_security` block of
`../../../benchmark_manifest.json`). It is not an active install target.

The adapter lock and bridge were never published with this repository; only
this README was. Their identities are recorded here for provenance:

| File (source repository layout) | SHA-256 |
|---|---|
| `experiments/week1/adapters/node/package-lock.json` (Stagehand-era lock) | `28d70a21401de20411e8a4da0fdf1fce3fbe5dc3a1c1e4deddc2d796697fac00` |
| Stagehand bridge (`stagehand_bridge.mjs`) | `7ebaf9ac770fc34b8cca14a51bec1ba9a1e1776b70bcc16409d1aae3bac41e9b` |
| Supplemental pilot lock (`experiments/stagehand/package-lock.json`) | `78079db644e4bda185aab177e1e32201f32e8a43f76d50694e2ee17351192aac` |
| Supplemental pilot runner (`run_stagehand_agent.mjs`) | `56ddebf78274a4136221d6a7bdaea0973f5d26fa61ca4a86d1b635fb3ef56d96` |

No setup, dispatch, CI job or reproduction command installs or executes
Stagehand: `ACTIVE_FRAMEWORKS` in `../../adapter_contract.py` lists only
Browser-Use and Skyvern, and the dispatcher has no Stagehand entry. The
3 x 3 x 3 formal-study declaration remains an unaltered historical protocol with
0/27 completed attempts; it cannot be resumed as an active matrix.

No replacement adapter is implemented. Any future framework requires a new
reviewed configuration, lock, adapter contract and separately labelled evidence
rather than a change to this record.
