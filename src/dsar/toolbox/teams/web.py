"""Teams viewer routes, mounted at /toolbox/teams/."""
import re

from flask import Blueprint, jsonify, render_template, request

from dsar.toolbox.mailbox import http
from dsar.toolbox.mailbox.store import MailboxError
from dsar.toolbox.teams import chats, pdf
from dsar.webapp import records

TOOL = "teams"
bp = Blueprint("tool_teams", __name__, template_folder="templates", static_folder="static",
               static_url_path="/static")


@bp.route("/")
def index():
    return render_template("teams/index.html")


@bp.route("/api/sources")
def sources():
    return http.sources_response()


@bp.route("/api/add", methods=["POST"])
def add():
    return http.add_path_response()


@bp.route("/api/browse/start", methods=["POST"])
def browse_start():
    return http.browse_start()


@bp.route("/api/browse/poll/<job>")
def browse_poll(job):
    return http.browse_poll(job)


def _index(opened) -> list[dict]:
    """The Teams index for a mailbox, built once per server run (call with the lock held)."""
    if "teams" not in opened.cache:
        opened.cache["teams"] = chats.build_index(opened.handle)
    return opened.cache["teams"]


@bp.route("/api/open", methods=["POST"])
def open_mailbox():
    sid = (request.get_json(silent=True) or {}).get("id", "")
    try:
        opened = http.open_source(TOOL, sid)
        with opened.lock:
            idx = _index(opened)
            convs, people = chats.conversations(idx), chats.participants(idx)
    except MailboxError as exc:
        return http.fail(str(exc))
    return jsonify({"success": True, "source": opened.source.as_dict(), "sha256": opened.sha256,
                    "messages": len(idx), "conversations": convs, "participants": people})


@bp.route("/api/conversation")
def conversation():
    try:
        opened = http.store().get(request.args.get("m", ""))
        with opened.lock:
            msgs, meta = chats.messages(opened.handle, _index(opened), request.args.get("key", ""))
    except MailboxError as exc:
        return http.fail(str(exc))
    if not msgs:
        return http.fail("no messages in that conversation", 404)
    return jsonify({"success": True, "messages": msgs, "meta": meta})


@bp.route("/api/between")
def between():
    try:
        opened = http.store().get(request.args.get("m", ""))
        with opened.lock:
            key = chats.find_between(_index(opened), request.args.get("a", ""), request.args.get("b", ""))
    except MailboxError as exc:
        return http.fail(str(exc))
    if not key:
        return http.fail("no 1:1 Teams messages between those two people. Pick names from the participant list.", 404)
    return jsonify({"success": True, "key": key})


@bp.route("/api/pdf", methods=["POST"])
def save_pdf():
    """Case-bound: save the transcript read-only into output/toolbox/teams/ and log it. Free: download."""
    try:
        opened = http.store().get(request.values.get("m", ""))
        key = request.values.get("key", "")
        with opened.lock:
            msgs, meta = chats.messages(opened.handle, _index(opened), key)
    except MailboxError as exc:
        return http.fail(str(exc))
    if not msgs:
        return http.fail("no messages in that conversation", 404)
    data = pdf.conversation_pdf(msgs, meta, source=opened.source.label, source_sha256=opened.sha256,
                                reviewer=records.reviewer())
    people = "_and_".join(re.sub(r"[^\w.-]+", "_", p).strip("_") for p in meta["participants"][:4]) or "conversation"
    more = f"_and_{len(meta['participants']) - 4}_more" if len(meta["participants"]) > 4 else ""
    return http.deliver(TOOL, f"Teams_{people}{more}.pdf", data, {
        "mailbox": opened.source.label, "mailbox_sha256": opened.sha256, "conversation": key,
        "participants": meta["participants"], "messages": meta["count"], "first": meta["first"], "last": meta["last"]},
        mimetype="application/pdf")
