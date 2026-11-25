# How classification works

Classification turns a case's exported data into passages scored 0–10 for personal information about the data subject, so reviewers start from the passages most likely to matter. A score is a pointer for the reviewer, never a decision: nothing goes into the response because of a score alone.

Everything runs on the case machine with a local model (Ollama). No case text is sent to an external service.

```
export ─► manifest ─► ingest ─► alias pre-filter ─► scope ─► model check ─► classify ─► evidence check ─► QA ─► review
          (hashes)   (passages)  (names the subject?)                      (0–10 + reason + quote)
```

## 1. From files to passages (`dsar ingest`)

Every file under the case's `input/` is read into **passages** (`chunks` in `dsar.sqlite`): one per email, document section, spreadsheet row block, Teams message or chat transcript part. [COVERAGE.md](COVERAGE.md) lists what each source type gives.

- **Size.** A passage is at most `chunking.max_chars` characters (8,000 by default). Longer text is split at paragraph boundaries with `chunking.overlap_chars` (200) of overlap, so a sentence on a boundary appears whole in one of the two parts.
- **Nothing is stripped.** Email bodies are kept verbatim: no signature or quoted-history removal, because a "Kind regards" before a forwarded message would otherwise cut the whole forward.
- **Repeated email text.** In mailbox emails, a paragraph already read from an earlier email in the case (matched by its hash) is left out of the later email's passage, and the passage records which emails it repeated. Short paragraphs are always kept. An email that is *only* earlier text is recorded as a duplicate of the email it repeats, and not classified.
- **Empty items** are recorded with the marker `[Email empty]` or `[File empty]`, never dropped.

`dsar qa ingest` must pass before classification: every file has passages or a recorded reason why not, nothing Purview listed is missing, and no passage is over the size the model can read.

## 2. Which passages are classified

A passage is sent to the model only if all of these hold:

1. **It names the subject (alias pre-filter).** Its text, or for an email its subject line, sender or recipients, matches one of the subject's aliases or email addresses in `config.yaml` (`subject.aliases`, `subject.emails`). The match is case-insensitive, with word boundaries round every alias, so a short alias such as initials doesn't match inside other words. Passages that don't name the subject are kept in the database but not scored.
2. **It isn't a duplicate** (section 1).
3. **It is in scope.** `classify_scope` in the config limits classification to chosen provenance labels or paths, for example to keep another request's case files, found inside an export, out of this one. Every exclusion is a logged decision, made with the operator.
4. **It hasn't been classified yet.** A run that stops resumes where it left off.

`dsar classify --plan` shows how many passages are in and out of scope, and how many are already done, per provenance label, before anything is sent.

**What the pre-filter misses.** A passage about the subject that never names them (an indirect reference, a document they wrote without their name in it) is not scored. Documents the subject wrote are reviewed as whole documents instead (OPERATIONS §5).

## 3. Before the first run: the model check

`dsar classify` refuses to start without:

- a passing `dsar qa ingest` for the current database, and
- a passing `dsar model-check` for the **exact** model build (its digest), system prompt, `max_tokens` and `temperature`.

The model check scores synthetic examples across the whole scale (the subject's name substituted in). It sends a full-size passage with a sensitive sentence first and then last, to catch the model reading only part of it. It also checks the answer format and that the same request gives the same answer, and it estimates the run time. Changing the prompt or updating the model means a new check. `--skip-checks "<reason>"` overrides the gate, and the reason is written to the audit log.

## 4. Scoring a passage (`dsar classify`)

Each passage is sent to the local model with:

- **the system prompt**: the case's `claude_strict.md`, made from [claude_strict.example.md](../claude_strict.example.md). It holds the subject's name and aliases, the scale, the rules and worked examples. The live copy holds personal data and is never committed.
- **the request**: *"Classify the following {source type} (Source: {file}) regarding Data Subject {name} using the defined 0-10 scale:"* followed by the passage text.

Settings come from `model:` in the config. `temperature` is 0, so the same passage gets the same answer. `max_tokens` is 300, and the model's hidden "thinking" is switched off, because only one line is wanted. `concurrency` must equal Ollama's `OLLAMA_NUM_PARALLEL`.

### The scale

| Score | Meaning | Examples |
|---|---|---|
| 0 | False positive: the match isn't the subject | A business acronym that matches their initials, a bounced-email notice |
| 1–4 | Incidental mention | CC lists, distribution emails, a one-word turn in a meeting, system notifications |
| 5 | Contextual | Business discussion that specifically concerns them |
| 6–7 | Specific personal information | Home address, personal email or phone, salary, date of birth, contract terms, a named HR action |
| 8–9 | Behavioural | Opinions on their conduct or performance, grievances |
| 10 | High risk | Highly opinionated, derogatory or sensitive remarks |

### The answer

The model must answer with one line: `Score | Description | Evidence`. For example: `7 | Specific PI: home address, salary | 1 Example Street; £40,000`. Evidence (a literal quote from the passage) is required for scores of 7 or more. Because every passage has already been shown to name the subject, the model must always give a number. The only exceptions are `[Email empty]` and `[File empty]`.

A score outside 0–10, or an answer that can't be read, is stored as unparseable and counted by QA. The model's full answer is always kept (`raw_response`).

## 5. Checking the evidence

The model has quoted its own prompt examples, and once invented an email address. So **nothing it quotes is trusted until it is found in the passage** (`src/dsar/evidence.py`):

- The quote and the passage are normalised: letters and digits only, case, curly quotes and line-break hyphens ignored.
- **yes**: every quoted fragment is in the passage (or its email subject line).
- **partial**: at least 80% of the quote's four-word runs are there.
- **no**: less than that. **missing**: a score of 7+ with no quote.

When the check gives *no* or *missing*, the model is asked once more. The request says that its evidence wasn't in the text, and that it must quote exactly or score lower. The answer that passes the check is kept, otherwise the first answer is kept. Both answers stay on the record. `dsar verify-evidence` re-checks every 7+ quote after the run, and `--supersede` withdraws failed ones (they move to `superseded_classifications`, never deleted).

The check catches invented quotes, not bad judgement: a reviewer still sanity-checks high scores.

## 6. Quality checks (`dsar qa classify`)

- **Format**: unparseable answers above `qa.max_unparseable_rate` fail the run.
- **Coverage**: in-scope passages still unclassified.
- **Evidence**: 7+ quotes not fully found.
- **Possible misses**: passages scored under 5 that mention health, criminal or other special-category matters, each to be read by a person.
- **Distribution**: scores by custodian and source type, to spot one mailbox scored very differently.
- **Consistency**: a sample is scored again with the same request. It fails if fewer than `qa.min_rescore_agreement` land within ±1.
- **Blind sample**: a workbook (`qa_sample_<time>.xlsx`) with a random sample from every score band, for a reviewer to score without seeing the model's score. `dsar qa import-sample` measures the agreement and logs it.

QA never changes a score.

## 7. Paragraph scores inside the Personal Data Reviewer

Classification scores whole passages. In the Personal Data Reviewer, each passage is split into **elements** (paragraphs, header blocks, speaker turns), and each element gets its own score from the same model, prompt and scale:

- `dsar markup score <folder>` scores every element in a folder. In items with more than 50 elements (long reports, big spreadsheets), only elements that name the subject are sent; the rest are recorded as 0 (`alias-prefilter`).
- **Table items keep their item's score.** A line on its own loses the context that made it personal data (a lone name cell, one learner row). So, in an item scored at or above the review line, lines that name the subject, and every line of an item of 50 lines or fewer, take the item's score wherever the model scored them lower. The method is `item-score-carried`, and the model's own score is kept in the description.
- **Suggest score** on an element asks for a live score, with reasons and an evidence check (method `live`).

## 8. The review line

Items and elements scored **6 or more** need a reviewer's decision (`MIN_SCORE` in `src/dsar/review.py`, an operator instruction of 23 Nov 2026). Those below it need none and stay out of the report, unless a reviewer opens one and extracts it. Anything not yet scored, or with no number from the model, needs a decision. The reviewer can always look below the line; the line only decides what *must* be looked at.

## 9. Records

| Where | What |
|---|---|
| `classifications` (dsar.sqlite) | Score, description, evidence, special return, full model answer, model, token counts, per passage |
| `superseded_classifications` | Withdrawn scores, with why |
| `<folder>/markup_scores.jsonl` | Element scores, with model, prompt hash and method |
| `output/model_check_<time>.json` | Each model check |
| `output/qa_classify_<time>.csv` | Each QA finding |
| `output/AUDIT_LOG.md` / `.jsonl` | Every run: scope, pending count, Ollama version and model digest, settings, counts, evidence retries, failures, the database's SHA-256 |

A run where a passage fails (a timeout, a server error) carries on. The passage stays unclassified and the audit log names it. Running `dsar classify` again retries it.

## Settings at a glance

| Setting (config.yaml) | Default | Effect |
|---|---|---|
| `subject.name`, `aliases`, `emails` | | Who is matched by the pre-filter and named in the request |
| `chunking.max_chars` / `overlap_chars` | 8000 / 200 | Passage size |
| `classify_scope` | everything | Which provenance labels or paths are classified |
| `model.default` | `gemma4:e4b` | The model |
| `model.expected_digest` | | The validated build; `dsar doctor` fails on any other |
| `model.temperature` / `max_tokens` | 0 / 300 | Repeatable, one-line answers |
| `model.concurrency` | 2 | Parallel requests; must equal `OLLAMA_NUM_PARALLEL` |

Running the model (context window, memory, sleep): [OPERATIONS.md §3](OPERATIONS.md).
