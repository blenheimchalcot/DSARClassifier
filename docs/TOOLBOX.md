# Toolbox

Stand-alone tools used alongside the pipeline during a case. Each lives in its own folder under `src/dsar/toolbox/` and runs on this machine only, against a case folder. Case data never goes into the code folder, OneDrive or git. The review web app ([REVIEW_APP_PLAN.md](REVIEW_APP_PLAN.md)) links to each tool from its Toolbox menu.

| Tool | Folder | Command | For |
|---|---|---|---|
| Redaction | `src/dsar/toolbox/redaction/` | `dsar markup serve`, `dsar markup score` | Deciding, element by element, what is released and what is redacted, and exporting a redacted copy |

## Redaction

### Preparing a folder

Put the documents to review in a folder inside the case, as UTF-8 `.txt` files. The redaction tool reads only `.txt` files. For example, `output/deep_dive_<date>/` with a call transcript, a summary and full email texts rendered from the PST. Copy them; never point the tool at `input/`.

### Running it

```sh
# Optional: a 0-10 hint per element from the local model (case prompt and scale), never a decision
dsar markup score <folder> --config <case config> --system-prompt <case prompt>
# Open the tool in the browser (127.0.0.1 only, one-off access token in the address)
dsar markup serve <folder> --reviewer "Your Name" --subject <subject first name>
```

`Ctrl-C` stops the server. Restarting it gives a new address, so close the old tab.

### Using it

- **Elements:** each speaker turn in a Call AI transcript, plus the meeting details, and each header block and paragraph in any other file.
- **Decisions:** for each element, **Approve** (release as it is), **Redact all** (choose a reason) or **Redact part** (select the passages to redact, each with a reason, then **Save**). Keys: `A`, `R`, `P`, `↓`/`↑` to move, `Enter` to confirm, `Esc` to cancel. Decided elements fold into the **Processed** bar at the top; open it to review or change them.
- **Original / Redacted** switches between the text with redactions struck through, and the release view with black bars.
- **⋯ → Split** cuts an element in two where the transcript gives two people's speech to one speaker. **Undo split** reverses it. A decision made before a split must be made again; the tool shows the earlier decision and never applies it to the parts.
- **Export redacted copy** gives a printable page with the redacted text *removed*, not hidden, followed by a redaction log. The page says DRAFT until every element is decided. Print it to PDF. Each export is saved read-only in `<folder>/redacted_exports/` and logged in the case audit log.

### Records

- `<folder>/markup_events.jsonl`: every decision, reset and split, with reviewer and time. Append-only and hash-chained. Each decision is tied to the file's SHA-256 and the exact span of text it covered.
- `<folder>/markup_scores.jsonl`: the model's hints, with the model and prompt hash.
- The case `AUDIT_LOG`: start and stop of each session (with the events file's hash), each export, and each scoring run.
