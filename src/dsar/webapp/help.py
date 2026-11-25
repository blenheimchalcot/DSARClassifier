"""
Help pages: the process, step by step, for whoever is at the keyboard.

/help/          the whole process on one page, with a link to each step
/help/<topic>   one step: what it is for, what to do, what to check

Each page of the app links to its own step (help_for). The pages are plain templates in
templates/help/, so they change with the app they describe. They read nothing from the case.
"""
from flask import Blueprint, abort, render_template

from dsar import review
from dsar.toolbox.redaction import markup

bp = Blueprint("help", __name__, url_prefix="/help")

# (key, title, one line for the index): in the order a request goes through.
TOPICS = [
    ("setup", "1. Set up and sign in", "Install or update the app, point it at the case, start it and sign in."),
    ("prepare", "2. Prepare the case", "Hash, read and score the data subject's data, from the terminal."),
    ("review", "3. Review in the Personal Data Reviewer", "Decide, paragraph by paragraph, what is the subject's personal data."),
    ("report", "4. Build the Personal Data Report", "Clear what's left, check the preview and build the final report."),
    ("release", "5. Release the response", "Work through the case page's checklist and record what was sent."),
    ("rules", "Ground rules", "What never to do, and why."),
]
TITLES = {k: t for k, t, _ in TOPICS}

# Which step each page of the app belongs to, by endpoint or blueprint.
_BY_ENDPOINT = {"main.signin": "setup", "main.cases_page": "setup", "main.toolbox_index": "prepare",
                "main.home": "release", "review.report_page": "report", "review.queue": "review",
                "review.item": "review"}
_BY_BLUEPRINT = {"tool_redaction": "review", "tool_mail": "prepare", "tool_teams": "prepare"}


def help_for(endpoint: str | None, blueprint: str | None) -> str:
    """The help topic for a page of the app ('' for the help index itself)."""
    return _BY_ENDPOINT.get(endpoint or "") or _BY_BLUEPRINT.get(blueprint or "") or ""


@bp.route("/")
def index():
    return render_template("help/index.html", topics=TOPICS, current="")


@bp.route("/<topic>")
def topic(topic):
    if topic not in TITLES:
        abort(404)
    keys = [k for k, _, _ in TOPICS]
    i = keys.index(topic)
    return render_template(f"help/{topic}.html", topics=TOPICS, current=topic, title=TITLES[topic],
                           prev=TOPICS[i - 1] if i else None, next=TOPICS[i + 1] if i + 1 < len(TOPICS) else None,
                           min_score=review.MIN_SCORE, reasons=markup.REASONS)
