"""Mail viewer routes, mounted at /toolbox/mail/."""
import io
import mimetypes
import platform
import shutil
import subprocess
import tempfile
import atexit
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_file

from dsar.toolbox.mailbox import http, reader
from dsar.toolbox.mailbox.store import MailboxError
from dsar.toolbox.mailviewer import export
from dsar.webapp import records

TOOL = "mail"
bp = Blueprint("tool_mail", __name__, template_folder="templates", static_folder="static",
               static_url_path="/static")

# Attachments the browser may show itself. Served with a sandbox CSP, never as HTML.
_VIEWABLE = {"application/pdf", "image/png", "image/jpeg", "image/gif", "text/plain", "text/csv"}


@bp.route("/")
def index():
    return render_template("mailviewer/index.html")


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


@bp.route("/api/open", methods=["POST"])
def open_mailbox():
    sid = (request.get_json(silent=True) or {}).get("id", "")
    try:
        opened = http.open_source(TOOL, sid)
        with opened.lock:
            tree = reader.folder_tree(opened.handle.get_root_folder())
    except MailboxError as exc:
        return http.fail(str(exc))
    return jsonify({"success": True, "tree": tree, "source": opened.source.as_dict(), "sha256": opened.sha256})


def _folder(opened, ix):
    folder = reader.get_folder(opened.handle, ix)
    if folder is None:
        raise MailboxError("folder not found")
    return folder


def _ints(*names):
    try:
        return [int(request.values.get(n, 0)) for n in names]
    except ValueError:
        raise MailboxError("bad number")


@bp.route("/api/messages")
def messages():
    try:
        opened = http.store().get(request.args.get("m", ""))
        page, per_page = _ints("page", "per_page")
        with opened.lock:
            out = reader.list_messages(_folder(opened, request.args.get("f", "")), max(1, page),
                                       max(1, min(per_page or 50, 200)))
    except MailboxError as exc:
        return http.fail(str(exc))
    return jsonify({"success": True, **out})


@bp.route("/api/message")
def message():
    try:
        opened = http.store().get(request.args.get("m", ""))
        (index,) = _ints("i")
        with opened.lock:
            detail = reader.message_detail(_folder(opened, request.args.get("f", "")), index)
            detail["folder_name"] = reader.folder_name_path(opened.handle, request.args.get("f", ""))
    except (MailboxError, IndexError, OSError) as exc:
        return http.fail(f"could not read the message: {exc}")
    return jsonify({"success": True, **detail})


@bp.route("/api/search")
def search():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return http.fail("type at least two characters")
    try:
        opened = http.store().get(request.args.get("m", ""))
        with opened.lock:
            results = reader.search(opened.handle, q)
    except MailboxError as exc:
        return http.fail(str(exc))
    return jsonify({"success": True, "results": results, "limited": len(results) >= 200})


def _attachment(opened):
    index, att = _ints("i", "a")
    with opened.lock:
        return reader.attachment_data(_folder(opened, request.values.get("f", "")), index, att)


@bp.route("/api/attachment/view")
def attachment_view():
    """Show an attachment in the browser if it's a type the browser can show safely."""
    try:
        opened = http.store().get(request.args.get("m", ""))
        name, data = _attachment(opened)
    except (MailboxError, ValueError, IndexError, OSError) as exc:
        return http.fail(f"could not read the attachment: {exc}")
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if mime not in _VIEWABLE:
        return http.fail(f"{name} can't be shown in the browser; save it instead")
    resp = send_file(io.BytesIO(data), mimetype=mime, download_name=records.safe_filename(name))
    resp.headers["Content-Disposition"] = "inline"
    resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src data: 'self'; style-src 'unsafe-inline'"
    return resp


@bp.route("/api/save", methods=["POST"])
def save():
    """Save a message (.eml), an attachment, or the whole mailbox (.xlsx).

    Case-bound: written read-only into output/toolbox/mail/ and logged. Free-standing: downloaded.
    """
    kind = request.values.get("kind", "")
    try:
        opened = http.store().get(request.values.get("m", ""))
        f = request.values.get("f", "")
        details = {"mailbox": opened.source.label, "mailbox_sha256": opened.sha256, "kind": kind}
        if kind == "attachment":
            name, data = _attachment(opened)
            index, att = _ints("i", "a")
            details.update(folder=f, message_index=index, attachment_index=att, attachment=name)
        elif kind == "eml":
            (index,) = _ints("i")
            with opened.lock:
                msg = _folder(opened, f).get_sub_message(index)
                data = reader.message_to_eml(msg)
                subject = reader.safe_str(msg.subject) or "message"
            name = f"{subject[:80]}.eml"
            details.update(folder=f, message_index=index, subject=subject)
        elif kind == "xlsx":
            with opened.lock:
                data, n = export.mailbox_xlsx(opened.handle, opened.source.label, opened.sha256)
            name = f"{Path(opened.source.label).stem}_messages.xlsx"
            details.update(messages=n)
        else:
            return http.fail("unknown kind")
    except (MailboxError, ValueError, IndexError, OSError) as exc:
        return http.fail(str(exc))
    return http.deliver(TOOL, name, data, details)


_open_dir: list[str] = []


@bp.route("/api/attachment/open", methods=["POST"])
def attachment_open():
    """Free-standing on macOS only: open an attachment in its default app from a private temp folder."""
    if http.store().case_bound:
        return http.fail("in a case, save the attachment into the case instead")
    if platform.system() != "Darwin":
        return http.fail("only available on macOS")
    try:
        opened = http.store().get(request.values.get("m", ""))
        name, data = _attachment(opened)
    except (MailboxError, ValueError, IndexError, OSError) as exc:
        return http.fail(str(exc))
    if not _open_dir:
        _open_dir.append(tempfile.mkdtemp(prefix="dsar_mail_"))
        atexit.register(shutil.rmtree, _open_dir[0], True)
    out = Path(tempfile.mkdtemp(dir=_open_dir[0])) / records.safe_filename(name, "attachment")
    out.write_bytes(data)
    try:
        subprocess.run(["open", str(out)], check=True, capture_output=True, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return http.fail(f"no app could open {out.name}: {exc}")
    return jsonify({"success": True, "name": out.name})
