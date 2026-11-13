# Reviewing a DSAR workbook: guide for HR and privacy reviewers

You have been sent a workbook (`review_<date>.xlsx`) from a Data Subject Access Request. Each row is a passage of an email, chat or document that an automated classifier scored **7 or more out of 10** for personal information about the person who made the request. Your job is to check the score and decide what happens to each passage in the response.

The workbook holds personal data. Keep it on the device it was given to you on, don't email it or upload it anywhere, and give it back to the operator when you're done.

## The score

| Score | Meaning |
|---|---|
| 6–7 | Specific personal information: home address, date of birth, specific HR actions |
| 8–9 | Opinions about the person's behaviour or performance, grievances |
| 10 | Highly opinionated, derogatory or sensitive remarks |

## What to look at in each row

- **status**: *To review* rows (listed first) still need your decision; *Done* rows were decided in an earlier workbook and are shown for reference. You can still change them.
- **context**: the passage, with the classifier's evidence between 【 】 brackets. Read the whole passage, not just the bracketed text.
- **evidence_found_in_source**: whether the quoted evidence really appears in the original. `yes` is normal. With `partial`, `no` or `missing`, the classifier may have invented or misquoted the evidence, so judge from the context only.
- **date, from, to, subject, custodian, location**: where the passage came from, so you can find the original if you need to.

## What to fill in (the yellow columns)

| Column | What to enter |
|---|---|
| assessment | *Agree*, *Score too high* or *Score too low* |
| adjusted_score | Your score from 0 to 10 if you disagree; leave it blank if you agree |
| decision | *Disclose*, *Disclose with redactions*, *Withhold (exemption applies)* or *Not the subject's personal data* |
| reason | Why you redact or withhold: choose from the list (*None* if disclosing in full) |
| notes | What to redact, why you disagree, or anything the operator or DPO should know |
| reviewer | Your name |
| review_date | Today's date |

The drop-down lists keep entries consistent. If none of the reasons fits, choose *Other (explain in notes)*.

**Redacting or withholding always needs a reason.** Rows without one are sent back.

## Common situations

- **The passage also identifies other people.** That is third-party personal data. You usually disclose the requester's information and redact the others'. Choose *Disclose with redactions* and *Third-party personal data*, and say what to redact in notes.
- **The passage is legal advice** from or to a lawyer: *Legal professional privilege*. Check with the DPO.
- **The passage is about the organisation, not the person** (for example a project code that happens to match their initials): *Not the subject's personal data*.
- **You are unsure whether an exemption applies:** choose *Other (explain in notes)* and ask the DPO. Don't guess.

## Rules

- Don't change the grey columns, re-order rows or delete rows. The `review_id` links your decision back to the case.
- You can return a partly finished workbook. Rows without a decision stay outstanding and come back to you in the next workbook, with your earlier decisions already filled in.
- Your decisions are recorded with your name and the date in the case audit trail. The classifier's original score is kept alongside them, never overwritten.

## For the operator

```sh
dsar review export                      # writes output/review_<date>.xlsx (scores 7-10)
dsar review import <returned workbook>  # loads decisions; lists rows that need fixing
dsar review status                      # decided vs outstanding per score
```

Imports are append-only: re-importing a corrected or later workbook adds new rows, and the newest decision per item counts. Each import is audit-logged with the workbook's SHA-256.
