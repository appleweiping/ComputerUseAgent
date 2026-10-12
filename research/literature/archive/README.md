# Archived literature files

These files document an early checkpoint of the literature review (a 43-item survey
with 12, later 17, deep reads). Each one is superseded by the 68-item corpus in
`../generated/`, and the Markdown files say so in their own headers. No builder reads
them.

| File | What it was |
|---|---|
| `Literature_Survey_Week1.csv` | The 43-row survey table |
| `Literature_Deep_Read_12.csv` | The first 12 deep-read rows |
| `DEEP_READ_SYNTHESIS_12.md` | Narrative synthesis of those 12 deep reads |
| `HIGH_PRIORITY_DEEP_READ_ADDENDUM_5.md` | Five further deep reads (12 to 17) |
| `literature_high_priority_deep_read_addendum_qa.json` | QA record for that addendum |
| `FINAL_LITERATURE_AUDIT.md` | Audit of the 43-item checkpoint |

They are kept byte-for-byte as recorded, because they bind one another by SHA-256: the
QA record hashes both CSV files and the addendum, and the audit hashes both CSV files.
Their wording reflects the original working context: they mention an editable
spreadsheet export and an institutional shared-sheet workflow that are not part of this
repository, and some counts differ from the current corpus. Use `../generated/literature_truth_68.json` and
the parent [README](../README.md) for anything current.
