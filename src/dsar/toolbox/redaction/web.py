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


def friendly(path: Path) -> str:
    """A plain name for a review folder: what it holds and when it was made."""
    import re
    from datetime import datetime
    name = path.name
    m = re.search(r"(\d{4})(\d{2})(\d{2})T\d{6}Z", name) or re.search(r"(\d{4})-(\d{2})-(\d{2})", name)
    when = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%-d %b") if m else ""
    s = re.search(r"_score(\d+)$", name)
    if s:
        what = f"Items scored {s.group(1)}"
    elif name.startswith("review_stack_"):
        what = "Workbook items"
    elif name.startswith("deep_dive"):
        what = "Deep-dive documents"
    elif name.startswith("close_review"):
        what = "Copy of the Call AI interview"
    else:
        what = name.replace("_", " ")
    return f"{what} ({when})" if when else what


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
    skip = _left_out()
    loaded = {fid: (path, markup.Folder(path, "", None).docs()) for fid, path in folders().items()}
    sources = markup.repeat_sources([v for v in loaded.values() if str(v[0]) not in skip])
    for fid, (path, docs) in loaded.items():
        f = markup.Folder(path, "", None)
        rows.append({"id": fid, "label": _label(path), "name": friendly(path), "files": len(docs), "left_out": str(path) in skip,
                     "events": sum(1 for _ in f.events.lines()) if f.events.path.exists() else 0,
                     "sections": markup.section_counts(docs, markup.repeats_for(docs, sources))})
    live = [r for r in rows if not r["left_out"]]
    n_conf = len(markup.conflicts([(fid, docs) for fid, (path, docs) in loaded.items() if str(path) not in skip]))
    total = {k: sum(r["sections"][k] for r in live) for k in ("review", "low", "repeats", "done")} if live else None
    return render_template("redaction/index.html", rows=rows, total=total, n_conf=n_conf, intake=_intake_counts(),
                           min_score=markup.REVIEW_MIN_SCORE, error=request.args.get("error", ""))


def _intake_counts() -> dict:
    """Unreviewed classified items by score, from the request's review database (read only)."""
    s = current_app.config["DSAR"]
    if not s.case_bound:
        return {}
    import sqlite3

    from dsar import disclosure
    from dsar.webapp import cases
    db_path = cases.family(s).review_dir / "dsar.sqlite"
    if not db_path.is_file():
        return {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return {k: len(v) for k, v in disclosure.intake(conn).items()}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


@bp.route("/all/")
def all_review():
    """The consolidated review: every section across all of the request's folders."""
    s = current_app.config["DSAR"]
    skip = _left_out()
    live = [fid for fid, path in folders().items() if str(path) not in skip]
    if not s.case_bound or not live:
        return redirect(url_for(".index"))
    return redirect(url_for(".page", fid=live[-1], section=request.args.get("section", "review"), all=1))


@bp.route("/review-due", methods=["POST"])
def review_due():
    """Go to review: bring every unreviewed item at or above the review line into the reviewer
    (logged as a review intake), then open the Personal Data Reviewer."""
    from dsar import db, disclosure, personal_data_report
    from dsar.webapp import cases
    s = current_app.config["DSAR"]
    if not s.case_bound:
        return redirect(url_for(".all_review"))
    fam = cases.family(s)
    with db.connect(fam.review_dir / "dsar.sqlite") as conn:
        db.init_schema(conn)
        due = {k: v for k, v in disclosure.intake(conn).items() if k >= markup.REVIEW_MIN_SCORE}
        for score, ids in due.items():
            result = personal_data_report.send_to_reviewer(conn, fam.review_output, ids, records.reviewer(),
                                                           label=f"score{score}")
            entry = {**result, "files": len(result["files"]), "chunk_ids": ids, "intake_score": score,
                     "request": fam.main_ref, "via": "Go to review"}
            for out in fam.logs(s.output_dir):
                audit.AuditLog(out).append("review", "review intake", entry)
    return redirect(url_for(".all_review", section="review"))


@bp.route("/intake", methods=["POST"])
def intake():
    """Bring unreviewed classified items into review as a new folder (one file per item, exact text)."""
    from dsar import db, disclosure, personal_data_report
    from dsar.webapp import cases
    s = current_app.config["DSAR"]
    if not s.case_bound:
        abort(404)
    fam = cases.family(s)
    try:
        score = int(request.form.get("score", ""))
    except ValueError:
        abort(400)
    with db.connect(fam.review_dir / "dsar.sqlite") as conn:
        db.init_schema(conn)
        ids = disclosure.intake(conn).get(score, [])
        if not ids:
            return redirect(url_for(".index", error=f"No unreviewed items scored {score}."))
        result = personal_data_report.send_to_reviewer(conn, fam.review_output, ids, records.reviewer(),
                                                       min_score=0 if score < markup.REVIEW_MIN_SCORE else None,
                                                       label=f"score{score}")
    entry = {**result, "files": len(result["files"]), "chunk_ids": ids, "intake_score": score, "request": fam.main_ref}
    for out in fam.logs(s.output_dir):
        audit.AuditLog(out).append("review", "review intake", entry)
    return redirect(url_for(".page", fid=_fid(Path(result["folder"])), section="review"))


@bp.route("/f/<fid>/api/decide-file", methods=["POST"])
def decide_file(fid):
    """Decide every undecided element of one file at once: the whole item extracted, or left out with a reason."""
    f = _folder(fid)
    body = request.get_json(silent=True) or {}
    doc = next((d for d in f.docs() if d["name"] == body.get("file")), None)
    if doc is None:
        abort(404)
    decision = body.get("decision")
    if decision not in ("release", "redact_full"):
        return jsonify({"error": "unknown decision"}), 400
    done = 0
    for el in doc["elements"]:
        if el["id"] in doc["decisions"] or el.get("blank"):
            continue
        try:
            f.act({"file": doc["name"], "sha256": doc["sha256"], "action": "decide", "element": el["id"],
                   "decision": decision, "reason": body.get("reason"), "note": "Whole item decided at once"})
            done += 1
        except ValueError as exc:
            return jsonify({"error": str(exc), "decided": done}), 400
    data = {"folder": str(f.folder), "file": doc["name"], "file_sha256": doc["sha256"], "decision": decision,
            "reason": body.get("reason"), "elements": done}
    records.log(TOOL, "decide-file", data)
    owner = _owner(f.folder)
    s = current_app.config["DSAR"]
    if owner is not None and owner != s.output_dir:
        audit.AuditLog(owner).append("toolbox", f"toolbox {TOOL} decide-file", {"reviewer": records.reviewer(), **data})
    return jsonify({"decided": done})


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
                                **{k: v for k, v in request.args.items() if k in ("file", "el", "section", "all")}))
    from flask import session
    request_ref = s.case_ref or "DSAR"
    if s.case_bound:
        from dsar.webapp import cases
        request_ref = cases.family(s).main_ref
    head = f'<head><meta name="csrf" content="{html.escape(session.get("csrf", ""))}">'
    bar = (f'<body><div style="padding:6px 16px;font:13px -apple-system,sans-serif;border-bottom:1px solid #8884">'
           f'<a href="{url_for("main.home")}">{html.escape(s.case_ref or "DSAR")}</a> · '
           f'<a href="{url_for(".all_review")}">Personal Data Reviewer</a> · <b>all of {html.escape(request_ref)}</b> · <a href="{url_for(".index")}">Folders and intake</a>'
           f'{(" · <a href=" + chr(34) + url_for("review.report_page") + "#preview" + chr(34) + ">← Report preview</a>") if s.case_bound else ""}'
           f'{" · <b>free-standing: not audit-logged</b>" if not s.case_bound else ""}</div>')
    names: list[str] = [subject] if subject else []
    if s.config_path:
        from dsar.config import load_config
        try:
            p = load_config(s.config_path).subject
            names += [p.name, *p.aliases, *p.emails]
        except Exception:                                       # noqa: BLE001 - names only highlight; never block the page
            pass
    return markup.app_page(names).replace("<head>", head, 1).replace("<body>", bar, 1)


def _repeats(f: markup.Folder, docs: list[dict]) -> dict:
    """This folder's undecided elements that repeat a decision made in any of the request's folders."""
    others = [(p, docs if p == f.folder else markup.Folder(p, "", None).docs()) for p in folders().values()]
    if all(p != f.folder for p, _ in others):
        others.append((f.folder, docs))
    return markup.repeats_for(docs, markup.repeat_sources(others))


@bp.route("/f/<fid>/api/docs")
def docs(fid):
    f = _folder(fid)
    if request.args.get("all") and current_app.config["DSAR"].case_bound:
        return jsonify(_all_folders(f))
    d = f.docs()
    shown = markup.screen_docs(d)
    for doc, item in zip(shown, _item_scores(f.folder, [x["name"] for x in d])):
        if item:
            doc["item"] = item
    return jsonify({"reviewer": f.reviewer, "docs": shown, "chain_ok": f.events.verify(),
                    "repeats": _repeats(f, d)})


def _left_out() -> set[str]:
    """Folders left out of the Personal Data Report (Report page): not part of the review."""
    s = current_app.config["DSAR"]
    if not s.case_bound:
        return set()
    import sqlite3

    from dsar import personal_data_report
    from dsar.webapp import cases
    db_path = cases.family(s).review_dir / "dsar.sqlite"
    if not db_path.is_file():
        return set()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return {k for k, v in personal_data_report.folder_choices(conn).items() if not v["included"]}
    except sqlite3.OperationalError:                       # no choices table yet
        return set()
    finally:
        conn.close()


def _all_folders(f: markup.Folder) -> dict:
    """Every Personal Data Reviewer folder of the request (except those left out of the report)."""
    skip = _left_out()
    loaded = [(fid, path, markup.Folder(path, records.reviewer(), _owner(path)).docs()) for fid, path in folders().items()
              if str(path) not in skip or path == f.folder]
    sources = markup.repeat_sources([(path, docs) for _, path, docs in loaded])
    out = []
    for fid, path, docs in loaded:
        shown = markup.screen_docs(docs)
        for doc, item in zip(shown, _item_scores(path, [x["name"] for x in docs])):
            if item:
                doc["item"] = item
        out.append({"fid": fid, "label": _label(path), "name": friendly(path), "docs": shown, "repeats": markup.repeats_for(docs, sources),
                    "chain_ok": markup.Folder(path, "", None).events.verify()})
    return {"reviewer": f.reviewer, "folders": out, "chain_ok": all(x["chain_ok"] for x in out),
            "conflicts": markup.conflicts([(fid, docs) for fid, _, docs in loaded])}


@bp.route("/f/<fid>/api/context")
def context(fid):
    """The element's whole file, and (when the file comes from the case database) its whole item and conversation."""
    import sqlite3

    from dsar import disclosure, personal_data_report
    f = _folder(fid)
    name = request.args.get("file", "")
    doc = next((d for d in f.docs() if d["name"] == name), None)
    if doc is None:
        abort(404)
    el = next((e for e in doc["elements"] if e["id"] == request.args.get("el")), None)
    out = {"file": name, "text": markup.screen_text(doc["text"]), "parts": el["parts"] if el else [], "db": None}
    s = current_app.config["DSAR"]
    if s.case_bound:
        from dsar.webapp import cases
        db_path = cases.family(s).review_dir / "dsar.sqlite"
        if db_path.is_file():
            idx = f.folder / personal_data_report.STACK_INDEX
            cid = (json.loads(idx.read_text(encoding="utf-8")).get(name) or {}).get("chunk_id") if idx.is_file() else None
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                if not cid and el:
                    cid = disclosure.find_chunk(conn, markup.element_text(doc["text"], el), doc["text"])
                out["db"] = disclosure.context(conn, cid) if cid else None
                out["matched"] = "stack index" if idx.is_file() else "text match"
            finally:
                conn.close()
    return jsonify(out)


def _item_scores(folder: Path, names: list[str]) -> list[dict | None]:
    """For a review stack: each file's item as classified (model score, reason, evidence), from the case database."""
    import sqlite3

    from dsar import evidence, personal_data_report, sources
    idx = folder / personal_data_report.STACK_INDEX
    db_path = folder.parent.parent / "dsar.sqlite"
    if not idx.is_file() or not db_path.is_file():
        return [None] * len(names)
    index = json.loads(idx.read_text(encoding="utf-8"))
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        origin = sources.for_chunks(conn, [v.get("chunk_id") for v in index.values() if v.get("chunk_id")])
        out = []
        for name in names:
            cid = (index.get(name) or {}).get("chunk_id")
            row = conn.execute("SELECT cl.score, cl.description, cl.evidence, c.content, c.subject FROM chunks c "
                               "LEFT JOIN classifications cl ON cl.chunk_id = c.chunk_id WHERE c.chunk_id = ?",
                               (cid,)).fetchone() if cid else None
            out.append({"chunk_id": cid, "score": row[0], "reason": row[1] or "",
                        "evidence": row[2] or "", "evidence_check": evidence.verify(row[0], row[2], row[3], row[4]),
                        **origin.get(cid, {})}
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


@bp.route("/f/<fid>/api/suggest", methods=["POST"])
def suggest(fid):
    """Live score for one paragraph from the local model (Ollama on this machine), logged in the case audit."""
    from dsar import classify
    from dsar.config import load_config
    from dsar.webapp import cases

    f = _folder(fid)
    s = current_app.config["DSAR"]
    if not f.reviewer:
        return jsonify({"error": "Sign in first: the score is recorded with your name."}), 403
    if not s.config_path or not s.data_dir:
        return jsonify({"error": "Live scoring needs a case (its config and prompt)."}), 400
    cfg = load_config(s.config_path)
    prompt_path = Path(s.data_dir) / "claude_strict.md"
    body = request.get_json(force=True)
    try:
        row = markup.score_one(f.folder, body["file"], body["element"], system_prompt=classify.load_system_prompt(prompt_path),
                               model=cfg.model.default, subject_name=cfg.subject.name, max_tokens=cfg.model.max_tokens,
                               temperature=cfg.model.temperature, reviewer=f.reviewer,
                               expected_digest=cfg.model.expected_digest)
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc).strip("'\"")}), 400
    except Exception as exc:                                   # noqa: BLE001 - shown to the reviewer, nothing recorded
        return jsonify({"error": f"The local model didn't answer: {exc}"}), 502
    entry = {"folder": str(f.folder), "file": row["file"], "sha256": row["sha256"], "span": row["span"],
             "score": row["score"], "evidence_check": row["evidence_check"], "model": row["model"],
             "model_digest": row["model_digest"], "ollama_url": row["ollama_url"],
             "prompt": str(prompt_path), "prompt_sha256": row["prompt_sha256"], "reviewer": f.reviewer}
    for out in (cases.family(s).logs(s.output_dir) if s.case_bound else []):
        audit.AuditLog(out).append("review", "live score", entry)
    return jsonify(row)


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
