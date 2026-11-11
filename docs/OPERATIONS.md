# Operating rules and run-book

These rules come from running real cases. Each one exists because something went wrong without it. Follow them on every case, on every machine.

## 1. Non-negotiable rules

| Rule | Why |
|---|---|
| **One directory and one `dsar.sqlite` per case**, outside any cloud-synced folder. Check `DSAR_DATA_DIR` and the subject in `config.yaml` before every command. | One DSAR must never be contaminated by another. |
| **No subject data in the code folder or in git.** `config.yaml`, `claude_strict.md`, `CLAUDE.md`, `.env`, `.claude/` and all case data are gitignored. Run the PD scan (section 6) before every commit. | The code folder is synced and the repo is shared. |
| **Hash before you touch.** SHA-256 manifest of the Purview ZIPs and every extracted file before ingest (`output/pst_manifest_<date>.sha256`). | Proves the bytes classified are the bytes Purview exported. |
| **Never delete.** Superseded DBs and outputs are moved (`mv`) to `output/archive_pre-reingest_<date>/runN_<reason>/` and re-hashed after the move. Processed PSTs go to `output/processed_pst_<date>_DELETABLE/`; only the operator deletes. | Chain of custody. |
| **Append-only `output/AUDIT_LOG.md`** from case open: UTC time, actor, action, evidence (hashes, counts, log paths). Log failures and retries too. Correct by a new entry, never by editing an old one. | The case must be demonstrable. |
| **Never silently drop content.** Persist email bodies verbatim (no signature or quoted-history stripping). After ingest, spot-check large files that produced few chunks. | A missing paragraph can be the only copy of something the subject is entitled to. |
| **Never trust model output.** Every score 7+ quote must be found in the source (`evidence_in_source`). The check catches invented quotes, not bad judgement, so sanity-check high scores too. | The model has quoted its own prompt example, and invented an email address. |
| **Exclude, don't delete.** Other DSARs' work product found in an export (results files, reports, prompts naming the subject) is labelled with `provenance:` rules and kept out of scope with `classify_scope`, with the decision logged. Ask the operator; don't assume. | Prior case output can be most of the hits. |

## 2. Case run order

```sh
# 0. Case dir: <root>/<CASE-REF>/data/{input,output}; AUDIT_LOG.md started; .env points at it
# 1. Hash the Purview ZIPs, extract to input/PSTs, input/Files, input/metadata, hash every file
dsar attachments --config <case config>   # 2. PST attachments out + catalogued (stage-4 hashes)
dsar ingest      --config <case config>   # 3. chunk into dsar.sqlite
dsar provenance  --config <case config>   # 4. verify against manifest; custodian/location per file
#    -> review other-DSAR material with the operator, set provenance labels + classify_scope
dsar classify --plan --config <case config>   # 5. check scope and chunk counts
dsar classify        --config <case config>   # 6. score (evidence checked, one retry)
dsar verify-evidence                          # 7. re-check every score 7+ quote
dsar report --out <case>/output/results.csv   # 8. full report
#    then derive results_scored.csv = rows with a raw_response (reviewers use this)
```

Before step 6, check `max(length(content))` for in-scope chunks; anything far over `chunking.max_chars` gets truncated by Ollama and the model summarises instead of scoring.

Hash `dsar.sqlite`, every output and every SQL script you run, and log them.

## 3. Ollama run-book (before any classify)

1. **Context window:** the default 4,096 tokens silently truncates 8,000-char chunks.
   `OLLAMA_CONTEXT_LENGTH=8192 OLLAMA_NUM_PARALLEL=<model.concurrency> ollama serve`
2. **Parallelism:** `OLLAMA_NUM_PARALLEL` must equal `model.concurrency` in the config, or requests just queue.
3. **Memory:** with gemma4:e4b (~10 GB) on 16 GB RAM, concurrency 2 is the ceiling; 4 degraded and then crashed. More RAM or a GPU server can go higher.
4. **Sleep:** on macOS run `caffeinate -d -i -s` for the whole run; display sleep caused ~16-minute stalls. On Linux servers, run classify under `tmux`/`systemd-run` so a dropped SSH session doesn't kill it.
5. **macOS app:** the Ollama app ignores shell variables. Run the binary directly (`.../Ollama.app/Contents/Resources/ollama serve`) rather than opening the app.
6. Record `ollama --version` and the model digest (`ollama list`) in the audit log. Validated: Ollama 0.40.13, `gemma4:e4b` digest `c6eb396dbd59`.

Long runs: classify resumes where it stopped; it only scores chunks without a classification. To prioritise, run a pass with a narrower `classify_scope` config first.

## 4. Fixing a defect after classification has started

1. Let any running classify finish.
2. Hash `dsar.sqlite` and logs, `mv` them to `output/archive_pre-reingest_<date>/runN_<reason>/`, re-hash.
3. Fingerprint code and config, re-run `dsar ingest` and `dsar provenance`.
4. Carry over only byte-identical classifications: `ATTACH` the archived DB read-only (`file:...?mode=ro`) and copy where `chunk_id`, `source_path`, `item_ref` **and** `content` all match. Save and hash the SQL. Record carried-over rows in `imported_classifications` when reusing across cases.
5. Classify the remainder, report, hash, log.

To withdraw individual classifications (for example failed evidence), use `dsar verify-evidence --supersede`: rows move to `superseded_classifications`, never deleted. Copy the DB to the archive first.

## 5. Known limitations (manual review needed)

- Embedded emails inside PST attachments (attach method 5, `IPM.Note`): pypff gives headers only. Read the body in Outlook or with `readpst`.
- Encrypted / sensitivity-labelled Office files, images, audio, video: catalogued, not read.
- Attachments inside `.eml` files: listed by name, not extracted.
- The alias prefilter only scores chunks that name the subject. Review documents the subject wrote as whole documents.
- Recordings referenced in emails (call recorders, Teams) are often not in the export; ask for them.
- `dsar report` writes every alias-matched chunk; on a big case that is over 1 GB. Reviewers use `results_scored.csv`.

## 6. Pre-commit PD scan

```sh
git add -A
git grep --cached -n -i -E '<subject names>|<aliases>|<emails>|<third-party names>|<phone/salary fragments>'
# must return nothing; then commit and push
```

## 7. Working with Claude Code on this project

Claude's own memory lives on the machine (`~/.claude/projects/...`), not in the repo. These docs are the durable record of how to work. A new machine's Claude session should be pointed at `README.md`, `docs/OPERATIONS.md` and the case's `AUDIT_LOG.md` before doing anything. Claude must confirm with the operator before anything destructive, outward-facing, or that changes a case's results.
