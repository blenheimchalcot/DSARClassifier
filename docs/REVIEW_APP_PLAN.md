# Plan: a local web app for reviewing and disclosing DSARs

Status: **draft for discussion**, not started. The spreadsheet workflow (`dsar review export` / `import`) stays in use until the app reaches parity, and stays as a fallback afterwards.

## 1. Why the spreadsheet isn't enough

Running a real case through the review workbook showed where it breaks down:

| Problem seen | Cause |
|---|---|
| Unclear which file to open; several near-identical workbooks in `output/` | Each change means a new file, and the reviewer has to track versions |
| Reviewer comments seemed to vanish | A workbook filtered to undecided items left out finished rows, and rows the import rejected weren't stored anywhere |
| Rows rejected on import (redaction without a reason) | Validation only happens after the reviewer hands the file back |
| Not clear which rows were done | No status until one was added; filtering on `decision` doesn't distinguish part-done rows |
| Excel "found a problem with some content" | The rich-text highlighting the library writes isn't accepted by Excel; every fix meant another file |
| The decision list didn't match how we disclose | It assumed each item is sent as it is, not extracted into a Personal Data Report or released as a whole document |
| Bulk decisions ("this other person with the same first name isn't the subject") done by script | The workbook has no way to say "all items matching X", or to show what X matched |
| An interview transcript couldn't be found in the workbook | Documents are labelled by file name (`00_transcript-<id>`), not by what they are, and the parts of one document are spread across the sheet by score |
| A context passage isn't enough to decide | The reviewer needs the whole email, the thread and the attachment, not a 3,000-character slice |
| A second search for the same request had to be merged by hand | One case = one search; there is no "request" above it |
| Disclosure files, open items and reviewer notes were scattered across folders and the audit log | No single place records what goes out and what is still open |

## 2. What it should do

A web app that **runs on the case machine, against the case database**, and guides an operator and reviewers from intake to disclosure.

1. **Shows where the case is.** One dashboard per request: pipeline stages (hash, attachments, ingest, QA, model check, classify, verify evidence, review, disclosure, sign-off), each with its status taken from the audit log, plus the counts still to do.
2. **Gives reviewers a queue, not a file.** "To review", "Needs a reason", "Done", "Flagged for second review", filtered by score, source, sender, thread or custodian. Progress is saved on every click; there's nothing to save, return or import.
3. **Shows the whole item, under a readable name.** Documents are titled by what they are (a call recording's meeting title and date, an attachment's parent email), and the parts of one document are kept together. Shows the full email or document, with the evidence highlighted, its thread, its attachments, and the other chunks of the same item. Originals open as read-only copies, never the files in `input/`.
4. **Records decisions in our own terms**, and validates them at once:
   - *Extract to Personal Data Report*: select the passages to extract;
   - *Release in full*: the whole document goes out (CV, application form);
   - *Release in full with redactions*: select the passages to redact, with a reason for each;
   - *Withhold (exemption applies)*: needs an exemption;
   - *Not the subject's personal data*.

   Decisions can be made per chunk, per item (email or document) or per thread.
5. **Supports rule-based bulk decisions, with evidence.** For example, "every item where *other person's name* is sender, recipient or named in the text → not the subject's data". The app previews the matches, the reviewer confirms, and the rule, its query and the matched item IDs are stored. New items that match later are listed for confirmation, never applied silently.
6. **Builds the output.** The Personal Data Report is built from the extracts, with each passage referenced to its source item and date. The disclosure package holds the files released in full or with redactions, plus an index. Everything is hashed and logged.
7. **Tracks open items** (controller entity, images to read by hand, missing recordings) as tasks with an owner and a status, instead of rows in the audit log.
8. **Handles more than one search per request.** The request is the top level, and each Purview export is a collection under it. Duplicates are matched by content hash and Message-ID. Where two runs scored the same item differently, the highest score is used and the difference is shown.
9. **Leaves the forensic rules unchanged.** Decisions are append-only, and the model's scores are never overwritten. Every action goes into the hash-chained audit log under the person's name. Nothing leaves the machine.

## 3. Running the pipeline from the app

The app should guide a case from the start, not only from review. Each stage is a page with a **Run** button, live progress, the warnings to acknowledge, and the outputs with their hashes. A stage unlocks only when the one before it has passed. That gating already exists for `classify`; the app extends it to every stage.

| Stage | What the operator does | What the app enforces or shows |
|---|---|---|
| **1. Open the request** | Enters the data subject, aliases, emails and phone numbers, the controller, the date received and the Purview KQL query for each search | Generates `config.yaml` and the subject prompt from the template, hashed. The aliases are pre-filled from the KQL, and short or common names (a bare first name) are flagged as false-positive risks. Each case gets its own folder and database, so no `DSAR_DATA_DIR` switching. |
| **2. Add the export** | Points at the Purview ZIPs | Hashes the ZIPs, extracts them, hashes every file (`manifest`) |
| **3. Prepare** | Nothing | Runs `attachments` then `ingest` in that order, then `provenance` |
| **4. Check the ingest** | Reviews each warning and low-yield file, and records a decision (for example "Teams items, confirmed ingested"; "exclude other DSAR's work product") | `qa ingest` must pass. Exclusions are recorded as decisions with a reason, and files to read by hand become tasks. |
| **5. Check the model** | Nothing, unless the check fails | Pre-flight (model digest, context length, parallelism, keep the machine awake), then `model-check`. Shows the estimated run time. |
| **6. Classify** | Starts it, and can leave it running | Progress bar, items done and failed, ETA, pause and resume. A re-run archives the earlier results first and carries over byte-identical classifications only. |
| **7. Verify** | Nothing | `verify-evidence`. Quotes not found in the source are flagged in the review queue. |
| **8. Review** | Section 2, items 2 to 5 | The queue starts with the highest scores, plus the items flagged in stages 4 and 7 |
| **9. Disclose and close** | Builds the report and package, then signs off | Section 2, item 6. Closing the case records the final hashes, and the processed exports are marked for deletion (only the operator deletes). |

How it runs: each **Run** button starts the same `dsar` CLI command as a background process, so there is one implementation and the audit entries are identical. The app reads the command's progress file and audit entries to show status, so a run carries on if the browser is closed. Only one long-running stage runs per case at a time.

## 3a. Toolbox

The app has a **Toolbox** menu linking to the stand-alone tools in `src/dsar/toolbox/` ([TOOLBOX.md](TOOLBOX.md)). The first is the **redaction tool** (`dsar markup`), built and in use on a live case. From the app's item view, "Redact this document" opens the tool on that item. Once the app's own server exists, the tool's pages are mounted under `/toolbox/redaction/` with the same sign-in, rather than started as a separate server. Its event log and records are unchanged. Lessons from using it so far:

- Approve and redact are the common tasks: keep them as the only primary buttons, with keyboard shortcuts and auto-advance. Rare actions (split, clear) go in a menu.
- Fold decided items away, but keep them one click from review.
- Tie decisions to an exact span of text and the file's hash, so re-parsing or splitting never moves a decision onto other text.
- Show the model's score as a hint beside each item, never as a decision.

## 4. Constraints

- **Local only.** Binds to `127.0.0.1` by default. No CDN, fonts or analytics; all assets are served from the package. No subject data in the code folder or git (unchanged).
- **Same database and audit log.** The app is another front end on `dsar.sqlite` and `AUDIT_LOG.jsonl`, like the CLI. The CLI keeps working, and both write the same audit entries.
- **No build step.** Server-rendered HTML with a little JavaScript, so it runs from the existing venv with `dsar serve`. A suggested stack is FastAPI (or Starlette) with Jinja templates and htmx, which adds about three dependencies.
- **Read-only originals.** Files in `input/` are served only as copies with a content-disposition header, and their hash is checked against the manifest on every view.
- **Works with one person or a few.** Reviewers sign in with their name; it is not an identity system. Multi-user use on a LAN is a later option and needs real authentication first (see the open questions).

## 5. Data model additions

New tables in `dsar.sqlite`, all append-only, with the current state worked out from the newest row:

| Table | Holds |
|---|---|
| `requests`, `collections` | A request (data subject, controller, deadline) and the Purview exports under it, with each export's KQL query |
| `decisions` | Replaces `review_decisions`: scope (chunk / item / thread), decision, reason, notes, reviewer, time, and `rule_id` if made by a rule |
| `decision_spans` | Character ranges marked to extract or redact, each with a reason |
| `rules` | Bulk decisions: the query, a preview of the matched IDs, who confirmed it and when |
| `tasks` | Open items, with owner, status and links to items |
| `outputs` | Generated reports and package files, with their hashes |
| `stage_runs` | Each pipeline stage run: command, arguments, start and end, status, process ID, log file, and the warnings acknowledged (who, when, decision) |

Existing `review_decisions` rows are migrated into `decisions` unchanged, with their source workbook and its hash kept.

## 6. Phases

The review queue comes first because that's where the spreadsheet hurts most. The pipeline stages follow, starting with the ones that run longest.

| Phase | Delivers | Done when |
|---|---|---|
| **0. Read-only browser** | `dsar serve`; case dashboard showing each stage's status from the audit log; item viewer with the full email or document, thread and attachments; search and filter | A reviewer can find and read any item faster than in Excel |
| **1. Review queue** | Decisions in the new vocabulary, validated on entry; status and progress; notes; export and import still available | The current case's remaining review can be done in the app instead of a workbook |
| **2. Classify and verify in the app** | Stages 5 to 7: pre-flight, model check, classify with progress, ETA, pause and resume, verify evidence | A full classify run is started and watched from the app, with nobody needing to ask "how is it going" |
| **3. Item-level work** | Decisions per item and per thread; selecting passages to extract or redact; rule-based bulk decisions with preview | The bulk exclusions done by script in the pilot case can be done and evidenced in the app |
| **4. Intake and ingest in the app** | Stages 1 to 4: request set-up with an alias-risk check, export hashing, attachments, then ingest in order, provenance, and QA with acknowledged warnings and exclusions | A new case goes from Purview ZIPs to "ready to classify" without a terminal |
| **5. Output and close** | Personal Data Report (format to agree), disclosure package and index, all hashed; sign-off; case close | A complete response can be produced from the app |
| **6. Requests and collections** | Several searches per request, de-duplication, highest score, comparing the runs | The two-search pilot case would need no hand merging |

Phases 0 and 1 are a few days each. The spreadsheet problems in section 1 go away at phase 1.

## 7. Open questions for the operator

1. **Who will use it?** Only the operator on the case machine, or HR and privacy reviewers too? If others, on the same machine or over the network? (The network option needs authentication and TLS, and is a bigger step.)
2. **The Personal Data Report:** is there a current template (Word, PDF)? What does each entry show: the extract, date, source, sender?
3. **The decision vocabulary** in section 2.4: is it right? Is a second reviewer or DPO sign-off needed, and for which decisions?
4. **Redaction of released documents:** done in the app (text documents only), or marked in the app and done in another tool (PDFs and images)?
5. **Deadlines:** should the app track the statutory deadline and any extension?
6. **Pipeline order:** should the review queue come first (as proposed), or the guided ingest and classification?
