"""
Choosing the case the web app works on.

Cases are found under the cases root (default: the folder holding the starting
case, e.g. ~/DSARClassifier): every <CASE-REF>/data with an output/ folder.
Only cases found there can be opened; the browser never sends a path.

Each case's audit log is checked before it is listed:
  ok         hash-chained AUDIT_LOG.jsonl, chain intact
  problems   AUDIT_LOG.jsonl whose chain check fails (opened with a warning;
             the problems are recorded in the case-open entry)
  legacy     only a hand-written AUDIT_LOG.md (cases run before the platform
             logged itself), or no log at all
Opening a legacy case needs the operator to confirm: it starts the case's
hash-chained log, and the first entry says that earlier work is not in it (and
records the SHA-256 of the existing AUDIT_LOG.md, which is appended to, never
rewritten). From then on everything done in the app is logged.

Switching is recorded in both cases' audit logs (`toolbox app case-close` in the
case left, `toolbox app case-open` in the case opened) under the signed-in
reviewer. Open mailboxes are closed, and the CSRF value is rotated so a page
still showing the old case (another tab) can't change anything until reloaded.
A switched-to case uses only its own data/config.yaml: a --config given for the
starting case is never carried into another case.
"""
import json
import secrets
import threading
from pathlib import Path

from flask import current_app, session

from dsar import audit

_switch_lock = threading.Lock()


class CaseError(Exception):
    pass


def discover(root: Path | None) -> list[dict]:
    """Every case data dir under `root`, sorted by case reference."""
    if root is None or not Path(root).is_dir():
        return []
    out = []
    for case_dir in sorted(Path(root).iterdir(), key=lambda p: p.name.casefold()):
        data = case_dir / "data"
        if case_dir.name.startswith((".", "_")) or not (data / "output").is_dir():
            continue
        out.append(describe(data))
    return out


def describe(data_dir: Path) -> dict:
    from dsar import webapp

    s = webapp.Settings.for_case(data_dir)
    log = audit.AuditLog(data_dir / "output")
    entries, last, problems = 0, "", []
    if log.jsonl.is_file():
        entries, _, problems = log.verify()
        lines = log.jsonl.read_text(encoding="utf-8").splitlines()
        try:
            last = json.loads(lines[-1]).get("ts", "") if lines else ""
        except ValueError:
            last = ""
        status = "problems" if problems else "ok"
    else:
        status = "legacy"
    return {"case_ref": s.case_ref, "data_dir": data_dir, "subject": s.subject_name,
            "config": s.config_path is not None, "entries": entries, "last": last,
            "status": status, "problems": problems, "legacy_md": log.md.is_file()}


def switch(case_ref: str, reviewer: str, confirm_legacy: bool = False) -> dict:
    """Make `case_ref` (one found by discover) the app's case; returns its description."""
    from dsar.toolbox.mailbox.store import MailboxStore
    from dsar.webapp import Settings

    app = current_app._get_current_object()
    s = app.config["DSAR"]
    with _switch_lock:
        found = {c["case_ref"]: c for c in discover(s.cases_root)}
        target = found.get(case_ref)
        if target is None:
            raise CaseError(f"{case_ref} is not a case under {s.cases_root}")
        if target["data_dir"].resolve() == s.data_dir.resolve():
            return target
        if target["status"] == "legacy" and not confirm_legacy:
            raise CaseError(f"{case_ref} has no hash-chained audit log. Tick the box to start one, then open it.")
        new = Settings.for_case(target["data_dir"])
        audit.AuditLog(s.output_dir).append("toolbox", "toolbox app case-close",
                                            {"reviewer": reviewer, "to_case": new.case_ref})
        app.extensions["dsar_mailboxes"].close_all()
        app.extensions["dsar_mailboxes"] = MailboxStore(new.data_dir)
        old_ref = s.case_ref
        s.data_dir, s.config_path = new.data_dir, new.config_path
        s.subject_name, s.case_ref = new.subject_name, new.case_ref
        s.markup_folders = []          # --markup-folder belonged to the starting case
        data = {"reviewer": reviewer, "from_case": old_ref, "data_dir": str(s.data_dir),
                "audit_log": target["status"]}
        sha = {}
        if s.config_path:
            sha["config"] = audit.sha256_file(s.config_path)
        if target["status"] == "legacy":
            data["note"] = ("First hash-chained entry for this case, started from the web app with the operator's "
                            "confirmation. Earlier work is not in this log" +
                            ("; it is in the hand-written AUDIT_LOG.md, whose SHA-256 before this entry is recorded."
                             if target["legacy_md"] else "; this case had no audit log."))
            if target["legacy_md"]:
                sha["AUDIT_LOG.md (before)"] = audit.sha256_file(s.output_dir / audit.MD_NAME)
        if target["problems"]:
            data["chain_problems"] = target["problems"][:20]
        if sha:
            data["sha256"] = sha
        audit.AuditLog(s.output_dir).append("toolbox", "toolbox app case-open", data)
        session["csrf_prev"] = session.get("csrf", "")
        session["csrf"] = secrets.token_urlsafe(24)
        return describe(s.data_dir)
