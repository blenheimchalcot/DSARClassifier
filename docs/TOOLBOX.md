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

Options: `--config` (case config, if it isn't in the data dir), `--markup-folder DIR` (an extra folder for the redaction tool, repeatable), `--cases-root DIR` (where the Cases page looks; default: the folder holding this case), `--port`, `--no-browser`.

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

## Review and Personal Data Report

**Review** replaces the review workbook for deciding on classified items. **Report** builds the Personal Data Report from those decisions and the redaction tool. Both need a case with a `dsar.sqlite`; the app never creates one.

### Review queue

Lists every item the model scored 7 or more (change *Min score* to see lower ones), plus any item already decided, under *To review*, *Needs passages* and *Done*. Search looks in subject, sender, file and text. An item page shows the whole chunk with the model's evidence highlighted (and whether that quote is really in the text), its metadata and source, the other chunks of the same item or thread, and every decision made on it.

Decisions:

| Decision | Needs | Goes to |
|---|---|---|
| Extract to Personal Data Report | One or more passages selected (*Extract selection*, or *Extract whole passage*) | The report, as text only |
| Release in full | | The disclosure package (listed on the Report page; package building is still to do) |
| Release in full with redactions | One or more passages selected to redact, each with a reason | As above |
| Withhold (exemption applies) | The exemption | Not disclosed |
| Not the subject's personal data | | Not disclosed |

Optional: the score assessment and your own score, and notes (needed when a reason is *Other*). **Save and next** validates at once and moves to the next item to review. Decisions are append-only in `decisions` and `decision_spans` (dsar.sqlite); the newest one is current, and earlier ones stay in the item's history. Each passage is a character range of the chunk text, stored with the text's SHA-256: if the text changes, the decision no longer applies and the item is listed as outstanding. Every decision is written to the audit log (`review decide`) with your name, the decision, the number of passages and the text's hash. The model's score is never changed.

Workbook decisions (`dsar review import`) keep their wording and count as made. For the report, a workbook *Disclose* is taken as the whole passage and flagged so it can be narrowed. A workbook *Disclose with redactions* recorded what to redact only in notes, so it is listed under *Needs passages* until the passages are selected in the app. The workbook commands still work as a fallback; `dsar review status` counts decisions from both.

### Personal Data Report

The Report page shows what the report holds now, what is outstanding, a preview of every extract with its sources, and earlier builds. **Build report** fills a copy of the controller's template:

- Template: `Personal Data Report.docx` in `Templates/` in the project folder, or the folder in `DSAR_TEMPLATES_DIR`. Its intro is kept, the *INSERT DATA REPORT* guidance and *SAMPLE TABLE* label are removed, and the sample table becomes *Data Subject* (from the case config) then one *Extracted data N* row per extract.
- Contents: passages extracted in Review, workbook *Disclose* passages, and every passage released in the redaction tool's folders. Text only. Identical extracts are merged, so each piece of data appears once. Ordered by the source's date (an item's sent date, or a `YYYY-MM-DD` in a redaction file's name), undated last.
- Outstanding (makes the build a **DRAFT**, in the file name and in red on the first page): items 7+ not reviewed, workbook *Disclose with redactions* without passages, text changed since a decision, redaction-tool elements not yet decided, or no data subject name.
- Output, read-only in `output/personal_data_report/`: `Personal_Data_Report_<case>_<UTC>[_DRAFT].docx` and `personal_data_report_register_<case>_<UTC>[_DRAFT].csv`. The register is private: every source of every extract (origin, date, type, sender, subject, source file, item, position, text hash, decision, reviewer). Both hashes and the template's are logged (`report personal-data`).

Edit the Word copy for final wording if needed, save it as PDF and record what was sent (OPERATIONS). *Download copy* gives a copy of a build; downloads are logged too.

## Cases page (switching case)

**Cases** in the top bar lists every case under the cases root (each `<CASE-REF>/data` with an `output/` folder; folders starting `_` are skipped), with its data subject and the state of its audit log:

| State | Meaning | Opening it |
|---|---|---|
| chain intact | Hash-chained `AUDIT_LOG.jsonl`, chain verified | Opens |
| chain problems | `AUDIT_LOG.jsonl` whose chain check fails | Opens; the problems are recorded in the case-open entry. Investigate before relying on the case |
| legacy | Run before the platform logged itself: a hand-written `AUDIT_LOG.md` only, or no log | Needs a tick to confirm. It starts the case's hash-chained log; the first entry says earlier work isn't in it and records the SHA-256 of the existing `AUDIT_LOG.md`, which is appended to, never rewritten |

Only cases found there can be opened (the browser never sends a path). Switching:

- writes `toolbox app case-close` in the case left and `toolbox app case-open` in the case opened, under the signed-in reviewer (with the config's SHA-256);
- closes open mailboxes and uses only the new case's own `data/config.yaml`. `--config` and `--markup-folder` belong to the starting case and are not carried over;
- rotates the session's CSRF value: a tab still showing the previous case gets *409, reload the page* on any change, and nothing is done.

`serve` start and end stay in the starting case's log. If the app is stopped while another case is open, that case gets a `case-close` entry too.

Legacy cases may lack what the toolbox expects (a mailbox opens only from a `dsar manifest`; stage status comes from the hash-chained log only). Treat them as read-mostly until their state has been checked.

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
- **Export Personal Data extracts** (sidebar) collects every released passage across all files into `<folder>/personal_data_extracts/`:
  - `personal_data_extracts_<time>.txt`: the report text. Plain extracts only, identical ones merged, no headings or sources.
  - `extract_register_<time>.csv`: private, for the case record. Each extract with every file, element, speaker and time it came from.

  Unreviewed elements are left out and the file names end in `_DRAFT`. Redact repeated data with the reason *Duplicate – already in the Personal Data Report*, so the log records why it was left out.
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
