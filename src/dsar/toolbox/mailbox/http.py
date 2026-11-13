"""Flask helpers shared by the mailbox tools (mail viewer, Teams viewer)."""
import io
import mimetypes
import platform
import subprocess
import threading
import uuid

from flask import current_app, jsonify, request, send_file

from dsar.toolbox.mailbox.store import MailboxError, MailboxStore
from dsar.webapp import records


def store() -> MailboxStore:
    return current_app.extensions["dsar_mailboxes"]


def fail(message: str, code: int = 400):
    return jsonify({"success": False, "error": message}), code


def sources_response():
    s = store()
    manifest = s.manifest_path()
    return jsonify({"success": True, "case_bound": s.case_bound,
                    "manifest": manifest.name if manifest else None,
                    "sources": [x.as_dict() for x in s.sources()]})


def add_path_response():
    try:
        src = store().add_path((request.get_json(silent=True) or {}).get("path", ""))
    except MailboxError as exc:
        return fail(str(exc))
    return jsonify({"success": True, "source": src.as_dict()})


def open_source(tool: str, sid: str):
    """Open a mailbox (hash-checked in a case) and log the first open per server run."""
    opened, new = store().open(sid)
    if new:
        records.log(tool, "open", {"mailbox": opened.source.label,
                                   "sha256": {opened.source.path.name: opened.sha256} if opened.sha256 else None})
    return opened


def deliver(tool: str, filename: str, data: bytes, details: dict, mimetype: str | None = None):
    """Case-bound: save read-only into the case and answer with JSON. Free-standing: send as a download."""
    saved = records.save_output(tool, filename, data, details)
    if saved:
        path, digest = saved
        return jsonify({"success": True, "saved": str(path), "sha256": digest})
    return send_file(io.BytesIO(data), as_attachment=True, download_name=records.safe_filename(filename),
                     mimetype=mimetype or mimetypes.guess_type(filename)[0] or "application/octet-stream")


# Native file picker (free-standing mode on macOS only). Runs in a thread and is
# polled, because Safari drops a fetch left idle while the window lacks focus.
_browse_jobs: dict[str, dict] = {}
_browse_lock = threading.Lock()


def browse_start():
    if store().case_bound:
        return fail("in a case, choose from the manifest list")
    if platform.system() != "Darwin":
        return fail("the file picker is only available on macOS; type the path instead")
    job = uuid.uuid4().hex
    with _browse_lock:
        _browse_jobs[job] = {"status": "pending"}

    def run():
        script = ('set f to choose file with prompt "Select a PST or MBOX file" of type {"pst", "mbox"}\n'
                  'POSIX path of f')
        try:
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
            out = ({"status": "done", "success": True, "path": r.stdout.strip()} if r.returncode == 0 else
                   {"status": "done", "success": False, "cancelled": True})
        except Exception as exc:  # noqa: BLE001
            out = {"status": "done", "success": False, "error": str(exc)}
        with _browse_lock:
            _browse_jobs[job] = out

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"success": True, "job_id": job})


def browse_poll(job: str):
    with _browse_lock:
        out = _browse_jobs.get(job)
        if out and out.get("status") == "done":
            del _browse_jobs[job]
    if out is None:
        return fail("unknown job", 404)
    return jsonify(out)
