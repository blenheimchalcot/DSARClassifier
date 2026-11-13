"""Case pages: sign-in, dashboard, toolbox index."""
from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

from dsar.webapp import case, records

bp = Blueprint("main", __name__)


@bp.route("/")
def home():
    s = current_app.config["DSAR"]
    info = case.summary(s.output_dir) if s.case_bound else None
    return render_template("home.html", info=info)


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
