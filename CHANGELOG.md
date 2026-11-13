# Changelog

## 0.3.0 (unreleased)

### Added
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
