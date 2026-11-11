# Deploying on a new machine or server

Use this to build the pipeline for a different organisation (a new controller) or on a new server. Read [OPERATIONS.md](OPERATIONS.md) before running a case.

The model is always the same: **the process runs on the local machine, the Purview export sits on that machine's local hard drive, and the platform is drawn down from GitHub** (see the README's design principle). A server here means a machine the operator works on directly, holding its own data.

## 0. Before you start: separation between organisations

- **Bring code only.** Nothing from another organisation's cases (databases, results, configs, prompts, audit logs, memory notes) goes onto the new machine. Everything case-specific is gitignored, so a clean clone is clean.
- **Their data stays on their infrastructure.** Case data lives on the server's local disk, outside any synced folder, under a directory only the operator can read (`chmod 700`).
- **No outbound data.** The model runs locally through Ollama; nothing is sent to any external service. Confirm the server's own policies allow running a local model and that model files may be downloaded (or bring them on approved media, section 3).
- **Agree access first:** who can log in, where case data lives, how long it is kept, and how outputs are handed over.

## 1. Hardware

| | Minimum | Better |
|---|---|---|
| RAM | 16 GB (concurrency 2) | 32 GB+ (concurrency 4) |
| GPU | Apple Silicon or none (CPU is very slow) | NVIDIA GPU with 12 GB+ VRAM |
| Disk | 3× the size of the Purview export, plus ~10 GB for the model | SSD |

Expect ~5–7 chunks a minute on a 16 GB Apple Silicon laptop. A large mailbox case can run for days, so plan the run time.

## 2. Install the code

Requires Python 3.10+ (validated on 3.13) and git.

```sh
git clone https://github.com/blenheimchalcot/DSARClassifier.git
cd DSARClassifier
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock && pip install -e . --no-deps   # exact validated versions
# (or: pip install -e ".[dev]" for latest compatible versions)
python -m pytest -q          # expect all tests to pass
dsar --help
```

PSTs are read with `libpff-python` (pypff). If no wheel exists for the platform it is compiled during install, so install build tools first (Linux: `build-essential python3-dev`; macOS: Xcode command-line tools). `libratom` is no longer used: it pinned old versions of many packages and could not be installed alongside the rest.

The repo is private: the server needs a GitHub account with access, or copy a release archive (`git archive --format=zip -o DSARClassifier.zip main`) onto it.

## 3. Install Ollama and the model

- macOS: download Ollama, keep the app in `~/Applications`, run the binary directly (see OPERATIONS §3).
- Linux: install from ollama.com and run as a service, or `ollama serve` in `tmux`.

```sh
ollama pull gemma4:e4b
ollama list                  # validated digest: c6eb396dbd59
OLLAMA_CONTEXT_LENGTH=8192 OLLAMA_NUM_PARALLEL=2 ollama serve
```

Linux service: set `Environment="OLLAMA_CONTEXT_LENGTH=8192" "OLLAMA_NUM_PARALLEL=2"` with `systemctl edit ollama`. Keep Ollama bound to `127.0.0.1`; never expose port 11434.

If the server has no internet access, copy `~/.ollama/models/` from a machine that has the model, and check the digest matches.

## 4. Set up a case

```sh
mkdir -p /srv/dsar/<CASE-REF>/data/{input,output} && chmod -R 700 /srv/dsar/<CASE-REF>
cp .env.example .env                                   # DSAR_DATA_DIR=/srv/dsar/<CASE-REF>/data
cp config.example.yaml /srv/dsar/<CASE-REF>/data/config.yaml        # subject, aliases, emails
cp claude_strict.example.md /srv/dsar/<CASE-REF>/data/claude_strict.md  # subject in the prompt
```

Keep the case's config and prompt **inside the case directory** and pass them explicitly, so two cases can never share a subject by accident:

```sh
dsar classify --config /srv/dsar/<CASE-REF>/data/config.yaml \
              --system-prompt /srv/dsar/<CASE-REF>/data/claude_strict.md
```

(With no `--system-prompt`, `claude_strict.md` in the current directory is used.)

Fill in the subject's name, aliases and emails in both files, mirroring the Purview KQL. Then check the machine:

```sh
dsar doctor --config /srv/dsar/<CASE-REF>/data/config.yaml \
            --system-prompt /srv/dsar/<CASE-REF>/data/claude_strict.md
```

It checks Python and every reader library, package versions against `requirements.lock`, that Ollama is local and not reachable from the network, the model and its digest (`model.expected_digest`), the context window and parallelism, that the data dir exists, is outside synced folders and is private, disk space, full-disk encryption, RAM, and that the config and prompt have no `<PLACEHOLDER>`s left. Fix every FAIL; decide on every WARN. The result goes into the case audit log.

Then record the case header with `dsar audit note "Case <CASE-REF>: controller <legal entity>, operator <name>, machine <host>"` and follow the run order in OPERATIONS §2.

## 5. Claude Code on the new machine (optional)

Copy `CLAUDE.example.md` to `CLAUDE.md` in the code folder and fill in the subject (it is gitignored). Point the first Claude session at `README.md`, `docs/OPERATIONS.md` and the case `AUDIT_LOG.md`. Claude's memory from other machines and organisations is **not** carried over, by design.

## 6. Checklist

- [ ] Clean clone; `git status` shows nothing case-specific
- [ ] Tests pass; `dsar --help` works
- [ ] Ollama running with `OLLAMA_CONTEXT_LENGTH=8192`, `OLLAMA_NUM_PARALLEL` = `model.concurrency`, bound to localhost
- [ ] Case dir outside synced folders, `chmod 700`, `.env` points at it
- [ ] Case config + prompt filled in, inside the case dir
- [ ] `dsar doctor` shows no FAIL (logged to the case audit log)
- [ ] Case header noted (`dsar audit note`); Purview ZIPs hashed before extraction (`dsar manifest`)
