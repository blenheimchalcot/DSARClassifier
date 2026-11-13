# Web app and toolbox

`dsar serve` opens a local web app for the case: a case page and a **Toolbox** of tools used alongside the pipeline. Everything runs on this machine (127.0.0.1 only). Nothing is loaded from the internet, and case data never goes into the code folder, OneDrive or git.

| Tool | Folder | For |
|---|---|---|
| Mail viewer | `src/dsar/toolbox/mailviewer/` | Browsing a PST or MBOX: folders, messages, attachments, search. Saving messages and attachments into the case, or a mailbox to Excel |
| Teams viewer | `src/dsar/toolbox/teams/` | Teams chats in a PST: every conversation and participant, read in the browser, saved as a PDF transcript |
| Redaction | `src/dsar/toolbox/redaction/` | Deciding, element by element, what is released and what is redacted, and exporting a redacted copy |

The Mail and Teams viewers started as the separate PSTViewer project and now live here. They share `src/dsar/toolbox/mailbox/` (PST and MBOX reading). The app itself is `src/dsar/webapp/`.

## Starting it

```sh
dsar serve                       # case-bound: uses DSAR_DATA_DIR (and its config.yaml for the subject's name)
dsar serve --free                # free-standing: no case (see below)
```

Options: `--config` (case config, if it isn't in the data dir), `--markup-folder DIR` (an extra folder for the redaction tool, repeatable), `--port`, `--no-browser`.

The terminal prints a one-off address with a token. Open it, then **sign in with your full name**: it is recorded with everything you decide or export. `Ctrl-C` stops the server. Restarting it gives a new address, so close the old tab. Only this machine can connect, and pages from other sites can't use the session (strict same-site cookie, a CSRF check on every change, and only `127.0.0.1` / `localhost` accepted as the host). Request lines are not printed, because they carry search terms and file names.

### Two modes

| | Case-bound (default) | Free-standing (`--free`) |
|---|---|---|
| Which mailboxes | Only PST / MBOX files listed in the case's newest input manifest (`dsar manifest`). Files under `input/` that aren't in it are shown greyed out | Any path typed in, or picked with **Browse…** (macOS) |
| Before opening | SHA-256 checked against the manifest; a changed file is refused | No check |
| Saving | Written **read-only** to `output/toolbox/<tool>/<UTC time>_<name>`, with its SHA-256 in the audit log | Downloaded by the browser |
| Audit log | Server start and stop, sign-ins, each mailbox opened (with its hash), each export (with its hash, source and reviewer), each redaction folder opened | Nothing written. A banner on every page says so |
| Open attachment in another app | Not offered (it would copy subject data out of the case) | Offered on macOS, from a private temp folder deleted on exit |

Use free-standing mode only for work that isn't a case, such as checking an export's structure.

## Case page

Shows each pipeline stage (manifest, attachments, ingest, provenance, ingest QA, model check, classify, verify evidence, classification QA, review workbook out and in, report) with its last status from the audit log, the latest `progress.json`, the audit-log chain check, and recent entries. Stages are still run from the terminal (OPERATIONS §2); running them from the app is a later phase ([REVIEW_APP_PLAN.md](REVIEW_APP_PLAN.md)).

## Mail viewer

- Choose a mailbox and **Open**. Folders, then messages (50 per page), then the message.
- Recipients come from the transport headers, or from the MAPI display names where there are none (sent items, drafts, most Purview items). Bodies are shown as HTML, plain text, or text taken from the RTF body.
- HTML bodies are shown in a sandbox: no scripts, and the page's security policy blocks remote images and other content, so opening an email never contacts a tracking server.
- **Search** looks in subject, sender and recipients across the whole mailbox (first 200 matches).
- **Save message to case (.eml)** rebuilds the message with its attachments. **Save to case** on an attachment saves its bytes. PDFs, images and text files can also be **View**ed in the browser.
- **Save to case as Excel** writes one row per message: folder, date, from, to, cc, bcc, subject, message class, attachment count, body. Bodies longer than an Excel cell are cut and marked. An *About* sheet records the source and its SHA-256.

## Teams viewer

- Choose a PST and **Open**. It lists every Teams conversation, newest first, and every participant.
- A conversation is every message with the same Teams thread ID (Purview stores it in property `0x366D`): a 1:1 chat, a group or meeting chat, or a channel. Everyone who took part is listed, even if members changed. Messages with no thread ID are grouped by their exact set of participants, and the page says so.
- Conversations that include the data subject are starred. **People** lists each person with messages sent and received and their number of 1:1 partners. Click a person to filter the conversations. Meeting chats can list hundreds of people, because Purview records the whole meeting roster.
- **Save PDF to case** writes a transcript: participants, thread ID, date range, the source file and its SHA-256, who produced it and when, then every message in time order. A message with no text (an image, call or reaction) is shown as such, never left out.

## Redaction

### Preparing a folder

Put the documents to review in a folder inside the case output, as UTF-8 `.txt` files. The redaction tool reads only `.txt` files. For example, `output/deep_dive_<date>/` with a call transcript, a summary and full email texts. Copy them; never point the tool at `input/`. In a case, the Redaction page lists every folder under `output/` that holds `.txt` files.

### Running it

Open **Redaction** in the app and choose the folder. The reviewer is the signed-in name. The tool can also still be run on its own:

```sh
# Optional: a 0-10 hint per element from the local model (case prompt and scale), never a decision
dsar markup score <folder> --config <case config> --system-prompt <case prompt>
# Stand-alone, without the app
dsar markup serve <folder> --reviewer "Your Name" --subject <subject first name>
```

### Using it

- **Elements:** each speaker turn in a Call AI transcript, plus the meeting details, and each header block and paragraph in any other file.
- **Decisions:** for each element, **Approve** (release as it is), **Redact all** (choose a reason) or **Redact part** (select the passages to redact, each with a reason, then **Save**). Keys: `A`, `R`, `P`, `↓`/`↑` to move, `Enter` to confirm, `Esc` to cancel. Decided elements fold into the **Processed** bar at the top; open it to review or change them.
- **Original / Redacted** switches between the text with redactions struck through, and the release view with black bars.
- **⋯ → Split** cuts an element in two where the transcript gives two people's speech to one speaker. **Undo split** reverses it. A decision made before a split must be made again; the tool shows the earlier decision and never applies it to the parts.
- **Export redacted copy** gives a printable page with the redacted text *removed*, not hidden, followed by a redaction log. The page says DRAFT until every element is decided. Print it to PDF. Each export is saved read-only in `<folder>/redacted_exports/` and logged in the case audit log.

### Records

- `<folder>/markup_events.jsonl`: every decision, reset and split, with reviewer and time. Append-only and hash-chained. Each decision is tied to the file's SHA-256 and the exact span of text it covered.
- `<folder>/markup_scores.jsonl`: the model's hints, with the model and prompt hash.
- The case `AUDIT_LOG`: each time a folder is opened (with the events file's hash), each export, and each scoring run.

## Adding a tool

1. Make a sub-package under `src/dsar/toolbox/<name>/` with a `web.py` holding a Flask blueprint `bp` named `tool_<key>`, plus `templates/<name>/` and `static/` if it needs them. Pages extend `base.html`. Use relative URLs (`api/...`) so the tool works under `/toolbox/<key>/`.
2. Add it to `TOOLS` in `src/dsar/toolbox/__init__.py`. It appears in the top bar, on the Toolbox page and on the case page.
3. Write case actions through `dsar.webapp.records`: `log()` for audit entries, `save_output()` for files (read-only, hashed, logged). In free-standing mode both write nothing. Load nothing from outside the package.
4. Tests go with the others: see `tests/test_webapp.py` and the PST fakes in `tests/pst_fakes.py`.
