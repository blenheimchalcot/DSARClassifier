"""Review decisions in the web app, and the Personal Data Report built from them (synthetic data only)."""
import csv
import re
import stat
from pathlib import Path

import docx
import pytest

from dsar import audit, db, disclosure, personal_data_report, webapp
from dsar.chunking import Chunk
from tests.test_webapp import client_for

TEXT_A = ("Hello team,\n\nSam Sample's new address is 1 Example Road.\n\n"
          "Alex Other said the rota is fine.\n\nThanks")
TEXT_B = "Minutes: Sam Sample raised a grievance about the rota."
TEXT_C = "Sam Sample was copied on the newsletter."


def _chunk(cid, content, score, subject, sent):
    return Chunk(chunk_id=cid, source_type="email", source_path=f"/case/input/{cid}.pst", item_ref=f"msg-{cid}",
                 content=content, subject=subject, metadata={"sent": sent, "sender": "someone@example.test"},
                 alias_matched=True)


@pytest.fixture
def case(tmp_path):
    data = tmp_path / "DSAR-TEST-1" / "data"
    (data / "output").mkdir(parents=True)
    (data / "config.yaml").write_text(
        "subject:\n  name: Sam Sample\n  aliases: [Sam]\n  emails: [sam@example.test]\n"
        "model:\n  default: m\n  max_tokens: 10\n  temperature: 0\n  concurrency: 1\n"
        "chunking:\n  max_chars: 100\n  overlap_chars: 0\n")
    with db.connect(data / "dsar.sqlite") as conn:
        db.init_schema(conn)
        for cid, content, score, subject, sent in (("a", TEXT_A, 7, "Address change", "2026-03-02T09:00:00"),
                                                   ("b", TEXT_B, 9, "Minutes", "2026-01-15T09:00:00"),
                                                   ("c", TEXT_C, 2, "Newsletter", "2026-02-01T09:00:00"),
                                                   ("d", TEXT_B + " Again.", 8, "Old workbook", "2026-04-01")):
            db.upsert_chunk(conn, _chunk(cid, content, score, subject, sent))
            db.insert_classification(conn, chunk_id=cid, score=score, description="test", evidence="Sam Sample",
                                     special_return=None, raw_response="{}", model="m", input_tokens=None,
                                     output_tokens=None, cache_read_tokens=None)
    return data


@pytest.fixture
def template(tmp_path, monkeypatch):
    d = tmp_path / "Templates"
    d.mkdir()
    doc = docx.Document()
    doc.add_paragraph("Personal Data Report ")
    doc.add_paragraph("Enclosed with this letter is a copy of the personal data held by us.")
    doc.add_paragraph("INSERT DATA REPORT: for example, signature.")
    doc.add_paragraph("SAMPLE TABLE")
    t = doc.add_table(rows=4, cols=2)
    for row, (k, v) in zip(t.rows, [("Details of Personal Data", "Extracted Data"), ("Data Subject: ", "John Smith"),
                                    ("Full Name:", "Mr John Smith"), ("Phone number: ", "07700000000")]):
        row.cells[0].text, row.cells[1].text = k, v
    doc.save(d / personal_data_report.TEMPLATE_NAME)
    monkeypatch.setenv("DSAR_TEMPLATES_DIR", str(d))
    return d / personal_data_report.TEMPLATE_NAME


def _decide(c, cid, body):
    with_sha = {"content_sha256": disclosure.content_sha256({"a": TEXT_A, "b": TEXT_B, "c": TEXT_C,
                                                            "d": TEXT_B + " Again."}[cid]), **body}
    return c.post(f"/review/api/item/{cid}/decide", json=with_sha, headers={"X-CSRF": c.csrf})


def test_queue_lists_7_plus_and_decided_items(case):
    app, c = client_for(webapp.Settings.for_case(case))
    page = c.get("/review/").get_data(as_text=True)
    assert "Address change" in page and "Minutes" in page and "Newsletter" not in page
    assert "To review (3)" in page
    page = c.get("/review/item/a").get_data(as_text=True)
    assert "Address change" in page and "Extract selection" in page


def test_decisions_are_validated_stored_append_only_and_audited(case):
    app, c = client_for(webapp.Settings.for_case(case))
    r = _decide(c, "a", {"decision": disclosure.EXTRACT, "spans": []})
    assert r.status_code == 400 and "select at least one passage" in r.get_json()["error"]
    r = _decide(c, "a", {"decision": disclosure.WITHHOLD})
    assert r.status_code == 400 and "exemption" in r.get_json()["error"]
    r = _decide(c, "a", {"decision": disclosure.RELEASE_REDACTED, "spans": [{"kind": "redact", "start": 0, "end": 5}]})
    assert r.status_code == 400 and "reason" in r.get_json()["error"]
    r = c.post("/review/api/item/a/decide", json={"decision": disclosure.RELEASE, "content_sha256": "0" * 64},
               headers={"X-CSRF": c.csrf})
    assert r.status_code == 400 and "changed" in r.get_json()["error"]

    a = TEXT_A.index("Sam Sample's"), TEXT_A.index("Road.") + 5
    r = _decide(c, "a", {"decision": disclosure.EXTRACT, "spans": [{"kind": "extract", "start": a[0], "end": a[1]}]})
    assert r.status_code == 200 and r.get_json()["success"]
    r = _decide(c, "a", {"decision": disclosure.NOT_SUBJECT})          # a change of mind is a new row
    assert r.status_code == 200
    with db.connect(case / "dsar.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM decisions WHERE chunk_id='a'").fetchone()[0] == 2
        assert disclosure.latest(conn)["a"]["decision"] == disclosure.NOT_SUBJECT
        assert conn.execute("SELECT score FROM classifications WHERE chunk_id='a'").fetchone()[0] == 7
    entries = [e for e in audit.AuditLog(case / "output").entries() if e["command"] == "review decide"]
    assert len(entries) == 2 and entries[0]["data"]["reviewer"] == "Test Reviewer"
    assert entries[0]["data"]["content_sha256"] == disclosure.content_sha256(TEXT_A)


def test_report_merges_review_workbook_and_redaction_extracts_into_the_template(case, template):
    from dsar.toolbox.redaction import markup

    app, c = client_for(webapp.Settings.for_case(case))
    a = TEXT_A.index("Sam Sample's"), TEXT_A.index("Road.") + 5
    assert _decide(c, "a", {"decision": disclosure.EXTRACT,
                            "spans": [{"kind": "extract", "start": a[0], "end": a[1]}]}).status_code == 200
    assert _decide(c, "b", {"decision": disclosure.EXTRACT, "spans": [{"kind": "extract", "start": 0,
                                                                        "end": len(TEXT_B)}]}).status_code == 200
    # A workbook decision: "Disclose with redactions" can't be applied from notes, so it stays outstanding.
    with db.connect(case / "dsar.sqlite") as conn:
        conn.execute("INSERT INTO review_decisions (chunk_id, decision, reason, reviewer, workbook, workbook_sha256, "
                     "imported_at) VALUES ('d', 'Disclose with redactions', 'Other (explain in notes)', 'R', "
                     "'wb.xlsx', 'x', '2026-11-01T00:00:00+00:00')")
    # A redaction-tool folder whose passage repeats a review extract (merged) and adds one more.
    folder = case / "output" / "deep_dive"
    folder.mkdir()
    (folder / "01_2026-02-10_note.txt").write_text(TEXT_B + "\n\nSam Sample starts on Monday.\n")
    f = markup.Folder(folder, "Test Reviewer", None)
    doc = f.docs()[0]
    for el in doc["elements"]:
        f.act({"file": doc["name"], "sha256": doc["sha256"], "action": "decide", "element": el["id"],
               "decision": "release"})

    page = c.get("/report/").get_data(as_text=True)
    assert "Left to process" in page and "Select the passages" in page and 'id="preview"' in page
    assert "Data Subject: </td>" in page or "Data Subject:" in page       # preview mirrors the template table
    r = c.post("/report/build", headers={"X-CSRF": c.csrf})
    j = r.get_json()
    assert j["success"] and j["draft"] and j["extracts"] == 3 and j["outstanding"] == 1
    out = case / "output" / personal_data_report.OUT_DIR
    report_path = out / j["report"]
    assert "_DRAFT" in report_path.name and not report_path.stat().st_mode & stat.S_IWUSR
    d = docx.Document(str(report_path))
    body = "\n".join(p.text for p in d.paragraphs)
    assert body.startswith("DRAFT") and "INSERT DATA REPORT" not in body and "SAMPLE TABLE" not in body
    rows = [[cell.text for cell in row.cells] for row in d.tables[0].rows]
    assert rows[1] == ["Data Subject: ", "Sam Sample"]
    # One unnumbered block: every extract, oldest first, a blank line between.
    assert [r[0] for r in rows[2:]] == ["Extracted data:"]
    assert rows[2][1] == "\n\n".join([TEXT_B, "Sam Sample starts on Monday.", "Sam Sample's new address is 1 Example Road."])
    # Oldest first, text only: no sender, subject or date in the report.
    assert "someone@example.test" not in body + str(rows) and "John Smith" not in str(rows)
    with (out / j["register"]).open() as fh:
        reg = list(csv.DictReader(fh))
    assert {r["origin"] for r in reg if r["extract_no"] == "1"} == {"review", "redaction"}
    entry = audit.AuditLog(case / "output").entries()[-1]
    assert entry["command"] == "report personal-data" and entry["data"]["sha256"][report_path.name] == \
        audit.sha256_file(report_path) and "template" in entry["data"]["sha256"]


def test_workbook_disclose_puts_nothing_in_and_changed_text_is_outstanding(case, template):
    with db.connect(case / "dsar.sqlite") as conn:
        conn.execute("INSERT INTO review_decisions (chunk_id, decision, reviewer, workbook, workbook_sha256, "
                     "imported_at) VALUES ('b', 'Disclose', 'R', 'wb.xlsx', 'x', '2026-11-01T00:00:00+00:00')")
        disclosure.record(conn, "a", {"decision": disclosure.EXTRACT, "content_sha256": disclosure.content_sha256(TEXT_A),
                                      "spans": [{"kind": "extract", "start": 0, "end": 11}]}, "R")
        conn.execute("UPDATE chunks SET content = 'changed' WHERE chunk_id = 'a'")
        data = disclosure.report_inputs(conn)
    # A workbook "Disclose" never puts the whole passage in: it waits for passages to be chosen.
    assert data["whole_from_workbook"] == ["b"] and data["pieces"] == []
    assert {"chunk_id": "b", "why": "workbook 'Disclose': choose the passages", "score": 9} in data["outstanding"]
    assert [o["chunk_id"] for o in data["changed"]] == ["a"]


def test_review_pages_need_a_case_and_a_database(case, tmp_path):
    app, c = client_for(webapp.Settings())
    assert c.get("/review/").status_code == 404
    empty = tmp_path / "DSAR-EMPTY" / "data"
    (empty / "output").mkdir(parents=True)
    app, c = client_for(webapp.Settings.for_case(empty))
    assert c.get("/review/").status_code == 404 and not (empty / "dsar.sqlite").exists()


def test_a_case_tailored_template_keeps_its_rows_and_flags_placeholders(case, template, monkeypatch):
    own = case / "templates"
    own.mkdir()
    doc = docx.Document()
    doc.add_paragraph("Personal Data Report")
    doc.add_paragraph("INSERT DATA REPORT: list each item once.")
    t = doc.add_table(rows=5, cols=2)
    for row, (k, v) in zip(t.rows, [("Personal data", "Data held"), ("Data subject:", "[Insert]"),
                                    ("Email address:", "sam@example.test"),
                                    ("Internal emails that mention you:", "[Insert extracts]"),
                                    ("Recruitment decision:", "[Insert]")]):
        row.cells[0].text, row.cells[1].text = k, v
    doc.save(own / personal_data_report.TEMPLATE_NAME)
    with db.connect(case / "dsar.sqlite") as conn:
        for cid, text in (("a", TEXT_A), ("b", TEXT_B)):
            disclosure.record(conn, cid, {"decision": disclosure.EXTRACT, "content_sha256": disclosure.content_sha256(text),
                                          "spans": [{"kind": "extract", "start": 0, "end": len(text)}]}, "R")
        assert personal_data_report.template_path(case) == own / personal_data_report.TEMPLATE_NAME
        result = personal_data_report.build(conn, case / "output", "DSAR-TEST-1", "Sam Sample", {}, "R")
    assert result["draft"] and result["outstanding"] == 1 and result["placeholders_left"] == ["[Insert]"]  # d (score 8) undecided
    out = docx.Document(result["report"])
    rows = [[c.text for c in r.cells] for r in out.tables[0].rows]
    assert rows[:3] == [["Personal data", "Data held"], ["Data subject:", "Sam Sample"], ["Email address:", "sam@example.test"]]
    assert rows[3][0] == "Internal emails that mention you:"
    block = rows[3][1]
    assert "Extract" not in block and TEXT_B in block and "Sam Sample's new address" in block   # flat, unnumbered
    assert len(rows) == 5 and rows[4] == ["Recruitment decision:", "[Insert]"]
    assert "placeholder" in out.paragraphs[0].text and "INSERT DATA REPORT" not in "\n".join(p.text for p in out.paragraphs)


def test_cli_builds_the_report(case, template, monkeypatch):
    from click.testing import CliRunner

    from dsar import cli

    monkeypatch.setenv("DSAR_DATA_DIR", str(case))
    r = CliRunner().invoke(cli.main, ["personal-data-report", "--reviewer", "Test Reviewer"])
    assert r.exit_code == 0, r.output
    assert "DRAFT Personal Data Report" in r.output
    cmds = [e["command"] for e in audit.AuditLog(case / "output").entries()]
    assert "report personal-data" in cmds and cmds[-1] == "personal-data-report"


def test_a_redaction_folder_can_be_left_out_with_a_reason(case, template):
    from dsar.toolbox.redaction import web

    for name in ("deep", "copy"):
        d = case / "output" / name
        d.mkdir()
        (d / "01_note.txt").write_text("Sam Sample starts on Monday.\n")
    app, c = client_for(webapp.Settings.for_case(case))
    with app.test_request_context():
        ids = {p.name: fid for fid, p in web.folders().items()}
    page = c.get("/report/").get_data(as_text=True)
    assert "exact copy" in page
    r = c.post("/report/folder", data={"csrf": c.csrf, "folder_id": ids["copy"], "include": "0", "reason": ""})
    assert "error=" in r.headers["Location"]
    c.post("/report/folder", data={"csrf": c.csrf, "folder_id": ids["copy"], "include": "0", "reason": "Copies of deep"})
    with db.connect(case / "dsar.sqlite") as conn:
        a = personal_data_report.assemble(conn, {fid: case / "output" / n for n, fid in ids.items()})
    assert [e["folder"].rsplit("/", 1)[-1] for e in a["excluded_folders"]] == ["copy"]
    assert [o["file"].split("/")[-2] for o in a["outstanding"] if "file" in o] == ["deep"]
    entry = audit.AuditLog(case / "output").entries()[-1]
    assert entry["command"] == "report folder" and entry["data"]["reason"] == "Copies of deep"


@pytest.fixture
def family_case(case, template):
    """DSAR-TEST-1 as a deeper search (reviewed there) under a main case DSAR-TEST-0."""
    main = case.parent.parent / "DSAR-TEST-0" / "data"
    (main / "output").mkdir(parents=True)
    audit.AuditLog(main / "output").append("note", "audit note", {"text": "main case"})
    (case / "case.json").write_text('{"parent": "DSAR-TEST-0", "role": "deeper search", "review_here": true}')
    return main


def test_report_from_a_deeper_search_goes_to_the_main_case(case, family_case):
    s = webapp.Settings.for_case(case)
    assert s.parent_ref == "DSAR-TEST-0"
    app, c = client_for(s)
    assert "in its deeper search <b>DSAR-TEST-1</b>" in c.get("/report/").get_data(as_text=True)
    j = c.post("/report/build", headers={"X-CSRF": c.csrf}).get_json()
    assert j["success"] and j["report"].startswith("Personal_Data_Report_DSAR-TEST-0_")
    assert (family_case / "output" / personal_data_report.OUT_DIR / j["report"]).is_file()
    for out in (family_case / "output", case / "output"):
        assert audit.AuditLog(out).entries()[-1]["command"] == "report personal-data"
    app, c = client_for(webapp.Settings.for_case(family_case))
    page = c.get("/cases/").get_data(as_text=True)
    assert page.index("DSAR-TEST-0") < page.index("DSAR-TEST-1") and "main case" in page


def test_release_checklist_deadline_and_confirmations_are_logged_in_the_main_case(case, family_case, monkeypatch):
    from dsar.webapp import release

    monkeypatch.delenv("DSAR_CASE_DOCS_ROOT", raising=False)
    app, c = client_for(webapp.Settings.for_case(case))
    page = c.get("/").get_data(as_text=True)
    assert "Release of DSAR-TEST-0" in page and "Deadline unknown" in page and "Items scored 7+ decided" in page
    r = c.post("/release/check", data={"csrf": c.csrf, "item": "received", "done": "1", "value": "20/10/2026"})
    assert "error=" in r.headers["Location"]
    c.post("/release/check", data={"csrf": c.csrf, "item": "received", "done": "1", "value": "2026-10-20"})
    assert "Fri 20 Nov 2026" in c.get("/").get_data(as_text=True)
    entry = audit.AuditLog(family_case / "output").entries()[-1]
    assert entry["command"] == "release check" and entry["data"] == {"item": "received", "done": True,
                                                                     "value": "2026-10-20", "reviewer": "Test Reviewer"}
    import datetime as dt
    assert release.one_month_after(dt.date(2026, 1, 31)) == dt.date(2026, 2, 28)


def test_release_documents_are_checked_against_the_templates(case, family_case, tmp_path, monkeypatch):
    from dsar.webapp import release

    root = tmp_path / "docs"
    (root / "DSAR-TEST-0" / "Templates").mkdir(parents=True)
    (root / "DSAR-TEST-0" / "Output").mkdir()

    def save(path, *paras):
        d = docx.Document()
        for p in paras:
            d.add_paragraph(p)
        d.save(path)

    t, o = root / "DSAR-TEST-0" / "Templates", root / "DSAR-TEST-0" / "Output"
    save(t / "Exemptions Report.docx", "Exemptions Report", "[Insert exemptions]")
    save(t / "Data not in Scope.docx", "Data not in Scope")
    save(t / "Subject Access Request - Final Response Letter Template.docx", "Dear [Name]")
    save(o / "DSAR-TEST-0 Response Letter.docx", "Dear Sam", "Enclosed documents", "Exemptions Report",
         "Data not in Scope", "Privacy Policy v1")
    save(o / "Exemptions Report.docx", "Exemptions Report", "Search scope – confirm before sending")
    (o / "Data not in Scope.docx").write_bytes((t / "Data not in Scope.docx").read_bytes())
    monkeypatch.setenv("DSAR_CASE_DOCS_ROOT", str(root))
    d = release.documents("DSAR-TEST-0", family_case / "output")
    status = {r["document"]: r["status"] for r in d["rows"]}
    assert status == {"Exemptions Report": "to complete", "Data not in Scope": "unchanged template",
                      "Response letter": "ready"}
    assert {e["name"]: e["present"] for e in d["enclosures"]} == {"Exemptions Report": True, "Data not in Scope": True,
                                                                  "Privacy Policy v1": False}
    assert not d["ready"]


def test_everything_is_worked_from_the_main_case(case, family_case):
    """From the main case: Review, Report and the Personal Data Reviewer use the deeper search's data."""
    from dsar.toolbox.redaction import web

    folder = case / "output" / "deep_dive"
    folder.mkdir()
    (folder / "01_note.txt").write_text("Sam Sample starts on Monday.\n")
    s = webapp.Settings.for_case(family_case)
    assert s.subject_name == ""                                   # the main case has no config of its own
    app, c = client_for(s)
    page = c.get("/review/").get_data(as_text=True)
    assert "Address change" in page and "deeper search <b>DSAR-TEST-1</b>" in page
    assert "data subject <b>Sam Sample</b>" in page
    a = TEXT_A.index("Sam Sample's"), TEXT_A.index("Road.") + 5
    assert _decide(c, "a", {"decision": disclosure.EXTRACT,
                            "spans": [{"kind": "extract", "start": a[0], "end": a[1]}]}).status_code == 200
    with db.connect(case / "dsar.sqlite") as conn:                 # saved in the deeper search's database
        assert disclosure.latest(conn)["a"]["decision"] == disclosure.EXTRACT
    assert not (family_case / "dsar.sqlite").exists()
    for out in (family_case / "output", case / "output"):
        e = audit.AuditLog(out).entries()[-1]
        assert e["command"] == "review decide" and e["data"]["request"] == "DSAR-TEST-0"
    with app.test_request_context():
        labels = {web._label(p) for p in web.folders().values()}
    assert labels == {"DSAR-TEST-1 › deep_dive"}
    assert "DSAR-TEST-1 › deep_dive" in c.get("/toolbox/redaction/").get_data(as_text=True)
    j = c.post("/report/build", headers={"X-CSRF": c.csrf}).get_json()
    assert j["success"] and j["extracts"] == 1 and j["report"].startswith("Personal_Data_Report_DSAR-TEST-0_")  # folder not yet decided
    assert (family_case / "output" / personal_data_report.OUT_DIR / j["report"]).is_file()
    home = c.get("/").get_data(as_text=True)
    assert "Release of DSAR-TEST-0" in home and 'href="/review/' in home


def test_outstanding_items_are_stacked_for_the_personal_data_reviewer(case, template):
    """Outstanding items go into a review stack folder; the report then takes them only from what is extracted there."""
    from dsar.toolbox.redaction import markup

    with db.connect(case / "dsar.sqlite") as conn:
        conn.execute("INSERT INTO review_decisions (chunk_id, decision, reviewer, workbook, workbook_sha256, imported_at) "
                     "VALUES ('b', 'Disclose', 'R', 'wb.xlsx', 'x', '2026-11-01T00:00:00+00:00')")
    app, c = client_for(webapp.Settings.for_case(case))
    page = c.get("/report/").get_data(as_text=True)
    assert "Work through all" in page and "Choose the passages (workbook" in page and "Current case: <b>DSAR-TEST-1</b>" in page
    assert page.index("Cases</a>") < page.index("Current case:")
    r = c.post("/report/stack", data={"csrf": c.csrf, "group": "all"})
    assert r.status_code == 302 and "/toolbox/redaction/f/" in r.headers["Location"]
    stack = next((case / "output").glob(personal_data_report.STACK_PREFIX + "*"))
    files = sorted(p.name for p in stack.glob("*.txt"))
    # Not reviewed (a: 7, d: 8) and the whole passage from the workbook (b), each the item's exact text.
    assert len(files) == 3 and files[0].startswith("01_") and all(f.endswith(("_a.txt", "_b.txt", "_d.txt")) for f in files)
    b = next(stack.glob("*_b.txt"))
    assert audit.sha256_file(b) == disclosure.content_sha256(TEXT_B)
    with db.connect(case / "dsar.sqlite") as conn:
        assert {k: v["decision"] for k, v in disclosure.latest(conn).items()} == {
            k: disclosure.IN_REVIEWER for k in ("a", "b", "d")}
        # Nothing comes from the database now, and the stack's undecided elements are what's left.
        data = personal_data_report.assemble(conn, {"s": stack})
        assert data["from_review"] == 0 and data["extracts"] == []
        assert {Path(o["file"]).name for o in data["outstanding"]} == set(files)
    f = markup.Folder(stack, "R", None)
    for doc in f.docs():
        for el in doc["elements"]:
            f.act({"file": doc["name"], "sha256": doc["sha256"], "action": "decide", "element": el["id"],
                   "decision": "release" if doc["name"].endswith("_b.txt") else "redact_full",
                   "reason": markup.REASONS[0]})
    with db.connect(case / "dsar.sqlite") as conn:
        data = personal_data_report.assemble(conn, {"s": stack})
        assert [e["text"] for e in data["extracts"]] == [" ".join(TEXT_B.split())] and data["outstanding"] == []
        # Deciding b again in Review takes over: its stack file is then ignored, so nothing is counted twice.
        disclosure.record(conn, "b", {"decision": disclosure.NOT_SUBJECT, "content_sha256": disclosure.content_sha256(TEXT_B)}, "R")
        assert personal_data_report.assemble(conn, {"s": stack})["extracts"] == []
    entry = audit.AuditLog(case / "output").entries()[-1]
    assert entry["command"] == "report review-stack" and entry["data"]["items"] == 3


def test_an_extract_containing_text_left_out_in_the_reviewer_is_held_out(case, template):
    """The same text decided twice: left out in the Personal Data Reviewer, extracted in Review. It must not go in."""
    from dsar.toolbox.redaction import markup

    folder = case / "output" / "deep_dive"
    folder.mkdir()
    (folder / "01_2026-03-02_address.txt").write_text(TEXT_A)
    f = markup.Folder(folder, "R", None)
    doc = f.docs()[0]
    for el in doc["elements"]:
        f.act({"file": doc["name"], "sha256": doc["sha256"], "action": "decide", "element": el["id"],
               "decision": "redact_full", "reason": markup.REASONS[0]})
    with db.connect(case / "dsar.sqlite") as conn:
        disclosure.record(conn, "a", {"decision": disclosure.EXTRACT, "content_sha256": disclosure.content_sha256(TEXT_A),
                                      "spans": [{"kind": "extract", "start": 0, "end": len(TEXT_A)}]}, "R")
        data = personal_data_report.assemble(conn, {"x": folder})
    assert data["extracts"] == []
    held = [o for o in data["outstanding"] if o["why"].startswith("conflict")]
    assert [o["chunk_id"] for o in held] == ["a"] and "01_2026-03-02_address.txt" in held[0]["why"]
    app, c = client_for(webapp.Settings.for_case(case))
    page = c.get("/report/").get_data(as_text=True)
    assert "Held out: conflicts with what you left out" in page
    assert "No extract contains text left out" in c.get("/").get_data(as_text=True)


def test_each_extract_in_the_preview_links_back_to_its_decision(case, template):
    """Preview: an extract from the Personal Data Reviewer opens its element there; one from Review can be stacked."""
    from dsar.toolbox.redaction import markup

    folder = case / "output" / "deep_dive"
    folder.mkdir()
    (folder / "01_2026-02-10_note.txt").write_text("Sam Sample starts on Monday.\n")
    f = markup.Folder(folder, "R", None)
    doc = f.docs()[0]
    el = doc["elements"][0]["id"]
    f.act({"file": doc["name"], "sha256": doc["sha256"], "action": "decide", "element": el, "decision": "release"})
    with db.connect(case / "dsar.sqlite") as conn:
        disclosure.record(conn, "b", {"decision": disclosure.EXTRACT, "content_sha256": disclosure.content_sha256(TEXT_B),
                                      "spans": [{"kind": "extract", "start": 0, "end": len(TEXT_B)}]}, "R")
    app, c = client_for(webapp.Settings.for_case(case))
    page = c.get("/report/").get_data(as_text=True)
    link = re.search(r'href="(/toolbox/redaction/f/[^"]+)">Edit in Personal Data Reviewer', page).group(1).replace("&amp;", "&")
    assert f"file=01_2026-02-10_note.txt" in link and f"el={el}" in link
    r = c.get(link)                                          # the subject redirect keeps file and element
    assert r.status_code == 302 and "file=01_2026-02-10_note.txt" in r.headers["Location"] and f"el={el}" in r.headers["Location"]
    assert "Report preview" in c.get(r.headers["Location"]).get_data(as_text=True)
    assert "Narrow in Personal Data Reviewer" in page
    r = c.post("/report/stack", data={"csrf": c.csrf, "chunk_id": "b"})
    assert "/toolbox/redaction/f/" in r.headers["Location"]
    stack = next((case / "output").glob(personal_data_report.STACK_PREFIX + "*"))
    assert [p.name.endswith("_b.txt") for p in stack.glob("*.txt")] == [True]
    with db.connect(case / "dsar.sqlite") as conn:
        assert disclosure.latest(conn)["b"]["decision"] == disclosure.IN_REVIEWER


def test_review_stack_files_show_their_classification_and_skip_blank_lines(case, template):
    from dsar.toolbox.redaction import markup, web

    with db.connect(case / "dsar.sqlite") as conn:
        conn.execute("UPDATE chunks SET content = ? WHERE chunk_id = 'd'", ("Sam Sample raised a grievance.\n\n---\n\nThanks",))
    app, c = client_for(webapp.Settings.for_case(case))
    c.post("/report/stack", data={"csrf": c.csrf, "group": "all"})
    stack = next((case / "output").glob(personal_data_report.STACK_PREFIX + "*"))
    with app.test_request_context():
        fid = next(k for k, p in web.folders().items() if p == stack)
    j = c.get(f"/toolbox/redaction/f/{fid}/api/docs").get_json()
    d = next(x for x in j["docs"] if x["name"].endswith("_d.txt"))
    assert d["item"]["score"] == 8 and d["item"]["chunk_id"] == "d" and d["item"]["reason"] == "test"
    blank = [el for el in d["elements"] if el["blank"]]
    assert len(blank) == 1 and markup.undecided_count(d) == len(d["elements"]) - 1


def test_context_shows_the_whole_item_and_its_thread(case):
    with db.connect(case / "dsar.sqlite") as conn:
        conn.execute("UPDATE chunks SET thread_id = 't1' WHERE chunk_id IN ('a', 'b')")
        conn.execute("INSERT INTO chunks (chunk_id, source_type, source_path, item_ref, subject, content, metadata_json, "
                     "alias_matched, thread_id, created_at) VALUES ('z', 'email', '/x/z.pst', 'msg-z', 'Re: rota', "
                     "'A reply that does not name anyone.', '{\"sent\": \"2026-05-01\"}', 0, 't1', '2026-01-01')")
    app, c = client_for(webapp.Settings.for_case(case))
    j = c.get("/review/api/context/a").get_json()
    assert [x["chunk_id"] for x in j["item"]] == ["a"] and j["item"][0]["this"]
    conv = [x["chunk_id"] for x in j["conversation"]]
    assert conv == ["b", "a", "z"]                                  # oldest first, including the reply not naming her
    assert not next(x for x in j["conversation"] if x["chunk_id"] == "z")["about_subject"]
    assert "Whole item" in c.get("/review/item/a").get_data(as_text=True)
    with db.connect(case / "dsar.sqlite") as conn:
        assert disclosure.find_chunk(conn, "Sam Sample's new address is 1 Example Road.") == "a"


def test_unreviewed_items_are_brought_into_the_personal_data_reviewer(case, template):
    from dsar.toolbox.redaction import markup, web

    with db.connect(case / "dsar.sqlite") as conn:
        conn.execute("UPDATE classifications SET score = 5 WHERE chunk_id = 'c'")
        assert disclosure.intake(conn) == {9: ["b"], 8: ["d"], 7: ["a"], 5: ["c"]}
    app, c = client_for(webapp.Settings.for_case(case))
    page = c.get("/toolbox/redaction/").get_data(as_text=True)
    assert "Bring 1 item(s) scored 5 into review" in page and 'href="/toolbox/redaction/"' in c.get("/").get_data(as_text=True)
    r = c.post("/toolbox/redaction/intake", data={"csrf": c.csrf, "score": "5"})
    assert "section=review" in r.headers["Location"]
    folder = next((case / "output").glob(personal_data_report.STACK_PREFIX + "*_score5"))
    f = markup.Folder(folder, "Test Reviewer", None)
    assert f.min_score == markup.REVIEW_MIN_SCORE                 # a folder can't lower the review line
    assert all(el["below"] for el in f.docs()[0]["elements"])     # unscored paragraphs take the item's score (5)
    with db.connect(case / "dsar.sqlite") as conn:
        assert disclosure.latest(conn)["c"]["decision"] == disclosure.IN_REVIEWER
        assert 5 not in disclosure.intake(conn)
    assert audit.AuditLog(case / "output").entries()[-1]["command"] == "review intake"
    # Whole item: leave out every paragraph at once.
    with app.test_request_context():
        fid = next(k for k, p in web.folders().items() if p == folder)
    name = f.docs()[0]["name"]
    r = c.post(f"/toolbox/redaction/f/{fid}/api/decide-file", json={"file": name, "decision": "redact_full",
                                                                  "reason": "Not the requester's personal data"},
               headers={"X-CSRF": c.csrf})
    assert r.get_json()["decided"] == len(f.docs()[0]["elements"])
    assert markup.undecided_count(markup.Folder(folder, "", None).docs()[0]) == 0
