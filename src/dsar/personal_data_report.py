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

The template is `Personal Data Report.docx`, taken from the case's own
data/templates/ folder if it has one (a copy tailored for the case), else from
DSAR_TEMPLATES_DIR (default <project>/Templates). The intro is kept and the
"INSERT DATA REPORT" guidance and "SAMPLE TABLE" label are removed. Then:
  - the controller's generic template (it has the SAMPLE TABLE): the sample table
    becomes Data Subject, then one "Extracted data N" row per extract;
  - a tailored template: every row is kept as written. The extracts go in rows
    "Extract N" after the row whose label mentions extracts or "mention you" (or at
    the end), and that row's own [placeholder] points to them. The Data subject
    row is filled only if it is empty or a [placeholder].

A report built while anything is outstanding (items not reviewed, passages not yet
selected, redaction elements undecided, text changed since a decision, or any
[placeholder] left in the template for the operator to complete) is marked DRAFT
in its name and on its first page. Both files are written read-only to
output/personal_data_report/ and logged with their SHA-256 and the template's.
"""
import copy
import csv
import io
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from dsar import audit, db, disclosure

TEMPLATE_NAME = "Personal Data Report.docx"
OUT_DIR = "personal_data_report"
_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
REGISTER_COLUMNS = ["extract_no", "extract", "origin", "date", "source_type", "from", "subject", "source",
                    "item_ref", "position", "content_sha256", "decision", "decision_source", "reviewer", "decided_at"]


def templates_dir() -> Path:
    env = os.environ.get("DSAR_TEMPLATES_DIR")
    return Path(env).expanduser() if env else Path(__file__).resolve().parents[2] / "Templates"


def template_path(*data_dirs: Path | None) -> Path:
    """The first of these cases' own templates that exists, else the shared one."""
    for data_dir in data_dirs:
        if data_dir is not None:
            own = Path(data_dir) / "templates" / TEMPLATE_NAME
            if own.is_file():
                return own
    return templates_dir() / TEMPLATE_NAME


_PLACEHOLDER = re.compile(r"\[[^\]\n]{2,}\]")
_ANCHOR = re.compile(r"extract|mention you", re.I)


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text).strip().lower()


STACK_PREFIX = "review_stack_"
STACK_INDEX = "stack_index.json"


def _slug(text: str, n: int = 50) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:n] or "item"


def send_to_reviewer(conn, output_dir: Path, chunk_ids: list[str], reviewer: str) -> dict:
    """Write these items into a new review stack folder for the Personal Data Reviewer.

    Each file is the item's text exactly (its SHA-256 equals the decision's content
    hash), named <NN>_<date>_<title>_<chunk id>.txt in the given order. Each item gets
    an IN_REVIEWER decision, so the report takes its content only from that folder.
    """
    import json

    from dsar import report as results

    if not reviewer:
        raise ValueError("sign in first")
    folder = Path(output_dir) / (STACK_PREFIX + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    folder.mkdir(parents=True, exist_ok=False)
    latest = disclosure.latest(conn)
    index, written = {}, []
    for n, cid in enumerate(dict.fromkeys(chunk_ids), start=1):
        r = conn.execute("SELECT * FROM chunks WHERE chunk_id = ?", (cid,)).fetchone()
        if r is None:
            continue
        try:
            meta = json.loads(r["metadata_json"] or "{}")
        except ValueError:
            meta = {}
        date = (results._sent_field(meta, r["source_type"]) or "")[:10] or "undated"
        title = r["subject"] or Path(r["source_path"].replace("\\", "/")).name
        name = f"{n:02d}_{date}_{_slug(title)}_{cid}.txt"
        path = folder / name
        path.write_text(r["content"] or "", encoding="utf-8")
        sha = audit.sha256_file(path)
        if sha != disclosure.content_sha256(r["content"] or ""):
            raise ValueError(f"{name}: written file does not match the item's text")
        did = disclosure.record_in_reviewer(conn, cid, sha, f"{folder.name}/{name}", reviewer)
        prior = latest.get(cid) or {}
        index[name] = {"chunk_id": cid, "sha256": sha, "decision_id": did, "previous_decision": prior.get("decision", ""),
                       "previous_notes": prior.get("notes", ""), "previous_source": prior.get("source", "")}
        written.append(name)
    (folder / STACK_INDEX).write_text(json.dumps(index, indent=1), encoding="utf-8")
    conn.commit()
    return {"folder": str(folder), "items": len(written), "files": written,
            "index_sha256": audit.sha256_file(folder / STACK_INDEX), "reviewer": reviewer}


def _stack_skip(conn, path: Path) -> set[str]:
    """Files in a review stack whose item has since been decided in Review (newest decision isn't IN_REVIEWER)."""
    import json

    idx = path / STACK_INDEX
    if conn is None or not idx.is_file():
        return set()
    latest = disclosure.latest(conn)
    return {name for name, v in json.loads(idx.read_text(encoding="utf-8")).items()
            if latest.get(v["chunk_id"], {}).get("decision") != disclosure.IN_REVIEWER}


def redaction_pieces(folders: dict[str, Path], conn=None) -> tuple[list[dict], list[dict]]:
    """Released passages from each redaction folder, and the undecided elements per file."""
    from dsar.toolbox.redaction import markup

    pieces, undecided = [], []
    for path in folders.values():
        skip = _stack_skip(conn, path)
        docs = [d for d in markup.Folder(path, "", None).docs() if d["name"] not in skip]
        for d in docs:
            left = len(d["elements"]) - len(d["decisions"])
            if left:
                undecided.append({"file": str(path / d["name"]), "why": f"{left} element(s) undecided in the Personal Data Reviewer"})
        for item in markup.extracts(docs):
            for src in item["sources"]:
                m = _DATE.search(src["file"])
                pieces.append({"origin": "redaction", "text": item["text"], "date": m.group(1) if m else "",
                               "source_type": "document", "from": src["speaker"], "subject": "",
                               "source_path": str(path / src["file"]), "item_ref": src["element"],
                               "span": src["time"], "content_sha256": src["file_sha256"],
                               "decision": src["decision"], "decision_source": "Personal Data Reviewer",
                               "reviewer": src["reviewer"], "decided_at": src["decided"]})
    return pieces, undecided


def folder_choices(conn) -> dict[str, dict]:
    out = {}
    for r in conn.execute("SELECT * FROM report_folder_choices ORDER BY decided_at, rowid"):
        out[r["folder"]] = {"included": bool(r["included"]), "reason": r["reason"] or "",
                            "reviewer": r["reviewer"], "at": r["decided_at"]}
    return out


def choose_folder(conn, folder: Path, included: bool, reason: str, reviewer: str) -> dict:
    reason = reason.strip()
    if not included and not reason:
        raise ValueError("leaving a folder out of the report needs a reason")
    if not reviewer:
        raise ValueError("sign in first")
    conn.execute("INSERT INTO report_folder_choices (folder, included, reason, reviewer, decided_at) VALUES (?,?,?,?,?)",
                 (str(folder), int(included), reason or None, reviewer, db._now()))
    conn.commit()
    return {"folder": str(folder), "included": included, "reason": reason, "reviewer": reviewer}


def copies_elsewhere(folders: dict[str, Path]) -> dict[str, str]:
    """Folders whose every .txt file is byte-identical to a file in another folder: {folder: other folder}."""
    hashes = {fid: {audit.sha256_file(f) for f in p.glob("*.txt")} for fid, p in folders.items()}
    out = {}
    for fid, mine in hashes.items():
        for other, theirs in hashes.items():
            if other != fid and mine and mine <= theirs and (mine != theirs or fid > other):
                out[fid] = str(folders[other])
                break
    return out


def folder_rows(conn, folders: dict[str, Path]) -> list[dict]:
    """Each redaction folder with whether it counts towards the report, and why not."""
    choices, copies = folder_choices(conn), copies_elsewhere(folders)
    rows = []
    for fid, path in folders.items():
        c = choices.get(str(path))
        rows.append({"id": fid, "path": path, "included": c["included"] if c else True, "choice": c,
                     "copy_of": copies.get(fid, "")})
    return rows


def assemble(conn, folders: dict[str, Path]) -> dict:
    """Everything the report would contain now, merged and ordered, plus what is outstanding.

    Redaction folders the operator left out (report_folder_choices) are skipped and listed.
    """
    rows = folder_rows(conn, folders)
    excluded = [{"folder": str(r["path"]), **r["choice"]} for r in rows if not r["included"]]
    folders = {r["id"]: r["path"] for r in rows if r["included"]}
    review = disclosure.report_inputs(conn)
    red_pieces, undecided = redaction_pieces(folders, conn)
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
            "folders": [str(p) for p in folders.values()], "excluded_folders": excluded, "folder_rows": rows}


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


def _fill(template: Path, subject_name: str, extracts: list[dict]):
    import docx

    doc = docx.Document(str(template))
    generic = False
    for p in list(doc.paragraphs):
        t = p.text.strip()
        generic = generic or t == "SAMPLE TABLE"
        if t.startswith("INSERT DATA REPORT") or t == "SAMPLE TABLE":
            p._p.getparent().remove(p._p)
    if not doc.tables:
        raise ValueError(f"{template.name} has no table to fill")
    table = doc.tables[0]
    rows = table.rows
    if len(rows) < 2:
        raise ValueError(f"{template.name}: the table needs a header row and a 'Data subject' row")
    if generic:
        model = copy.deepcopy(rows[1]._tr)
        for r in list(rows)[2:]:
            r._tr.getparent().remove(r._tr)
        _set_cell(table.rows[1].cells[1], subject_name)
        for n, e in enumerate(extracts, start=1):
            table._tbl.append(copy.deepcopy(model))
            row = table.rows[-1]
            _set_cell(row.cells[0], f"Extracted data {n}:")
            _set_cell(row.cells[1], e["text"])
        return doc
    subject_row = rows[1]
    value = subject_row.cells[1].text.strip()
    if subject_name and (not value or _PLACEHOLDER.fullmatch(value)):
        _set_cell(subject_row.cells[1], subject_name)
    anchor = next((r for r in list(rows)[1:] if _ANCHOR.search(r.cells[0].text)), rows[-1])
    if extracts and _PLACEHOLDER.fullmatch(anchor.cells[1].text.strip()):
        _set_cell(anchor.cells[1], f"See Extract 1{f' to Extract {len(extracts)}' if len(extracts) > 1 else ''} below.")
    model, after = copy.deepcopy(anchor._tr), anchor._tr
    for n, e in enumerate(extracts, start=1):
        tr = copy.deepcopy(model)
        after.addnext(tr)
        after = tr
        from docx.table import _Row
        row = _Row(tr, table)
        _set_cell(row.cells[0], f"Extract {n}:")
        _set_cell(row.cells[1], e["text"])
    return doc


_HEADER_LINE = re.compile(r"^(From|To|Cc|Bcc|Sent|Date|Subject):", re.M)
_EXTRACT_LABEL = re.compile(r"Extract(?:ed data)? (\d+):")


def has_headers(text: str) -> bool:
    """Email header lines in an extract: they usually name other people and need narrowing."""
    return bool(_HEADER_LINE.search(text))


def preview(template: Path, subject_name: str, extracts: list[dict]) -> dict:
    """The report as it would be built now: intro paragraphs and table rows, from the same fill as build()."""
    doc = _fill(template, subject_name, extracts)
    intro = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    rows = []
    for i, row in enumerate(doc.tables[0].rows):
        label, value = row.cells[0].text.strip(), row.cells[1].text.strip() if len(row.cells) > 1 else ""
        m = _EXTRACT_LABEL.fullmatch(label)
        if i == 0:
            kind = "header"
        elif m:
            kind = "extract"
        elif _PLACEHOLDER.search(value):
            kind = "placeholder"
        else:
            kind = "field"
        rows.append({"label": label, "value": value, "kind": kind,
                     "extract": extracts[int(m.group(1)) - 1] if m else None})
    return {"intro": intro, "rows": rows, "placeholders": placeholders(doc)}


def placeholders(doc) -> list[str]:
    """[Bracketed] text left in the document for the operator to complete."""
    texts = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for row in t.rows:
            if re.match(r"Extract(ed data)? \d+:", row.cells[0].text.strip()):
                continue        # extract text is the source's own, brackets and all
            texts += [c.text for c in row.cells]
    return list(dict.fromkeys(m for t in texts for m in _PLACEHOLDER.findall(t)))


def build_docx(template: Path, subject_name: str, extracts: list[dict], draft_note: str = "") -> bytes:
    doc = _fill(template, subject_name, extracts)
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


def build(conn, output_dir: Path, case_ref: str, subject_name: str, folders: dict[str, Path], reviewer: str,
          report_output_dir: Path | None = None) -> dict:
    """Write the report and its private register read-only, and log them.

    output_dir / case_ref: the case whose decisions and folders are used. report_output_dir:
    where the files go when that case is a deeper search under a main case (the files then
    carry the main case's reference, passed as case_ref). Logged in both cases.
    """
    report_output_dir = Path(report_output_dir or output_dir)
    template = template_path(Path(output_dir).parent, report_output_dir.parent)
    if not template.is_file():
        raise FileNotFoundError(f"Template not found: {template}. Put '{TEMPLATE_NAME}' there or set DSAR_TEMPLATES_DIR.")
    data = assemble(conn, folders)
    n_out = len(data["outstanding"])
    left = placeholders(_fill(template, subject_name, data["extracts"]))
    draft = n_out > 0 or not subject_name or bool(left)
    note = ""
    if draft:
        bits = [f"{n_out} item(s) outstanding"] if n_out else []
        if not subject_name:
            bits.append("no data subject name in the case config")
        if left:
            bits.append(f"{len(left)} [placeholder](s) in the template still to complete")
        note = f"DRAFT: not for release. {'; '.join(bits)}."
    out_dir = report_output_dir / OUT_DIR
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
              "excluded_folders": data["excluded_folders"],
              "sha256": {report_path.name: audit.sha256_file(report_path),
                         register_path.name: audit.sha256_file(register_path),
                         "template": audit.sha256_file(template)},
              "template": str(template), "placeholders_left": left, "reviewer": reviewer,
              "decisions_from": str(Path(output_dir).parent)}
    audit.AuditLog(report_output_dir).append("report", "report personal-data", result)
    if report_output_dir.resolve() != Path(output_dir).resolve():
        audit.AuditLog(output_dir).append("report", "report personal-data", result)
    return result


def builds(output_dir: Path) -> list[dict]:
    d = Path(output_dir) / OUT_DIR
    if not d.is_dir():
        return []
    return [{"name": p.name, "draft": "_DRAFT" in p.name, "bytes": p.stat().st_size}
            for p in sorted(d.glob("Personal_Data_Report_*.docx"), reverse=True)]
