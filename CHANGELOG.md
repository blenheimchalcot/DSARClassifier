# Changelog

## 0.3.0 (unreleased)

### Added
- **Web app: `dsar serve`** (`src/dsar/webapp/`, [docs/TOOLBOX.md](docs/TOOLBOX.md)). One local Flask server (127.0.0.1, one-off token, sign-in by name) for the case page and every toolbox tool. The case page shows each pipeline stage's status, `progress.json` and the audit-log chain check. Case-bound by default: mailboxes open only from the input manifest after a hash check, exports are saved read-only in `output/toolbox/` and audit-logged under the reviewer. `--free` runs without a case and logs nothing.
- **Mail viewer** (from the PSTViewer project): PST and MBOX folders, messages, attachments and search. Recipients come from the display properties when there are no headers, and the RTF body is used when there is no other. HTML bodies are sandboxed with remote content blocked. Saves messages (.eml), attachments and a whole mailbox (.xlsx) into the case.
- **Teams viewer** (from the PSTViewer project): every Teams conversation in a PST, grouped by thread ID (property `0x366D`) so a group or meeting chat stays whole when members change, plus participants and a PDF transcript with source hash and producer.
- The redaction tool is mounted in the app under the signed-in reviewer. Its events file now has one lock per file, shared across requests.
- `flask` and `reportlab` added to the dependencies.
- **Toolbox** (`src/dsar/toolbox/`, [docs/TOOLBOX.md](docs/TOOLBOX.md)), with its first tool: **redaction**. `dsar markup serve FOLDER` is a local (127.0.0.1, token) web page to approve, redact all or redact part of each element (transcript turn or paragraph), split mis-attributed turns, and export a redacted copy with the text removed and a redaction log. Decisions are append-only, hash-chained and tied to each file's SHA-256 and span. `dsar markup score FOLDER` adds a 0-10 hint per element from the local model.
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
