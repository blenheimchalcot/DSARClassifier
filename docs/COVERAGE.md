# What the pipeline reads

Every file under `input/` ends in one state: **chunked**, **skipped with a reason**, or **failed**. `dsar qa ingest` checks this and reconciles it with Purview's item report. This table lists what each source gives and where a person has to step in.

| Source (Purview export) | Format | Handler | What is read | Known limitations (manual review) |
|---|---|---|---|---|
| Exchange mailboxes | `.pst` | `chunking/pst.py` (pypff) | Every message: headers, body (plain → HTML → RTF), threaded, paragraph-deduplicated | Corrupt records are skipped individually (logged) |
| Email attachments | inside `.pst` | `pst_attachments.py` via `dsar attachments` | Each attachment written to `input/PST_attachments/` and chunked like any file; catalogued against its parent email | **Embedded emails** (attach method 5, `IPM.Note`): headers and subject recovered, **body not** (pypff limitation). Listed in `pst_attachments_catalogue.csv`; read in Outlook or with `readpst` |
| Saved emails | `.eml`, `.msg` | `chunking/eml_message.py`, `chunking/outlook_msg.py` | Header and body | |
| Attachments inside saved emails | inside `.eml` / `.msg` | `message_attachments.py` via `dsar attachments` | Written to `input/PST_attachments/_messages/`, chunked, catalogued in `message_attachments_catalogue.csv`; nested emails opened to depth 3; provenance inherited from the exported file | Unreadable or unsupported attachment types are catalogued with a note |
| Teams chats | `SPOOLS/*.json` | `chunking/teams_chat.py` | Messages per conversation | |
| Teams meeting transcripts | `MicrosoftMeetingTranscripts/*.html` | `chunking/html_transcript.py` | Transcript text | Recordings themselves are often not exported: ask for them |
| Substrate holds (Teams/Planner records) | `SubstrateHolds/*.json` | `chunking/substrate_hold.py` | Record fields | |
| Word, PDF, text | `.docx`, `.pdf`, `.txt`, `.md` | `chunking/documents.py` | Body, tables, headers and footers (Word); text layer (PDF) | **Scanned / image-only PDFs** have no text layer: `qa ingest` lists them as low-yield. OCR is not built in |
| Excel | `.xlsx`, `.xls`, `.xlsb`, `.csv` | `chunking/spreadsheets.py`, `chunking/csv_table.py` | One chunk per row, with headers | |
| PowerPoint | `.pptx` | `chunking/pptx_deck.py` | Slide text and notes | |
| Other text | `.json`, `.xml`, `.htm(l)` | `chunking/generic_text.py` | Text content | |
| Protected Office files | sensitivity-labelled / encrypted | n/a | Not readable | Fail or yield no text: listed by `qa ingest`; ask the owner for an unprotected copy |
| OneNote | `.one` | n/a | Not readable | Skipped as *review by hand*: `qa ingest` lists each one. Export the section to PDF/.docx and add it to the case |
| Loop / Fluid / Whiteboard | `.loop`, `.fluid`, `.whiteboard` | n/a | Not readable | Skipped as *review by hand*: open in the app, export to PDF/.docx |
| Images, audio, video | `.png`, `.jpg`, `.mp4`, ... | n/a | Not read | Skipped with the type as the reason. Photos and recordings can hold personal data: decide case by case |
| Build / binary noise | `.dll`, `.js`, `.css`, `.venv/`, ... | n/a | Not read | Skipped with a reason |

The alias prefilter only sends chunks that name the subject to the model. Documents the subject wrote themselves may not name them: review those as whole documents.

## Not built yet (from issue #3)

- **Embedded email bodies in PSTs** (attach method 5). Needs a second reader (e.g. `readpst`) and real PSTs to verify against.
- **OCR** for scanned PDFs and images (optional Tesseract dependency).
- **Document versions** from SharePoint exports, if the export includes them.
- **Confirming Teams coverage** against a current export: which Teams content arrives as PST items, JSON or HTML.
