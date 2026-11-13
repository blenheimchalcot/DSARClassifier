"""
The Personal Data Report: built from the case's decisions into the controller's template.

Contents (extract text only; each piece of data once):
  - passages extracted in the review queue (dsar.disclosure), and workbook
    "Disclose" decisions taken as the whole passage;
  - passages released in the redaction tool's folders (markup.extracts).
Identical extracts are merged. They are ordered by the source's date where one is
known (a review item's sent date, or a YYYY-MM-DD in a redaction file's name),
undated last. The data subject sees the text only; every source of every extract
goes in a private register beside the report, for the case record.

The template is `Personal Data Report.docx` in the templates folder
(DSAR_TEMPLATES_DIR, default <project>/Templates). Its intro is kept; the
"INSERT DATA REPORT" guidance and "SAMPLE TABLE" label are removed, and the sample
table becomes: Data Subject, then one "Extracted data N" row per extract.

A report built while anything is outstanding (items not reviewed, passages not yet
selected, redaction elements undecided, text changed since a decision) is marked
DRAFT in its name and on its first page. Both files are written read-only to
output/personal_data_report/ and logged with their SHA-256 and the template's.
"""
import copy
import csv
import io
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from dsar import audit, disclosure

TEMPLATE_NAME = "Personal Data Report.docx"
OUT_DIR = "personal_data_report"
_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
REGISTER_COLUMNS = ["extract_no", "extract", "origin", "date", "source_type", "from", "subject", "source",
                    "item_ref", "position", "content_sha256", "decision", "decision_source", "reviewer", "decided_at"]


def templates_dir() -> Path:
    env = os.environ.get("DSAR_TEMPLATES_DIR")
    return Path(env).expanduser() if env else Path(__file__).resolve().parents[2] / "Templates"


def template_path() -> Path:
    return templates_dir() / TEMPLATE_NAME


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text).strip().lower()


def redaction_pieces(folders: dict[str, Path]) -> tuple[list[dict], list[dict]]:
    """Released passages from each redaction folder, and the undecided elements per file."""
    from dsar.toolbox.redaction import markup

    pieces, undecided = [], []
    for path in folders.values():
        docs = markup.Folder(path, "", None).docs()
        for d in docs:
            left = len(d["elements"]) - len(d["decisions"])
            if left:
                undecided.append({"file": str(path / d["name"]), "why": f"{left} element(s) undecided in the redaction tool"})
        for item in markup.extracts(docs):
            for src in item["sources"]:
                m = _DATE.search(src["file"])
                pieces.append({"origin": "redaction", "text": item["text"], "date": m.group(1) if m else "",
                               "source_type": "document", "from": src["speaker"], "subject": "",
                               "source_path": str(path / src["file"]), "item_ref": src["element"],
                               "span": src["time"], "content_sha256": src["file_sha256"],
                               "decision": src["decision"], "decision_source": "redaction tool",
                               "reviewer": src["reviewer"], "decided_at": src["decided"]})
    return pieces, undecided


def assemble(conn, folders: dict[str, Path]) -> dict:
    """Everything the report would contain now, merged and ordered, plus what is outstanding."""
    review = disclosure.report_inputs(conn)
    red_pieces, undecided = redaction_pieces(folders)
    merged: dict[str, dict] = {}
    for p in review["pieces"] + red_pieces:
        key = _norm(p["text"])
        if not key:
            continue
        entry = merged.setdefault(key, {"text": p["text"], "date": p["date"], "sources": []})
        if p["date"] and (not entry["date"] or p["date"] < entry["date"]):
            entry["date"] = p["date"]
        entry["sources"].append(p)
    extracts = sorted(merged.values(), key=lambda e: (not e["date"], e["date"][:10]))
    outstanding = review["outstanding"] + review["changed"] + undecided
    return {"extracts": extracts, "outstanding": outstanding, "review": review,
            "from_review": len(review["pieces"]), "from_redaction": len(red_pieces),
            "folders": [str(p) for p in folders.values()]}


def _set_cell(cell, text: str) -> None:
    """Replace a cell's text, keeping the first run's formatting; one paragraph per line."""
    paras = cell.paragraphs
    first = paras[0]
    run_fmt = copy.deepcopy(first.runs[0]._r.rPr) if first.runs and first.runs[0]._r.rPr is not None else None
    for p in paras[1:]:
        p._p.getparent().remove(p._p)
    for r in list(first.runs):
        r._r.getparent().remove(r._r)
    lines = text.split("\n") or [""]
    for i, line in enumerate(lines):
        p = first if i == 0 else cell.add_paragraph()
        if i:
            p.style = first.style
            p.paragraph_format.alignment = first.paragraph_format.alignment
        run = p.add_run(line)
        if run_fmt is not None:
            run._r.insert(0, copy.deepcopy(run_fmt))


def build_docx(template: Path, subject_name: str, extracts: list[dict], draft_note: str = "") -> bytes:
    import docx

    doc = docx.Document(str(template))
    for p in list(doc.paragraphs):
        t = p.text.strip()
        if t.startswith("INSERT DATA REPORT") or t == "SAMPLE TABLE":
            p._p.getparent().remove(p._p)
    if not doc.tables:
        raise ValueError(f"{template.name} has no table to fill")
    table = doc.tables[0]
    rows = table.rows
    if len(rows) < 2:
        raise ValueError(f"{template.name}: the table needs a header row and a 'Data Subject' row")
    model = copy.deepcopy(rows[1]._tr)
    for r in list(rows)[2:]:
        r._tr.getparent().remove(r._tr)
    _set_cell(table.rows[1].cells[1], subject_name)
    for n, e in enumerate(extracts, start=1):
        tr = copy.deepcopy(model)
        table._tbl.append(tr)
        row = table.rows[-1]
        _set_cell(row.cells[0], f"Extracted data {n}:")
        _set_cell(row.cells[1], e["text"])
    if draft_note:
        first = doc.paragraphs[0]
        para = first.insert_paragraph_before()
        run = para.add_run(draft_note)
        run.bold = True
        from docx.shared import RGBColor
        run.font.color.rgb = RGBColor(0xB0, 0x00, 0x00)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def register_csv(extracts: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(REGISTER_COLUMNS)
    for n, e in enumerate(extracts, start=1):
        for s in e["sources"]:
            w.writerow([n, e["text"], s["origin"], s["date"], s["source_type"], s["from"], s["subject"],
                        s.get("source_path", ""), s.get("item_ref", "") or s.get("chunk_id", ""), s.get("span", ""),
                        s["content_sha256"], s["decision"], s["decision_source"], s["reviewer"], s["decided_at"]])
    return buf.getvalue().encode("utf-8")


def build(conn, output_dir: Path, case_ref: str, subject_name: str, folders: dict[str, Path], reviewer: str) -> dict:
    """Write the report and its private register into the case, read-only, and log them."""
    template = template_path()
    if not template.is_file():
        raise FileNotFoundError(f"Template not found: {template}. Put '{TEMPLATE_NAME}' there or set DSAR_TEMPLATES_DIR.")
    data = assemble(conn, folders)
    n_out = len(data["outstanding"])
    draft = n_out > 0 or not subject_name
    note = ""
    if draft:
        bits = [f"{n_out} item(s) outstanding"] if n_out else []
        if not subject_name:
            bits.append("no data subject name in the case config")
        note = f"DRAFT: not for release. {'; '.join(bits)}."
    out_dir = Path(output_dir) / OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    n = 1
    while True:
        stamp = base + (f"_{n}" if n > 1 else "") + ("_DRAFT" if draft else "")
        report_path = out_dir / f"Personal_Data_Report_{case_ref}_{stamp}.docx"
        register_path = out_dir / f"personal_data_report_register_{case_ref}_{stamp}.csv"
        if not report_path.exists() and not register_path.exists():
            break
        n += 1
    report_path.write_bytes(build_docx(template, subject_name, data["extracts"], note))
    register_path.write_bytes(register_csv(data["extracts"]))
    for p in (report_path, register_path):
        p.chmod(0o444)
    result = {"report": str(report_path), "register": str(register_path), "draft": draft,
              "extracts": len(data["extracts"]), "from_review": data["from_review"],
              "from_redaction": data["from_redaction"], "outstanding": n_out,
              "whole_passages_from_workbook": len(data["review"]["whole_from_workbook"]),
              "decisions": data["review"]["decisions"], "redaction_folders": data["folders"],
              "sha256": {report_path.name: audit.sha256_file(report_path),
                         register_path.name: audit.sha256_file(register_path),
                         "template": audit.sha256_file(template)},
              "template": str(template), "reviewer": reviewer}
    audit.AuditLog(output_dir).append("report", "report personal-data", result)
    return result


def builds(output_dir: Path) -> list[dict]:
    d = Path(output_dir) / OUT_DIR
    if not d.is_dir():
        return []
    return [{"name": p.name, "draft": "_DRAFT" in p.name, "bytes": p.stat().st_size}
            for p in sorted(d.glob("Personal_Data_Report_*.docx"), reverse=True)]
