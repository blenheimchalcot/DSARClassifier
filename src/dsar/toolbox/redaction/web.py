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
        return redirect(url_for(".page", fid=fid, subject=subject.split()[0]))
    from flask import session
    head = f'<head><meta name="csrf" content="{html.escape(session.get("csrf", ""))}">'
    bar = (f'<body><div style="padding:6px 16px;font:13px -apple-system,sans-serif;border-bottom:1px solid #8884">'
           f'<a href="{url_for("main.home")}">{html.escape(s.case_ref or "DSAR")}</a> · '
           f'<a href="{url_for(".index")}">Personal Data Reviewer</a> · {html.escape(str(f.folder.name))}'
           f'{" · <b>free-standing: not audit-logged</b>" if not s.case_bound else ""}</div>')
    return markup.APP_PAGE.replace("__REASONS__", json.dumps(markup.REASONS)).replace("<head>", head, 1).replace(
        "<body>", bar, 1)


@bp.route("/f/<fid>/api/docs")
def docs(fid):
    f = _folder(fid)
    return jsonify({"reviewer": f.reviewer, "docs": f.docs(), "chain_ok": f.events.verify()})


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
