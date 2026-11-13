"""Review decisions in the web app, and the Personal Data Report built from them (synthetic data only)."""
import csv
import re
import stat

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
    assert "Outstanding" in page and "select the passages" in page
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
    assert [r[0] for r in rows[2:]] == ["Extracted data 1:", "Extracted data 2:", "Extracted data 3:"]
    # Oldest first, text only: no sender, subject or date in the report.
    assert rows[2][1] == TEXT_B and rows[3][1] == "Sam Sample starts on Monday."
    assert rows[4][1] == "Sam Sample's new address is 1 Example Road."
    assert "someone@example.test" not in body + str(rows) and "John Smith" not in str(rows)
    with (out / j["register"]).open() as fh:
        reg = list(csv.DictReader(fh))
    assert {r["origin"] for r in reg if r["extract_no"] == "1"} == {"review", "redaction"}
    entry = audit.AuditLog(case / "output").entries()[-1]
    assert entry["command"] == "report personal-data" and entry["data"]["sha256"][report_path.name] == \
        audit.sha256_file(report_path) and "template" in entry["data"]["sha256"]


def test_workbook_disclose_is_whole_passage_and_changed_text_is_outstanding(case, template):
    with db.connect(case / "dsar.sqlite") as conn:
        conn.execute("INSERT INTO review_decisions (chunk_id, decision, reviewer, workbook, workbook_sha256, "
                     "imported_at) VALUES ('b', 'Disclose', 'R', 'wb.xlsx', 'x', '2026-11-01T00:00:00+00:00')")
        disclosure.record(conn, "a", {"decision": disclosure.EXTRACT, "content_sha256": disclosure.content_sha256(TEXT_A),
                                      "spans": [{"kind": "extract", "start": 0, "end": 11}]}, "R")
        conn.execute("UPDATE chunks SET content = 'changed' WHERE chunk_id = 'a'")
        data = disclosure.report_inputs(conn)
    assert data["whole_from_workbook"] == ["b"] and data["pieces"][0]["text"] == TEXT_B
    assert [o["chunk_id"] for o in data["changed"]] == ["a"]


def test_review_pages_need_a_case_and_a_database(case, tmp_path):
    app, c = client_for(webapp.Settings())
    assert c.get("/review/").status_code == 404
    empty = tmp_path / "DSAR-EMPTY" / "data"
    (empty / "output").mkdir(parents=True)
    app, c = client_for(webapp.Settings.for_case(empty))
    assert c.get("/review/").status_code == 404 and not (empty / "dsar.sqlite").exists()
