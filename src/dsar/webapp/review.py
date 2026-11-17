"""
Review queue and Personal Data Report pages (case-bound only).

/review/            items scored 7+ (and any already decided), filtered by status
/review/item/<id>   the whole item, evidence highlighted; decide, select passages
/report/            what the Personal Data Report holds now, what's outstanding; build it

They work on the request's review data (webapp.cases.Family): from the main case,
a deeper search's database and folders are used directly, with no switching.
Decisions go to that dsar.sqlite (dsar.disclosure, append-only) and each one to the
audit logs of the current case, the main case and the case holding the data
(`review decide`), under the signed-in reviewer. Building the report is logged by
dsar.personal_data_report (`report personal-data`) in the main case and the data's.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, send_file, url_for

from dsar import audit, db, disclosure, personal_data_report
from dsar.webapp import cases, records

bp = Blueprint("review", __name__)


def _settings():
    s = current_app.config["DSAR"]
    if not s.case_bound:
        abort(404)
    return s


def _family():
    return cases.family(_settings())


@contextmanager
def _conn():
    fam = _family()
    path = fam.review_dir / "dsar.sqlite"
    if not path.is_file():
        # Never create a database in a case that doesn't have one.
        abort(404, description=f"No dsar.sqlite in {fam.review_dir}")
    with db.connect(path) as conn:
        db.init_schema(conn)
        yield conn


def _redaction_folders() -> dict[str, Path]:
    from dsar.toolbox.redaction import web
    return web.folders()


@bp.route("/review/")
def queue():
    fam = _family()
    status = request.args.get("status", disclosure.TO_REVIEW)
    if status not in ("", disclosure.TO_REVIEW, disclosure.NEEDS_PASSAGES, disclosure.DONE):
        status = disclosure.TO_REVIEW
    try:
        min_score = max(0, min(10, int(request.args.get("min", disclosure.MIN_SCORE))))
    except ValueError:
        min_score = disclosure.MIN_SCORE
    q = request.args.get("q", "")
    with _conn() as conn:
        data = disclosure.queue(conn, min_score, status, q)
    return render_template("review.html", data=data, status=status, min_score=min_score, q=q, fam=fam,
                           statuses=[disclosure.TO_REVIEW, disclosure.NEEDS_PASSAGES, disclosure.DONE])


@bp.route("/review/item/<chunk_id>")
def item(chunk_id):
    with _conn() as conn:
        it = disclosure.item(conn, chunk_id)
    if it is None:
        abort(404)
    return render_template("review_item.html", it=it, fam=_family(), decisions=disclosure.DECISIONS, reasons=disclosure.REASONS,
                           assessments=disclosure.ASSESSMENTS, EXTRACT=disclosure.EXTRACT,
                           RELEASE_REDACTED=disclosure.RELEASE_REDACTED, WITHHOLD=disclosure.WITHHOLD)


@bp.route("/review/api/item/<chunk_id>/decide", methods=["POST"])
def decide(chunk_id):
    s = _settings()
    body = request.get_json(silent=True) or {}
    with _conn() as conn:
        try:
            result = disclosure.record(conn, chunk_id, body, records.reviewer())
        except disclosure.DecisionError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        nxt = [r["chunk_id"] for r in disclosure.queue(conn, status=disclosure.TO_REVIEW)["rows"]
               if r["chunk_id"] != chunk_id]
    fam = cases.family(s)
    for out in fam.logs(s.output_dir):
        audit.AuditLog(out).append("review", "review decide",
                                   {"reviewer": records.reviewer(), "request": fam.main_ref, "data_case": fam.review_ref,
                                    **result})
    return jsonify({"success": True, **result,
                    "next": url_for("review.item", chunk_id=nxt[0]) if nxt else url_for("review.queue")})


@bp.route("/report/")
def report_page():
    s = _settings()
    fam = cases.family(s)
    template = personal_data_report.template_path(fam.review_dir, fam.main_dir)
    folders = _redaction_folders()
    with _conn() as conn:
        data = personal_data_report.assemble(conn, folders)
        items = {r["chunk_id"]: r for r in disclosure.queue(conn, min_score=0, status="")["rows"]}
        for cid, dec in disclosure.latest(conn).items():
            if cid in items:
                items[cid]["notes"] = dec.get("notes", "")
    pv = personal_data_report.preview(template, fam.subject_name, data["extracts"]) if template.is_file() else None
    return render_template("report.html", data=data, template=template, template_ok=template.is_file(), fam=fam,
                           todo=_left_to_process(data, items, folders, pv), pv=pv,
                           builds=personal_data_report.builds(fam.main_dir / "output"), error=request.args.get("error", ""))


def _left_to_process(data: dict, items: dict, folders: dict[str, Path], pv: dict | None) -> list[dict]:
    """What is left before the report is final, grouped by what the reviewer has to do."""
    def item(cid, why=""):
        r = items.get(cid, {})
        return {"chunk_id": cid, "title": r.get("title") or cid, "date": (r.get("date") or "")[:10],
                "score": r.get("score"), "from": r.get("from", ""), "why": why, "notes": r.get("notes", "")}

    out = []
    by = lambda prefix: [o for o in data["outstanding"] if o.get("why", "").startswith(prefix)]
    groups = [
        ("Not reviewed yet", "Open each and decide.", [item(o["chunk_id"]) for o in by("not reviewed")]),
        ("Select the passages", "Workbook 'Disclose with redactions': the redactions were only written in notes. "
         "Work through them in the Personal Data Reviewer, or open each in Review.",
         [item(o["chunk_id"]) for o in by("workbook 'Disclose with redactions'")]),
        ("Text changed since the decision", "Decide again on the current text.",
         [item(o["chunk_id"]) for o in by("text changed")]),
    ]
    for title, hint, rows in groups:
        if rows:
            out.append({"title": title, "hint": hint, "kind": "items", "rows": rows, "blocking": True})
    whole = []
    for cid in data["review"]["whole_from_workbook"]:
        text = next((p["text"] for p in data["review"]["pieces"] if p["chunk_id"] == cid), "")
        whole.append({**item(cid), "headers": personal_data_report.has_headers(text), "chars": len(text)})
    if whole:
        whole.sort(key=lambda r: not r["headers"])
        out.append({"title": "Narrow whole passages", "kind": "items", "rows": whole, "blocking": False,
                    "hint": "Workbook 'Disclose' put the whole passage in the report. Narrow it to the subject's data in the "
                            "Personal Data Reviewer (Leave out headers and other people's details). Those with email "
                            "headers name other people."})
    fids = {str(p): fid for fid, p in folders.items()}
    files = []
    for o in data["outstanding"]:
        if "file" in o:
            path = Path(o["file"])
            files.append({"file": path.name, "folder": path.parent.name, "fid": fids.get(str(path.parent)),
                          "count": int(o["why"].split()[0])})
    if files:
        out.append({"title": "Finish in the Personal Data Reviewer", "kind": "files", "rows": files, "blocking": True,
                    "hint": "Decide every element (Extract, Leave out, Leave part out), or leave a whole folder out of "
                            "the report below with a reason."})
    if pv and pv["placeholders"]:
        out.append({"title": "Complete the template", "kind": "text", "rows": pv["placeholders"], "blocking": True,
                    "hint": "Fill these in the case's template (data/templates/) so every build has them, or in the Word "
                            "copy after building."})
    return out


@bp.route("/report/build", methods=["POST"])
def report_build():
    s = _settings()
    with _conn() as conn:
        fam = cases.family(s)
        try:
            result = personal_data_report.build(conn, fam.review_output, fam.main_ref, fam.subject_name,
                                                _redaction_folders(), records.reviewer(),
                                                report_output_dir=fam.main_dir / "output")
        except (FileNotFoundError, ValueError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
    return jsonify({"success": True, **result, "report": Path(result["report"]).name,
                    "register": Path(result["register"]).name})


@bp.route("/report/folder", methods=["POST"])
def report_folder():
    s = _settings()
    path = _redaction_folders().get(request.form.get("folder_id", ""))
    if path is None:
        abort(404)
    with _conn() as conn:
        try:
            result = personal_data_report.choose_folder(conn, path, request.form.get("include") == "1",
                                                        request.form.get("reason", ""), records.reviewer())
        except ValueError as exc:
            return redirect(url_for("review.report_page", error=str(exc)))
    fam = cases.family(s)
    for out in fam.logs(s.output_dir):
        audit.AuditLog(out).append("report", "report folder", {**result, "request": fam.main_ref})
    return redirect(url_for("review.report_page"))


@bp.route("/report/stack", methods=["POST"])
def report_stack():
    """Send outstanding items to a review stack folder and open it in the Personal Data Reviewer."""
    s = _settings()
    fam = cases.family(s)
    wanted = request.form.get("group", "")
    with _conn() as conn:
        data = personal_data_report.assemble(conn, _redaction_folders())
        items = {r["chunk_id"]: r for r in disclosure.queue(conn, min_score=0, status="")["rows"]}
        groups = [g for g in _left_to_process(data, items, _redaction_folders(), None)
                  if g["kind"] == "items" and (wanted == "all" or g["title"] == wanted)]
        ids = [r["chunk_id"] for g in groups for r in g["rows"]]
        if not ids:
            return redirect(url_for("review.report_page", error="Nothing to send to the Personal Data Reviewer."))
        try:
            result = personal_data_report.send_to_reviewer(conn, fam.review_output, ids, records.reviewer())
        except ValueError as exc:
            return redirect(url_for("review.report_page", error=str(exc)))
    entry = {**result, "files": len(result["files"]), "chunk_ids": ids, "groups": [g["title"] for g in groups],
             "request": fam.main_ref}
    for out in fam.logs(s.output_dir):
        audit.AuditLog(out).append("report", "report review-stack", entry)
    from dsar.toolbox.redaction import web
    fid = web._fid(Path(result["folder"]))
    return redirect(url_for("tool_redaction.page", fid=fid))


@bp.route("/report/file/<name>")
def report_file(name):
    s = _settings()
    folder = cases.family(s).main_dir / "output" / personal_data_report.OUT_DIR
    path = folder / name
    if path.parent != folder or not path.is_file() or not name.startswith("Personal_Data_Report_"):
        abort(404)
    records.log("report", "download", {"file": name, "sha256": {name: audit.sha256_file(path)}})
    return send_file(path, as_attachment=True, download_name=name)
