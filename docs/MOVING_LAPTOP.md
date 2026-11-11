# Moving to another laptop

What moves by itself, what doesn't, and how to bring each part across.

| Part | Where it lives | How it gets to the new laptop |
|---|---|---|
| Code, docs, templates | OneDrive folder + GitHub | OneDrive syncs it. Re-attach git (step 2). |
| Live case config and prompts (`config.yaml`, `claude_strict.md`, `CLAUDE.md`, `.env`) | OneDrive folder (gitignored) | OneDrive syncs it. Check `.env` paths (step 4). |
| Git history | `~/Git/DSARClassifier.git` (local, deliberately not in OneDrive) | Re-created from GitHub (step 2). |
| Python environment (`.venv`) | OneDrive folder | **Doesn't transfer**; rebuild it (step 3). |
| Ollama + model | `~/Applications/Ollama.app`, `~/.ollama/models` | Reinstall and pull (step 5). |
| **Case data** (`<root>/<CASE-REF>/data`: PSTs, DBs, results, audit logs) | Local disk only, never synced | **You move it**, encrypted (step 6). |
| Claude's memory notes | `~/.claude/projects/<folder>/memory` (local) | Copied with the case data (step 7). |

## 1. Before leaving the old laptop

- No classify runs in progress (`ps aux | grep "dsar classify"`).
- `git status` in the OneDrive folder shows nothing uncommitted; push.
- OneDrive shows the folder fully synced.
- Hash every case directory you are taking (step 6) and log it in each case `AUDIT_LOG.md`.

## 2. Re-attach git

Once OneDrive has synced the folder on the new laptop (and GitHub access works: `gh auth login`):

```sh
cd "<OneDrive>/Sandboxes/DSARClassifier"
sh scripts/attach_git.sh            # creates ~/Git/DSARClassifier.git from GitHub
git status                          # should be clean
```

Don't copy `~/Git/...` into OneDrive, and don't run `git init` inside OneDrive.

## 3. Rebuild the Python environment

```sh
rm -rf .venv                        # the synced one points at the old laptop's Python
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock && pip install -e . --no-deps
python -m pytest -q && dsar --help
```

## 4. Check `.env` and paths

`.env` holds `DSAR_DATA_DIR`. If the username or case root differs on the new laptop, update it and record the change in the case audit log.

## 5. Ollama

Install Ollama to `~/Applications`, then `ollama pull gemma4:e4b` and check `ollama list` shows digest `c6eb396dbd59`. Start it as in OPERATIONS §3.

## 6. Move case data (personal data: handle as such)

Use an encrypted external drive (APFS encrypted, or an encrypted disk image), not OneDrive, email or a file-sharing service.

```sh
# old laptop
cd <root>                                   # e.g. ~/DSARClassifier
find <CASE-REF> -type f -print0 | sort -z | xargs -0 shasum -a 256 > <CASE-REF>_transfer_<date>.sha256
rsync -a <CASE-REF> <CASE-REF>_transfer_<date>.sha256 /Volumes/<encrypted drive>/

# new laptop
rsync -a /Volumes/<encrypted drive>/<CASE-REF> ~/DSARClassifier/
cd ~/DSARClassifier && shasum -a 256 -c <CASE-REF>_transfer_<date>.sha256 | grep -v ': OK$'   # must print nothing
```

Log the transfer (both hash files, drive, date) in the case `AUDIT_LOG.md`. Keep the old laptop's copy until the new one verifies; delete only when you're satisfied and your retention rules allow.

## 7. Claude's memory

Claude Code's memory for this project is stored per machine, under a folder named after the project path. A handover copy is kept next to the case data (`<root>/_handover_<date>/claude_memory/`) because some notes name data subjects; it moves with the case data, not through OneDrive or GitHub.

On the new laptop, open Claude Code in the project folder once (so it creates its project folder), then:

```sh
cp <root>/_handover_<date>/claude_memory/*.md ~/.claude/projects/<project-folder>/memory/
```

`<project-folder>` is the project path with `/` and `.` replaced by `-` (for example `-Users-<you>-Library-CloudStorage-OneDrive-...-DSARClassifier`). If it is missing, Claude still has `README.md`, `docs/` and the audit logs, which hold everything durable.
