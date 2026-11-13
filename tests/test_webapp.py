"""The local web app: access, sign-in, case-bound vs free-standing behaviour, toolbox routes."""
import hashlib
import json
import re
import stat

import pytest

from dsar import audit, webapp
from dsar.toolbox.mailbox import reader
from tests.pst_fakes import sample_mailbox


@pytest.fixture
def case(tmp_path):
    data = tmp_path / "DSAR-TEST-1" / "data"
    (data / "input" / "export").mkdir(parents=True)
    (data / "output").mkdir()
    pst = data / "input" / "export" / "mailbox.pst"
    pst.write_bytes(b"synthetic pst bytes")
    (data / "input" / "export" / "unlisted.pst").write_bytes(b"not hashed yet")
    digest = hashlib.sha256(pst.read_bytes()).hexdigest()
    (data / "output" / "pst_manifest_2026-01-01.sha256").write_text(f"# test\n{digest}  export/mailbox.pst\n")
    (data / "config.yaml").write_text(
        "subject:\n  name: Sam Sample\n  aliases: [Sam]\n  emails: [sam@example.test]\n"
        "model:\n  default: m\n  max_tokens: 10\n  temperature: 0\n  concurrency: 1\n"
        "chunking:\n  max_chars: 100\n  overlap_chars: 0\n")
    return data


@pytest.fixture(autouse=True)
def fake_open(monkeypatch):
    monkeypatch.setattr(reader, "open_mailbox", lambda path: sample_mailbox())


def client_for(settings, name="Test Reviewer"):
    app = webapp.create_app(settings)
    c = app.test_client()
    r = c.get(f"/?token={settings.token}")
    assert r.status_code == 302
    if name:
        page = c.get("/signin").get_data(as_text=True)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        assert c.post("/signin", data={"name": name, "csrf": csrf}).status_code == 302
        c.csrf = csrf
    return app, c


def post(c, url, json_body=None, data=None):
    if json_body is not None:
        return c.post(url, json=json_body, headers={"X-CSRF": c.csrf})
    return c.post(url, data=data or {}, headers={"X-CSRF": c.csrf})


def test_serve_command_is_audited_in_a_case_and_refuses_a_non_case_dir(case, tmp_path, monkeypatch):
    from click.testing import CliRunner

    from dsar import cli

    class Server:
        def __init__(self, app):
            self.app = app

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(webapp, "make_server", lambda s, port: (Server(webapp.create_app(s)), "http://127.0.0.1:1/?token=x"))
    monkeypatch.setenv("DSAR_DATA_DIR", str(case))
    r = CliRunner().invoke(cli.main, ["serve", "--no-browser"])
    assert r.exit_code == 0, r.output
    assert "case DSAR-TEST-1" in r.output
    serve = [e for e in audit.AuditLog(case / "output").entries() if e["command"] == "serve"]
    assert [e["event"] for e in serve] == ["start", "end"] and serve[1]["data"]["status"] == "ok"
    monkeypatch.setenv("DSAR_DATA_DIR", str(tmp_path / "nowhere"))
    assert CliRunner().invoke(cli.main, ["serve", "--no-browser"]).exit_code != 0
    r = CliRunner().invoke(cli.main, ["serve", "--free", "--no-browser"])
    assert r.exit_code == 0 and "FREE-STANDING" in r.output


def test_access_needs_the_token_then_a_name(case):
    s = webapp.Settings.for_case(case)
    app = webapp.create_app(s)
    c = app.test_client()
    assert c.get("/").status_code == 403
    assert c.get("/?token=wrong").status_code == 403
    assert c.get(f"/?token={s.token}").status_code == 302
    r = c.get("/")
    assert r.status_code == 302 and "/signin" in r.headers["Location"]
    assert c.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_post_without_csrf_is_refused_and_headers_lock_down_the_page(case):
    app, c = client_for(webapp.Settings.for_case(case))
    assert c.post("/toolbox/mail/api/open", json={"id": "x"}).status_code == 403
    r = c.get("/")
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert "img-src 'self' data:" in r.headers["Content-Security-Policy"]
    assert r.headers["Cache-Control"] == "no-store"


def test_case_dashboard_reads_the_audit_log(case):
    with audit.run(case / "output", "ingest", {}) as run:
        run.data["files"] = 3
    s = webapp.Settings.for_case(case)
    assert s.case_ref == "DSAR-TEST-1" and s.subject_name == "Sam Sample"
    app, c = client_for(s)
    page = c.get("/").get_data(as_text=True)
    assert "DSAR-TEST-1" in page and "Sam Sample" in page and "hash chain intact" in page
    assert re.search(r"dsar ingest</td>\s*<td><span class=\"pill ok\">ok", page)
    entries = audit.AuditLog(case / "output").entries()
    assert entries[-1]["command"] == "toolbox app sign-in" and entries[-1]["data"]["reviewer"] == "Test Reviewer"


def test_case_mail_viewer_opens_only_manifest_files_after_a_hash_check(case):
    app, c = client_for(webapp.Settings.for_case(case))
    src = c.get("/toolbox/mail/api/sources").get_json()
    by_label = {x["label"]: x for x in src["sources"]}
    assert by_label["export/mailbox.pst"]["in_manifest"] and not by_label["export/unlisted.pst"]["in_manifest"]
    r = post(c, "/toolbox/mail/api/open", {"id": by_label["export/unlisted.pst"]["id"]}).get_json()
    assert not r["success"] and "manifest" in r["error"]
    assert not post(c, "/toolbox/mail/api/add", {"path": "/tmp/x.pst"}).get_json()["success"]

    sid = by_label["export/mailbox.pst"]["id"]
    r = post(c, "/toolbox/mail/api/open", {"id": sid}).get_json()
    assert r["success"] and r["sha256"] and r["tree"]["children"][0]["children"][0]["name"] == "Inbox"
    opened = [e for e in audit.AuditLog(case / "output").entries() if e["command"] == "toolbox mail open"]
    assert len(opened) == 1 and opened[0]["data"]["mailbox"] == "export/mailbox.pst"

    msgs = c.get(f"/toolbox/mail/api/messages?m={sid}&f=0.0").get_json()
    assert [m["subject"] for m in msgs["messages"]] == ["Quarterly plan", "Re: Quarterly plan"]
    msg = c.get(f"/toolbox/mail/api/message?m={sid}&f=0.0&i=1").get_json()
    assert msg["to"] == "Alex Example"
    assert c.get(f"/toolbox/mail/api/search?m={sid}&q=quarterly").get_json()["results"]

    saved = post(c, "/toolbox/mail/api/save", data={"m": sid, "f": "0.0", "i": 0, "a": 0, "kind": "attachment"}).get_json()
    path = case / "output" / "toolbox" / "mail" / saved["saved"].split("/")[-1]
    assert path.read_bytes() == b"the plan" and not path.stat().st_mode & stat.S_IWUSR
    assert saved["sha256"] == hashlib.sha256(b"the plan").hexdigest()
    export = [e for e in audit.AuditLog(case / "output").entries() if e["command"] == "toolbox mail export"][-1]
    assert export["data"]["reviewer"] == "Test Reviewer" and saved["sha256"] in json.dumps(export["data"])
    assert post(c, "/toolbox/mail/api/save", data={"m": sid, "kind": "xlsx"}).get_json()["success"]
    assert not post(c, "/toolbox/mail/api/attachment/open", data={"m": sid, "f": "0.0", "i": 0, "a": 0}).get_json()["success"]


def test_case_mail_viewer_refuses_a_changed_file(case):
    app, c = client_for(webapp.Settings.for_case(case))
    (case / "input" / "export" / "mailbox.pst").write_bytes(b"tampered")
    sid = next(x["id"] for x in c.get("/toolbox/mail/api/sources").get_json()["sources"] if x["in_manifest"])
    r = post(c, "/toolbox/mail/api/open", {"id": sid}).get_json()
    assert not r["success"] and "no longer matches" in r["error"]


def test_free_mode_downloads_and_logs_nothing(tmp_path):
    pst = tmp_path / "any.pst"
    pst.write_bytes(b"x")
    app, c = client_for(webapp.Settings())
    assert "Free-standing mode" in c.get("/").get_data(as_text=True)
    src = post(c, "/toolbox/mail/api/add", {"path": str(pst)}).get_json()["source"]
    assert post(c, "/toolbox/mail/api/open", {"id": src["id"]}).get_json()["success"]
    r = post(c, "/toolbox/mail/api/save", data={"m": src["id"], "f": "0.0", "i": 0, "kind": "eml"})
    assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
    assert b"Subject: Quarterly plan" in r.data
    assert not (tmp_path / "output").exists()


def test_teams_viewer_lists_conversations_and_saves_a_pdf_to_the_case(case):
    app, c = client_for(webapp.Settings.for_case(case))
    sid = next(x["id"] for x in c.get("/toolbox/teams/api/sources").get_json()["sources"] if x["in_manifest"])
    r = post(c, "/toolbox/teams/api/open", {"id": sid}).get_json()
    assert r["success"] and r["messages"] == 6 and len(r["conversations"]) == 3
    key = c.get(f"/toolbox/teams/api/between?m={sid}&a=Sam Sample&b=Alex Example").get_json()["key"]
    conv = c.get(f"/toolbox/teams/api/conversation?m={sid}&key={key}").get_json()
    assert [m["sender"] for m in conv["messages"]] == ["Sam Sample", "Sam Sample", "Alex Example"]
    saved = post(c, "/toolbox/teams/api/pdf", data={"m": sid, "key": key}).get_json()
    assert saved["success"] and saved["saved"].endswith(".pdf")
    entry = [e for e in audit.AuditLog(case / "output").entries() if e["command"] == "toolbox teams export"][-1]
    assert entry["data"]["participants"] and entry["data"]["messages"] == 3
    assert audit.AuditLog(case / "output").verify()[2] == []


def test_redaction_tool_is_mounted_with_the_signed_in_reviewer(case):
    folder = case / "output" / "deep_dive"
    folder.mkdir()
    (folder / "note.txt").write_text("First paragraph.\n\nSecond paragraph about Sam.\n", encoding="utf-8")
    app, c = client_for(webapp.Settings.for_case(case))
    listing = c.get("/toolbox/redaction/").get_data(as_text=True)
    fid = re.search(r"/toolbox/redaction/f/([0-9a-f]{12})/", listing).group(1)
    page = c.get(f"/toolbox/redaction/f/{fid}/?subject=Sam").get_data(as_text=True)
    assert f'<meta name="csrf" content="{c.csrf}">' in page and 'fetch("api/docs' in page
    docs = c.get(f"/toolbox/redaction/f/{fid}/api/docs").get_json()
    assert docs["reviewer"] == "Test Reviewer"
    doc = docs["docs"][0]
    el = doc["elements"][0]["id"]
    r = c.post(f"/toolbox/redaction/f/{fid}/api/act", headers={"X-Token": c.csrf},
               json={"file": doc["name"], "sha256": doc["sha256"], "action": "decide", "element": el,
                     "decision": "release"})
    assert r.status_code == 200 and r.get_json()["reviewer"] == "Test Reviewer"
    assert c.post(f"/toolbox/redaction/f/{fid}/api/act", json={}).status_code == 403
