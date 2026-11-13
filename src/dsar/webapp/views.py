"""Case pages: sign-in, dashboard, case picker, toolbox index."""
from flask import Blueprint, abort, current_app, redirect, render_template, request, session, url_for

from dsar.webapp import case, cases, records

bp = Blueprint("main", __name__)


@bp.route("/")
def home():
    s = current_app.config["DSAR"]
    info = case.summary(s.output_dir) if s.case_bound else None
    return render_template("home.html", info=info)


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
    if request.method == "POST":
        name = " ".join(request.form.get("name", "").split())
        if len(name) < 2:
            error = "Enter your full name. It is recorded with everything you do."
        else:
            session["name"] = name[:80]
            records.log("app", "sign-in", {"remote": request.remote_addr})
            return redirect(nxt)
    return render_template("signin.html", error=error, next=nxt)


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
