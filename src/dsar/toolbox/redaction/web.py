"""
The Personal Data Reviewer (formerly the redaction tool) inside the web app, at /toolbox/redaction/.

The same tool as `dsar markup serve` (markup.py: event log, decisions, export),
with the signed-in reviewer as the reviewer. Folders offered:
  - case-bound: every folder under the case output/ that holds .txt files
    (not the toolbox's own export folders), plus any --markup-folder;
  - free-standing: the --markup-folder folders, plus any added on the page.
"""
import hashlib
import html
import json
from collections import Counter
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, url_for

from dsar import audit
from dsar.toolbox.redaction import markup
from dsar.webapp import records

TOOL = "redaction"
bp = Blueprint("tool_redaction", __name__, template_folder="templates")
_SKIP = {markup.EXPORT_DIR, markup.EXTRACT_DIR, records.TOOLBOX_DIR}
_added: dict[str, Path] = {}


def _fid(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12]


def case_folders(output_dir: Path) -> dict[str, Path]:
    """Every folder under a case output/ that holds .txt files (not the tools' own export folders)."""
    found: list[Path] = []
    if output_dir.is_dir():
        for d in sorted(p for p in output_dir.rglob("*") if p.is_dir()):
            rel = d.relative_to(output_dir).parts
            if len(rel) <= 3 and not _SKIP.intersection(rel) and any(d.glob("*.txt")):
                found.append(d)
    return {_fid(p): p for p in found}


def _outputs() -> list[Path]:
    """Case output folders to look in: this case's and, for a request with a deeper search, the review data's."""
    s = current_app.config["DSAR"]
    if not s.case_bound:
        return []
    from dsar.webapp import cases
    fam = cases.family(s)
    return [s.output_dir] + ([fam.review_output] if fam.review_output.resolve() != s.output_dir.resolve() else [])


def folders() -> dict[str, Path]:
    s = current_app.config["DSAR"]
    found = [p for out in _outputs() for p in case_folders(out).values()]
    found += [Path(p) for p in s.markup_folders] + list(_added.values())
    return {_fid(p): p for p in found}


def _owner(path: Path) -> Path | None:
    """The case output folder a folder belongs to: its exports are logged there."""
    return next((out for out in _outputs() if path.is_relative_to(out)), None)


def _label(path: Path) -> str:
    s = current_app.config["DSAR"]
    out = _owner(path)
    if out is None:
        return str(path)
    rel = str(path.relative_to(out))
    return rel if out == s.output_dir else f"{out.parent.parent.name} › {rel}"


def _folder(fid: str) -> markup.Folder:
    path = folders().get(fid)
    if path is None:
        abort(404)
    return markup.Folder(path, records.reviewer(), _owner(path))


@bp.route("/")
def index():
    s = current_app.config["DSAR"]
    rows = []
    for fid, path in folders().items():
        f = markup.Folder(path, "", None)
        label = _label(path)
        rows.append({"id": fid, "label": label, "files": len(f.files()),
                     "events": sum(1 for _ in f.events.lines()) if f.events.path.exists() else 0})
    return render_template("redaction/index.html", rows=rows, error=request.args.get("error", ""))


@bp.route("/add", methods=["POST"])
def add():
    s = current_app.config["DSAR"]
    path = Path(request.form.get("path", "").strip()).expanduser()
    if s.case_bound:
        return redirect(url_for(".index", error="In a case, put the folder under the case output/ folder."))
    if not path.is_dir() or not any(path.glob("*.txt")):
        return redirect(url_for(".index", error=f"No .txt files in {path}"))
    _added[_fid(path)] = path
    return redirect(url_for(".index"))


@bp.route("/f/<fid>/")
def page(fid):
    f = _folder(fid)
    s = current_app.config["DSAR"]
    data = {"folder": str(f.folder), "files": len(f.files()),
            "events_sha256": audit.sha256_file(f.events.path) if f.events.path.exists() else None}
    records.log(TOOL, "open", data)
    owner = _owner(f.folder)
    if owner is not None and owner != s.output_dir:
        audit.AuditLog(owner).append("toolbox", f"toolbox {TOOL} open", {"reviewer": records.reviewer(), **data})
    subject = s.subject_name
    if s.case_bound and not subject:
        from dsar.webapp import cases
        subject = cases.family(s).subject_name
    if "subject" not in request.args and subject:
        return redirect(url_for(".page", fid=fid, subject=subject.split()[0],
                                **{k: v for k, v in request.args.items() if k in ("file", "el")}))
    from flask import session
    head = f'<head><meta name="csrf" content="{html.escape(session.get("csrf", ""))}">'
    bar = (f'<body><div style="padding:6px 16px;font:13px -apple-system,sans-serif;border-bottom:1px solid #8884">'
           f'<a href="{url_for("main.home")}">{html.escape(s.case_ref or "DSAR")}</a> · '
           f'<a href="{url_for(".index")}">Personal Data Reviewer</a> · {html.escape(str(f.folder.name))}'
           f'{(" · <a href=" + chr(34) + url_for("review.report_page") + "#preview" + chr(34) + ">← Report preview</a>") if s.case_bound else ""}'
           f'{" · <b>free-standing: not audit-logged</b>" if not s.case_bound else ""}</div>')
    return markup.APP_PAGE.replace("__REASONS__", json.dumps(markup.REASONS)).replace("<head>", head, 1).replace(
        "<body>", bar, 1)


def _repeats(f: markup.Folder, docs: list[dict]) -> dict:
    """This folder's undecided elements that repeat a decision made in any of the request's folders."""
    others = [(p, docs if p == f.folder else markup.Folder(p, "", None).docs()) for p in folders().values()]
    if all(p != f.folder for p, _ in others):
        others.append((f.folder, docs))
    return markup.repeats_for(docs, markup.repeat_sources(others))


@bp.route("/f/<fid>/api/docs")
def docs(fid):
    f = _folder(fid)
    d = f.docs()
    shown = markup.screen_docs(d)
    for doc, item in zip(shown, _item_scores(f.folder, [x["name"] for x in d])):
        if item:
            doc["item"] = item
    return jsonify({"reviewer": f.reviewer, "docs": shown, "chain_ok": f.events.verify(),
                    "repeats": _repeats(f, d)})


def _item_scores(folder: Path, names: list[str]) -> list[dict | None]:
    """For a review stack: each file's item as classified (model score, reason, evidence), from the case database."""
    import sqlite3

    from dsar import evidence, personal_data_report
    idx = folder / personal_data_report.STACK_INDEX
    db_path = folder.parent.parent / "dsar.sqlite"
    if not idx.is_file() or not db_path.is_file():
        return [None] * len(names)
    index = json.loads(idx.read_text(encoding="utf-8"))
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out = []
        for name in names:
            cid = (index.get(name) or {}).get("chunk_id")
            row = conn.execute("SELECT cl.score, cl.description, cl.evidence, c.content, c.subject FROM chunks c "
                               "LEFT JOIN classifications cl ON cl.chunk_id = c.chunk_id WHERE c.chunk_id = ?",
                               (cid,)).fetchone() if cid else None
            out.append({"chunk_id": cid, "score": row[0], "reason": row[1] or "",
                        "evidence": row[2] or "", "evidence_check": evidence.verify(row[0], row[2], row[3], row[4])}
                       if row else None)
        return out
    finally:
        conn.close()


@bp.route("/f/<fid>/api/apply-repeats", methods=["POST"])
def apply_repeats(fid):
    """Record, for each repeat, the same decision as the element it repeats, under the signed-in reviewer."""
    f = _folder(fid)
    if not f.reviewer:
        return jsonify({"error": "sign in first"}), 400
    d = f.docs()
    shas = {x["name"]: x["sha256"] for x in d}
    body_in = request.get_json(silent=True) or {}
    only = {(o.get("file"), o.get("element")) for o in body_in.get("only") or []} if body_in.get("only") else None
    applied, failed = [], []
    for name, els in _repeats(f, d).items():
        for el_id, info in els.items():
            if only is not None and (name, el_id) not in only:
                continue                                   # confirmed one by one in the Repeats view
            body = {"file": name, "sha256": shas[name], "action": "decide", "element": el_id,
                    "decision": info["decision"], "reason": info.get("reason"), "note": info.get("note"),
                    "redactions": info.get("redactions"), "copied_from": f"{info['from']}#{info['element']}"}
            try:
                f.act(body)
                applied.append({"file": name, "element": el_id, "decision": info["decision"], "copied_from": body["copied_from"]})
            except (KeyError, ValueError) as exc:
                failed.append({"file": name, "element": el_id, "error": str(exc)})
    data = {"folder": str(f.folder), "applied": len(applied), "failed": len(failed), "confirmed_individually": only is not None,
            "decisions": dict(Counter(a["decision"] for a in applied)),
            "events_sha256": audit.sha256_file(f.events.path) if f.events.path.exists() else None}
    records.log(TOOL, "apply-repeats", data)
    s = current_app.config["DSAR"]
    owner = _owner(f.folder)
    if owner is not None and owner != s.output_dir:
        audit.AuditLog(owner).append("toolbox", f"toolbox {TOOL} apply-repeats", {"reviewer": records.reviewer(), **data})
    return jsonify({**data, "errors": failed[:20]})


@bp.route("/f/<fid>/api/act", methods=["POST"])
def act(fid):
    f = _folder(fid)
    try:
        entry = f.act(request.get_json(force=True))
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(entry)


@bp.route("/f/<fid>/api/extracts")
def extracts(fid):
    return jsonify(_folder(fid).export_extracts())


@bp.route("/f/<fid>/export")
def export(fid):
    f = _folder(fid)
    try:
        page_html, _path = f.export(request.args.get("file", ""))
    except (KeyError, StopIteration):
        abort(404)
    return page_html
