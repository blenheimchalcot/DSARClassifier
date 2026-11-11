# Changelog

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
