"""
Review queue and Personal Data Report pages (case-bound only).

/review/            items scored 7+ (and any already decided), filtered by status
/review/item/<id>   the whole item, evidence highlighted; decide, select passages
/report/            what the Personal Data Report holds now, what's outstanding; build it

Decisions go to dsar.sqlite (dsar.disclosure, append-only) and each one to the
case audit log (`review decide`) under the signed-in reviewer. Building the
report is logged by dsar.personal_data_report (`report personal-data`).
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_file, url_for

from dsar import audit, db, disclosure, personal_data_report
from dsar.webapp import records

bp = Blueprint("review", __name__)


def _settings():
    s = current_app.config["DSAR"]
    if not s.case_bound:
        abort(404)
    return s


@contextmanager
def _conn():
    s = _settings()
    path = s.data_dir / "dsar.sqlite"
    if not path.is_file():
        # Never create a database in a case that doesn't have one.
        abort(404, description=f"No dsar.sqlite in {s.data_dir}")
    with db.connect(path) as conn:
        db.init_schema(conn)
        yield conn


def _redaction_folders() -> dict[str, Path]:
    from dsar.toolbox.redaction import web
    return web.folders()


@bp.route("/review/")
def queue():
    _settings()
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
    return render_template("review.html", data=data, status=status, min_score=min_score, q=q,
                           statuses=[disclosure.TO_REVIEW, disclosure.NEEDS_PASSAGES, disclosure.DONE])


@bp.route("/review/item/<chunk_id>")
def item(chunk_id):
    with _conn() as conn:
        it = disclosure.item(conn, chunk_id)
    if it is None:
        abort(404)
    return render_template("review_item.html", it=it, decisions=disclosure.DECISIONS, reasons=disclosure.REASONS,
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
    audit.AuditLog(s.output_dir).append("review", "review decide", {"reviewer": records.reviewer(), **result})
    return jsonify({"success": True, **result,
                    "next": url_for("review.item", chunk_id=nxt[0]) if nxt else url_for("review.queue")})


@bp.route("/report/")
def report_page():
    s = _settings()
    template = personal_data_report.template_path()
    with _conn() as conn:
        data = personal_data_report.assemble(conn, _redaction_folders())
    return render_template("report.html", data=data, template=template, template_ok=template.is_file(),
                           builds=personal_data_report.builds(s.output_dir))


@bp.route("/report/build", methods=["POST"])
def report_build():
    s = _settings()
    with _conn() as conn:
        try:
            result = personal_data_report.build(conn, s.output_dir, s.case_ref, s.subject_name,
                                                _redaction_folders(), records.reviewer())
        except (FileNotFoundError, ValueError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
    return jsonify({"success": True, **result, "report": Path(result["report"]).name,
                    "register": Path(result["register"]).name})


@bp.route("/report/file/<name>")
def report_file(name):
    s = _settings()
    folder = s.output_dir / personal_data_report.OUT_DIR
    path = folder / name
    if path.parent != folder or not path.is_file() or not name.startswith("Personal_Data_Report_"):
        abort(404)
    records.log("report", "download", {"file": name, "sha256": {name: audit.sha256_file(path)}})
    return send_file(path, as_attachment=True, download_name=name)
