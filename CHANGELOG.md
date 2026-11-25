# Changelog

## Unreleased

### Changed
- **What keeps the report a DRAFT is named where you find it.** The Personal Data Reviewer's *What's left to do* lists repeats per folder (its plain name, e.g. *Items scored 7 (24 Nov)*, with a *Confirm them* button for that folder) and says these keep the Personal Data Report a DRAFT. The Report page's *Finish in the Personal Data Reviewer* uses the same folder names (the folder's own name below), with *Confirm N repeat(s)* and *Decide N* buttons on each file. `dsar personal-data-report` lists each outstanding thing under *Still to do* and where to clear it, instead of only a count; the list is also in the audit entry (`still_to_do`).

## 0.3.1 (2026-11-18)

### Changed
- **Personal Data Report: extracts as one flat, unnumbered block** in the extracts row (no *Extract N* rows); markdown `**` removed from the report text. The register keeps numbers and exact text.
- **One review page:** the Personal Data Reviewer replaces Review in the top bar. Unreviewed classified items are brought in by score (`review intake`), items below 7 showing every paragraph, and every file has whole-item Extract / Leave out. Repeats group identical text (*Confirm all*).
- **Context** button on every element in the Personal Data Reviewer and on the Review page: the whole document, the whole original item, and the conversation (email thread or chat) around it, oldest first.
- Repeats show your earlier decision first, then the repeat to decide.
- **Personal Data Reviewer sections**, across every file in a folder: **To review (7+)** (filter by score), **6 and below** (filter by score; extract one if it should be in), **Repeats** (each beside the element it matches, with *Confirm match*, *Not a match* and *Confirm all*), and **Reviewed** (filter by decision, in the report or not, by hand or copied, reason, score band). *By file* keeps the per-file view. Each element carries its file, so decisions go to the right file and hash.
- **Personal Data Reviewer: only elements the model scores 7+ need a decision** (`REVIEW_MIN_SCORE`, operator instruction). Elements scored below 7 fold into a *Below 7* bar: no decision needed and nothing in the report, unless the reviewer opens one and extracts it. Elements not yet scored still need a decision. Elements with no letters or digits (`---`) need no decision either.
- Review-stack files show the item's own classification (model score, reason, evidence) at the top and in the file list.
- **Personal Data Reviewer: repeats.** An undecided element whose text you have already decided (in any of the request's folders) is folded into *Repeats*. **Apply the same decisions** records each one as your decision, with `copied_from` in the event log. Text extracted elsewhere becomes *Leave out: Duplicate – already in the Personal Data Report* (your practice); text left out for a reason gets the same reason; cuts are copied only onto identical text. Text you decided both ways is never copied. Short text must match exactly. The Report page shows how many undecided elements are repeats.
- **Fixed: text cut short after an emoji.** The browser counts an emoji as two characters, so text after one was shown a character or two short, and a *Leave part out* cut after it could land off. Such characters are now shown as □ (the file is unchanged), in the Personal Data Reviewer and on the Review page.
- Personal Data Reviewer: the reason boxes for **Leave out** and **Leave part out** open directly under the buttons, right-aligned, instead of after the element's text. *Leave out (Enter)* and *Cancel (Esc)* come first, with the reason below. The buttons show their keys: **E** Extract, **L** Leave out, **P** Leave part out (A and R still work). Esc closes *Leave part out* without asking when nothing is marked yet.
- **Fixed: an element extracted in part is one extract**, with its lines kept and the parts left out removed. Before, each stretch of text between cuts became its own extract, with its lines run together. A cut that spans a line break keeps the break; lines left empty are dropped. Text is never tidied beyond the cuts.
- Report preview: each source of each extract has **Edit in Personal Data Reviewer**, which opens that file with the element expanded and centred, ready to *Leave part out*. An extract chosen in Review instead has **Narrow in Personal Data Reviewer**, which sends that item as a one-item review stack. The Personal Data Reviewer links back to the Report preview.
- **Fixed: a workbook 'Disclose' no longer puts the whole passage in the Personal Data Report.** It carried headers, quoted threads and other people's details, including text left out in the Personal Data Reviewer. Such items now wait until passages are chosen. Any extract containing text left out in the Personal Data Reviewer (other than as a duplicate or already reviewed) is held out of the report and listed as a conflict.
- **Report page**: *Left to process*, grouped with titles, dates, scores and workbook notes, and a **Preview** laid out like the Word table. Outstanding items can be sent as a numbered **review stack** to the Personal Data Reviewer, to step through them there: each file is the item's exact text, and the report then takes them only from what is extracted.
- Top bar: **Cases** first, then **Current case: <ref>**.
- A request is worked entirely from its **main case**. Review, Report and the Personal Data Reviewer use the deeper search's database and folders directly, with no switching. Every change is logged in the main case and in the case holding the data. `dsar personal-data-report` works from the main case too.

## 0.3.0 (2026-11-17)

### Upgrading from an earlier pull of main
- `git pull`, then `pip install -r requirements.lock && pip install -e . --no-deps` and `python -m pytest -q`. No new packages since the web app (#19).
- No action needed on case databases: the new tables (`decisions`, `decision_spans`, `report_folder_choices`) are created, empty, the first time Review or Report is opened. Existing workbook decisions are read as they are.
- Put your organisation's `Personal Data Report.docx` in `Templates/` or point `DSAR_TEMPLATES_DIR` at it (see `.env.example`); it is not in git. Optionally set `DSAR_CASE_DOCS_ROOT` for the release checklist's document check.
- A case that is a deeper search under another needs `data/case.json` (docs/TOOLBOX.md); standalone cases need nothing.

### Added
- **Main case and deeper searches** (`data/case.json`), and a **release checklist** on the case page: deadline from the date received, what the case still needs (review, passages, Personal Data Reviewer folders, final report), the release documents checked against the templates (missing / unchanged / to complete / ready, plus the letter's enclosures), and confirmations logged in the main case. The redaction tool is now called the **Personal Data Reviewer**.
- **Review queue and Personal Data Report in the web app** (`dsar.disclosure`, `dsar.personal_data_report`, [docs/TOOLBOX.md](docs/TOOLBOX.md)). Review replaces the workbook: items scored 7+ with the whole text, evidence highlighted and checked, and decisions in five terms (extract, release, release with redactions, withhold, not the subject's data). Passages to extract or redact are selected in the text and tied to its SHA-256. Decisions are validated on entry, append-only in `decisions` / `decision_spans`, and each one is audit-logged. Workbook decisions carry over. Report fills the controller's `Personal Data Report.docx` template with the extracted text from Review and the redaction tool (each piece once, oldest first), plus a private source register, both read-only and hashed; DRAFT while anything is outstanding.
- **Cases page in the web app**: switch case without restarting `dsar serve`. Lists every case under the cases root (`--cases-root`) with its audit-log state (chain intact, chain problems, legacy). Switching is logged in both cases, closes open mailboxes, uses only the new case's config, and rotates the CSRF value so pages loaded for the old case can't act on the new one. Opening a legacy case needs confirmation and starts its hash-chained log.
- **Web app: `dsar serve`** (`src/dsar/webapp/`, [docs/TOOLBOX.md](docs/TOOLBOX.md)). One local Flask server (127.0.0.1, one-off token, sign-in by name) for the case page and every toolbox tool. The case page shows each pipeline stage's status, `progress.json` and the audit-log chain check. Case-bound by default: mailboxes open only from the input manifest after a hash check, exports are saved read-only in `output/toolbox/` and audit-logged under the reviewer. `--free` runs without a case and logs nothing.
- **Mail viewer** (from the PSTViewer project): PST and MBOX folders, messages, attachments and search. Recipients come from the display properties when there are no headers, and the RTF body is used when there is no other. HTML bodies are sandboxed with remote content blocked. Saves messages (.eml), attachments and a whole mailbox (.xlsx) into the case.
- **Teams viewer** (from the PSTViewer project): every Teams conversation in a PST, grouped by thread ID (property `0x366D`) so a group or meeting chat stays whole when members change, plus participants and a PDF transcript with source hash and producer.
- The redaction tool is mounted in the app under the signed-in reviewer. Its events file now has one lock per file, shared across requests.
- `flask` and `reportlab` added to the dependencies.
- **Toolbox** (`src/dsar/toolbox/`, [docs/TOOLBOX.md](docs/TOOLBOX.md)), with its first tool: **redaction**. `dsar markup serve FOLDER` is a local (127.0.0.1, token) web page to approve, redact all or redact part of each element (transcript turn or paragraph), split mis-attributed turns, and export a redacted copy with the text removed and a redaction log. Decisions are append-only, hash-chained and tied to each file's SHA-256 and span. `dsar markup score FOLDER` adds a 0-10 hint per element from the local model. "Export Personal Data extracts" writes the de-duplicated report text and a private extract register (sources) for the whole folder.
- `dsar review export --outstanding` (undecided items only) and `--ids FILE` (chosen items, any score).
- A **status** column in review workbooks (*To review* first, then *Done*).
- [docs/REVIEW_APP_PLAN.md](docs/REVIEW_APP_PLAN.md): plan for a local review web app.

### Fixed
- Review workbooks opened in Excel with "We found a problem with some content". The rich-text evidence highlighting written by openpyxl is rejected by Excel, so the evidence is now marked with 【 】 in plain text.
- Review export crashed on control characters in extracted text; they are now replaced with spaces.

## 0.2.0 (unreleased)

Productionised for handover to other organisations (#9).

### Added
- **Audit logging** in every command: `output/AUDIT_LOG.md` plus hash-chained `AUDIT_LOG.jsonl`; `dsar audit verify | show | note`; `output/progress.json` for long runs; `dsar manifest` for append-only input hashing (#4).
- **`dsar doctor`**: checks packages, Ollama (local, not on the network, model digest, context, parallelism), data dir, disk, encryption, RAM and placeholders (#2).
- **`dsar model-check`**: accuracy on synthetic examples, context-window truncation, format, determinism, throughput and run-time estimate. `classify` requires a pass (#8).
- **`dsar qa ingest`**: every file accounted for, reconciled with Purview's item report, low-yield and oversize detection. `classify` requires a pass (#5).
- **`dsar qa classify` / `qa import-sample`**: format, coverage, evidence, missed special categories, distribution, consistency, and a blind reviewer sample (#6).
- **`dsar review export | import | status`**: HR reviewer workbook for scores 7-10 with decisions and exemptions; `docs/REVIEWER_GUIDE.md` (#7).
- **Attachments inside saved `.eml` / `.msg` files** extracted and classified; `docs/COVERAGE.md` (#3, partial).
- **`dsar init-case`**, **`dsar --version`**, `docs/HANDOVER.md`, `scripts/release.sh` (#1).

### Changed
- PSTs are read with pypff directly; `libratom` removed. `requirements.lock` now installs cleanly with dependency resolution (it previously needed `--no-deps`).
- `classify` refuses to start without passing ingest QA and model check (`--skip-checks "<reason>"` overrides, audit-logged).
- Live tests are opt-in: `pytest -m live`.

### Upgrading a running case
Run `dsar qa ingest` and `dsar model-check` before the next `dsar classify`. Add `model.expected_digest` to the case config. Existing classifications are unaffected.

## 0.1.0

Initial pipeline: ingest (PST, PST attachments, SharePoint/OneDrive, Teams), provenance, classification with a local Ollama model, evidence verification, CSV report.
