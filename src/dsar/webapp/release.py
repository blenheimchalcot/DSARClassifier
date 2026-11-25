"""
What is left before the response to a request can be released (the case page).

One checklist per request, kept on the main case (webapp.cases.family):
  - worked out from the case: review decisions, whole passages to narrow, redaction
    folders still undecided, and whether a final (non-draft) report has been built;
  - confirmed by the operator: the steps the app can't see (receipt date, identity,
    extension, controller, letter and enclosures, sending). Each tick or un-tick is
    an audit entry (`release check`) in the main case under the signed-in reviewer;
    the newest entry per step counts.
Release documents: each template in the case's Templates folder (or the shared
one) is a document the response needs. The prepared copies are looked for in the
case documents folder, DSAR_CASE_DOCS_ROOT/<main CASE-REF>/Output (the operator's
OneDrive case folder), and the Personal Data Report also among the app's builds.
Each is missing, still the unchanged template, has text left to complete
([placeholders], INSERT guidance, "confirm before sending", "delete this ..."), or
ready. Every enclosure the letter lists under "Enclosed documents" must be there.
These files are only read, never changed.

The deadline is one calendar month from the date received (the same date next month,
or that month's last day when it has no such date); if it falls on a weekend or bank
holiday the response is due by the next working day.
"""
import calendar
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

from dsar import audit, db, disclosure, personal_data_report

COMMAND = "release check"
# (key, label, what to record)
CHECKS = [
    ("received", "Date the request was received confirmed", "date (YYYY-MM-DD)"),
    ("identity", "Requester's identity verified", "how"),
    ("extension", "Extension decided (Art. 12(3)): none, or taken and the requester told within the month", "decision"),
    ("controller", "Controller legal entity confirmed", "registered name"),
    ("scope", "Search scope and its rationale recorded", "note"),
    ("open_items", "Open items closed (images read, unmatched emails checked, …)", "note"),
    ("letter", "Response letter final", "note"),
    ("exemptions", "Exemptions Report final", "note"),
    ("not_in_scope", "Data not in Scope final", "note"),
    ("enclosures", "Enclosures ready (privacy notice, documents released in full or redacted)", "note"),
    ("sent", "Response sent", "date, method, and what was sent (with its SHA-256)"),
]
KEYS = {k for k, _, _ in CHECKS}


_TO_COMPLETE = re.compile(r"\[[^\]\n]{2,}\]|\bINSERT\b[^.\n]{0,60}|confirm before sending|[Dd]elete this (?:row|instruction)[^.\n]{0,40}")


_SF_DATALESS = 0x40000000      # macOS: file is online-only (OneDrive Files On-Demand); reading it downloads it


def online_only(path: Path) -> bool:
    return bool(getattr(path.stat(), "st_flags", 0) & _SF_DATALESS)


def case_docs_dir(main_ref: str) -> Path | None:
    root = os.environ.get("DSAR_CASE_DOCS_ROOT")
    return Path(root).expanduser() / main_ref if root else None


def _docx_texts(path: Path) -> list[str]:
    import docx

    d = docx.Document(str(path))
    return [p.text for p in d.paragraphs] + [c.text for tb in d.tables for r in tb.rows for c in r.cells]


def _requirement(template: Path) -> str:
    stem = template.stem
    return "Response letter" if "letter" in stem.lower() or stem.startswith("Subject Access Request") else stem


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def documents(main_ref: str, main_output: Path) -> dict:
    """Each release document from the templates, and whether a prepared copy is ready."""
    docs_dir = case_docs_dir(main_ref)
    out_dir = docs_dir / "Output" if docs_dir else None
    tdir = docs_dir / "Templates" if docs_dir and (docs_dir / "Templates").is_dir() else personal_data_report.templates_dir()
    templates = sorted(tdir.glob("*.docx")) if tdir.is_dir() else []
    prepared = sorted(p for p in out_dir.iterdir() if p.is_file() and not p.name.startswith(("~$", "."))) if out_dir and out_dir.is_dir() else []
    reqs: dict[str, list[Path]] = {}
    for tp in templates:
        reqs.setdefault(_requirement(tp), []).append(tp)
    rows = []
    for name, tps in reqs.items():
        key = "letter" if name == "Response letter" else _norm(name)
        cands = [p for p in prepared if p.suffix.lower() == ".docx" and
                 (key in _norm(p.stem) if key != "letter" else "letter" in p.stem.lower())]
        row = {"document": name, "templates": [tp.name for tp in tps], "file": "", "status": "missing", "left": []}
        if name == personal_data_report.TEMPLATE_NAME.removesuffix(".docx"):
            built = personal_data_report.builds(main_output)
            if built:
                b = built[0]
                row.update(file=f"{main_ref}/data/output/{personal_data_report.OUT_DIR}/{b['name']}",
                           status="draft (app build)" if b["draft"] else "ready")
                rows.append(row)
                continue
        if cands:
            f = max(cands, key=lambda p: p.stat().st_mtime)
            row["file"] = str(f.relative_to(docs_dir))
            if online_only(f):
                # Never trigger a download from the case page.
                row["status"] = "online only: set 'Always keep on this device' to check"
            elif any(not online_only(tp) and audit.sha256_file(f) == audit.sha256_file(tp) for tp in tps):
                row["status"] = "unchanged template"
            else:
                left = list(dict.fromkeys(m for tx in _docx_texts(f) for m in _TO_COMPLETE.findall(tx)))
                row.update(status="to complete" if left else "ready", left=left)
        rows.append(row)
    # Enclosures the letter lists.
    enclosures = []
    letter = next((r for r in rows if r["document"] == "Response letter" and r["file"]), None)
    if letter and not online_only(docs_dir / letter["file"]):
        lines, on = [], False
        for tx in _docx_texts(docs_dir / letter["file"]):
            if tx.strip().lower().startswith("enclosed documents"):
                on = True
                continue
            if on and tx.strip():
                lines.append(tx.strip())
        names = {_norm(r["document"]): r for r in rows}
        for line in lines:
            r = names.get(_norm(line))
            if r is not None:
                enclosures.append({"name": line, "present": bool(r["file"]), "file": r["file"]})
                continue
            hit = next((p for p in prepared if _norm(line) in _norm(p.stem) or (len(_norm(p.stem)) >= 8 and _norm(p.stem) in _norm(line))), None)
            enclosures.append({"name": line, "present": bool(hit), "file": str(hit.relative_to(docs_dir)) if hit else ""})
    ready = all(r["status"] == "ready" for r in rows) and all(e["present"] for e in enclosures) and bool(rows)
    return {"rows": rows, "enclosures": enclosures, "docs_dir": str(docs_dir) if docs_dir else "",
            "templates_dir": str(tdir), "ready": ready, "configured": docs_dir is not None}


def one_month_after(d: date) -> date:
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def manual(main_output: Path) -> dict[str, dict]:
    log = audit.AuditLog(main_output)
    out = {}
    if log.jsonl.exists():
        for e in log.entries():
            if e.get("command") == COMMAND and e["data"].get("item") in KEYS:
                out[e["data"]["item"]] = {**e["data"], "at": e["ts"], "seq": e["seq"]}
    return out


def record(main_output: Path, item: str, done: bool, value: str, reviewer: str) -> dict:
    value = value.strip()
    if item not in KEYS:
        raise ValueError("unknown step")
    if not reviewer:
        raise ValueError("sign in first")
    if done and item == "received":
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("enter the date received as YYYY-MM-DD") from None
    if done and item in ("sent", "controller", "extension") and not value:
        raise ValueError("record the details for this step")
    data = {"item": item, "done": done, "value": value, "reviewer": reviewer}
    audit.AuditLog(main_output).append("release", COMMAND, data)
    return data


def _computed(review_dir: Path, main_output: Path, folders: dict[str, Path]) -> list[dict]:
    rows = []
    db_path = review_dir / "dsar.sqlite"
    if not db_path.is_file():
        return [{"label": "Review", "done": False, "detail": f"no dsar.sqlite in {review_dir}"}]
    with db.connect(db_path) as conn:
        db.init_schema(conn)
        counts = disclosure.queue(conn)["counts"]
        data = personal_data_report.assemble(conn, folders)
    left = counts[disclosure.TO_REVIEW]
    rows.append({"label": f"Items scored {disclosure.MIN_SCORE}+ decided", "done": left == 0, "link": "reviewer",
                 "detail": f"{left} to review" if left else "all decided"})
    need = counts[disclosure.NEEDS_PASSAGES]
    rows.append({"label": "Passages selected for workbook 'Disclose with redactions'", "done": need == 0,
                 "link": "review", "detail": f"{need} need passages" if need else "none left"})
    whole = len(data["review"]["whole_from_workbook"])
    rows.append({"label": "Passages chosen for workbook 'Disclose' items", "done": whole == 0, "link": "report",
                 "detail": f"{whole} item(s): nothing from them is in the report yet" if whole else "none"})
    held = sum(1 for o in data["outstanding"] if o["why"].startswith("conflict"))
    rows.append({"label": "No extract contains text left out in the Personal Data Reviewer", "done": held == 0,
                 "link": "report", "detail": f"{held} held out of the report" if held else "none"})
    undecided = [o for o in data["outstanding"] if o.get("file") and not o["why"].startswith("conflict")]
    n = sum(int(o["why"].split()[0]) for o in undecided)
    rows.append({"label": "Personal Data Reviewer folders fully decided", "done": not undecided, "link": "report",
                 "detail": f"{n} element(s) undecided in {len(undecided)} file(s)" if undecided else
                 f"{len(data['folders'])} folder(s) in the report, {len(data['excluded_folders'])} left out"})
    built = personal_data_report.builds(main_output)
    final = next((b for b in built if not b["draft"]), None)
    rows.append({"label": "Personal Data Report built (final, not draft)", "done": bool(final), "link": "report",
                 "detail": final["name"] if final else (f"latest is a draft: {built[0]['name']}" if built else "not built")})
    return rows


def next_step(computed: list[dict], manual: list[dict], docs: dict) -> dict:
    """The single most important thing to do now: review, then report, then documents, then sign-off, then send."""
    for c in computed:
        if not c["done"]:
            # "review": the Personal Data Reviewer; "queue": the classic list (workbook passages are chosen there).
            where = ("report" if c["label"].startswith("Personal Data Report built")
                     else "queue" if c.get("link") == "review" else "review")
            return {"text": f"{c['label']}: {c['detail']}", "where": where}
    for d in docs.get("rows", []):
        if d["status"] != "ready":
            return {"text": f"{d['document']}: {d['status']}", "where": "docs"}
    for m in manual:
        if not m.get("done") and m["key"] != "sent":
            return {"text": f"Confirm: {m['label']}", "where": "confirm"}
    if not next((m for m in manual if m["key"] == "sent"), {}).get("done"):
        return {"text": "Send the response and record it (Response sent)", "where": "confirm"}
    return {"text": "Done: the response has been sent.", "where": ""}


def status(fam, folders: dict[str, Path]) -> dict:
    main_output = fam.main_dir / "output"
    checks = manual(main_output)
    received = checks.get("received")
    deadline = days_left = None
    if received and received.get("done"):
        try:
            deadline = one_month_after(date.fromisoformat(received["value"]))
            days_left = (deadline - datetime.now(timezone.utc).date()).days
        except ValueError:
            pass
    rows = [{"key": k, "label": label, "hint": hint, **({"done": False} | checks.get(k, {}))}
            for k, label, hint in CHECKS]
    computed = _computed(fam.review_dir, main_output, folders)
    docs = documents(fam.main_ref, main_output)
    left = (sum(not r["done"] for r in rows) + sum(not r["done"] for r in computed)
            + sum(r["status"] != "ready" for r in docs["rows"]) + sum(not e["present"] for e in docs["enclosures"]))
    nxt = next_step(computed, rows, docs)
    return {"computed": computed, "manual": rows, "documents": docs, "deadline": deadline, "days_left": days_left, "next": nxt,
            "left": left, "sent": bool(checks.get("sent", {}).get("done"))}
