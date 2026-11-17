"""Case pages: sign-in, dashboard, case picker, toolbox index."""
from flask import Blueprint, abort, current_app, redirect, render_template, request, session, url_for

from dsar.webapp import case, cases, records

bp = Blueprint("main", __name__)
REMEMBER_COOKIE = "dsar_reviewer_name"     # not tied to one server, so it survives restarts (cookies ignore the port)
REMEMBER_DAYS = 365


@bp.route("/")
def home():
    s = current_app.config["DSAR"]
    info = rel = fam = None
    if s.case_bound:
        info = case.summary(s.output_dir)
        fam = cases.family(s)
        from dsar.toolbox.redaction import web as redaction_web
        from dsar.webapp import release
        folders = redaction_web.case_folders(fam.review_dir / "output")
        rel = release.status(fam, folders)
    return render_template("home.html", info=info, rel=rel, fam=fam, error=request.args.get("error", ""))


@bp.route("/release/check", methods=["POST"])
def release_check():
    from dsar.webapp import release

    s = current_app.config["DSAR"]
    if not s.case_bound:
        abort(404)
    try:
        release.record(cases.family(s).main_dir / "output", request.form.get("item", ""),
                       request.form.get("done") == "1", request.form.get("value", ""), records.reviewer())
    except ValueError as exc:
        return redirect(url_for("main.home", error=str(exc)) + "#release")
    return redirect(url_for("main.home") + "#release")


@bp.route("/cases/")
def cases_page():
    s = current_app.config["DSAR"]
    rows = cases.discover(s.cases_root) if s.case_bound else []
    return render_template("cases.html", rows=rows, error=request.args.get("error", ""))


@bp.route("/cases/open", methods=["POST"])
def cases_open():
    s = current_app.config["DSAR"]
    if not s.case_bound:
        abort(404)
    try:
        cases.switch(request.form.get("case_ref", ""), records.reviewer(),
                     confirm_legacy=request.form.get("confirm_legacy") == "1")
    except cases.CaseError as exc:
        return redirect(url_for("main.cases_page", error=str(exc)))
    return redirect(url_for("main.home"))


@bp.route("/signin", methods=["GET", "POST"])
def signin():
    error = ""
    nxt = request.values.get("next") or url_for("main.home")
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = url_for("main.home")
    remembered = " ".join(request.cookies.get(REMEMBER_COOKIE, "").split())[:80]
    if request.method == "POST":
        name = " ".join(request.form.get("name", "").split())
        remember = request.form.get("remember") == "1"
        if len(name) < 2:
            error = "Enter your full name. It is recorded with everything you do."
        else:
            session["name"] = name[:80]
            records.log("app", "sign-in", {"remote": request.remote_addr, "remembered_name": remember})
            resp = redirect(nxt)
            # Only pre-fills the sign-in box next time: the reviewer still confirms who they are.
            if remember:
                resp.set_cookie(REMEMBER_COOKIE, session["name"], max_age=REMEMBER_DAYS * 86400,
                                httponly=True, samesite="Strict")
            else:
                resp.delete_cookie(REMEMBER_COOKIE)
            return resp
    return render_template("signin.html", error=error, next=nxt, remembered=remembered)


@bp.route("/signout", methods=["POST"])
def signout():
    records.log("app", "sign-out")
    session.pop("name", None)
    return redirect(url_for("main.signin"))


@bp.route("/toolbox/")
def toolbox_index():
    return render_template("toolbox.html")


@bp.route("/denied")
def denied():
    return render_template("denied.html"), 403
