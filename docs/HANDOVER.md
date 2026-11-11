# Handing the platform over to another organisation

Use this when another organisation (a new data controller) will run DSARs with this platform on its own machine. The receiving organisation's operator should be able to go from the release zip to a ready case using only the documents listed below.

## 1. The handover pack

Give the receiving organisation a **release**, not a working copy:

| Item | Where |
|---|---|
| Release zip `DSARClassifier-<version>.zip` and its SHA-256 | GitHub release, built by `scripts/release.sh` |
| README.md | overview, design principle, run order |
| docs/DEPLOYMENT.md | installing on their machine |
| docs/OPERATIONS.md | operating rules and run-book: **read before any case** |
| docs/COVERAGE.md | what is read and what needs a person |
| docs/REVIEWER_GUIDE.md | for their HR and privacy reviewers |
| CHANGELOG.md | what changed in this version |
| Templates | `config.example.yaml`, `claude_strict.example.md`, `CLAUDE.example.md`, `.env.example` |
| requirements.lock | exact validated package versions |

**Not part of the pack:** `docs/MOVING_LAPTOP.md` (our own operator's setup), any case data, configs, prompts, audit logs or Claude memory from our cases. A release zip built from a tag holds only tracked files, and every case-specific file is gitignored. Check anyway (§3).

## 2. Their setup, step by step

1. Unzip the release and check its SHA-256 against the release page.
2. Install: DEPLOYMENT §2 (Python, `pip install -r requirements.lock && pip install -e . --no-deps`, tests pass).
3. Ollama and the model: DEPLOYMENT §3. Check the digest matches `model.expected_digest`.
4. Create a case: `dsar init-case <CASE-REF> --root <local folder> --controller "<their legal entity>"`.
5. Fill in the subject in the case's `config.yaml` and `claude_strict.md`.
6. `dsar doctor --config ... --system-prompt ...`: no FAIL.
7. Follow OPERATIONS §2 from `dsar manifest` onwards.

## 3. Sign-off checklist

Record each answer with `dsar audit note` on their first case, and keep a copy with the handover record.

- [ ] Release version and zip SHA-256 handed over; `dsar --version` matches on their machine
- [ ] Release zip checked: no case data, configs, prompts or audit logs (`unzip -l`)
- [ ] Machine: owner, operator(s) with login access, full-disk encryption on (`dsar doctor`)
- [ ] Where case data lives (local path, outside any synced folder) and who can read it
- [ ] Retention: how long case data, outputs and audit logs are kept, and who deletes them
- [ ] How outputs (review workbook, results) reach their HR reviewers and DPO without leaving approved storage
- [ ] Their DPO has approved the reviewer exemption list (`dsar/review.py` `REASONS`) and `docs/REVIEWER_GUIDE.md`
- [ ] Model files: downloaded by them, or delivered on approved media with the digest checked
- [ ] First `dsar model-check` passed on their machine (throughput noted for planning)
- [ ] Support: who they contact, and how updates are delivered (new release + CHANGELOG)

## 4. Making a release (our side)

```sh
scripts/release.sh 0.2.0      # tests, then tag v0.2.0, build the zip, print its SHA-256
git push origin v0.2.0
gh release create v0.2.0 dist/DSARClassifier-0.2.0.zip --notes-file CHANGELOG.md
```

`scripts/release.sh` refuses to run with uncommitted changes, or if the version doesn't match `pyproject.toml` and `dsar/__init__.py`.
