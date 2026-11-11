from pathlib import Path

import pytest
from click.testing import CliRunner
from openpyxl import load_workbook

from dsar import db, review
from dsar.chunking import Chunk
from dsar.cli import main

ROWS = [
    ("c-9", "Before. Alex Example keeps ignoring the agreed process and is rude to staff. After.", 9,
     "keeps ignoring the agreed process"),
    ("c-7", "Alex Example lives at 14 Example Road.", 7, "lives at 14 Example Road"),
    ("c-8-invented", "Alex Example attended the meeting.", 8, "The data subject is an idiot"),
    ("c-3", "Alex Example was cc'd.", 3, None),
]


def _build(db_path: Path) -> None:
    with db.connect(db_path) as conn:
        db.init_schema(conn)
        for cid, content, score, ev in ROWS:
            db.upsert_chunk(conn, Chunk(chunk_id=cid, source_type="email", source_path="/in/x.pst", item_ref=cid,
                                        subject="Re: team", content=content,
                                        metadata={"sender": "Team Lead", "sent": "2026-01-05"}, alias_matched=True))
            db.insert_classification(conn, chunk_id=cid, score=score, description="reason", evidence=ev,
                                     special_return=None, raw_response="r", model="m", input_tokens=1,
                                     output_tokens=1, cache_read_tokens=0)


def test_evidence_highlight():
    content = "x" * 3000 + " Alex Example is rude. " + "y" * 3000
    spans = review.evidence_spans(content, '"Alex Example is rude"')
    text, shifted = review.context_window(content, spans)
    a, b = shifted[0]
    assert text[a:b] == "Alex Example is rude"
    assert text.startswith("…") and text.endswith("…")
    assert review.evidence_spans("abc", "not there") == []


def _fill(path: Path, decisions: dict) -> None:
    wb = load_workbook(path, rich_text=True)
    ws = wb["Review"]
    header = [c.value for c in ws[1]]
    col = {name: header.index(name) + 1 for name in header}
    for r in range(2, ws.max_row + 1):
        rid = ws.cell(r, col["review_id"]).value
        for name, value in decisions.get(rid, {}).items():
            ws.cell(r, col[name]).value = value
    wb.save(path)


def test_export_import_roundtrip(tmp_path: Path):
    _build(tmp_path / "dsar.sqlite")
    path = tmp_path / "review.xlsx"
    with db.connect(tmp_path / "dsar.sqlite") as conn:
        stats = review.export(conn, path)
    assert stats["rows"] == 3 and stats["evidence_not_found"] == 1      # c-3 is below 7

    wb = load_workbook(path, rich_text=True)
    assert wb.sheetnames == ["Guidance", "Summary", "Review", "Lists"]
    ws = wb["Review"]
    header = [c.value for c in ws[1]]
    first = {h: ws.cell(2, i + 1).value for i, h in enumerate(header)}
    assert first["review_id"] == "c-9" and first["custodian"] in ("", None) and first["from"] == "Team Lead"
    assert "keeps ignoring the agreed process" in str(first["context"])
    assert not isinstance(first["context"], str)                       # rich text: evidence highlighted

    _fill(path, {
        "c-9": {"assessment": "Agree", "decision": "Disclose", "reason": "None", "reviewer": "HR One",
                "review_date": "2026-11-12"},
        "c-7": {"assessment": "Score too high", "adjusted_score": 6, "decision": "Disclose with redactions",
                "reason": "None", "reviewer": "HR One"},                           # invalid: redaction needs a reason
        "c-8-invented": {"decision": "Maybe", "reviewer": ""},                  # invalid twice over
    })
    with db.connect(tmp_path / "dsar.sqlite") as conn:
        result = review.import_workbook(conn, path)
        assert result["imported"] == 1 and result["invalid"] == 2 and result["outstanding"] == 0
        errors = {p["review_id"]: p["errors"] for p in result["problems"]}
        assert "redacting or withholding needs a reason" in errors["c-7"]
        assert len(errors["c-8-invented"]) == 2
        # Model scores are untouched.
        assert conn.execute("SELECT score FROM classifications WHERE chunk_id = 'c-7'").fetchone()[0] == 7

        st = review.status(conn)
        assert st["items"] == 3 and st["decided"] == 1

        # A second export pre-fills earlier decisions; a corrected import supersedes.
        path2 = tmp_path / "review2.xlsx"
        assert review.export(conn, path2)["prefilled"] == 1
    _fill(path2, {"c-7": {"decision": "Disclose with redactions", "reason": REASON_3P, "reviewer": "HR One"},
                  "c-9": {"decision": "Withhold (exemption applies)", "reason": REASON_3P, "reviewer": "HR Two"}})
    with db.connect(tmp_path / "dsar.sqlite") as conn:
        result = review.import_workbook(conn, path2)
        assert result["imported"] == 2
        latest = review.latest_decisions(conn)
        assert latest["c-9"]["decision"] == "Withhold (exemption applies)" and latest["c-9"]["reviewer"] == "HR Two"
        assert conn.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0] == 3   # append-only


REASON_3P = "Third-party personal data (DPA 2018 Sch. 2 Part 3)"


def test_rejects_other_workbooks(tmp_path: Path):
    from openpyxl import Workbook
    wb = Workbook()
    wb.active.title = "Review"
    wb.active.append(["something", "else"])
    wb.save(tmp_path / "other.xlsx")
    _build(tmp_path / "dsar.sqlite")
    with db.connect(tmp_path / "dsar.sqlite") as conn, pytest.raises(ValueError):
        review.import_workbook(conn, tmp_path / "other.xlsx")


def test_cli(tmp_path: Path):
    data = tmp_path / "data"
    (data / "input").mkdir(parents=True)
    _build(data / "dsar.sqlite")
    env = {"DSAR_DATA_DIR": str(data)}
    out = data / "output" / "review.xlsx"
    runner = CliRunner()
    result = runner.invoke(main, ["review", "export", "--out", str(out)], env=env)
    assert result.exit_code == 0 and "3 items" in result.output, result.output
    assert runner.invoke(main, ["review", "export", "--out", str(out)], env=env).exit_code != 0   # never overwrite
    _fill(out, {"c-9": {"decision": "Disclose", "reviewer": "HR One"}})
    result = runner.invoke(main, ["review", "import", str(out)], env=env)
    assert result.exit_code == 0 and "1/3 items decided" in result.output, result.output
    result = runner.invoke(main, ["review", "status"], env=env)
    assert "score  9: 1/1" in result.output
