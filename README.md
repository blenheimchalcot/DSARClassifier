# DSARClassifier

Local pipeline for GDPR / UK GDPR Data Subject Access Requests over Microsoft Purview eDiscovery exports. It ingests PSTs, PST attachments and SharePoint/OneDrive/Teams files, finds the content that mentions the data subject, scores each piece 0–10 with a local LLM (Ollama), and produces a reviewer spreadsheet with full provenance.

Everything runs on the operator's machine. No subject data leaves it.

## Data handling rules

- Case data lives **outside** any cloud-synced folder, one directory per case: `<root>/<CASE-REF>/data/{input,output}`. Point `DSAR_DATA_DIR` in `.env` at it.
- `config.yaml` and `claude_strict.md` hold the data subject's details and are **gitignored**. Copy the `*.example.*` templates for each case.
- Every case keeps an append-only `output/AUDIT_LOG.md` (UTC times, actor, hashes).
- Nothing is deleted. Superseded databases are moved to `output/archive_pre-reingest_<date>/` with hashes re-verified after the move.

## Run order (per case)

```sh
cp .env.example .env                       # set DSAR_DATA_DIR
cp config.example.yaml config.yaml         # subject name, aliases, emails, provenance rules
cp claude_strict.example.md claude_strict.md

# 1. Put the unextracted Purview ZIPs in input/, hash them, extract to
#    input/PSTs, input/Files, input/metadata, then hash the extracted files
#    (output/pst_manifest_<date>.sha256).
dsar attachments            # 2. extract + catalogue every PST attachment (appends stage-4 hashes)
dsar ingest                 # 3. chunk everything into dsar.sqlite
dsar provenance             # 4. verify against the manifest, link to Purview custodian/location
dsar classify --plan        # 5. check scope
dsar classify               # 6. score with the local model
dsar report                 # 7. output/results.csv
```

Use `--config <path>` to run a case with its own config file.

## Ollama

Run the server with a context window large enough for the 8,000-character chunks, and match parallelism to `model.concurrency`:

```sh
OLLAMA_CONTEXT_LENGTH=8192 OLLAMA_NUM_PARALLEL=2 ollama serve
caffeinate -d -i -s &       # stop macOS sleeping during long runs
```

## Known limitations

- Embedded emails inside PST attachments (attach method 5, `IPM.Note`): pypff exposes headers and subject, not the body. These are flagged in `pst_attachments_catalogue.csv` for manual review.
- Attachments inside `.eml` files are listed by name, not extracted.
- Encrypted / sensitivity-labelled Office files, images, audio and video are catalogued and listed as skipped. They need manual review.
- The alias prefilter only scores chunks that name the subject. Review documents the subject wrote as whole files.
