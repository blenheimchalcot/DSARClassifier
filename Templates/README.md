# Response templates: employee requests

The documents a response to an **employee's** (or former employee's) subject access request needs. The web app's Report page builds the Personal Data Report from `Personal Data Report.docx`, and the case page's release checklist treats every `.docx` here as a document the response needs (see docs/TOOLBOX.md).

| Template | For |
|---|---|
| `Subject Access Request DSARYYYYMMDD-9.docx` | The response letter: confirms the processing and its purposes, the requester's rights, and what is enclosed. Rename with the case reference. |
| `Personal Data Report.docx` | The personal data, extracted. **Built by the app**, never filled by hand: a *Personal identifiers* table, then one row per extract under *Extracted data*. |
| `Exemptions Report.docx` | What was withheld under the Data Protection Act 2018, and why. |
| `Data not in Scope.docx` | What was asked for but isn't the requester's personal data, and why. |

Complete every `[placeholder]` and remove every *INSERT* instruction before sending. The case page shows what is still left in each prepared copy.

- **Another kind of requester** (customers, learners): use that set instead, by pointing `DSAR_TEMPLATES_DIR` at it. It is not in this repository.
- **One case only**: put a tailored `Personal Data Report.docx` in that case's `data/templates/` folder. It takes precedence for that case.
- **Never put case data here.** These files are shared with everyone who uses the repository.
