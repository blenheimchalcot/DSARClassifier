"""
Review workbook for HR / privacy reviewers: items the model scored 7-10.

`dsar review export` writes one row per high-scoring item with everything a
reviewer needs to decide on it without opening the database: the model's score
and reason, the evidence highlighted in its surrounding text, whether that
evidence was found in the source, and where the item came from (custodian,
location, date, sender). The reviewer records an assessment of the score, a
disclosure decision, the reason or exemption, notes, their name and the date.

`dsar review import` reads the decisions back into `review_decisions`
(append-only, never over the model's score) and reports what is outstanding.
Nothing leaves the machine: the workbook is written to the case output folder.
"""
import json
import re
import sqlite3
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from dsar import audit, db, evidence, report

MIN_SCORE = 7
STATUS_TO_REVIEW, STATUS_DONE = "To review", "Done"
EVIDENCE_OPEN, EVIDENCE_CLOSE = "【", "】"
CONTEXT_CHARS = 1500       # either side of the evidence
CELL_LIMIT = 32000         # Excel's cell limit is 32,767

ASSESSMENTS = ["Agree", "Score too high", "Score too low"]
DECISIONS = [
    "Disclose",
    "Disclose with redactions",
    "Withhold (exemption applies)",
    "Not the subject's personal data",
]
REASONS = [
    "None",
    "Third-party personal data (DPA 2018 Sch. 2 Part 3)",
    "Legal professional privilege (Sch. 2 para 19)",
    "Management forecasting or planning (Sch. 2 para 22)",
    "Negotiations with the requester (Sch. 2 para 23)",
    "Confidential reference (Sch. 2 para 24)",
    "Crime and taxation (Sch. 2 para 2)",
    "Other (explain in notes)",
]

MODEL_COLUMNS = ["review_id", "status", "model_score", "model_reason", "evidence", "evidence_found_in_source", "context",
                 "source_type", "date", "from", "to", "subject", "custodian", "location", "source_path", "item_ref"]
REVIEW_COLUMNS = ["assessment", "adjusted_score", "decision", "reason", "notes", "reviewer", "review_date"]
COLUMNS = MODEL_COLUMNS + REVIEW_COLUMNS

GUIDANCE = [
    "How to review",
    "",
    "Each row is a passage the model scored 7 or more on the 0-10 scale: 6-7 specific personal information "
    "(address, date of birth, HR actions), 8-9 opinions about the person's behaviour or performance, 10 highly "
    "opinionated, derogatory or sensitive.",
    "The 'context' column shows the passage with the model's evidence between 【 】 brackets. 'evidence_found_in_source' "
    "says whether that quote really appears in the document: yes / partial / no / missing. Treat anything but "
    "'yes' with care: the model can invent quotes.",
    "The 'status' column says which rows still need your decision (To review, listed first) and which are Done.",
    "Fill in the yellow columns only:",
    "  assessment       Is the model's score right? Agree / Score too high / Score too low.",
    "  adjusted_score   Your score (0-10) if you disagree; leave blank if you agree.",
    "  decision         What happens to this item in the response.",
    "  reason           The exemption or reason for redacting or withholding; 'None' if disclosed in full.",
    "  notes            Anything the operator or DPO needs to know (e.g. what to redact).",
    "  reviewer         Your name.",
    "  review_date      The date you reviewed it.",
    "Do not change the other columns, re-order rows or delete rows; the review_id links your decision back.",
    "You can return a partly completed workbook; unreviewed rows stay outstanding.",
    "If you are unsure whether an exemption applies, choose 'Other (explain in notes)' and ask the DPO.",
]


def _cell_safe(value):
    """Excel can't store most control characters; swap each for a space so evidence positions still line up."""
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    return ILLEGAL_CHARACTERS_RE.sub(" ", value) if isinstance(value, str) else value


def _rows(conn: sqlite3.Connection, min_score: int) -> list[dict]:
    out = []
    for r in conn.execute(report.REPORT_SQL):
        if r["score"] is None or r["score"] < min_score:
            continue
        try:
            meta = json.loads(r["metadata_json"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        st = r["source_type"]
        out.append({
            "review_id": r["chunk_id"],
            "model_score": r["score"],
            "model_reason": r["description"] or "",
            "evidence": r["evidence"] or "",
            "evidence_found_in_source": evidence.verify(r["score"], r["evidence"], r["content"], r["subject"]),
            "content": r["content"] or "",
            "source_type": st,
            "date": report._sent_field(meta, st),
            "from": report._from_field(meta, st),
            "to": report._to_field(meta, st),
            "subject": r["subject"] or "",
            "custodian": r["email_custodians"] or r["purview_custodian"] or "",
            "location": r["email_locations"] or r["purview_location"] or "",
            "source_path": r["source_path"],
            "item_ref": r["item_ref"],
        })
    return out


def evidence_spans(content: str, quote: str) -> list[tuple[int, int]]:
    """Where the evidence (or each quoted / '...'-separated part of it) appears in the text, ignoring case."""
    if not quote:
        return []
    parts = [a or b for a, b in re.findall(r'"([^"]+)"|“([^”]+)”', quote)] or re.split(r"\.\.\.|…|;", quote)
    spans = []
    low = content.lower()
    for part in parts:
        part = part.strip(" \"'“”.")
        if len(part) < 3:
            continue
        i = low.find(part.lower())
        if i >= 0:
            spans.append((i, i + len(part)))
    return sorted(spans)


def context_window(content: str, spans: list[tuple[int, int]]) -> tuple[str, list[tuple[int, int]]]:
    """A slice of the text around the evidence, with span positions relative to it."""
    if spans:
        start = max(0, spans[0][0] - CONTEXT_CHARS)
        end = min(len(content), spans[-1][1] + CONTEXT_CHARS)
    else:
        start, end = 0, min(len(content), 2 * CONTEXT_CHARS)
    end = min(end, start + CELL_LIMIT - 10)
    text = content[start:end]
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(content) else ""
    shifted = [(a - start + len(prefix), b - start + len(prefix)) for a, b in spans if a >= start and b <= end]
    return prefix + text + suffix, shifted


def _marked(text: str, spans: list[tuple[int, int]]) -> str:
    """Bracket each evidence span with 【 】. Plain text, because Excel rejects openpyxl's inline rich text as corrupt."""
    parts, pos = [], 0
    for a, b in spans:
        if a < pos:
            continue
        parts += [text[pos:a], EVIDENCE_OPEN, text[a:b], EVIDENCE_CLOSE]
        pos = b
    return "".join(parts) + text[pos:]

def export(conn: sqlite3.Connection, out_path: Path, min_score: int = MIN_SCORE, outstanding: bool = False,
           ids: set[str] | None = None) -> dict:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    rows = _rows(conn, min_score)
    # Rows already decided in an earlier import come pre-filled so the reviewer sees their work.
    decided = latest_decisions(conn)
    if outstanding:
        rows = [r for r in rows if r["review_id"] not in decided]
    if ids is not None:
        rows = [r for r in rows if r["review_id"] in ids]
    # Items still to review first; the sort is stable, so each group keeps its score order.
    rows.sort(key=lambda r: r["review_id"] in decided)

    wb = Workbook()
    ws = wb.active
    ws.title = "Review"
    ws.append(COLUMNS)
    for r in rows:
        r = {k: _cell_safe(v) for k, v in r.items()}
        spans = evidence_spans(r["content"], r["evidence"])
        context, shifted = context_window(r["content"], spans)
        prior = decided.get(r["review_id"], {})
        r["status"] = STATUS_DONE if prior else STATUS_TO_REVIEW
        ws.append([None if c == "context" else r[c] for c in MODEL_COLUMNS] + [prior.get(c) for c in REVIEW_COLUMNS])
        ws.cell(ws.max_row, MODEL_COLUMNS.index("context") + 1).value = _marked(context, shifted)

    grey, yellow = PatternFill("solid", fgColor="DDDDDD"), PatternFill("solid", fgColor="FFF2CC")
    green, orange = PatternFill("solid", fgColor="C6EFCE"), PatternFill("solid", fgColor="F8CBAD")
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = yellow if cell.value in REVIEW_COLUMNS else grey
    n = len(rows) + 1
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        for cell in row[len(MODEL_COLUMNS):]:
            cell.fill = yellow
        status = row[MODEL_COLUMNS.index("status")]
        status.font = Font(bold=True)
        status.fill = green if status.value == STATUS_DONE else orange
    widths = dict(review_id=18, status=12, model_score=7, model_reason=28, evidence=30, evidence_found_in_source=10,
                  context=80, source_type=12, date=12, subject=25, custodian=18, location=25, source_path=30,
                  assessment=14, adjusted_score=9, decision=22, reason=30, notes=35, reviewer=14, review_date=12)
    from openpyxl.utils import get_column_letter
    for i, name in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(name, 14)
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{n}"

    lists = wb.create_sheet("Lists")
    for col, values in enumerate((ASSESSMENTS, DECISIONS, REASONS), start=1):
        for i, v in enumerate(values, start=1):
            lists.cell(i, col).value = v
    lists.sheet_state = "hidden"

    def col(name: str) -> str:
        return get_column_letter(COLUMNS.index(name) + 1)

    for name, letter, count in (("assessment", "A", len(ASSESSMENTS)), ("decision", "B", len(DECISIONS)),
                                ("reason", "C", len(REASONS))):
        dv = DataValidation(type="list", formula1=f"=Lists!${letter}$1:${letter}${count}", allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(f"{col(name)}2:{col(name)}{max(n, 2)}")
    score_dv = DataValidation(type="whole", operator="between", formula1="0", formula2="10", allow_blank=True)
    ws.add_data_validation(score_dv)
    score_dv.add(f"{col('adjusted_score')}2:{col('adjusted_score')}{max(n, 2)}")
    date_dv = DataValidation(type="date", operator="greaterThan", formula1="2000-01-01", allow_blank=True)
    ws.add_data_validation(date_dv)
    date_dv.add(f"{col('review_date')}2:{col('review_date')}{max(n, 2)}")

    guide = wb.create_sheet("Guidance", 0)
    for line in GUIDANCE:
        guide.append([line])
    guide["A1"].font = Font(bold=True, size=14)
    guide.column_dimensions["A"].width = 130
    for row in guide.iter_rows(min_row=2):
        row[0].alignment = Alignment(wrap_text=True)

    summary = wb.create_sheet("Summary", 1)
    summary.append(["Score", "Items", "Decided", "Outstanding"])
    rng = f"Review!${col('decision')}$2:${col('decision')}${max(n, 2)}"
    score_rng = f"Review!${col('model_score')}$2:${col('model_score')}${max(n, 2)}"
    for i, s in enumerate(range(min_score, 11), start=2):
        summary.append([s, f"=COUNTIF({score_rng},{s})", f"=COUNTIFS({score_rng},{s},{rng},\"<>\")",
                        f"=B{i}-C{i}"])
    summary.append(["Total", f"=SUM(B2:B{i})", f"=SUM(C2:C{i})", f"=SUM(D2:D{i})"])
    for cell in summary[1]:
        cell.font = Font(bold=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return {"rows": len(rows), "by_score": dict(Counter(r["model_score"] for r in rows)),
            "prefilled": sum(1 for r in rows if r["review_id"] in decided),
            "evidence_not_found": sum(1 for r in rows if r["evidence_found_in_source"] != evidence.YES)}


def _cell_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    return str(v).strip()


def import_workbook(conn: sqlite3.Connection, path: Path) -> dict:
    from openpyxl import load_workbook

    ws = load_workbook(path, read_only=True)["Review"]
    rows = list(ws.iter_rows(values_only=True))
    header = list(rows[0])
    missing = [c for c in ("review_id", "model_score", *REVIEW_COLUMNS) if c not in header]
    if missing:
        raise ValueError(f"{path.name} is not a review workbook (missing columns {missing})")
    idx = {c: header.index(c) for c in header if c}
    known = {r[0] for r in conn.execute("SELECT chunk_id FROM classifications")}
    digest = audit.sha256_file(path)
    now = db._now()

    imported, outstanding, problems = 0, 0, []
    decisions: Counter = Counter()
    for line, row in enumerate(rows[1:], start=2):
        rid = _cell_text(row[idx["review_id"]])
        if not rid:
            continue
        values = {c: _cell_text(row[idx[c]]) for c in REVIEW_COLUMNS}
        if not values["decision"]:
            outstanding += 1
            continue
        errors = []
        if rid not in known:
            errors.append("review_id not in this case's database")
        if values["decision"] not in DECISIONS:
            errors.append(f"decision '{values['decision']}' not one of the listed options")
        if values["assessment"] and values["assessment"] not in ASSESSMENTS:
            errors.append(f"assessment '{values['assessment']}' not one of the listed options")
        if values["reason"] and values["reason"] not in REASONS:
            errors.append(f"reason '{values['reason']}' not one of the listed options")
        if values["decision"] in DECISIONS[1:3] and values["reason"] in ("", "None"):
            errors.append("redacting or withholding needs a reason")
        adjusted = None
        if values["adjusted_score"]:
            try:
                adjusted = int(float(values["adjusted_score"]))
                if not 0 <= adjusted <= 10:
                    raise ValueError
            except ValueError:
                errors.append(f"adjusted_score '{values['adjusted_score']}' is not 0-10")
        if not values["reviewer"]:
            errors.append("reviewer name missing")
        if errors:
            problems.append({"row": line, "review_id": rid, "errors": errors})
            continue
        model_score = row[idx["model_score"]]
        conn.execute(
            "INSERT INTO review_decisions (chunk_id, model_score, assessment, adjusted_score, decision, reason, "
            "notes, reviewer, review_date, workbook, workbook_sha256, imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, model_score if isinstance(model_score, int) else None, values["assessment"] or None, adjusted,
             values["decision"], values["reason"] or None, values["notes"] or None, values["reviewer"],
             values["review_date"] or None, path.name, digest, now))
        imported += 1
        decisions[values["decision"]] += 1
    conn.commit()
    return {"workbook": path.name, "workbook_sha256": digest, "imported": imported, "outstanding": outstanding,
            "invalid": len(problems), "problems": problems, "decisions": dict(decisions)}


def latest_decisions(conn: sqlite3.Connection) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT * FROM review_decisions ORDER BY imported_at, rowid"):
        out[r["chunk_id"]] = {c: r[c] for c in REVIEW_COLUMNS}
    return out


def status(conn: sqlite3.Connection, min_score: int = MIN_SCORE) -> dict:
    """Reviewed vs outstanding per model score, from the latest decision per item (workbooks and the web app)."""
    from dsar import disclosure

    decided = disclosure.latest(conn)
    per_score: dict = {}
    for (score, chunk_id) in conn.execute("SELECT score, chunk_id FROM classifications WHERE score >= ?", (min_score,)):
        s = per_score.setdefault(score, {"items": 0, "decided": 0})
        s["items"] += 1
        s["decided"] += chunk_id in decided
    return {"per_score": dict(sorted(per_score.items())),
            "decisions": dict(Counter(d["decision"] for d in decided.values())),
            "items": sum(s["items"] for s in per_score.values()),
            "decided": sum(s["decided"] for s in per_score.values())}
